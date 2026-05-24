from __future__ import annotations

import copy
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import ym_common


ADAPTER_VERSION = "0.1"
DEFAULT_REGION_NAME = ym_common.DEFAULT_REGION_NAME
DEFAULT_CURRENCY = ym_common.DEFAULT_CURRENCY

PRODUCT_ID_KEYS = (
    "modelId",
    "model_id",
    "productId",
    "product_id",
    "id",
    "marketSku",
    "sku",
    "skuId",
)
TITLE_KEYS = ("title", "name", "modelName", "productName", "displayName", "caption")
CURRENT_PRICE_KEYS = ("price", "currentPrice", "discountPrice", "value", "minPrice", "aprice")
OLD_PRICE_KEYS = ("oldPrice", "basePrice", "discountBase", "originalPrice", "priceWithoutDiscount")
IMAGE_KEYS = ("image", "imageUrl", "picture", "pictures", "photo", "photos", "imageUrls")


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


class YandexMarketClient:
    def __init__(
        self,
        timeout: float = 15,
        *,
        transport: str = ym_common.DEFAULT_TRANSPORT,
        impersonate: str = ym_common.DEFAULT_CURL_IMPERSONATE,
        fingerprint_profile: str | None = None,
        cookie_header: str | None = None,
        cookie_file: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.fingerprint_profile = fingerprint_profile
        self.cookie_source = "provided" if cookie_header else None
        if cookie_file:
            cookie_header = ym_common.cookie_header_from_file(cookie_file)
            self.cookie_source = "file"
        config = ym_common.resolve_fingerprint_config(
            transport,
            impersonate=impersonate,
            fingerprint_profile=fingerprint_profile,
        )
        self.transport = config["transport"]
        self.impersonate = config["impersonate"]
        self.session = ym_common.make_session(
            self.transport,
            impersonate=self.impersonate,
            fingerprint_profile=fingerprint_profile,
            cookie_header=cookie_header,
        )
        self.proxy_url = proxy_url
        if proxy_url:
            # Both requests.Session and curl_cffi.Session honour .proxies dict.
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.search_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=2)
        self.product_limiter = TokenBucketRateLimiter(rate_per_second=1, burst=2)

    def transport_meta(self) -> dict[str, Any]:
        meta = {"name": self.transport}
        if self.transport == "curl_cffi":
            meta["impersonate"] = self.impersonate
        if self.fingerprint_profile:
            meta["fingerprint_profile"] = self.fingerprint_profile
        if self.cookie_source:
            meta["cookies"] = self.cookie_source
        return meta

    def fetch_page(
        self,
        page_url: str,
        *,
        endpoint_kind: str,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        limiter = self.search_limiter if endpoint_kind == "search" else self.product_limiter
        limiter.wait()
        try:
            return ym_common.fetch_market_page(
                page_url,
                timeout=timeout or self.timeout,
                session=self.session,
            )
        except ym_common.YandexMarketHTTPError as exc:
            if exc.status_code in {403, 429}:
                limiter.backoff()
            raise


class YandexMarketProductParser:
    def __init__(
        self,
        client: YandexMarketClient | None = None,
        *,
        product_ttl_seconds: float = 60 * 60 * 12,
    ) -> None:
        self.client = client or YandexMarketClient()
        self.product_cache = TTLCache(product_ttl_seconds)

    def get_product(
        self,
        external_id_or_url: str | int,
        *,
        region_name: str = DEFAULT_REGION_NAME,
        region_id: int | None = None,
    ) -> dict[str, Any]:
        product_url = ym_common.product_page_path(str(external_id_or_url))
        if region_id is not None:
            product_url = ym_common.add_query_params(product_url, lr=str(region_id))
        cache_key = (product_url, region_name, region_id)
        cached = self.product_cache.get(cache_key)
        if cached is not None:
            return cached

        diagnostics: list[dict[str, Any]] = []
        endpoints: list[str] = []
        html_text = None
        final_url = ym_common.absolute_market_url(product_url)
        try:
            html_text, final_url = self.client.fetch_page(
                product_url,
                endpoint_kind="product",
                timeout=12,
            )
            endpoints.append(final_url)
        except Exception as exc:
            diagnostics.append({"stage": "product_page", "url": product_url, "message": str(exc)})

        payloads = extract_payloads_from_html(html_text or "")
        candidates = extract_yandex_market_products(payloads, html_text=html_text)
        product = best_product_candidate(candidates)
        if not product:
            product = minimal_product_from_reference(external_id_or_url, product_url)

        result = normalize_yandex_market_product(
            product,
            page_url=final_url,
            detail_payloads=payloads,
            detail_html=html_text,
            region_name=region_name,
            region_id=region_id,
            source="product",
            endpoints=endpoints,
            diagnostics=diagnostics,
            transport_meta=self.client.transport_meta(),
        )
        self.product_cache.set(cache_key, result)
        return result


class YandexMarketSearchParser:
    def __init__(
        self,
        client: YandexMarketClient | None = None,
        *,
        search_ttl_seconds: float = 60 * 15,
        product_parser: YandexMarketProductParser | None = None,
    ) -> None:
        self.client = client or YandexMarketClient()
        self.search_cache = TTLCache(search_ttl_seconds)
        self.product_parser = product_parser or YandexMarketProductParser(client=self.client)

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 20,
        region_name: str = DEFAULT_REGION_NAME,
        region_id: int | None = None,
        enrich: bool = False,
        enrich_limit: int | None = None,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        extra_query: str | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if len(query) < 3:
            raise ValueError("query must contain at least 3 characters")

        limit = max(1, min(limit, 100))
        page = max(1, page)
        enrich_limit = limit if enrich_limit is None else max(0, min(enrich_limit, limit))
        search_url = ym_common.search_page_path(query, page, region_id=region_id)

        products: list[dict[str, Any]] = []
        endpoints: list[str] = []
        page_urls: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        total = None
        max_pages = max(1, min(10, (limit + 9) // 10 + 1))

        for page_number in range(page, page + max_pages):
            page_url = ym_common.search_page_path(query, page_number, region_id=region_id)
            try:
                html_text, endpoint = self._fetch_search(
                    query, page=page_number, region_id=region_id, extra_query=extra_query,
                )
            except Exception as exc:
                if not products:
                    return {
                        "schema_version": "0.2",
                        "marketplace": "yandex_market",
                        "query": query,
                        "page": page,
                        "limit": limit,
                        "total": None,
                        "items": [],
                        "source_meta": {
                            "fetched_at": now_iso(),
                            "adapter": "yandex_market",
                            "adapter_version": ADAPTER_VERSION,
                            "endpoints": endpoints,
                            "region_params": {
                                "region": region_name,
                                "region_id": region_id,
                                "currency": DEFAULT_CURRENCY,
                            },
                            "page_url": search_url,
                            "transport": self.client.transport_meta(),
                            "diagnostics": [{"stage": "search_page", "page": page_number, "message": str(exc)}],
                        },
                    }
                diagnostics.append({"stage": "search_page", "page": page_number, "message": str(exc)})
                break

            endpoints.append(endpoint)
            page_urls.append(page_url)
            payloads = extract_payloads_from_html(html_text)
            total = total or extract_total(payloads)
            before = len(products)
            products = dedupe_candidates(products + extract_yandex_market_products(payloads, html_text=html_text))
            if len(products) >= limit:
                break
            if len(products) == before:
                break

        products = products[:limit]
        if total in (None, 0) and products:
            total = len(products)

        items = []
        for index, product in enumerate(products):
            item_diagnostics = []
            item_endpoints = list(dict.fromkeys(endpoints))
            item = None

            if enrich and index < enrich_limit:
                reference = product_reference(product)
                if reference:
                    try:
                        item = self.product_parser.get_product(
                            reference,
                            region_name=region_name,
                            region_id=region_id,
                        )
                        item["query"] = query
                        if not clean_string(item.get("title")):
                            item = None
                    except Exception as exc:
                        message = str(exc)
                        item_diagnostics.append({"stage": "product_enrichment", "message": message})
                        diagnostics.append(
                            {
                                "external_id": extract_external_id(product),
                                "stage": "product_enrichment",
                                "message": message,
                            }
                        )

            if item is None:
                item = normalize_yandex_market_product(
                    product,
                    query=query,
                    page_url=extract_url(product),
                    detail_payloads=[],
                    region_name=region_name,
                    region_id=region_id,
                    source="search",
                    endpoints=item_endpoints,
                    diagnostics=item_diagnostics,
                    transport_meta=self.client.transport_meta(),
                )
            if not clean_string(item.get("title")):
                diagnostics.append(
                    {
                        "external_id": extract_external_id(product),
                        "stage": "candidate_filter",
                        "message": "missing_title",
                    }
                )
                continue
            items.append(item)
            if item_callback is not None:
                item_callback(item, index)

        return {
            "schema_version": "0.2",
            "marketplace": "yandex_market",
            "query": query,
            "page": page,
            "limit": limit,
            "total": total,
            "items": items,
            "source_meta": {
                "fetched_at": now_iso(),
                "adapter": "yandex_market",
                "adapter_version": ADAPTER_VERSION,
                "endpoints": list(dict.fromkeys(endpoints)),
                "region_params": {
                    "region": region_name,
                    "region_id": region_id,
                    "currency": DEFAULT_CURRENCY,
                },
                "page_url": page_urls[0] if page_urls else search_url,
                "page_urls": page_urls,
                "transport": self.client.transport_meta(),
                "diagnostics": diagnostics,
            },
        }

    def _fetch_search(
        self,
        query: str,
        *,
        page: int,
        region_id: int | None = None,
        extra_query: str | None = None,
    ) -> tuple[str, str]:
        cache_key = (query, page, region_id, extra_query or "")
        cached = self.search_cache.get(cache_key)
        if cached:
            return cached["html"], cached["endpoint"]

        base_path = ym_common.search_page_path(query, page, region_id=region_id)
        if extra_query:
            sep = "&" if "?" in base_path else "?"
            base_path = f"{base_path}{sep}{extra_query.lstrip('&?')}"
        html_text, endpoint = self.client.fetch_page(
            base_path,
            endpoint_kind="search",
        )
        result = {"html": html_text, "endpoint": endpoint}
        self.search_cache.set(cache_key, result)
        return html_text, endpoint


def normalize_yandex_market_product(
    product: dict[str, Any],
    *,
    query: str | None = None,
    page_url: str | None = None,
    detail_payloads: list[Any] | None = None,
    detail_html: str | None = None,
    region_name: str = DEFAULT_REGION_NAME,
    region_id: int | None = None,
    source: str,
    endpoints: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    transport_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail_payloads = detail_payloads or []
    endpoints = endpoints or []
    diagnostics = diagnostics or []

    external_id = extract_external_id(product) or safe_parse_market_id(page_url)
    title = extract_title(product)
    brand_name, brand_id = extract_brand(product, detail_payloads)
    raw_attributes = extract_attributes(product, detail_payloads, html_text=detail_html)
    normalized_attributes = normalize_attributes(raw_attributes, title=title)
    category = extract_category(product, detail_payloads)
    price_current = extract_current_price(product)
    price_old = extract_old_price(product, current=price_current)
    image_urls = extract_image_urls(product, detail_payloads)
    marketplace_data = marketplace_specific(product, detail_payloads, page_url)
    product_url = product_url_from(product, page_url, external_id)

    source_meta = {
        "fetched_at": now_iso(),
        "adapter": "yandex_market",
        "adapter_version": ADAPTER_VERSION,
        "endpoints": endpoints,
        "region_params": {
            "region": region_name,
            "region_id": region_id,
            "currency": DEFAULT_CURRENCY,
        },
        "diagnostics": diagnostics,
    }
    if transport_meta is not None:
        source_meta["transport"] = transport_meta

    return {
        "schema_version": "0.2",
        "marketplace": "yandex_market",
        "query": query,
        "external_id": string_or_none(external_id),
        "group_id": string_or_none(extract_group_id(product) or marketplace_data.get("model_id")),
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
            "value": extract_rating(product),
            "reviews_count": extract_reviews_count(product),
        },
        "seller": extract_seller(product, detail_payloads),
        "availability": extract_availability(product, region_name=region_name),
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
            or raw_attributes.get("offerId"),
            "barcode": raw_attributes.get("Баркод") or raw_attributes.get("Штрихкод"),
            "color": normalized_attributes.get("color"),
            "size": normalized_attributes.get("size"),
            "semantic_key": semantic_key(brand_name, normalized_attributes, category),
        },
        "marketplace_specific": {
            "yandex_market": marketplace_data,
        },
        "source_meta": source_meta,
        "raw_data": {
            "product": strip_internal_keys(product),
            "detail_payloads": detail_payloads,
        },
    }


def extract_payloads_from_html(html_text: str) -> list[Any]:
    if not html_text:
        return []
    payloads: list[Any] = []
    for match in re.finditer(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html_text, re.S | re.I):
        payload = decode_json_string(html.unescape(match.group(1).strip()))
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    for match in re.finditer(r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>", html_text, re.S | re.I):
        payload = decode_json_string(html.unescape(match.group(1).strip()))
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    for match in re.finditer(r"data-zone-data=[\"']([^\"']+)[\"']", html_text, re.S | re.I):
        payload = decode_json_string(html.unescape(match.group(1)))
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    for match in re.finditer(
        r"<noframes[^>]*\bdata-apiary=[\"']patch[\"'][^>]*>(.*?)</noframes>",
        html_text,
        re.S | re.I,
    ):
        payload = decode_json_string(html.unescape(match.group(1).strip()))
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    for match in re.finditer(r"window\.__(?:INITIAL_STATE|PRELOADED_STATE|SSR_DATA)__?\s*=\s*({.*?})\s*</script>", html_text, re.S):
        payload = decode_json_string(match.group(1))
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def extract_yandex_market_products(payloads: list[Any], *, html_text: str | None = None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        if is_apiary_patch(payload):
            candidates.extend(products_from_apiary_patch(payload))
            continue
        candidates.extend(products_from_json_ld(payload))
        candidates.extend(search_result_items(payload))
        for path, node in iter_decoded_nodes(payload):
            if isinstance(node, dict):
                candidates.extend(candidate_products_from_node(node, path))
    if html_text and not candidates:
        candidates.extend(products_from_links(html_text))
    deduped = dedupe_candidates(candidates)
    inject_snippet_facts(deduped, payloads)
    return deduped


def inject_snippet_facts(candidates: list[dict[str, Any]], payloads: list[Any]) -> None:
    if not candidates:
        return
    facts = collect_snippet_facts(payloads)
    if not facts:
        return
    for candidate in candidates:
        key = candidate_lookup_key(candidate)
        if key is None:
            continue
        fact = facts.get(key)
        if not fact:
            continue
        if "stockCount" in fact and candidate.get("stockCount") is None:
            candidate["stockCount"] = fact["stockCount"]
        if "deliveryInfo" in fact and "deliveryInfo" not in candidate:
            candidate["deliveryInfo"] = fact["deliveryInfo"]


def collect_snippet_facts(payloads: list[Any]) -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for _, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            baobab = node.get("baobabPayload")
            if not isinstance(baobab, dict):
                continue
            stock = baobab.get("stockCount")
            osku = baobab.get("oskuId") or baobab.get("marketSku")
            ware = baobab.get("wareId")
            model = baobab.get("modelId")
            delivery = None
            product_payload = node.get("productPayload")
            if isinstance(product_payload, dict):
                delivery = product_payload.get("deliveryInfo")
                if not isinstance(delivery, dict):
                    for _, sub in iter_decoded_nodes(product_payload):
                        if isinstance(sub, dict) and isinstance(sub.get("deliveryInfo"), dict):
                            delivery = sub["deliveryInfo"]
                            break
            for key_value in (osku, ware, model):
                if key_value in (None, ""):
                    continue
                key = str(key_value)
                entry = facts.setdefault(key, {})
                if stock is not None and "stockCount" not in entry:
                    entry["stockCount"] = stock
                if isinstance(delivery, dict) and "deliveryInfo" not in entry:
                    entry["deliveryInfo"] = delivery
    return facts


def candidate_lookup_key(candidate: dict[str, Any]) -> str | None:
    for key in ("oskuId", "skuId", "marketSku", "wareId", "modelId", "productId", "id"):
        value = candidate.get(key)
        if value in (None, ""):
            continue
        return str(value)
    return None


def products_from_json_ld(payload: Any) -> list[dict[str, Any]]:
    products = []
    for _, node in iter_decoded_nodes(payload):
        if not isinstance(node, dict):
            continue
        node_type = node.get("@type")
        if is_search_page_aggregate_product(node):
            continue
        if node_type == "Product" or (isinstance(node_type, list) and "Product" in node_type):
            product = copy.deepcopy(node)
            product["_source_path"] = "json_ld"
            products.append(product)
        if node_type == "ItemList" and isinstance(node.get("itemListElement"), list):
            for item in node["itemListElement"]:
                if isinstance(item, dict):
                    nested = item.get("item") or item
                    if isinstance(nested, dict):
                        nested = copy.deepcopy(nested)
                        nested["_source_path"] = "json_ld/item_list"
                        products.append(nested)
    return products


def is_apiary_patch(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("widgets"), dict) and isinstance(payload.get("meta"), dict)


def products_from_apiary_patch(payload: dict[str, Any]) -> list[dict[str, Any]]:
    products = []
    for path, node in iter_decoded_nodes(payload):
        if not isinstance(node, dict) or not looks_like_apiary_product(node):
            continue
        product = copy.deepcopy(node)
        urls = product.get("urls")
        if isinstance(urls, dict) and not extract_url(product):
            direct_url = urls.get("direct")
            if isinstance(direct_url, str):
                product["url"] = direct_url
        product["_source_path"] = "apiary_patch/" + "/".join(path)
        products.append(product)
    return products


def looks_like_apiary_product(node: dict[str, Any]) -> bool:
    has_id = any(node.get(key) not in (None, "") for key in ("productId", "modelId", "skuId", "oskuId", "offerId"))
    if not has_id:
        return False
    if not clean_string(first_direct(node, ("title", "name", "productName", "modelName"))):
        return False
    has_product_payload = (
        parse_money(node.get("price")) is not None
        or clean_string(node.get("picture")) is not None
        or bool(urls_from_value(node.get("pictures")))
        or extract_url(node) is not None
    )
    return has_product_payload


def search_result_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path, node in iter_decoded_nodes(payload):
        if not isinstance(node, dict):
            continue
        path_text = "/".join(path).lower()
        if not any(token in path_text for token in ("search", "catalog", "products", "offers", "items")):
            continue
        raw_items = node.get("items") or node.get("products") or node.get("offers")
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
    for key in ("product", "model", "offer", "sku", "item"):
        value = decode_json_string(node.get(key))
        if isinstance(value, dict) and looks_like_product(value, path + (key,)):
            candidates.append(merge_product_candidate(value, node, path + (key,)))
    if looks_like_product(node, path):
        node_copy = copy.deepcopy(node)
        node_copy["_source_path"] = "/".join(path)
        candidates.append(node_copy)
    return candidates


def merge_product_candidate(product: dict[str, Any], container: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    merged = copy.deepcopy(product)
    for key in ("url", "link", "href", "price", "oldPrice", "rating", "pictures", "shop", "supplier"):
        if key not in merged and key in container:
            merged[key] = copy.deepcopy(container[key])
    merged["_container"] = copy.deepcopy(container)
    merged["_source_path"] = "/".join(path)
    return merged


def products_from_links(html_text: str) -> list[dict[str, Any]]:
    products = []
    seen = set()
    pattern = re.compile(
        r"<a[^>]+href=[\"']([^\"']*?/(?:product(?:--[^\"']+)?/\d+|card/[^\"']+/\d+)[^\"']*)[\"'][^>]*>(.*?)</a>",
        re.S | re.I,
    )
    for match in pattern.finditer(html_text):
        href = html.unescape(match.group(1))
        title = strip_tags(match.group(2))
        product_id = safe_parse_market_id(href)
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        products.append(
            {
                "modelId": product_id,
                "url": href,
                "name": title,
                "_source_path": "html_link_fallback",
            }
        )
    return products


def looks_like_product(node: dict[str, Any], path: tuple[str, ...]) -> bool:
    path_text = "/".join(path).lower()
    if any(token in path_text for token in ("/category", "/shop", "/seller", "/supplier", "/brand", "/rating")):
        return False
    if not has_direct_product_signal(node):
        return False
    if score_product_candidate(node) >= 6:
        return True
    if not any(token in path_text for token in ("product", "model", "offer", "sku", "item", "card")):
        return False
    return score_product_candidate(node) >= 4


def has_direct_product_signal(node: dict[str, Any]) -> bool:
    for key in PRODUCT_ID_KEYS:
        value = node.get(key)
        if is_scalar(value) and is_usable_product_id(value, key=key):
            return True
    if extract_url_from_direct_fields(node):
        return True
    has_title = clean_string(first_direct(node, TITLE_KEYS)) is not None
    has_price = any(parse_money(node.get(key)) is not None for key in CURRENT_PRICE_KEYS + OLD_PRICE_KEYS)
    return has_title and has_price


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
    """Collapse YM SERP tiles to one entry per product_id.

    YM groups variants (color, SIM, storage) under one product_id but the
    SERP shows each variant as a separate tile. Without product-id dedupe a
    query like "iphone 17" returns 10 near-identical cards. We keep the
    cheapest tile per product_id; alternative offers are stashed on
    `marketplace_specific.yandex_market.alt_offers` for any modal that
    wants to surface them later.
    """
    best_by_key: dict[str, dict[str, Any]] = {}
    siblings: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for candidate in candidates:
        if not is_usable_product_candidate(candidate):
            continue
        key = candidate_key(candidate)
        if not key:
            continue
        if key not in best_by_key:
            order.append(key)
            best_by_key[key] = candidate
            siblings[key] = []
            continue
        current = best_by_key[key]
        if _candidate_rank(candidate) < _candidate_rank(current):
            siblings[key].append(current)
            best_by_key[key] = candidate
        else:
            siblings[key].append(candidate)
    result: list[dict[str, Any]] = []
    for key in order:
        winner = best_by_key[key]
        extras = siblings.get(key) or []
        if extras:
            winner["_alt_offers"] = extras
        result.append(winner)
    return result


def _candidate_rank(candidate: dict[str, Any]) -> tuple[float, float]:
    """Lower is better. Cheapest priced candidate wins per product_id."""
    price = extract_current_price(candidate)
    price_key = price if price is not None and price > 0 else float("inf")
    # Higher score → lower rank (negative) so dedupe still favors complete data
    # when prices tie or are missing.
    return (price_key, -score_product_candidate(candidate))


def is_usable_product_candidate(product: dict[str, Any]) -> bool:
    if is_search_page_aggregate_product(product):
        return False
    title = extract_title(product)
    if not title or is_promotional_title(title):
        return False
    if extract_external_id(product) or extract_url(product):
        return True
    return extract_current_price(product) is not None


def is_promotional_title(title: str) -> bool:
    normalized = normalize_text(title) or ""
    return normalized in {"распродажа", "скидка", "скидки", "акция", "акции"}


def candidate_key(product: dict[str, Any]) -> str | None:
    # NOTE: prefer product_id (external_id) over offer_id/variant_id so a
    # single product with N variant tiles collapses to one card. The price
    # rank in dedupe_candidates picks the cheapest variant as the winner.
    external_id = extract_external_id(product)
    if external_id:
        return f"id:{external_id}"
    offer_id = extract_offer_id(product, {})
    if offer_id:
        return f"offer:{offer_id}"
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
        if is_scalar(value) and is_usable_product_id(value, key=key):
            return str(value)
    url_id = safe_parse_market_id(extract_url(product) or "")
    if url_id:
        return url_id
    container = product.get("_container")
    if isinstance(container, dict):
        for key in PRODUCT_ID_KEYS:
            value = container.get(key)
            if is_scalar(value) and is_usable_product_id(value, key=key):
                return str(value)
    return None


def extract_group_id(product: dict[str, Any]) -> str | None:
    return string_or_none(first_direct(product, ("modelId", "model_id", "productId", "product_id")))


def extract_variant_id(product: dict[str, Any]) -> str | None:
    return string_or_none(first_direct(product, ("sku", "skuId", "marketSku", "variantId")))


def extract_offer_id(product: dict[str, Any], raw_attributes: dict[str, Any]) -> str | None:
    return string_or_none(
        first_direct(product, ("offerId", "offer_id", "wareId", "wareMd5", "feedOfferId", "shopSku"))
        or raw_attributes.get("Артикул")
        or raw_attributes.get("offerId")
    )


def extract_title(product: dict[str, Any]) -> str | None:
    value = first_direct(product, TITLE_KEYS)
    if value is not None:
        return clean_string(value)
    value = find_value(product, TITLE_KEYS, max_depth=4)
    return clean_string(value)


def extract_url(product: dict[str, Any]) -> str | None:
    direct_url = extract_url_from_direct_fields(product)
    if direct_url:
        return ym_common.absolute_market_url(direct_url)
    value = find_value(product, ("url", "link", "href"), max_depth=4)
    if isinstance(value, str) and is_market_product_url(value):
        return ym_common.absolute_market_url(value)
    return None


def extract_url_from_direct_fields(product: dict[str, Any]) -> str | None:
    for key in ("url", "link", "href", "productUrl", "modelUrl"):
        value = product.get(key)
        if isinstance(value, str) and is_market_product_url(value):
            return value
    return None


def extract_current_price(product: dict[str, Any]) -> float | None:
    for key in CURRENT_PRICE_KEYS:
        price = parse_money(product.get(key))
        if price is not None:
            return price
    offers = product.get("offers")
    if isinstance(offers, dict):
        price = parse_money(offers.get("price"))
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
    value = first_direct(product, ("rating", "ratingValue", "grade", "averageGrade"))
    if isinstance(value, dict):
        value = first_direct(value, ("value", "rating", "average", "grade"))
    if value is None:
        aggregate = product.get("aggregateRating")
        if isinstance(aggregate, dict):
            value = aggregate.get("ratingValue")
    if value is None:
        value = find_value(product, ("ratingValue", "rating", "averageGrade"), max_depth=4)
    return number_or_none(value)


def extract_reviews_count(product: dict[str, Any]) -> int | None:
    value = first_direct(product, ("reviewsCount", "reviewCount", "opinions", "opinionsCount", "votes"))
    if isinstance(value, dict):
        value = first_direct(value, ("count", "value"))
    if value is None:
        aggregate = product.get("aggregateRating")
        if isinstance(aggregate, dict):
            value = aggregate.get("reviewCount")
    if value is None:
        value = find_value(product, ("reviewsCount", "reviewCount", "opinionsCount"), max_depth=4)
    return int_or_none(value)


def extract_brand(product: dict[str, Any], payloads: list[Any]) -> tuple[str | None, str | None]:
    brand = first_direct(product, ("brand", "vendor", "vendorName", "manufacturer"))
    brand_id = first_direct(product, ("brandId", "vendorId", "vendor_id"))
    if isinstance(brand, dict):
        brand_id = brand.get("id") or brand.get("brandId") or brand_id
        brand = brand.get("name") or brand.get("title")
    raw_attrs = extract_attributes(product, payloads)
    brand = brand or raw_attrs.get("Бренд") or raw_attrs.get("Производитель")
    return clean_string(brand), string_or_none(brand_id)


def extract_category(product: dict[str, Any], payloads: list[Any]) -> dict[str, Any]:
    path = extract_breadcrumbs(product)
    for payload in payloads:
        if path:
            break
        path = extract_breadcrumbs(payload)

    category = first_direct(product, ("category", "categoryName", "type", "hid"))
    external_id = first_direct(product, ("categoryId", "hid", "nid"))
    if isinstance(category, dict):
        external_id = category.get("id") or category.get("hid") or external_id
        category = category.get("name") or category.get("title")
    name = clean_string(category) or (path[-1] if path else None)
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


def extract_seller(product: dict[str, Any], payloads: list[Any]) -> dict[str, Any]:
    seller = first_direct(product, ("seller", "shop", "supplier", "merchant"))
    seller_id = first_direct(product, ("shopId", "supplierId", "sellerId", "businessId"))
    seller_rating = first_direct(product, ("shopRating", "sellerRating", "supplierRating"))
    seller_name = None
    if isinstance(seller, dict):
        seller_id = seller.get("id") or seller.get("shopId") or seller_id
        seller_rating = seller.get("rating") or seller.get("grade") or seller_rating
        seller_name = first_direct(seller, ("name", "title", "shopName", "sellerName"))
    elif is_scalar(seller):
        seller_name = seller
    seller_name = seller_name or first_direct(product, ("shopName", "sellerName", "supplierName"))
    if not seller_name:
        for payload in payloads:
            seller_name = find_value(payload, ("shopName", "sellerName", "supplierName"), max_depth=8)
            if seller_name:
                break
    return {
        "id": string_or_none(seller_id),
        "name": clean_string(seller_name),
        "rating": number_or_none(seller_rating),
    }


def extract_availability(product: dict[str, Any], *, region_name: str) -> dict[str, Any]:
    available = first_direct(product, ("isAvailable", "available", "inStock", "onStock"))
    quantity = int_or_none(first_direct(product, ("quantity", "stock", "stockCount", "stockItemCount", "availableCount")))
    if quantity is None:
        nested = find_value(
            product,
            ("stockCount", "stockItemCount", "stockItemsCount", "quantity"),
            max_depth=8,
        )
        quantity = int_or_none(nested)
    if quantity is None:
        last_stock = find_value(product, ("lastStock",), max_depth=8)
        if isinstance(last_stock, dict):
            quantity = int_or_none(first_direct(last_stock, ("stockItemCount", "stockCount", "quantity")))

    if not isinstance(available, bool):
        nested_available = find_value(
            product,
            ("isAvailable", "available", "inStock", "onStock"),
            max_depth=8,
        )
        if isinstance(nested_available, bool):
            available = nested_available

    status = "unknown"
    if isinstance(available, bool):
        status = "in_stock" if available else "out_of_stock"
    elif quantity is not None:
        status = "in_stock" if quantity > 0 else "out_of_stock"
    else:
        text = normalize_text(first_direct(product, ("availability", "status", "stockStatus"))) or ""
        if any(token in text for token in ("нет", "out", "unavailable")):
            status = "out_of_stock"
        elif any(token in text for token in ("есть", "available", "stock", "достав")):
            status = "in_stock"
    return {
        "status": status,
        "quantity": quantity,
        "region": region_name,
        "warehouse_id": string_or_none(first_direct(product, ("warehouseId", "warehouse_id"))),
    }


def extract_delivery(product: dict[str, Any], payloads: list[Any], *, region_name: str) -> dict[str, Any]:
    delivery = first_direct(product, ("delivery", "deliveryInfo", "deliveryOptions"))
    time_min: float | int | None = None
    time_max: float | int | None = None
    price = None
    if isinstance(delivery, dict):
        time_min = number_or_none(first_direct(delivery, ("timeMinHours", "minHours", "min")))
        time_max = number_or_none(first_direct(delivery, ("timeMaxHours", "maxHours", "max")))
        price = parse_money(first_direct(delivery, ("price", "deliveryPrice")))

    if time_min is None or time_max is None:
        fastest = fastest_delivery_option([product, *payloads])
        if fastest:
            time_min = time_min if time_min is not None else fastest[0]
            time_max = time_max if time_max is not None else fastest[1]

    if time_min is None or time_max is None:
        for payload in payloads:
            text = find_value(payload, ("deliveryTime", "deliveryDate", "delivery"), max_depth=8)
            hours = hours_from_delivery_text(text)
            if hours:
                time_min = time_min if time_min is not None else hours[0]
                time_max = time_max if time_max is not None else hours[1]
                break
    if time_min is None or time_max is None:
        for text in delivery_text_candidates([product, *payloads]):
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


def fastest_delivery_option(sources: list[Any]) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    for source in sources:
        for _, node in iter_decoded_nodes(source):
            if not isinstance(node, dict):
                continue
            options = node.get("options")
            if not isinstance(options, list):
                continue
            for option in options:
                if not isinstance(option, dict):
                    continue
                hours = option_hours(option)
                if hours is None:
                    continue
                if best is None or hours[0] < best[0] or (hours[0] == best[0] and hours[1] < best[1]):
                    best = hours
    return best


def option_hours(option: dict[str, Any]) -> tuple[float, float] | None:
    day_from = number_or_none(option.get("dayFrom"))
    day_to = number_or_none(option.get("dayTo"))
    text_candidates: list[Any] = []
    baobab = option.get("baobabModel")
    if isinstance(baobab, dict):
        text_candidates.append(baobab.get("deliveryText"))
    for key in ("typePrefix", "consoleDate", "date"):
        block = option.get(key)
        if isinstance(block, dict):
            text_candidates.append(block.get("value"))
    if (day_from == 0 and day_to == 0) or day_from is None:
        for text in text_candidates:
            hours = hours_from_delivery_text(text)
            if hours:
                return hours
    if day_from is None and day_to is None:
        return None
    df = day_from if day_from is not None else day_to
    dt = day_to if day_to is not None else day_from
    if df == 0 and dt == 0:
        return 1.0, 24.0
    return float(df) * 24, float(dt) * 24


def delivery_text_candidates(payloads: list[Any]) -> list[str]:
    result: list[str] = []
    for payload in payloads:
        for path, node in iter_decoded_nodes(payload):
            path_text = "/".join(path).lower()
            if not any(token in path_text for token in ("delivery", "courier", "pickup")):
                continue
            for text in flatten_text_values(node):
                normalized = normalize_text(text) or ""
                if any(token in normalized for token in ("достав", "курьер", "пункт", "сегодня", "завтра", "мин", "час")):
                    result.append(text)
    return ym_common.unique(result)


def flatten_text_values(value: Any) -> list[str]:
    value = decode_json_string(value)
    if is_scalar(value):
        text = clean_string(value)
        return [text] if text else []
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("title", "subtitle", "text", "value", "label", "description", "deliveryTime", "deliveryDate"):
            if key in value:
                result.extend(flatten_text_values(value.get(key)))
        for key in ("content", "items", "signals", "options", "deliveryBlocks"):
            if key in value:
                result.extend(flatten_text_values(value.get(key)))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(flatten_text_values(item))
        return result
    return []


def extract_image_urls(product: dict[str, Any], payloads: list[Any]) -> list[str]:
    urls = []
    for key in IMAGE_KEYS:
        urls.extend(urls_from_value(product.get(key)))
    container = product.get("_container")
    if isinstance(container, dict):
        for key in IMAGE_KEYS:
            urls.extend(urls_from_value(container.get(key)))
    for payload in payloads:
        for _, node in iter_decoded_nodes(payload):
            if isinstance(node, dict):
                for key in IMAGE_KEYS:
                    urls.extend(urls_from_value(node.get(key)))
    return ym_common.unique([url for url in urls if is_image_url(url)])[:30]


def extract_description(product: dict[str, Any], payloads: list[Any]) -> str | None:
    value = first_direct(product, ("description", "descriptionText", "annotation"))
    if clean_string(value):
        return clean_string(value)
    for payload in payloads:
        value = find_value(payload, ("description", "descriptionText", "annotation"), max_depth=8)
        if clean_string(value):
            return clean_string(value)
    return None


def extract_attributes(
    product: dict[str, Any],
    payloads: list[Any],
    *,
    html_text: str | None = None,
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
    for key in ("offerId", "wareMd5", "shopSku", "vendorCode"):
        value = clean_string(first_direct(product, (key,)))
        if value:
            result.setdefault("Артикул", value)
    for key in ("specs", "parameters", "params", "characteristics", "filters", "properties"):
        collect_attribute_value(result, product.get(key))
    for payload in payloads:
        for path, node in iter_decoded_nodes(payload):
            if not isinstance(node, dict):
                continue
            path_text = "/".join(path).lower()
            if not any(token in path_text for token in ("spec", "param", "characteristic", "attribute", "property")):
                continue
            for key in ("specs", "parameters", "params", "characteristics", "filters", "properties"):
                collect_attribute_value(result, node.get(key))
    if html_text:
        for name, value in extract_specs_from_html(html_text).items():
            result.setdefault(name, value)
    return result


_PRODUCT_SPEC_MARKER = re.compile(r'data-auto="product-spec"')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.DOTALL)
_LABEL_AFTER_MARKER_RE = re.compile(r">(.*?)</span>", re.DOTALL)
_SPEC_NOISE = {
    "все характеристики",
    "характеристики",
    "подробнее",
    "развернуть",
    "свернуть",
}


def _strip_tags(value: str) -> str:
    text = _TAG_RE.sub("", value)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def extract_specs_from_html(html_text: str) -> dict[str, str]:
    """Pull characteristics from the rendered YM product page DOM.

    Why: YM SSR-renders product specs as <span data-auto="product-spec">label</span>
    + a sibling value span. The embedded JSON payloads sometimes omit them, leaving
    `attributes.raw` empty even though the page shows 15-20 characteristics.
    """
    if not html_text:
        return {}
    positions = [m.start() for m in _PRODUCT_SPEC_MARKER.finditer(html_text)]
    if not positions:
        return {}
    positions.append(len(html_text))
    specs: dict[str, str] = {}
    for i in range(len(positions) - 1):
        chunk = html_text[positions[i] : positions[i + 1]]
        label_match = _LABEL_AFTER_MARKER_RE.search(chunk)
        if not label_match:
            continue
        label = _strip_tags(label_match.group(1))
        if not label:
            continue
        rest = chunk[label_match.end() :]
        value: str | None = None
        for span_match in _SPAN_RE.finditer(rest):
            text = _strip_tags(span_match.group(1))
            if not text:
                continue
            if text.lower() in _SPEC_NOISE:
                continue
            value = text
            break
        if value and label not in specs:
            specs[label] = value
    return specs


def collect_attribute_value(result: dict[str, Any], value: Any) -> None:
    value = decode_json_string(value)
    if isinstance(value, dict):
        name = clean_string(first_direct(value, ("name", "title", "key", "attributeName", "xslname")))
        attr_value = first_direct(value, ("value", "text", "values", "attributeValue"))
        if name and attr_value is not None and not isinstance(attr_value, (dict, list)):
            result.setdefault(name, clean_string(attr_value) or attr_value)
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
        "model": first_attr(raw, ("Модель", "Название модели", "model", "Model")) or extract_model_from_title(title),
    }


def marketplace_specific(product: dict[str, Any], payloads: list[Any], product_url: str | None) -> dict[str, Any]:
    external_id = extract_external_id(product)
    return {
        "model_id": int_or_none(first_direct(product, ("modelId", "model_id", "productId")) or external_id),
        "sku_id": int_or_none(first_direct(product, ("skuId", "sku", "marketSku"))),
        "offer_id": string_or_none(first_direct(product, ("offerId", "offer_id", "wareId", "wareMd5", "feedOfferId"))),
        "ware_md5": string_or_none(first_direct(product, ("wareMd5", "ware_md5", "wareId"))),
        "feed_id": int_or_none(first_direct(product, ("feedId", "feed_id"))),
        "shop_id": int_or_none(first_direct(product, ("shopId", "supplierId", "sellerId"))),
        "category_id": int_or_none(first_direct(product, ("categoryId", "hid", "nid"))),
        "brand_id": int_or_none(first_direct(product, ("brandId", "vendorId"))),
        "url_path": ym_common.market_page_path(product_url) if product_url else None,
        "source_path": product.get("_source_path"),
        "embedded_payload_count": len(payloads),
    }


def product_url_from(product: dict[str, Any], page_url: str | None, external_id: str | None) -> str | None:
    explicit_url = page_url or extract_url(product)
    if explicit_url:
        return ym_common.absolute_market_url(explicit_url)
    if external_id:
        return ym_common.absolute_market_url(ym_common.product_page_path(str(external_id)))
    return None


def product_reference(product: dict[str, Any]) -> str | None:
    return extract_url(product) or extract_external_id(product)


def minimal_product_from_reference(reference: str | int, product_url: str) -> dict[str, Any]:
    external_id = safe_parse_market_id(str(reference)) or safe_parse_market_id(product_url) or str(reference)
    return {
        "modelId": external_id,
        "url": product_url,
        "_source_path": "minimal_product_from_reference",
    }


def extract_total(payloads: list[Any]) -> int | None:
    zero_seen = False
    for payload in payloads:
        value = find_value(payload, ("total", "totalCount", "totalItems", "found", "count"), max_depth=8)
        total = int_or_none(value)
        if total is not None and total > 0:
            return total
        if total == 0:
            zero_seen = True
    return 0 if zero_seen else None


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
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("valueFmt", "text", "amount", "price", "current", "min", "max", "value"):
            price = parse_money(value.get(key))
            if price is not None:
                return price
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return int(result) if result.is_integer() else round(result, 2)
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
        return "https:" + value
    return value


def is_image_url(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return lowered.startswith("http") and any(token in lowered for token in (".jpg", ".jpeg", ".png", ".webp", "/get-", "/mpic"))


def has_video(product: dict[str, Any], payloads: list[Any]) -> bool:
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
            return min(numbers), max(numbers)
    if any(token in text for token in ("день", "дня", "дней", "дн")):
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if numbers:
            return min(numbers) * 24, max(numbers) * 24
    if "послезавтра" in text:
        return 48, 72
    if "завтра" in text:
        return 24, 48
    if "сегодня" in text:
        return 1, 24
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


def semantic_key(brand: str | None, attributes: dict[str, Any], category: dict[str, Any]) -> str:
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
        value = first_direct(value, ("value", "rating", "score", "grade"))
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


def is_usable_product_id(value: Any, *, key: str) -> bool:
    if key == "id":
        return bool(re.fullmatch(r"\d{5,}", str(value)))
    return True


def is_search_page_aggregate_product(product: dict[str, Any]) -> bool:
    offers = product.get("offers")
    title = normalize_text(product.get("name") or product.get("title")) or ""
    if not isinstance(offers, dict):
        return False
    offer_type = offers.get("@type")
    if offer_type != "AggregateOffer":
        return False
    return "купить" in title or offers.get("offerCount") is not None


def clean_string(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()
    return text or None


def strip_tags(value: str) -> str | None:
    text = re.sub(r"<[^>]+>", " ", value)
    return clean_string(html.unescape(text))


def is_market_product_url(value: str) -> bool:
    return "/product" in value or "/card/" in value


def safe_parse_market_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return ym_common.parse_market_id(value)
    except ValueError:
        return None


def strip_internal_keys(product: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in product.items() if not key.startswith("_")}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
