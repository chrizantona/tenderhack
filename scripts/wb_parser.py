from __future__ import annotations

import copy
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

import requests

try:
    from curl_cffi import requests as cffi_requests
    CFFI_AVAILABLE = True
except Exception:
    CFFI_AVAILABLE = False

# Chrome 120 JA3/TLS fingerprint matches what real WB users send. Plain
# `requests` gets banned within a handful of calls because its OpenSSL
# handshake is trivially identifiable.
WB_CFFI_IMPERSONATE = "chrome120"

from wb_common import (
    CARD_ENDPOINTS,
    DEFAULT_CURRENCY,
    DEFAULT_DEST,
    DEFAULT_HEADERS,
    DEFAULT_REGION,
    SEARCH_ENDPOINTS,
    basket_card_url,
    basket_host_candidates,
    basket_price_history_url,
    extract_old_price,
    extract_price,
    extract_products,
    image_url,
    int_or_none,
    latest_history_price,
    money,
    number_or_none,
    parse_nm_id,
)


ADAPTER_VERSION = "0.2"
DEFAULT_REGION_NAME = "RU/Moscow"


class WildberriesHTTPError(RuntimeError):
    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"GET {url} failed: {status_code} {body[:300]}")


class TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._items[key]
                return None
            return copy.deepcopy(value)

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + self.ttl_seconds, copy.deepcopy(value))


class TokenBucketRateLimiter:
    def __init__(self, rate_per_second: float, burst: int, backoff_seconds: float = 300) -> None:
        self.rate_per_second = rate_per_second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.backoff_seconds = backoff_seconds
        self.updated_at = time.monotonic()
        self.backoff_until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self.backoff_until:
                    sleep_for = self.backoff_until - now
                else:
                    elapsed = now - self.updated_at
                    self.updated_at = now
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
                    if self.tokens >= 1:
                        self.tokens -= 1
                        return
                    sleep_for = (1 - self.tokens) / self.rate_per_second
            time.sleep(max(sleep_for, 0.01))

    def backoff(self) -> None:
        with self._lock:
            self.backoff_until = max(self.backoff_until, time.monotonic() + self.backoff_seconds)


class WildberriesClient:
    """WB HTTP client with curl_cffi TLS impersonation + proxy rotation.

    WB rate-limits aggressively per source IP: even a real browser hits 429
    after a handful of queries from the same address. To stay under the
    threshold we keep one Chrome-impersonating session per proxy URL and
    round-robin between them on each request. With 10 proxies we effectively
    get 10× the per-IP budget.
    """

    def __init__(
        self,
        timeout: float = 15,
        *,
        proxy_url: str | None = None,
        proxy_pool: list[str] | None = None,
        impersonate: str | None = WB_CFFI_IMPERSONATE,
        warmup: bool = True,
    ) -> None:
        self.timeout = timeout
        self.impersonate = impersonate
        self.cffi_active = False
        # Build the list of session "lanes" — one session per proxy URL, plus
        # an unproxied lane if none were supplied. proxy_url is kept for
        # backwards compatibility with single-proxy callers.
        proxies = list(proxy_pool) if proxy_pool else ([proxy_url] if proxy_url else [None])
        self._lanes: list[Any] = [self._build_session(p) for p in proxies]
        self._lane_lock = threading.Lock()
        self._lane_idx = 0
        # Slower rate keeps us under WB's per-IP threshold even per-lane. The
        # burst still lets a fresh search land instantly.
        self.search_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=3)
        self.basket_limiter = TokenBucketRateLimiter(rate_per_second=3, burst=6)
        self.card_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=2)
        # Expose .session for legacy callers that grab it directly (the
        # round-robin happens inside get_json, so .session is just lane 0).
        self.session = self._lanes[0]
        self.proxy_url = proxies[0] if proxies else None
        self._warmed_up = False
        if warmup:
            self._warmup()

    def _build_session(self, proxy_url: str | None) -> Any:
        if CFFI_AVAILABLE and self.impersonate:
            session = cffi_requests.Session(impersonate=self.impersonate)
            self.cffi_active = True
        else:
            session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
        return session

    def _next_session(self) -> Any:
        """Round-robin pick a lane. Single-lane setups always return the same."""
        if len(self._lanes) == 1:
            return self._lanes[0]
        with self._lane_lock:
            sess = self._lanes[self._lane_idx % len(self._lanes)]
            self._lane_idx += 1
            return sess

    def _warmup(self) -> None:
        # One GET per lane seeds cookies (csrf, locale, region) that WB's edge
        # expects on subsequent API calls. Cheap insurance — runs once.
        if self._warmed_up:
            return
        for lane in self._lanes:
            try:
                lane.get("https://www.wildberries.ru/", timeout=self.timeout, allow_redirects=True)
            except Exception:
                pass
        self._warmed_up = True

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        endpoint_kind: str,
        timeout: float | None = None,
    ) -> Any:
        limiter = self._limiter_for(endpoint_kind)
        limiter.wait()
        session = self._next_session()
        response = session.get(url, params=params, timeout=timeout or self.timeout)
        if response.status_code == 429:
            limiter.backoff()
        if response.status_code != 200:
            body = response.text[:300].replace("\n", " ")
            raise WildberriesHTTPError(response.status_code, response.url, body)
        if not response.content:
            raise RuntimeError(f"GET {response.url} returned an empty response")
        return response.json()

    def _limiter_for(self, endpoint_kind: str) -> TokenBucketRateLimiter:
        if endpoint_kind == "search":
            return self.search_limiter
        if endpoint_kind == "basket":
            return self.basket_limiter
        return self.card_limiter


