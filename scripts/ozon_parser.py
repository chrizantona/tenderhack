from __future__ import annotations

import copy
import json
import re
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

import ozon_common
import ozon_headless_common


ADAPTER_VERSION = "0.1"
DEFAULT_REGION_NAME = ozon_common.DEFAULT_REGION_NAME
DEFAULT_CURRENCY = ozon_common.DEFAULT_CURRENCY
DEFAULT_SEARCH_PAGE_SIZE = 36

COMPOSER_ENDPOINTS = ozon_common.COMPOSER_ENDPOINTS
DEFAULT_HEADERS = ozon_common.DEFAULT_HEADERS

PRODUCT_ID_KEYS = (
    "sku",
    "skuId",
    "sku_id",
    "id",
    "productId",
    "product_id",
    "ozonId",
    "itemId",
    "cardId",
)
TITLE_KEYS = ("title", "name", "productName", "shortName", "caption")
CURRENT_PRICE_KEYS = (
    "finalPrice",
    "final_price",
    "priceSale",
    "salePrice",
    "currentPrice",
    "cardPrice",
    "price",
)
OLD_PRICE_KEYS = (
    "priceBase",
    "basePrice",
    "originalPrice",
    "oldPrice",
    "beforeDiscountPrice",
    "price",
)
IMAGE_KEYS = ("image", "imageUrl", "image_url", "coverImage", "mainImage", "picture")


class OzonHTTPError(RuntimeError):
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


try:
    from curl_cffi import requests as cffi_requests
    CFFI_AVAILABLE = True
except Exception as _cffi_exc:
    CFFI_AVAILABLE = False
    CFFI_IMPORT_ERROR = repr(_cffi_exc)


CFFI_IMPERSONATE = "chrome120"


class OzonClient:
    def __init__(
        self,
        timeout: float = 15,
        *,
        impersonate: str = CFFI_IMPERSONATE,
        warmup: bool = True,
    ) -> None:
        self.timeout = timeout
        self.impersonate = impersonate
        self.search_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=2)
        self.product_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=2)
        self.cffi_active = False
        if CFFI_AVAILABLE:
            self.session = cffi_requests.Session(impersonate=self.impersonate)
            self.session.headers.update(ozon_common.DEFAULT_HEADERS)
            self.cffi_active = True
        else:
            self.session = ozon_common.make_session()
        self._warmed_up = False
        if warmup:
            self._warmup()

    def _warmup(self) -> None:
        if self._warmed_up:
            return
        for url in (
            "https://api.ozon.ru/",
            "https://api.ozon.ru/composer-api.bx/page/json/v2?url=%2F",
        ):
            try:
                self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except Exception:
                pass
        self._warmed_up = True

    def fetch_page(
        self,
        page_url: str,
        *,
        endpoint_kind: str,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        normalized_url = ozon_common.ozon_page_path(page_url)
        errors: list[str] = []
        for endpoint in ozon_common.COMPOSER_ENDPOINTS:
            try:
                payload = self.get_json(
                    endpoint,
                    params={"url": normalized_url},
                    endpoint_kind=endpoint_kind,
                    timeout=timeout,
                )
                return payload, endpoint
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")
        raise RuntimeError("All Ozon composer endpoints failed:\n" + "\n".join(errors))

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        endpoint_kind: str,
        timeout: float | None = None,
    ) -> Any:
        limiter = self._limiter_for(endpoint_kind)
        last_error: Exception | None = None
        for attempt in range(3):
            limiter.wait()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=timeout or self.timeout,
                    allow_redirects=True,
                )
            except Exception as exc:
                last_error = exc
                continue
            if response.status_code == 429:
                limiter.backoff()
                last_error = OzonHTTPError(429, str(response.url), "rate limited")
                continue
            if response.status_code == 200 and response.content:
                try:
                    return response.json()
                except ValueError:
                    last_error = RuntimeError(
                        f"GET {response.url} returned non-JSON: {ozon_common.response_error_summary(response)}"
                    )
                    continue
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                last_error = OzonHTTPError(
                    response.status_code,
                    str(response.url),
                    f"unfollowed redirect to {location}",
                )
                continue
            body = ozon_common.response_error_summary(response)
            last_error = OzonHTTPError(response.status_code, str(response.url), body)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"GET {url} failed without an explicit error")

    def _limiter_for(self, endpoint_kind: str) -> TokenBucketRateLimiter:
        if endpoint_kind == "search":
            return self.search_limiter
        return self.product_limiter


class OzonProductParser:
    def __init__(
        self,
        client: OzonClient | None = None,
        *,
        product_ttl_seconds: float = 60 * 60 * 12,
    ) -> None:
        self.client = client or OzonClient()
        self.product_cache = TTLCache(product_ttl_seconds)

    def get_product(
        self,
        external_id_or_url: str | int,
        *,
        region_name: str = DEFAULT_REGION_NAME,
        include_extra_layouts: bool = True,
        fetch_timeout: float = 12,
    ) -> dict[str, Any]:
        product_url = ozon_common.product_page_path(str(external_id_or_url))
        cache_key = (product_url, region_name, include_extra_layouts)
        cached = self.product_cache.get(cache_key)
        if cached is not None:
            return cached

        diagnostics: list[dict[str, Any]] = []
        endpoints: list[str] = []
        payloads: list[dict[str, Any]] = []

        for index, page_url in enumerate(ozon_common.product_layout_urls(product_url, include_extra_layouts)):
            try:
                payload, endpoint = self.client.fetch_page(
                    page_url,
                    endpoint_kind="product",
                    timeout=fetch_timeout,
                )
                payloads.append(payload)
                endpoints.append(endpoint)
            except Exception as exc:
                diagnostics.append(
                    {
                        "stage": "product_page" if index == 0 else "product_extra_layout",
                        "url": page_url,
                        "message": str(exc),
                    }
                )

        candidates = extract_ozon_products(payloads)
        product = best_product_candidate(candidates)
        if not product:
            product = minimal_product_from_reference(external_id_or_url, product_url)

        result = normalize_ozon_product(
            product,
            page_url=product_url,
            detail_payloads=payloads,
            region_name=region_name,
            source="product",
            endpoints=endpoints,
            diagnostics=diagnostics,
        )
        self.product_cache.set(cache_key, result)
        return result


