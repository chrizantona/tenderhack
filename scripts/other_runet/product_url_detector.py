from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


PRODUCT_URL_HINTS: tuple[str, ...] = (
    "/product/", "/products/", "/item/", "/items/", "/goods/", "/tovar/",
    "/catalog/item/", "/catalog/product/", "/p/", "/sku/", "/shop/",
)

LISTING_URL_HINTS: tuple[str, ...] = (
    "/search", "/category", "/categories", "/listing",
)

CART_LIKE_URL_HINTS: tuple[str, ...] = (
    "/cart", "/login", "/compare", "/favorite", "/checkout", "/profile",
    "/about", "/contacts", "/delivery", "/oferta", "/help", "/news",
    "/blog", "/feedback",
)

PRICE_REGEX = re.compile(r"(\d{1,3}(?:[\s ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб|р\b)", re.I)
SKU_TEXT_REGEX = re.compile(r"(?:артикул|sku|код товара|vendor[\s_-]?code)\s*[:#№]?\s*([a-z0-9./_-]{3,32})", re.I)


def url_only_product_likelihood(url: str) -> float:
    path = urlparse(url).path.lower()
    if not path:
        return 0.0
    if any(hint in path for hint in CART_LIKE_URL_HINTS):
        return 0.0
    if any(hint in path for hint in PRODUCT_URL_HINTS):
        base = 0.6
    elif any(hint in path for hint in LISTING_URL_HINTS):
        base = 0.1
    else:
        base = 0.25
    if re.search(r"\d{4,}", path):
        base += 0.15
    if path.endswith(".html"):
        base += 0.05
    return min(1.0, base)


def score_html_for_product(html_text: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    signals: dict[str, Any] = {
        "json_ld_product": 0.0,
        "price_detected": 0.0,
        "sku_or_vendor_code": 0.0,
        "product_url_pattern": 0.0,
        "breadcrumbs_product_depth": 0.0,
        "offer_availability": 0.0,
        "canonical_self": 0.0,
    }

    json_ld_product, json_ld_offer, json_ld_payloads = _scan_json_ld(soup)
    if json_ld_product:
        signals["json_ld_product"] = 1.0
    if json_ld_offer:
        signals["offer_availability"] = 1.0

    has_price = False
    if json_ld_offer and "price" in str(json_ld_payloads).lower():
        has_price = True
    if not has_price and soup.find(attrs={"itemprop": "price"}):
        has_price = True
    if not has_price:
        for selector in ("meta[property='product:price:amount']", "meta[name='price']"):
            if soup.select_one(selector):
                has_price = True
                break
    if not has_price and PRICE_REGEX.search(soup.get_text(" ", strip=True)):
        has_price = True
    if has_price:
        signals["price_detected"] = 1.0

    sku_found = False
    if soup.find(attrs={"itemprop": "sku"}):
        sku_found = True
    if not sku_found:
        for payload in json_ld_payloads:
            if "sku" in payload or "mpn" in payload or "gtin13" in payload:
                sku_found = True
                break
    if not sku_found and SKU_TEXT_REGEX.search(soup.get_text(" ", strip=True)):
        sku_found = True
    if sku_found:
        signals["sku_or_vendor_code"] = 1.0

    signals["product_url_pattern"] = 1.0 if any(hint in url.lower() for hint in PRODUCT_URL_HINTS) else 0.0

    breadcrumbs_depth = _detect_breadcrumbs_depth(soup, json_ld_payloads)
    signals["breadcrumbs_product_depth"] = min(1.0, breadcrumbs_depth / 4.0)

    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        canonical_url = urljoin(url, canonical_tag["href"])
        if _normalize_url(canonical_url) == _normalize_url(url):
            signals["canonical_self"] = 1.0

    score = (
        signals["json_ld_product"] * 0.30
        + signals["price_detected"] * 0.20
        + signals["sku_or_vendor_code"] * 0.15
        + signals["product_url_pattern"] * 0.10
        + signals["breadcrumbs_product_depth"] * 0.10
        + signals["offer_availability"] * 0.10
        + signals["canonical_self"] * 0.05
    )
    return {
        "score": round(score, 3),
        "signals": signals,
        "classification": _classify(score),
        "json_ld_product": json_ld_product,
        "json_ld_offer": json_ld_offer,
        "json_ld_payloads": json_ld_payloads,
    }


def _classify(score: float) -> str:
    if score >= 0.65:
        return "product"
    if score >= 0.45:
        return "ambiguous"
    return "ignore"


def _scan_json_ld(soup: BeautifulSoup) -> tuple[bool, bool, list[dict[str, Any]]]:
    has_product = False
    has_offer = False
    payloads: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            decoded = json.loads(html.unescape(raw.strip()))
        except json.JSONDecodeError:
            continue
        for node in _walk_json_ld(decoded):
            payloads.append(node)
            types = node.get("@type")
            type_list = types if isinstance(types, list) else [types]
            normalized_types = [str(item).lower() for item in type_list if item]
            if "product" in normalized_types:
                has_product = True
            if any(name in normalized_types for name in ("offer", "aggregateoffer")):
                has_offer = True
            offers = node.get("offers")
            if offers:
                has_offer = True
    return has_product, has_offer, payloads


def _walk_json_ld(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_walk_json_ld(item))
        return result
    if not isinstance(value, dict):
        return []
    nodes = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            nodes.extend(_walk_json_ld(item))
    return nodes


def _detect_breadcrumbs_depth(soup: BeautifulSoup, payloads: list[dict[str, Any]]) -> int:
    for payload in payloads:
        types = payload.get("@type")
        type_list = types if isinstance(types, list) else [types]
        if any(str(item).lower() == "breadcrumblist" for item in type_list if item):
            items = payload.get("itemListElement") or []
            if isinstance(items, list):
                return len(items)
    crumbs = soup.select("[itemtype*='Breadcrumb'] [itemprop='item']")
    if crumbs:
        return len(crumbs)
    crumbs = soup.select(".breadcrumbs a, nav.breadcrumb a, ol.breadcrumb li, .crumbs a")
    return len(crumbs)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{(parsed.netloc or '').lower()}{parsed.path or ''}".rstrip("/")