class WildberriesProductParser:
    def __init__(
        self,
        client: WildberriesClient | None = None,
        *,
        card_ttl_seconds: float = 60 * 60 * 24,
        price_history_ttl_seconds: float = 60 * 60 * 12,
    ) -> None:
        self.client = client or WildberriesClient()
        self.card_cache = TTLCache(card_ttl_seconds)
        self.price_history_cache = TTLCache(price_history_ttl_seconds)
        self.host_cache = TTLCache(60 * 60 * 24 * 30)

    def get_product(
        self,
        nm_or_url: str | int,
        *,
        dest: str = DEFAULT_DEST,
        region_name: str = DEFAULT_REGION_NAME,
        include_price_history: bool = True,
    ) -> dict[str, Any]:
        nm_id = parse_nm_id(str(nm_or_url))
        diagnostics: list[dict[str, Any]] = []
        endpoints: list[str] = []

        basket_card = None
        basket_host = None
        try:
            basket_host, basket_card = self.get_basket_card(nm_id)
            endpoints.append("wbbasket/info/ru/card.json")
        except Exception as exc:
            diagnostics.append({"stage": "basket_card", "message": str(exc)})

        card_product = None
        card_endpoint = None
        try:
            card_product, card_endpoint = self.get_card_product(nm_id, dest=dest)
            if card_endpoint:
                endpoints.append(card_endpoint)
        except Exception as exc:
            diagnostics.append({"stage": "card_product", "message": str(exc)})

        price_history = None
        if include_price_history and basket_host:
            try:
                price_history = self.get_price_history(nm_id, basket_host)
                endpoints.append("wbbasket/info/price-history.json")
            except Exception as exc:
                diagnostics.append({"stage": "price_history", "message": str(exc)})

        product = card_product or minimal_product_from_basket(nm_id, basket_card)
        return normalize_wb_product(
            product,
            basket_card=basket_card,
            basket_host=basket_host,
            price_history=price_history,
            dest=dest,
            region_name=region_name,
            stock_reliable=False,
            endpoints=endpoints,
            diagnostics=diagnostics,
        )

    def get_basket_card(self, nm_id: int) -> tuple[str, dict[str, Any]]:
        cached = self.card_cache.get(nm_id)
        if cached:
            return cached["host"], cached["payload"]

        cached_host = self.host_cache.get(nm_id)
        hosts = []
        if cached_host:
            hosts.append(cached_host)
        hosts.extend(host for host in basket_host_candidates(nm_id) if host != cached_host)

        errors: list[str] = []
        for host in hosts:
            try:
                payload = self.client.get_json(
                    basket_card_url(nm_id, host),
                    endpoint_kind="basket",
                    timeout=6,
                )
                self.host_cache.set(nm_id, host)
                self.card_cache.set(nm_id, {"host": host, "payload": payload})
                return host, payload
            except Exception as exc:
                errors.append(f"{host}: {exc}")

        raise RuntimeError(
            f"Cannot resolve wbbasket host for nmID={nm_id}. Last errors: "
            + "; ".join(errors[-3:])
        )

    def get_card_product(
        self,
        nm_id: int,
        *,
        dest: str = DEFAULT_DEST,
    ) -> tuple[dict[str, Any] | None, str | None]:
        cache_key = ("card_product", nm_id, dest)
        cached = self.card_cache.get(cache_key)
        if cached is not None:
            return cached["product"], cached["endpoint"]

        params = {
            "appType": 1,
            "curr": DEFAULT_CURRENCY,
            "dest": dest,
            "spp": 30,
            "ab_testing": "false",
            "nm": nm_id,
        }

        errors: list[str] = []
        for endpoint in CARD_ENDPOINTS:
            try:
                payload = self.client.get_json(endpoint, params=params, endpoint_kind="card")
                products = extract_products(payload)
                if products:
                    result = {"product": products[0], "endpoint": endpoint}
                    self.card_cache.set(cache_key, result)
                    return products[0], endpoint
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")

        self.card_cache.set(cache_key, {"product": None, "endpoint": None})
        if errors:
            raise RuntimeError("; ".join(errors))
        return None, None

    def get_price_history(self, nm_id: int, basket_host: str) -> list[dict[str, Any]]:
        cache_key = ("price_history", nm_id, basket_host)
        cached = self.price_history_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self.client.get_json(
            basket_price_history_url(nm_id, basket_host),
            endpoint_kind="basket",
            timeout=6,
        )
        self.price_history_cache.set(cache_key, payload)
        return payload