class OzonSearchParser:
    def __init__(
        self,
        client: OzonClient | None = None,
        *,
        search_ttl_seconds: float = 60 * 15,
        product_parser: OzonProductParser | None = None,
    ) -> None:
        self.client = client or OzonClient()
        self.search_cache = TTLCache(search_ttl_seconds)
        self.product_parser = product_parser or OzonProductParser(client=self.client)

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 20,
        region_name: str = DEFAULT_REGION_NAME,
        enrich: bool = False,
        enrich_limit: int | None = None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        enrich_executor: Any | None = None,
        enrich_client_factory: Callable[[], Any] | None = None,
        enrich_block_check: Callable[[], bool] | None = None,
        enrich_block_trigger: Callable[[float], None] | None = None,
        enrich_stagger_seconds: float = 0.0,
        enrich_task_timeout: float = 30.0,
        base_emit_stagger_seconds: float = 0.15,
    ) -> dict[str, Any]:
        query = query.strip()
        if len(query) < 3:
            raise ValueError("query must contain at least 3 characters")

        limit = max(1, min(limit, 100))
        page = max(1, page)
        enrich_limit = limit if enrich_limit is None else max(0, min(enrich_limit, limit))

        products: list[dict[str, Any]] = []
        endpoints: list[str] = []
        page_urls: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        total = None
        max_pages = max(1, min(5, (limit + DEFAULT_SEARCH_PAGE_SIZE - 1) // DEFAULT_SEARCH_PAGE_SIZE + 1))

        for page_number in range(page, page + max_pages):
            try:
                payload, endpoint, search_url = self._fetch_search(query, page=page_number)
            except Exception as exc:
                search_url = ozon_common.search_page_path(query, page_number)
                if not products:
                    return {
                        "schema_version": "0.2",
                        "marketplace": "ozon",
                        "query": query,
                        "page": page,
                        "limit": limit,
                        "total": None,
                        "items": [],
                        "source_meta": {
                            "fetched_at": now_iso(),
                            "adapter": "ozon",
                            "adapter_version": ADAPTER_VERSION,
                            "endpoints": endpoints,
                            "region_params": {"region": region_name, "currency": DEFAULT_CURRENCY},
                            "page_url": search_url,
                            "diagnostics": [{"stage": "search_page", "page": page_number, "message": str(exc)}],
                        },
                    }
                diagnostics.append({"stage": "search_page", "page": page_number, "message": str(exc)})
                break

            endpoints.append(endpoint)
            page_urls.append(search_url)
            total = total or extract_total(payload)
            before = len(products)
            products = dedupe_candidates(products + extract_ozon_products([payload]))
            if len(products) >= limit:
                break
            if len(products) == before:
                break

        products = products[:limit]

        items: list[dict[str, Any]] = []
        base_items: list[dict[str, Any]] = []
        will_enrich = bool(enrich and enrich_limit > 0)
        for index, product in enumerate(products):
            item_endpoints = list(dict.fromkeys(endpoints))
            base_item = normalize_ozon_product(
                product,
                query=query,
                page_url=extract_url(product),
                detail_payloads=[],
                region_name=region_name,
                source="search",
                endpoints=item_endpoints,
                diagnostics=[],
            )
            base_items.append(base_item)
            items.append(base_item)
            # When enrichment is on, each item will re-emit with full data as
            # its enrichment finishes — emitting a "naked" base item first
            # makes cards appear without characteristics for ~15-20s, then
            # refresh. WB/YM emit each card already fully populated; match that
            # by suppressing the base emit for items we'll enrich.
            if item_callback is not None and not (will_enrich and index < enrich_limit):
                item_callback(base_item, index)
                # Search-page returns all tiles at once — stagger emits so the
                # UI shows items appearing gradually.
                if base_emit_stagger_seconds > 0 and index < len(products) - 1:
                    time.sleep(base_emit_stagger_seconds)

        if enrich and enrich_limit > 0:
            if enrich_executor is not None and enrich_client_factory is not None:
                self._enrich_parallel(
                    products=products,
                    base_items=base_items,
                    items=items,
                    enrich_limit=enrich_limit,
                    region_name=region_name,
                    query=query,
                    diagnostics=diagnostics,
                    item_callback=item_callback,
                    executor=enrich_executor,
                    factory=enrich_client_factory,
                    block_check=enrich_block_check,
                    block_trigger=enrich_block_trigger,
                    stagger_seconds=enrich_stagger_seconds,
                    task_timeout=enrich_task_timeout,
                )
            else:
                self._enrich_serial(
                    products=products,
                    base_items=base_items,
                    items=items,
                    enrich_limit=enrich_limit,
                    region_name=region_name,
                    query=query,
                    diagnostics=diagnostics,
                    item_callback=item_callback,
                )

        return {
            "schema_version": "0.2",
            "marketplace": "ozon",
            "query": query,
            "page": page,
            "limit": limit,
            "total": total,
            "items": items,
            "source_meta": {
                "fetched_at": now_iso(),
                "adapter": "ozon",
                "adapter_version": ADAPTER_VERSION,
                "endpoints": list(dict.fromkeys(endpoints)),
                "region_params": {"region": region_name, "currency": DEFAULT_CURRENCY},
                "page_url": page_urls[0] if page_urls else ozon_common.search_page_path(query, page),
                "page_urls": page_urls,
                "diagnostics": diagnostics,
            },
        }

    def _enrich_serial(
        self,
        *,
        products: list[dict[str, Any]],
        base_items: list[dict[str, Any]],
        items: list[dict[str, Any]],
        enrich_limit: int,
        region_name: str,
        query: str,
        diagnostics: list[dict[str, Any]],
        item_callback: Callable[[dict[str, Any], int], None] | None,
    ) -> None:
        for index in range(min(enrich_limit, len(products))):
            product = products[index]
            reference = product_reference(product)
            if not reference:
                continue
            try:
                enriched = self.product_parser.get_product(
                    reference,
                    region_name=region_name,
                    include_extra_layouts=False,
                    fetch_timeout=15,
                )
            except Exception as exc:
                diagnostics.append(
                    {
                        "external_id": extract_external_id(product),
                        "stage": "product_enrichment",
                        "message": str(exc),
                    }
                )
                continue
            enriched["query"] = query
            if is_enriched_blocked(enriched):
                diagnostics.append(
                    {
                        "external_id": extract_external_id(product),
                        "stage": "product_enrichment",
                        "message": "ozon_antibot_captcha",
                    }
                )
                continue
            merged = merge_normalized_products(base_items[index], enriched)
            items[index] = merged
            if item_callback is not None:
                item_callback(merged, index)

    def _enrich_parallel(
        self,
        *,
        products: list[dict[str, Any]],
        base_items: list[dict[str, Any]],
        items: list[dict[str, Any]],
        enrich_limit: int,
        region_name: str,
        query: str,
        diagnostics: list[dict[str, Any]],
        item_callback: Callable[[dict[str, Any], int], None] | None,
        executor: Any,
        factory: Callable[[], Any],
        block_check: Callable[[], bool] | None,
        block_trigger: Callable[[float], None] | None,
        stagger_seconds: float,
        task_timeout: float,
    ) -> None:
        submit_state: dict[str, float] = {"last": 0.0}
        submit_lock = threading.Lock()

        def stagger_wait() -> None:
            if stagger_seconds <= 0:
                return
            jitter_min = max(0.3, stagger_seconds * 0.7)
            jitter_max = stagger_seconds * 1.6
            actual_gap = random.uniform(jitter_min, jitter_max)
            sleep_for = 0.0
            with submit_lock:
                now = time.monotonic()
                gap = submit_state["last"] + actual_gap - now
                if gap > 0:
                    sleep_for = gap
                    submit_state["last"] = now + gap
                else:
                    submit_state["last"] = now
            if sleep_for > 0:
                time.sleep(sleep_for)

        def task(reference: str) -> dict[str, Any]:
            if block_check is not None and block_check():
                raise RuntimeError("ozon_block_active")
            stagger_wait()
            client = factory()
            parser = OzonProductParser(client=client)
            return parser.get_product(
                reference,
                region_name=region_name,
                include_extra_layouts=False,
                fetch_timeout=15,
            )

        futures: dict[int, Any] = {}
        for index in range(min(enrich_limit, len(products))):
            product = products[index]
            reference = product_reference(product)
            if not reference:
                continue
            futures[index] = executor.submit(task, reference)

        for index, future in futures.items():
            product = products[index]
            try:
                enriched = future.result(timeout=task_timeout)
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                if block_trigger is not None and "429" in lowered:
                    block_trigger(15.0)
                diagnostics.append(
                    {
                        "external_id": extract_external_id(product),
                        "stage": "product_enrichment",
                        "message": message,
                    }
                )
                continue
            enriched["query"] = query
            if is_enriched_blocked(enriched):
                diagnostics.append(
                    {
                        "external_id": extract_external_id(product),
                        "stage": "product_enrichment",
                        "message": "ozon_antibot_captcha",
                    }
                )
                continue
            merged = merge_normalized_products(base_items[index], enriched)
            items[index] = merged
            if item_callback is not None:
                item_callback(merged, index)

    def _fetch_search(self, query: str, *, page: int) -> tuple[dict[str, Any], str, str]:
        cache_key = (query, page)
        cached = self.search_cache.get(cache_key)
        if cached:
            return cached["payload"], cached["endpoint"], cached["search_url"]

        search_url = ozon_common.search_page_path(query, page)
        payload, endpoint = self.client.fetch_page(search_url, endpoint_kind="search")
        result = {"payload": payload, "endpoint": endpoint, "search_url": search_url}
        self.search_cache.set(cache_key, result)
        return payload, endpoint, search_url


def is_enriched_blocked(enriched: dict[str, Any]) -> bool:
    if not isinstance(enriched, dict):
        return False
    title = enriched.get("title")
    if ozon_headless_common.is_blocked_text(title):
        return True
    raw_data = enriched.get("raw_data") or {}
    detail_payloads = raw_data.get("detail_payloads") or []
    for payload in detail_payloads:
        if isinstance(payload, dict) and payload.get("_blocked"):
            return True
        if isinstance(payload, dict) and ozon_headless_common.is_blocked_text(payload.get("title")):
            return True
    return False


def merge_normalized_products(base: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(base, dict):
        return enriched
    if not isinstance(enriched, dict):
        return base
    result = copy.deepcopy(enriched)
    for key, base_value in base.items():
        if base_value in (None, "", [], {}):
            continue
        enriched_value = result.get(key)
        if enriched_value in (None, "", [], {}):
            result[key] = copy.deepcopy(base_value)
            continue
        if isinstance(base_value, dict) and isinstance(enriched_value, dict):
            result[key] = _merge_dict_keep_base(base_value, enriched_value)
        elif isinstance(base_value, list) and isinstance(enriched_value, list) and not enriched_value:
            result[key] = copy.deepcopy(base_value)
    return result


def _merge_dict_keep_base(base: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(enriched)
    for key, base_value in base.items():
        if base_value in (None, "", [], {}):
            continue
        enriched_value = result.get(key)
        if enriched_value in (None, "", [], {}):
            result[key] = copy.deepcopy(base_value)
            continue
        if isinstance(base_value, dict) and isinstance(enriched_value, dict):
            result[key] = _merge_dict_keep_base(base_value, enriched_value)
    return result


def normalize_ozon_product(
    product: dict[str, Any],
    *,
    query: str | None = None,
    page_url: str | None = None,
    detail_payloads: list[dict[str, Any]] | None = None,
    region_name: str = DEFAULT_REGION_NAME,
    source: str,
    endpoints: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detail_payloads = detail_payloads or []
    endpoints = endpoints or []
    diagnostics = diagnostics or []

    external_id = extract_external_id(product) or ozon_common.extract_id_from_url(page_url or "")
    title = extract_title(product)
    brand_name, brand_id = extract_brand(product, detail_payloads)
    raw_attributes = extract_attributes(product, detail_payloads)
    normalized_attributes = normalize_attributes(raw_attributes, title=title)
    category = extract_category(product, detail_payloads)
    price_current = extract_current_price(product)
    price_old = extract_old_price(product, current=price_current)
    rating_value = extract_rating(product)
    reviews_count = extract_reviews_count(product)
    seller = extract_seller(product, detail_payloads)
    product_url_candidate = page_url or extract_url(product)
    if not product_url_candidate and external_id:
        try:
            product_url_candidate = ozon_common.product_page_path(str(external_id))
        except ValueError:
            product_url_candidate = None
    product_url = ozon_common.absolute_ozon_url(product_url_candidate) if product_url_candidate else None
    image_urls = extract_image_urls(product, detail_payloads)
    marketplace_data = marketplace_specific(product, detail_payloads, product_url)

    return {
        "schema_version": "0.2",
        "marketplace": "ozon",
        "query": query,
        "external_id": string_or_none(external_id),
        "group_id": string_or_none(extract_group_id(product) or marketplace_data.get("product_id")),
        "variant_id": string_or_none(extract_variant_id(product) or external_id),
        "offer_id": string_or_none(extract_offer_id(product, raw_attributes)),
        "url": product_url,
        "title": title,
        "normalized_title": normalize_text(" ".join(value for value in (brand_name, title) if value)),
        "description": extract_description(product, detail_payloads),
        "brand": {
            "name": brand_name,
            "external_id": string_or_none(brand_id),
        },
        "category": category,
        "price": {
            "current": price_current,
            "old": price_old,
            "currency": DEFAULT_CURRENCY,
            "discount_percent": discount_percent(price_current, price_old),
            "source": source,
            "updated_at": now_iso(),
        },
        "rating": {
            "value": rating_value,
            "reviews_count": reviews_count,
        },
        "seller": seller,
        "availability": extract_availability(product, detail_payloads, region_name=region_name),
        "delivery": extract_delivery(product, detail_payloads, region_name=region_name),
        "media": {
            "main_image_url": image_urls[0] if image_urls else None,
            "image_urls": image_urls,
            "photo_count": len(image_urls) or None,
            "has_video": has_video(product, detail_payloads),
        },
        "attributes": {
            "raw": raw_attributes,
            "normalized": normalized_attributes,
        },
        "match_keys": {
            "brand": normalize_text(brand_name),
            "model": normalized_attributes.get("model"),
            "manufacturer_part_number": raw_attributes.get("Код производителя")
            or raw_attributes.get("Manufacturer Part Number"),
            "vendor_code": raw_attributes.get("Артикул")
            or raw_attributes.get("Артикул продавца")
            or raw_attributes.get("article"),
            "barcode": raw_attributes.get("Баркод") or raw_attributes.get("Штрихкод"),
            "color": normalized_attributes.get("color"),
            "size": normalized_attributes.get("size"),
            "semantic_key": semantic_key(brand_name, normalized_attributes, category),
        },
        "marketplace_specific": {
            "ozon": marketplace_data,
        },
        "source_meta": {
            "fetched_at": now_iso(),
            "adapter": "ozon",
            "adapter_version": ADAPTER_VERSION,
            "endpoints": endpoints,
            "region_params": {"region": region_name, "currency": DEFAULT_CURRENCY},
            "diagnostics": diagnostics,
        },
        "raw_data": {
            "product": strip_internal_keys(product),
            "detail_payloads": detail_payloads,
        },
    }


def extract_ozon_products(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        candidates.extend(search_result_items(payload))
        for path, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            candidates.extend(candidate_products_from_node(node, path))

    return dedupe_candidates(candidates)


def search_result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path, node in iter_decoded_nodes(payload):
        if not isinstance(node, dict):
            continue
        path_text = "/".join(path).lower()
        if "searchresult" not in path_text and "catalog" not in path_text:
            continue
        raw_items = node.get("items")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if isinstance(item, dict):
                item = copy.deepcopy(item)
                item["_source_path"] = "/".join(path + ("items",))
                items.append(item)
    return items


def candidate_products_from_node(node: dict[str, Any], path: tuple[str, ...]) -> list[dict[str, Any]]:
    candidates = []
    tracking = decode_json_string(node.get("cellTrackingInfo"))
    if isinstance(tracking, dict) and isinstance(tracking.get("product"), dict):
        candidates.append(merge_product_candidate(tracking["product"], node, path))

    for key in ("product", "sku", "item"):
        value = decode_json_string(node.get(key))
        if isinstance(value, dict) and looks_like_product(value, path):
            candidates.append(merge_product_candidate(value, node, path + (key,)))

    if looks_like_product(node, path):
        node_copy = copy.deepcopy(node)
        node_copy["_source_path"] = "/".join(path)
        candidates.append(node_copy)

    return candidates


def merge_product_candidate(
    product: dict[str, Any],
    container: dict[str, Any],
    path: tuple[str, ...],
) -> dict[str, Any]:
    merged = copy.deepcopy(product)
    for key in ("url", "link", "href", "action", "image", "imageUrl", "images", "rating"):
        if key not in merged and key in container:
            merged[key] = copy.deepcopy(container[key])
    merged["_container"] = copy.deepcopy(container)
    merged["_source_path"] = "/".join(path)
    return merged


def looks_like_product(node: dict[str, Any], path: tuple[str, ...]) -> bool:
    if score_product_candidate(node) >= 6:
        return True
    path_text = "/".join(path).lower()
    if not any(token in path_text for token in ("product", "searchresult", "catalog", "sku", "tile")):
        return False
    return score_product_candidate(node) >= 4


def score_product_candidate(product: dict[str, Any]) -> int:
    score = 0
    if extract_external_id(product):
        score += 3
    if extract_title(product):
        score += 3
    if extract_current_price(product) is not None or extract_old_price(product) is not None:
        score += 3
    if extract_url(product):
        score += 2
    if extract_image_urls(product, []):
        score += 1
    if extract_rating(product) is not None:
        score += 1
    return score


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = candidate_key(candidate)
        if not key:
            continue
        if key not in best_by_key:
            order.append(key)
            best_by_key[key] = candidate
            continue
        if score_product_candidate(candidate) > score_product_candidate(best_by_key[key]):
            best_by_key[key] = candidate
    return [best_by_key[key] for key in order]


def candidate_key(product: dict[str, Any]) -> str | None:
    external_id = extract_external_id(product)
    if external_id:
        return f"id:{external_id}"
    url = extract_url(product)
    if url:
        return f"url:{url}"
    title = extract_title(product)
    if title:
        return f"title:{normalize_text(title)}"
    return None


def best_product_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    return max(candidates, key=score_product_candidate)


def extract_external_id(product: dict[str, Any]) -> str | None:
    for key in PRODUCT_ID_KEYS:
        value = product.get(key)
        if is_scalar(value):
            return str(value)
    url_id = ozon_common.extract_id_from_url(extract_url(product) or "")
    if url_id:
        return url_id
    container = product.get("_container")
    if isinstance(container, dict):
        for key in PRODUCT_ID_KEYS:
            value = container.get(key)
            if is_scalar(value):
                return str(value)
    return None


def extract_group_id(product: dict[str, Any]) -> str | None:
    return string_or_none(
        first_direct(product, ("productId", "product_id", "cardId", "modelId", "variantGroupId"))
    )


def extract_variant_id(product: dict[str, Any]) -> str | None:
    return string_or_none(first_direct(product, ("sku", "skuId", "sku_id", "variantId")))


def extract_offer_id(product: dict[str, Any], raw_attributes: dict[str, Any]) -> str | None:
    return string_or_none(
        first_direct(product, ("offerId", "offer_id", "article", "vendorCode", "sellerProductId"))
        or raw_attributes.get("Артикул")
        or raw_attributes.get("Артикул продавца")
    )


def extract_title(product: dict[str, Any]) -> str | None:
    value = first_direct(product, TITLE_KEYS)
    if value is not None:
        return clean_string(value)
    value = find_value(product, TITLE_KEYS, max_depth=4)
    return clean_string(value)


def extract_url(product: dict[str, Any]) -> str | None:
    for key in ("url", "link", "href", "productUrl", "webUrl"):
        value = product.get(key)
        if isinstance(value, str) and "/product/" in value:
            return ozon_common.absolute_ozon_url(value)
    action = product.get("action")
    if isinstance(action, dict):
        for key in ("link", "url"):
            value = action.get(key)
            if isinstance(value, str) and "/product/" in value:
                return ozon_common.absolute_ozon_url(value)
    value = find_value(product, ("url", "link", "href"), max_depth=4)
    if isinstance(value, str) and "/product/" in value:
        return ozon_common.absolute_ozon_url(value)
    return None


def extract_current_price(product: dict[str, Any]) -> float | None:
    for key in CURRENT_PRICE_KEYS:
        price = parse_money(product.get(key))
        if price is not None:
            return price
    value = find_value(product, CURRENT_PRICE_KEYS, max_depth=4)
    return parse_money(value)


def extract_old_price(product: dict[str, Any], *, current: float | None = None) -> float | None:
    values = []
    for key in OLD_PRICE_KEYS:
        price = parse_money(product.get(key))
        if price is not None:
            values.append(price)
    value = find_value(product, OLD_PRICE_KEYS, max_depth=4)
    price = parse_money(value)
    if price is not None:
        values.append(price)

    for value in values:
        if current is None or value > current:
            return value
    return None


def extract_rating(product: dict[str, Any]) -> float | int | None:
    value = first_direct(product, ("rating", "ratingValue", "score", "stars"))
    if isinstance(value, dict):
        value = first_direct(value, ("value", "rating", "score"))
    if value is None:
        value = find_value(product, ("ratingValue", "rating", "score"), max_depth=4)
    return number_or_none(value)


def extract_reviews_count(product: dict[str, Any]) -> int | None:
    value = first_direct(
        product,
        ("reviewsCount", "reviewCount", "feedbacks", "commentsCount", "comments", "opinions"),
    )
    if isinstance(value, dict):
        value = first_direct(value, ("count", "value"))
    if value is None:
        value = find_value(
            product,
            ("reviewsCount", "reviewCount", "feedbacks", "commentsCount"),
            max_depth=4,
        )
    return int_or_none(value)


def extract_brand(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    brand = first_direct(product, ("brand", "brandName"))
    brand_id = first_direct(product, ("brandId", "brand_id"))
    if isinstance(brand, dict):
        brand_id = brand.get("id") or brand.get("brandId") or brand_id
        brand = brand.get("name") or brand.get("title")
    raw_attrs = extract_attributes(product, payloads)
    brand = brand or raw_attrs.get("Бренд") or raw_attrs.get("brand")
    return clean_string(brand), string_or_none(brand_id)


def extract_category(product: dict[str, Any], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    path = extract_breadcrumbs(product)
    for payload in payloads:
        if path:
            break
        path = extract_breadcrumbs(payload)

    name = clean_string(first_direct(product, ("category", "categoryName", "type", "typeName")))
    external_id = first_direct(product, ("categoryId", "category_id", "typeId"))
    if not name and path:
        name = path[-1]
    parent_name = path[-2] if len(path) > 1 else None
    return {
        "name": name,
        "external_id": string_or_none(external_id),
        "parent_name": parent_name,
        "parent_external_id": None,
        "path": path,
    }


def extract_breadcrumbs(source: Any) -> list[str]:
    for _, node in iter_decoded_nodes(source):
        if not isinstance(node, dict):
            continue
        for key in ("breadcrumbs", "breadCrumbs", "categoryPath", "path"):
            value = node.get(key)
            path = breadcrumb_list(value)
            if path:
                return path
    return []


def breadcrumb_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, str):
            text = clean_string(item)
        elif isinstance(item, dict):
            text = clean_string(first_direct(item, ("name", "title", "text", "caption")))
        else:
            text = None
        if text:
            result.append(text)
    return result


def extract_seller(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    seller = first_direct(product, ("seller", "merchant", "shop"))
    seller_id = first_direct(product, ("sellerId", "merchantId", "shopId"))
    seller_rating = first_direct(product, ("sellerRating", "merchantRating", "shopRating"))
    seller_name = None
    if isinstance(seller, dict):
        seller_id = seller.get("id") or seller.get("sellerId") or seller_id
        seller_rating = seller.get("rating") or seller_rating
        seller_name = first_direct(seller, ("name", "title", "sellerName"))
    elif is_scalar(seller):
        seller_name = seller

    if not seller_name:
        seller_name = first_direct(product, ("sellerName", "merchantName", "shopName"))

    if not seller_name:
        for payload in payloads:
            seller_name = find_value(payload, ("sellerName", "merchantName", "shopName"), max_depth=8)
            if seller_name:
                break

    return {
        "id": string_or_none(seller_id),
        "name": clean_string(seller_name),
        "rating": number_or_none(seller_rating),
    }


def extract_availability(
    product: dict[str, Any],
    payloads: list[dict[str, Any]] | None = None,
    *,
    region_name: str,
) -> dict[str, Any]:
    payloads = payloads or []
    available = first_direct(product, ("isAvailable", "available", "inStock"))
    quantity = int_or_none(first_direct(product, ("quantity", "stock", "stockCount", "availableCount")))
    status: str | None = None
    if isinstance(available, bool):
        status = "in_stock" if available else "out_of_stock"
    elif quantity is not None:
        status = "in_stock" if quantity > 0 else "out_of_stock"

    if status is None:
        status = _availability_from_schema_text(product, payloads)

    if status is None:
        status = _availability_from_widget_states(payloads)

    if status is None:
        status = _availability_from_button_text(product, payloads)

    return {
        "status": status or "unknown",
        "quantity": quantity,
        "region": region_name,
        "warehouse_id": string_or_none(first_direct(product, ("warehouseId", "warehouse_id"))),
    }


def _availability_from_schema_text(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> str | None:
    sources: list[Any] = [product, *payloads]
    for source in sources:
        value = find_value(source, ("availability", "status", "stockStatus"), max_depth=8)
        if value is None:
            continue
        text = normalize_text(value) or ""
        if not text:
            continue
        if "outofstock" in text or "out of stock" in text or "unavailable" in text or "нет в наличии" in text:
            return "out_of_stock"
        if "instock" in text or "in stock" in text or "available" in text or "есть в наличии" in text:
            return "in_stock"
    return None


def _availability_from_widget_states(payloads: list[dict[str, Any]]) -> str | None:
    for payload in payloads:
        for path, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            path_text = "/".join(path).lower()
            if not any(token in path_text for token in ("websale", "webprice", "webaddtocart", "widgetstates", "offer")):
                continue
            value = node.get("isAvailable")
            if isinstance(value, bool):
                return "in_stock" if value else "out_of_stock"
    return None


def _availability_from_button_text(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> str | None:
    sources: list[Any] = [product, *payloads]
    out_markers = (
        "товар закончился",
        "нет в наличии",
        "товар недоступен",
        "out of stock",
    )
    in_markers = (
        "в корзину",
        "купить сейчас",
        "add to cart",
    )
    for source in sources:
        for _, node in iter_decoded_nodes(source):
            if not isinstance(node, dict):
                continue
            text = node.get("text")
            if not isinstance(text, str):
                continue
            lower = text.lower()
            if any(marker in lower for marker in out_markers):
                return "out_of_stock"
            if any(marker in lower for marker in in_markers):
                return "in_stock"
    return None


def extract_delivery(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
    *,
    region_name: str,
) -> dict[str, Any]:
    delivery = first_direct(product, ("delivery", "deliveryInfo"))
    time_min: float | int | None = None
    time_max: float | int | None = None
    price = None
    if isinstance(delivery, dict):
        time_min = number_or_none(first_direct(delivery, ("timeMinHours", "minHours", "min")))
        time_max = number_or_none(first_direct(delivery, ("timeMaxHours", "maxHours", "max")))
        price = parse_money(first_direct(delivery, ("price", "deliveryPrice")))

    if time_min is None or time_max is None:
        hours = _delivery_hours_from_days(product, payloads)
        if hours:
            time_min = time_min if time_min is not None else hours[0]
            time_max = time_max if time_max is not None else hours[1]

    if time_min is None or time_max is None:
        for source in (product, *payloads):
            text = find_value(
                source,
                (
                    "deliveryTime",
                    "deliveryDate",
                    "deliveryText",
                    "delivery",
                    "shippingText",
                    "shipping",
                ),
                max_depth=8,
            )
            hours = hours_from_delivery_text(text)
            if hours:
                time_min = time_min if time_min is not None else hours[0]
                time_max = time_max if time_max is not None else hours[1]
                break

    return {
        "region": region_name,
        "time_min_hours": time_min,
        "time_max_hours": time_max,
        "price": price,
        "currency": DEFAULT_CURRENCY,
    }


def _delivery_hours_from_days(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> tuple[float, float] | None:
    for source in (product, *payloads):
        for _, node in iter_decoded_nodes(source):
            if not isinstance(node, dict):
                continue
            day_from = node.get("dayFrom")
            day_to = node.get("dayTo")
            if not isinstance(day_from, (int, float)) and not isinstance(day_to, (int, float)):
                continue
            df = number_or_none(day_from)
            dt = number_or_none(day_to)
            if df is None and dt is None:
                continue
            df = df if df is not None else dt
            dt = dt if dt is not None else df
            if df == 0 and dt == 0:
                text_value = first_direct(node, ("deliveryText",))
                if text_value is None:
                    type_prefix = node.get("typePrefix")
                    if isinstance(type_prefix, dict):
                        text_value = type_prefix.get("value")
                if text_value is None:
                    date_value = node.get("date")
                    if isinstance(date_value, dict):
                        text_value = date_value.get("value")
                hours = hours_from_delivery_text(text_value)
                if hours:
                    return hours
                return 1, 24
            return float(df) * 24, float(dt) * 24
    return None


def extract_image_urls(product: dict[str, Any], payloads: list[dict[str, Any]]) -> list[str]:
    urls = []
    for key in IMAGE_KEYS:
        urls.extend(urls_from_value(product.get(key)))
    urls.extend(urls_from_value(product.get("images")))
    urls.extend(urls_from_value(product.get("photos")))
    container = product.get("_container")
    if isinstance(container, dict):
        for key in IMAGE_KEYS:
            urls.extend(urls_from_value(container.get(key)))

    for payload in payloads:
        for _, node in iter_decoded_nodes(payload):
            if isinstance(node, dict):
                for key in IMAGE_KEYS:
                    urls.extend(urls_from_value(node.get(key)))

    return unique([url for url in urls if is_image_url(url)])[:30]


def extract_description(product: dict[str, Any], payloads: list[dict[str, Any]]) -> str | None:
    value = first_direct(product, ("description", "descriptionText", "annotation"))
    if clean_string(value):
        return clean_string(value)
    for payload in payloads:
        for path, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            path_text = "/".join(path).lower()
            if "description" not in path_text and "pdp" not in path_text:
                continue
            value = first_direct(node, ("description", "descriptionText", "text", "content"))
            if clean_string(value):
                return clean_string(value)
    return None


def extract_attributes(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("color", "colorName"):
        value = clean_string(first_direct(product, (key,)))
        if value:
            result.setdefault("Цвет", value)
    for key in ("size", "sizeName"):
        value = clean_string(first_direct(product, (key,)))
        if value:
            result.setdefault("Размер", value)
    for key in ("article", "vendorCode"):
        value = clean_string(first_direct(product, (key,)))
        if value:
            result.setdefault("Артикул", value)

    collect_attribute_value(result, product.get("attributes"))
    collect_attribute_value(result, product.get("characteristics"))
    collect_attribute_value(result, product.get("properties"))
    collect_attribute_value(result, product.get("params"))

    for payload in payloads:
        for path, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            path_text = "/".join(path).lower()
            if not any(token in path_text for token in ("characteristic", "description", "pdp", "attribute")):
                continue
            for key in ("attributes", "characteristics", "properties", "params", "additions"):
                collect_attribute_value(result, node.get(key))
    return result


def collect_attribute_value(result: dict[str, Any], value: Any) -> None:
    value = decode_json_string(value)
    if isinstance(value, dict):
        name = clean_string(first_direct(value, ("name", "title", "key", "attributeName")))
        attr_value = first_direct(value, ("value", "text", "values", "attributeValue"))
        if name and attr_value is not None and not isinstance(attr_value, (dict, list)):
            result.setdefault(name, clean_string(attr_value) or attr_value)
        if name and isinstance(attr_value, list):
            text_parts = []
            for item in attr_value:
                item = decode_json_string(item)
                if isinstance(item, dict):
                    txt = first_direct(item, ("text", "title", "value", "name"))
                    if isinstance(txt, str) and txt.strip():
                        text_parts.append(txt.strip())
                elif isinstance(item, (str, int, float)) and item not in ("", None):
                    text_parts.append(str(item))
            if text_parts:
                result.setdefault(name, ", ".join(text_parts))
        if not name and all(
            isinstance(v, (str, int, float)) and v not in ("", None)
            for v in value.values()
        ):
            for key, scalar in value.items():
                text = clean_string(scalar)
                if text:
                    result.setdefault(key, text)
            return
        for item in value.values():
            collect_attribute_value(result, item)
    elif isinstance(value, list):
        for item in value:
            collect_attribute_value(result, item)


def normalize_attributes(raw: dict[str, Any], *, title: str | None) -> dict[str, Any]:
    return {
        "gender": normalize_gender(first_attr(raw, ("Пол", "gender", "Для кого"))),
        "color": normalize_color(first_attr(raw, ("Цвет", "color", "Color"))),
        "country": normalize_country(first_attr(raw, ("Страна производства", "country", "Country"))),
        "material": parse_material(first_attr(raw, ("Состав", "Материал", "material"))),
        "size": first_attr(raw, ("Размер", "size", "Size")),
        "memory_gb": parse_memory_gb(raw),
        "model": first_attr(raw, ("Модель", "Название модели", "model", "Model"))
        or extract_model_from_title(title),
    }


def marketplace_specific(
    product: dict[str, Any],
    payloads: list[dict[str, Any]],
    product_url: str | None,
) -> dict[str, Any]:
    external_id = extract_external_id(product)
    return {
        "sku": int_or_none(first_direct(product, ("sku", "skuId", "sku_id")) or external_id),
        "product_id": int_or_none(first_direct(product, ("productId", "product_id", "id"))),
        "ozon_id": string_or_none(external_id),
        "seller_id": int_or_none(first_direct(product, ("sellerId", "merchantId", "shopId"))),
        "category_id": int_or_none(first_direct(product, ("categoryId", "category_id", "typeId"))),
        "brand_id": int_or_none(first_direct(product, ("brandId", "brand_id"))),
        "url_path": ozon_common.ozon_page_path(product_url) if product_url else None,
        "source_path": product.get("_source_path"),
        "layout_payload_count": len(payloads),
    }


def product_reference(product: dict[str, Any]) -> str | None:
    return extract_url(product) or extract_external_id(product)


def product_page_path(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Ozon product id or URL is empty")
    if "ozon.ru" in value or value.startswith("/"):
        path = ozon_page_path(value)
        if "/product/" not in path:
            product_id = extract_id_from_url(path)
            if product_id:
                return f"/product/{product_id}/"
            raise ValueError(f"Cannot find Ozon product path in: {value!r}")
        return ensure_trailing_slash_without_query(path)
    product_id = extract_id_from_url(value) or re.sub(r"\D+", "", value)
    if not product_id:
        raise ValueError(f"Cannot find Ozon product id in: {value!r}")
    return f"/product/{product_id}/"


def product_layout_urls(product_url: str, include_extra_layouts: bool) -> list[str]:
    urls = [product_url]
    if include_extra_layouts:
        urls.append(add_query_params(product_url, layout_container="pdppage2copy", layout_page_index="2"))
        urls.append(add_query_params(product_url, layout_container="appPdpPage3", layout_page_index="3"))
    return unique(urls)


def minimal_product_from_reference(reference: str | int, product_url: str) -> dict[str, Any]:
    external_id = (
        ozon_common.extract_id_from_url(str(reference))
        or ozon_common.extract_id_from_url(product_url)
        or str(reference)
    )
    return {
        "id": external_id,
        "url": product_url,
        "_source_path": "minimal_product_from_reference",
    }


def search_page_path(query: str, page: int) -> str:
    # Keep the inner Ozon URL readable here. requests will percent-encode the
    # whole value once when it is sent as composer-api's `url` query parameter.
    return f"/search/?text={query}&from_global=true&page={page}"


def ozon_page_path(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return path
    if not value.startswith("/"):
        return "/" + value
    return value


def absolute_ozon_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value
    if value.startswith("//"):
        return "https:" + value
    if not value.startswith("/"):
        value = "/" + value
    return "https://www.ozon.ru" + value


def ensure_trailing_slash_without_query(path: str) -> str:
    parsed = urlparse(path)
    clean_path = parsed.path
    if not clean_path.endswith("/"):
        clean_path += "/"
    if parsed.query:
        return clean_path + "?" + parsed.query
    return clean_path


def add_query_params(path: str, **params: str) -> str:
    parsed = urlparse(path)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(("", "", parsed.path, "", urlencode(query), ""))


def extract_id_from_url(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"/product/[^/?#]*?(\d{5,})(?:[/?#]|$)", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{5,})\b", value)
    if match:
        return match.group(1)
    return None


def extract_total(payload: dict[str, Any]) -> int | None:
    value = find_value(payload, ("totalFound", "total", "totalItems", "itemsCount"), max_depth=8)
    return int_or_none(value)


def iter_decoded_nodes(value: Any, path: tuple[str, ...] = (), depth: int = 0) -> Any:
    if depth > 20:
        return
    decoded = decode_json_string(value)
    yield path, decoded
    if isinstance(decoded, dict):
        for key, item in decoded.items():
            yield from iter_decoded_nodes(item, path + (str(key),), depth + 1)
    elif isinstance(decoded, list):
        for index, item in enumerate(decoded):
            yield from iter_decoded_nodes(item, path + (str(index),), depth + 1)


def decode_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def first_direct(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def find_value(source: Any, keys: tuple[str, ...], *, max_depth: int) -> Any:
    for path, node in iter_decoded_nodes(source):
        if len(path) > max_depth:
            continue
        if not isinstance(node, dict):
            continue
        value = first_direct(node, keys)
        if value not in (None, ""):
            return value
    return None


def parse_money(value: Any) -> float | None:
    value = decode_json_string(value)
    if value is None:
        return None
    if isinstance(value, dict):
        for key in CURRENT_PRICE_KEYS + OLD_PRICE_KEYS + ("value", "amount"):
            price = parse_money(value.get(key))
            if price is not None:
                return price
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else round(float(value), 2)
    if not isinstance(value, str):
        return None
    text = value.replace("\u00a0", " ").replace("\u202f", " ")
    match = re.search(r"\d[\d\s.,]*", text)
    if not match:
        return None
    number = match.group(0).replace(" ", "").replace(",", ".")
    try:
        result = float(number)
    except ValueError:
        return None
    return int(result) if result.is_integer() else round(result, 2)


def clean_string(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()
    return text or None


def urls_from_value(value: Any) -> list[str]:
    value = decode_json_string(value)
    if isinstance(value, str):
        return [absolute_asset_url(value)]
    if isinstance(value, dict):
        urls = []
        for key in ("url", "src", "image", "imageUrl", "link"):
            if isinstance(value.get(key), str):
                urls.append(absolute_asset_url(value[key]))
        for item in value.values():
            urls.extend(urls_from_value(item))
        return urls
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(urls_from_value(item))
        return urls
    return []


def absolute_asset_url(value: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    return _upscale_ozon_image(value)


def _upscale_ozon_image(value: str) -> str:
    if not isinstance(value, str) or "ozon" not in value:
        return value
    return re.sub(r"/wc\d+/", "/wc1000/", value)


def is_image_url(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return lowered.startswith("http") and any(token in lowered for token in (".jpg", ".jpeg", ".png", ".webp", "/image"))


def has_video(product: dict[str, Any], payloads: list[dict[str, Any]]) -> bool:
    if bool(first_direct(product, ("hasVideo", "video", "videos"))):
        return True
    for payload in payloads:
        for _, node in iter_decoded_nodes(payload):
            if isinstance(node, dict) and bool(first_direct(node, ("hasVideo", "video", "videos"))):
                return True
    return False


def hours_from_delivery_text(value: Any) -> tuple[float, float] | None:
    text = normalize_text(value)
    if not text:
        return None
    if "мин" in text:
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if numbers:
            return min(numbers) / 60, max(numbers) / 60
    if "час" in text:
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if numbers:
            return float(min(numbers)), float(max(numbers))
    if any(token in text for token in ("дней", "дня", "день", " дн")):
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if numbers:
            return float(min(numbers)) * 24, float(max(numbers)) * 24
    if "послезавтра" in text:
        return 48.0, 72.0
    if "завтра" in text:
        return 24.0, 48.0
    if "сегодня" in text:
        return 1.0, 24.0
    return None


def first_attr(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if raw.get(key) not in (None, ""):
            return raw[key]
    normalized = {normalize_text(key): value for key, value in raw.items()}
    for key in keys:
        value = normalized.get(normalize_text(key))
        if value not in (None, ""):
            return value
    return None


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
        "россия": "Russia",
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
        if not any(token in key_text for token in ("памят", "memory", "накопител", "объем")):
            continue
        match = re.search(r"\d+", str(value))
        if match:
            return int(match.group(0))
    return None


def extract_model_from_title(title: str | None) -> str | None:
    if not title:
        return None
    return re.sub(r"\s+", " ", title).strip()


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


def discount_percent(current: float | None, old: float | None) -> int | None:
    if not current or not old or old <= 0 or current >= old:
        return None
    return round((1 - current / old) * 100)


def int_or_none(value: Any) -> int | None:
    value = decode_json_string(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value.replace(" ", ""))
        if match:
            return int(match.group(0))
    return None


def number_or_none(value: Any) -> float | int | None:
    value = decode_json_string(value)
    if isinstance(value, dict):
        value = first_direct(value, ("value", "rating", "score"))
    if isinstance(value, str):
        value = value.replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", value)
        value = match.group(0) if match else None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return int(as_float) if as_float.is_integer() else as_float


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and value not in ("", None)


def unique(values: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def strip_internal_keys(product: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in product.items() if not key.startswith("_")}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def response_error_summary(response: requests.Response) -> str:
    text = response.text[:1000].replace("\n", " ").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            incident_id = payload.get("incidentId") or payload.get("incident_id")
            if incident_id:
                return f"Ozon challenge or block response, incidentId={incident_id}"
            message = payload.get("message") or payload.get("error")
            if message:
                return str(message)[:300]
    if "abt-challenge" in text or "challenge" in text.lower():
        return "Ozon challenge page"
    return text[:300]