class WildberriesSearchParser:
    def __init__(
        self,
        client: WildberriesClient | None = None,
        *,
        search_ttl_seconds: float = 60 * 15,
        product_parser: WildberriesProductParser | None = None,
    ) -> None:
        self.client = client or WildberriesClient()
        self.search_cache = TTLCache(search_ttl_seconds)
        self.product_parser = product_parser or WildberriesProductParser(client=self.client)

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 20,
        sort: str = "popular",
        dest: str = DEFAULT_DEST,
        region_name: str = DEFAULT_REGION_NAME,
        enrich: bool = False,
        enrich_limit: int | None = None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if len(query) < 3:
            raise ValueError("query must contain at least 3 characters")

        limit = max(1, min(limit, 100))
        page = max(1, page)
        enrich_limit = limit if enrich_limit is None else max(0, min(enrich_limit, limit))
        diagnostics: list[dict[str, Any]] = []

        primary_error: Exception | None = None
        try:
            products, endpoints, total, last_payload = self._collect_products(
                query, page=page, limit=limit, sort=sort, dest=dest, extra_params=extra_params,
            )
        except Exception as exc:
            primary_error = exc
            products, endpoints, total, last_payload = [], [], None, None

        if not products and dest != DEFAULT_DEST:
            fallback_diag: dict[str, Any] = {
                "stage": "wb_dest_fallback",
                "from_dest": dest,
                "to_dest": DEFAULT_DEST,
            }
            if primary_error is not None:
                fallback_diag["primary_error"] = str(primary_error)
            diagnostics.append(fallback_diag)
            try:
                fallback_products, fallback_endpoints, fallback_total, fallback_payload = self._collect_products(
                    query, page=page, limit=limit, sort=sort, dest=DEFAULT_DEST, extra_params=extra_params,
                )
            except Exception as exc:
                diagnostics.append({"stage": "wb_dest_fallback_error", "message": str(exc)})
                fallback_products, fallback_endpoints, fallback_total, fallback_payload = [], [], None, None
            if fallback_products:
                products = fallback_products
                endpoints = list(dict.fromkeys(endpoints + fallback_endpoints))
                total = fallback_total or total
                last_payload = fallback_payload or last_payload
                dest = DEFAULT_DEST

        if not products and primary_error is not None:
            raise primary_error

        # Parallel basket-card enrichment: each call is a separate HTTPS GET to
        # the wbbasket CDN (~3-5s round-trip). Serial used to add up to 30-50s
        # for limit=10. With 5 workers we get ~6-10s total without rate-limit
        # warnings from WB (basket CDN tolerates concurrent reads).
        items: list[dict[str, Any] | None] = [None] * len(products)
        item_diags: list[list[dict[str, Any]]] = [[] for _ in products]

        def _fetch_basket(product: dict[str, Any]) -> tuple[str | None, dict | None, str | None]:
            try:
                nm_id = int(product["id"])
            except (KeyError, TypeError, ValueError):
                return None, None, "bad_nm_id"
            try:
                host, card = self.product_parser.get_basket_card(nm_id)
                return host, card, None
            except Exception as exc:
                return None, None, str(exc)

        if enrich and enrich_limit > 0:
            # Submit all fetches up front so they run in parallel, then iterate
            # in index order — emit each item as soon as its enrichment is done.
            # 8 workers handle basket-CDN concurrently (basket-X.wbbasket.ru is
            # a static CDN, no per-IP rate limit there).
            pool = ThreadPoolExecutor(max_workers=min(8, enrich_limit), thread_name_prefix="wb-basket")
            try:
                futures = {
                    idx: pool.submit(_fetch_basket, products[idx])
                    for idx in range(min(enrich_limit, len(products)))
                }
                for index, product in enumerate(products):
                    basket_host: str | None = None
                    basket_card: dict | None = None
                    if index in futures:
                        try:
                            basket_host, basket_card, err = futures[index].result(timeout=30)
                            if err:
                                item_diags[index].append({"stage": "basket_card", "message": err})
                                diagnostics.append({"nm_id": product.get("id"), "stage": "basket_card", "message": err})
                        except Exception as exc:
                            item_diags[index].append({"stage": "basket_card", "message": str(exc)})
                    item_endpoints = list(dict.fromkeys(endpoints))
                    if basket_card is not None:
                        item_endpoints.append("wbbasket/info/ru/card.json")
                    item = normalize_wb_product(
                        product,
                        query=query,
                        basket_card=basket_card,
                        basket_host=basket_host,
                        dest=dest,
                        region_name=region_name,
                        stock_reliable=True,
                        endpoints=item_endpoints,
                        diagnostics=item_diags[index],
                    )
                    items[index] = item
                    if item_callback is not None:
                        item_callback(item, index)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        else:
            for index, product in enumerate(products):
                item = normalize_wb_product(
                    product,
                    query=query,
                    basket_card=None,
                    basket_host=None,
                    dest=dest,
                    region_name=region_name,
                    stock_reliable=True,
                    endpoints=list(dict.fromkeys(endpoints)),
                    diagnostics=[],
                )
                items[index] = item
                if item_callback is not None:
                    item_callback(item, index)

        return {
            "schema_version": "0.2",
            "marketplace": "wildberries",
            "query": query,
            "page": page,
            "limit": limit,
            "total": total or (last_payload or {}).get("total") or ((last_payload or {}).get("data") or {}).get("total"),
            "items": items,
            "source_meta": {
                "fetched_at": now_iso(),
                "adapter": "wildberries",
                "adapter_version": ADAPTER_VERSION,
                "endpoints": list(dict.fromkeys(endpoints)),
                "region_params": {"dest": dest, "currency": DEFAULT_CURRENCY},
                "diagnostics": diagnostics,
            },
        }

    def _collect_products(
        self,
        query: str,
        *,
        page: int,
        limit: int,
        sort: str,
        dest: str,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], int | None, dict[str, Any] | None]:
        products: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        endpoints: list[str] = []
        total: int | None = None
        last_payload: dict[str, Any] | None = None
        max_pages = max(1, min(5, (limit + 29) // 30 + 1))

        for page_number in range(page, page + max_pages):
            payload, endpoint = self._fetch_search(query, page=page_number, sort=sort, dest=dest, extra_params=extra_params)
            last_payload = payload
            endpoints.append(endpoint)
            total = total or payload.get("total") or (payload.get("data") or {}).get("total")
            page_products = extract_products(payload)
            if not page_products:
                break
            added = 0
            for product in page_products:
                product_id = str(product.get("id") or product.get("nm_id") or "")
                if product_id and product_id in seen_ids:
                    continue
                if product_id:
                    seen_ids.add(product_id)
                products.append(product)
                added += 1
                if len(products) >= limit:
                    break
            if len(products) >= limit or added == 0:
                break

        return products[:limit], endpoints, total, last_payload

    def _fetch_search(
        self,
        query: str,
        *,
        page: int,
        sort: str,
        dest: str,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        extras_key = tuple(sorted((extra_params or {}).items()))
        cache_key = (query, page, sort, dest, extras_key)
        cached = self.search_cache.get(cache_key)
        if cached:
            return cached["payload"], cached["endpoint"]

        params: dict[str, Any] = {
            "appType": 1,
            "curr": DEFAULT_CURRENCY,
            "dest": dest,
            "lang": DEFAULT_REGION,
            "page": page,
            "query": query,
            "resultset": "catalog",
            "sort": sort,
            "spp": 30,
            "suppressSpellcheck": "false",
        }
        if extra_params:
            for k, v in extra_params.items():
                if v is None or v == "":
                    continue
                params[k] = v
        errors: list[str] = []
        for endpoint in SEARCH_ENDPOINTS:
            try:
                payload = self.client.get_json(endpoint, params=params, endpoint_kind="search")
                result = {"payload": payload, "endpoint": endpoint}
                self.search_cache.set(cache_key, result)
                return payload, endpoint
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")
        raise RuntimeError("All WB search endpoints failed:\n" + "\n".join(errors))


def normalize_wb_product(
    product: dict[str, Any],
    *,
    query: str | None = None,
    basket_card: dict[str, Any] | None = None,
    basket_host: str | None = None,
    price_history: list[dict[str, Any]] | None = None,
    dest: str = DEFAULT_DEST,
    region_name: str = DEFAULT_REGION_NAME,
    stock_reliable: bool = True,
    endpoints: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    endpoints = endpoints or []
    diagnostics = diagnostics or []
    nm_id = int(product.get("id") or product.get("nm_id") or (basket_card or {}).get("nm_id"))
    size = select_size(product)
    price_current = extract_price(product)
    price_old = extract_old_price(product)
    price_source = "search_or_card"
    if price_current is None and price_history:
        price_current = latest_history_price(price_history)
        price_source = "price_history"

    title = title_from(product, basket_card)
    brand_name = brand_from(product, basket_card)
    raw_attributes = raw_attributes_from(product, basket_card, size)
    normalized_attributes = normalize_attributes(raw_attributes, title=title)
    category = category_from(product, basket_card)
    quantity = product.get("totalQuantity") if stock_reliable else None
    rating_value = product.get("reviewRating") or product.get("nmReviewRating") or product.get("rating")
    photo_count = photo_count_from(product, basket_card)

    return {
        "schema_version": "0.2",
        "marketplace": "wildberries",
        "query": query,
        "external_id": str(nm_id),
        "group_id": string_or_none(product.get("root") or (basket_card or {}).get("imt_id")),
        "variant_id": str(nm_id),
        "offer_id": string_or_none((size or {}).get("optionId") or first_chrt_id(basket_card)),
        "url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        "title": title,
        "normalized_title": normalize_text(" ".join(value for value in (brand_name, title) if value)),
        "description": (basket_card or {}).get("description"),
        "brand": {
            "name": brand_name,
            "external_id": string_or_none(product.get("brandId")),
        },
        "category": category,
        "price": {
            "current": price_current,
            "old": price_old,
            "currency": "RUB",
            "discount_percent": discount_percent(price_current, price_old),
            "source": price_source,
            "updated_at": now_iso(),
        },
        "rating": {
            "value": number_or_none(rating_value),
            "reviews_count": int_or_none(product.get("feedbacks") or product.get("nmFeedbacks")),
        },
        "seller": {
            "id": string_or_none(product.get("supplierId") or ((basket_card or {}).get("selling") or {}).get("supplier_id")),
            "name": product.get("supplier"),
            "rating": number_or_none(product.get("supplierRating")),
        },
        "availability": {
            "status": availability_status(quantity, stock_reliable=stock_reliable),
            "quantity": int_or_none(quantity),
            "region": region_name,
            "warehouse_id": string_or_none((size or {}).get("wh") or product.get("wh")),
        },
        "delivery": wb_delivery_block(product, size, region_name),
        "media": {
            "main_image_url": image_url(nm_id, basket_host),
            "image_urls": image_urls(nm_id, basket_host, photo_count),
            "photo_count": photo_count,
            "has_video": bool(((basket_card or {}).get("media") or {}).get("has_video")),
        },
        "attributes": {
            "raw": raw_attributes,
            "normalized": normalized_attributes,
        },
        "match_keys": {
            "brand": normalize_text(brand_name),
            "model": normalized_attributes.get("model"),
            "manufacturer_part_number": raw_attributes.get("Код производителя"),
            "vendor_code": (basket_card or {}).get("vendor_code"),
            "barcode": raw_attributes.get("Баркод") or raw_attributes.get("Штрихкод"),
            "color": normalized_attributes.get("color"),
            "size": normalized_attributes.get("size"),
            "semantic_key": semantic_key(brand_name, normalized_attributes, category),
        },
        "marketplace_specific": {
            "wildberries": marketplace_specific(product, basket_card, basket_host, size),
        },
        "source_meta": {
            "fetched_at": now_iso(),
            "adapter": "wildberries",
            "adapter_version": ADAPTER_VERSION,
            "endpoints": endpoints,
            "region_params": {"dest": dest, "currency": DEFAULT_CURRENCY},
            "diagnostics": diagnostics,
        },
        "raw_data": {
            "product": product,
            "basket_card": basket_card,
            "price_history": price_history,
        },
    }


def minimal_product_from_basket(nm_id: int, basket_card: dict[str, Any] | None) -> dict[str, Any]:
    basket_card = basket_card or {}
    selling = basket_card.get("selling") or {}
    data = basket_card.get("data") or {}
    return {
        "id": nm_id,
        "root": basket_card.get("imt_id"),
        "name": basket_card.get("imt_name"),
        "brand": selling.get("brand_name"),
        "supplierId": selling.get("supplier_id"),
        "subjectId": data.get("subject_id"),
        "subjectParentId": data.get("subject_root_id"),
    }


def select_size(product: dict[str, Any]) -> dict[str, Any] | None:
    sizes = product.get("sizes") or []
    for size in sizes:
        if isinstance(size.get("price"), dict) and size.get("price"):
            return size
    return sizes[0] if sizes else None


def raw_attributes_from(
    product: dict[str, Any],
    basket_card: dict[str, Any] | None,
    size: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    colors = [item.get("name") for item in product.get("colors") or [] if item.get("name")]
    if colors:
        result["Цвет"] = colors[0]
        result["colors"] = colors
    if product.get("entity"):
        result["entity"] = product["entity"]
    if size:
        if size.get("name"):
            result["Размер"] = size["name"]
        if size.get("origName"):
            result["Размер производителя"] = size["origName"]

    if basket_card:
        for option in basket_card.get("options") or []:
            name = option.get("name")
            value = option.get("value")
            if name and value is not None:
                result[name] = value
        if basket_card.get("contents"):
            result["Комплектация"] = basket_card["contents"]
        if basket_card.get("vendor_code"):
            result["Артикул продавца"] = basket_card["vendor_code"]

    return result


def normalize_attributes(raw: dict[str, Any], *, title: str | None) -> dict[str, Any]:
    return {
        "gender": normalize_gender(raw.get("Пол")),
        "color": normalize_color(raw.get("Цвет")),
        "country": normalize_country(raw.get("Страна производства")),
        "material": parse_material(raw.get("Состав")),
        "size": raw.get("Размер"),
        "memory_gb": parse_memory_gb(raw),
        "model": raw.get("Модель") or extract_model_from_title(title),
    }


def category_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> dict[str, Any]:
    name = (basket_card or {}).get("subj_name") or product.get("entity")
    parent_name = (basket_card or {}).get("subj_root_name")
    external_id = product.get("subjectId") or ((basket_card or {}).get("data") or {}).get("subject_id")
    parent_external_id = product.get("subjectParentId") or ((basket_card or {}).get("data") or {}).get("subject_root_id")
    path = [item for item in (parent_name, name) if item]
    return {
        "name": name,
        "external_id": string_or_none(external_id),
        "parent_name": parent_name,
        "parent_external_id": string_or_none(parent_external_id),
        "path": path,
    }


def marketplace_specific(
    product: dict[str, Any],
    basket_card: dict[str, Any] | None,
    basket_host: str | None,
    size: dict[str, Any] | None,
) -> dict[str, Any]:
    data = (basket_card or {}).get("data") or {}
    selling = (basket_card or {}).get("selling") or {}
    full_colors = (basket_card or {}).get("full_colors") or []
    colors = (basket_card or {}).get("colors") or []
    return {
        "nm_id": int_or_none(product.get("id") or (basket_card or {}).get("nm_id")),
        "imt_id": int_or_none((basket_card or {}).get("imt_id") or product.get("root")),
        "root": int_or_none(product.get("root") or (basket_card or {}).get("imt_id")),
        "match_id": int_or_none(product.get("matchId")),
        "brand_id": int_or_none(product.get("brandId")),
        "subject_id": int_or_none(product.get("subjectId") or data.get("subject_id")),
        "subject_parent_id": int_or_none(product.get("subjectParentId") or data.get("subject_root_id")),
        "supplier_id": int_or_none(product.get("supplierId") or selling.get("supplier_id")),
        "option_id": int_or_none((size or {}).get("optionId") or first_chrt_id(basket_card)),
        "chrt_ids": data.get("chrt_ids") or [],
        "basket_host": basket_host,
        "warehouse_id": int_or_none((size or {}).get("wh") or product.get("wh")),
        "dtype": int_or_none((size or {}).get("dtype") or product.get("dtype")),
        "colors_nm_ids": colors,
        "full_colors_nm_ids": [item.get("nm_id") for item in full_colors if item.get("nm_id")],
        "size_name": (size or {}).get("name"),
        "size_orig_name": (size or {}).get("origName"),
        "sale_conditions": int_or_none((size or {}).get("saleConditions")),
    }


def photo_count_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> int | None:
    media = (basket_card or {}).get("media") or {}
    return int_or_none(media.get("photo_count") or product.get("pics"))


def image_urls(nm_id: int, basket_host: str | None, photo_count: int | None) -> list[str]:
    if not basket_host or not photo_count:
        return []
    return [url for index in range(1, photo_count + 1) if (url := image_url(nm_id, basket_host, index))]


def title_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> str | None:
    return (basket_card or {}).get("imt_name") or product.get("name")


def brand_from(product: dict[str, Any], basket_card: dict[str, Any] | None) -> str | None:
    return product.get("brand") or ((basket_card or {}).get("selling") or {}).get("brand_name")


def first_chrt_id(basket_card: dict[str, Any] | None) -> int | None:
    chrt_ids = ((basket_card or {}).get("data") or {}).get("chrt_ids") or []
    return chrt_ids[0] if chrt_ids else None


def wb_delivery_block(
    product: dict[str, Any],
    size: dict[str, Any] | None,
    region_name: str,
) -> dict[str, Any]:
    t1 = int_or_none(product.get("time1"))
    t2 = int_or_none(product.get("time2"))
    if t1 is None and t2 is None and size:
        t1 = int_or_none(size.get("time1"))
        t2 = int_or_none(size.get("time2"))
    times = [value for value in (t1, t2) if value is not None]
    time_min = min(times) if times else None
    time_max = max(times) if times else None
    price = money(((size or {}).get("price") or {}).get("logistics"))
    return {
        "region": region_name,
        "time_min_hours": time_min,
        "time_max_hours": time_max,
        "price": price,
        "currency": "RUB",
    }


def availability_status(quantity: Any, *, stock_reliable: bool) -> str:
    if not stock_reliable:
        return "unknown"
    qty = int_or_none(quantity)
    if qty is None:
        return "unknown"
    return "in_stock" if qty > 0 else "out_of_stock"


def discount_percent(current: float | None, old: float | None) -> int | None:
    if not current or not old or old <= 0 or current >= old:
        return None
    return round((1 - current / old) * 100)


def semantic_key(
    brand: str | None,
    attributes: dict[str, Any],
    category: dict[str, Any],
) -> str:
    parts = [
        brand,
        attributes.get("model"),
        attributes.get("color"),
        attributes.get("size"),
        attributes.get("gender"),
        category.get("name"),
    ]
    return normalize_text(" ".join(str(part) for part in parts if part))


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def normalize_gender(value: Any) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    if "муж" in text or text in {"male", "men"}:
        return "male"
    if "жен" in text or text in {"female", "women"}:
        return "female"
    if "дет" in text or "реб" in text or "kid" in text:
        return "kids"
    return text


def normalize_color(value: Any) -> str | None:
    text = normalize_text(value)
    mapping = {
        "черный": "black",
        "белый": "white",
        "синий": "blue",
        "голубой": "blue",
        "серый": "gray",
        "красный": "red",
        "зеленый": "green",
        "желтый": "yellow",
        "розовый": "pink",
        "коричневый": "brown",
        "фиолетовый": "purple",
        "оранжевый": "orange",
    }
    return mapping.get(text or "", text)


def normalize_country(value: Any) -> str | None:
    text = normalize_text(value)
    mapping = {
        "вьетнам": "Vietnam",
        "китай": "China",
        "индонезия": "Indonesia",
        "камбоджа": "Cambodia",
        "пакистан": "Pakistan",
        "таиланд": "Thailand",
        "филиппины": "Philippines",
    }
    return mapping.get(text or "", str(value) if value is not None else None)


def parse_material(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    result = []
    for part in str(value).split(";"):
        match = re.search(r"(.+?)\s+(\d+(?:[.,]\d+)?)\s*%?$", part.strip())
        if not match:
            continue
        name = normalize_material_name(match.group(1))
        percent = float(match.group(2).replace(",", "."))
        result.append({"name": name, "percent": int(percent) if percent.is_integer() else percent})
    return result


def normalize_material_name(value: str) -> str:
    text = normalize_text(value) or value.lower()
    mapping = {
        "полиэтилентерефталат": "polyester",
        "полиэтилен терефталат": "polyester",
        "полиэстер": "polyester",
        "хлопок": "cotton",
        "вискоза": "viscose",
        "эластан": "elastane",
        "полиамид": "polyamide",
    }
    return mapping.get(text, text)


def parse_memory_gb(raw: dict[str, Any]) -> int | None:
    for key, value in raw.items():
        key_text = normalize_text(key) or ""
        if "памят" not in key_text and "memory" not in key_text:
            continue
        match = re.search(r"\d+", str(value))
        if match:
            return int(match.group(0))
    return None


def extract_model_from_title(title: str | None) -> str | None:
    if not title:
        return None
    return re.sub(r"\s+", " ", title).strip()


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
