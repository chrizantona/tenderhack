from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag


SOURCE_RELIABILITY: dict[str, float] = {
    "feed": 0.95,
    "json_ld": 0.90,
    "internal_json": 0.85,
    "microdata": 0.80,
    "open_graph": 0.75,
    "html": 0.65,
    "snippet": 0.45,
    "qwen_fallback": 0.70,
}


PRICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d{1,3}(?:[\s ]\d{3})+(?:[.,]\d{1,2})?)\s*(?:₽|руб|р\b)", re.I),
    re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб|р\b)", re.I),
)

AVAILABILITY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("в наличии", "in_stock"),
    ("есть в наличии", "in_stock"),
    ("instock", "in_stock"),
    ("in_stock", "in_stock"),
    ("в магазине", "in_stock"),
    ("under order", "preorder"),
    ("preorder", "preorder"),
    ("под заказ", "preorder"),
    ("ожидается", "preorder"),
    ("нет в наличии", "out_of_stock"),
    ("out of stock", "out_of_stock"),
    ("outofstock", "out_of_stock"),
    ("снят с продажи", "out_of_stock"),
    ("закончился", "out_of_stock"),
)

JSON_LD_TYPES_PRODUCT = {"product", "individualproduct", "productmodel"}
JSON_LD_TYPES_OFFER = {"offer", "aggregateoffer"}


@dataclass
class Evidence:
    field: str
    value: Any
    source: str
    confidence: float
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "detail": self.detail,
        }


@dataclass
class ExtractedOffer:
    url: str
    title: str | None = None
    price_current: float | None = None
    price_old: float | None = None
    currency: str | None = None
    availability: str | None = None
    sku: str | None = None
    mpn: str | None = None
    gtin: str | None = None
    brand: str | None = None
    description: str | None = None
    category: str | None = None
    breadcrumbs: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    canonical_url: str | None = None
    extraction_path: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def has_minimum_fields(self) -> bool:
        return bool(self.title) and bool(self.price_current)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "price_current": self.price_current,
            "price_old": self.price_old,
            "currency": self.currency,
            "availability": self.availability,
            "sku": self.sku,
            "mpn": self.mpn,
            "gtin": self.gtin,
            "brand": self.brand,
            "description": self.description,
            "category": self.category,
            "breadcrumbs": list(self.breadcrumbs),
            "images": list(self.images),
            "attributes": dict(self.attributes),
            "canonical_url": self.canonical_url,
            "extraction_path": list(self.extraction_path),
            "evidence": [item.to_dict() for item in self.evidence],
        }


def extract_offer(html_text: str, url: str, *, recipe: dict[str, Any] | None = None) -> ExtractedOffer:
    soup = BeautifulSoup(html_text, "html.parser")
    offer = ExtractedOffer(url=url)
    json_ld_nodes = _collect_json_ld(soup)
    _apply_json_ld(offer, json_ld_nodes, url)
    _apply_microdata(offer, soup, url)
    _apply_open_graph(offer, soup, url)
    _apply_hydration(offer, soup, url)
    if recipe:
        _apply_recipe(offer, soup, recipe)
    _apply_html_fallback(offer, soup, url)
    _normalize(offer)
    return offer


def _collect_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            decoded = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        nodes.extend(_walk_nodes(decoded))
    return nodes


def _walk_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_walk_nodes(item))
        return result
    if not isinstance(value, dict):
        return []
    nodes = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            nodes.extend(_walk_nodes(item))
    return nodes


def _apply_json_ld(offer: ExtractedOffer, nodes: list[dict[str, Any]], base_url: str) -> None:
    product_nodes = [node for node in nodes if _matches_type(node, JSON_LD_TYPES_PRODUCT)]
    breadcrumb_nodes = [node for node in nodes if _matches_type(node, {"breadcrumblist"})]
    if not product_nodes and not breadcrumb_nodes:
        return
    offer.extraction_path.append("json_ld")

    if breadcrumb_nodes and not offer.breadcrumbs:
        crumbs = _read_breadcrumbs(breadcrumb_nodes[0])
        if crumbs:
            offer.breadcrumbs = crumbs
            offer.category = crumbs[-1] if crumbs else offer.category
            _add_evidence(offer, "breadcrumbs", crumbs, "json_ld", 0.85)

    if not product_nodes:
        return

    product = product_nodes[0]
    title = _string(product.get("name"))
    if title and not offer.title:
        offer.title = title
        _add_evidence(offer, "title", title, "json_ld", 0.95)

    brand_value = product.get("brand")
    brand = _string(brand_value.get("name")) if isinstance(brand_value, dict) else _string(brand_value)
    if brand and not offer.brand:
        offer.brand = brand
        _add_evidence(offer, "brand", brand, "json_ld", 0.9)

    description = _string(product.get("description"))
    if description and not offer.description:
        offer.description = description

    for key in ("sku", "mpn"):
        value = _string(product.get(key))
        if value and not getattr(offer, key):
            setattr(offer, key, value)
            _add_evidence(offer, key, value, "json_ld", 0.9)
    gtin = next(
        (_string(product.get(key)) for key in ("gtin13", "gtin12", "gtin8", "gtin") if product.get(key)),
        None,
    )
    if gtin and not offer.gtin:
        offer.gtin = gtin
        _add_evidence(offer, "gtin", gtin, "json_ld", 0.95)

    images = _image_list(product.get("image"))
    if images and not offer.images:
        offer.images = [urljoin(base_url, item) for item in images]
        _add_evidence(offer, "images", offer.images, "json_ld", 0.85)

    offers = product.get("offers")
    offer_payload = _flatten_offers(offers)
    if offer_payload:
        price = _parse_price(offer_payload.get("price") or offer_payload.get("lowPrice"))
        if price and not offer.price_current:
            offer.price_current = price
            _add_evidence(offer, "price", price, "json_ld", 0.95)
        old_price = _parse_price(offer_payload.get("priceSpecification", {}).get("price") if isinstance(offer_payload.get("priceSpecification"), dict) else None)
        if old_price and not offer.price_old:
            offer.price_old = old_price
        currency = _string(offer_payload.get("priceCurrency"))
        if currency and not offer.currency:
            offer.currency = currency
            _add_evidence(offer, "currency", currency, "json_ld", 0.95)
        availability = _string(offer_payload.get("availability"))
        if availability and not offer.availability:
            offer.availability = _normalize_availability(availability)
            _add_evidence(offer, "availability", offer.availability, "json_ld", 0.9)
        offer_url = _string(offer_payload.get("url"))
        if offer_url and not offer.canonical_url:
            offer.canonical_url = urljoin(base_url, offer_url)
            _add_evidence(offer, "canonical_url", offer.canonical_url, "json_ld", 0.6)
        for attr_key in ("color", "size", "material"):
            value = _string(offer_payload.get(attr_key))
            if value:
                offer.attributes.setdefault(attr_key, value)


def _matches_type(node: dict[str, Any], types: set[str]) -> bool:
    type_field = node.get("@type")
    type_list = type_field if isinstance(type_field, list) else [type_field]
    for item in type_list:
        if item and str(item).lower() in types:
            return True
    return False


def _flatten_offers(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        nested = value.get("offers")
        if isinstance(nested, (list, dict)):
            return _flatten_offers(nested)
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return _flatten_offers(item)
    return {}


def _read_breadcrumbs(node: dict[str, Any]) -> list[str]:
    items = node.get("itemListElement") or []
    result: list[str] = []
    if not isinstance(items, list):
        return result
    for entry in items:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item")
        name = entry.get("name")
        if isinstance(item, dict):
            name = name or item.get("name")
        name = _string(name)
        if name:
            result.append(name)
    return result


def _apply_microdata(offer: ExtractedOffer, soup: BeautifulSoup, base_url: str) -> None:
    product_scope = soup.find(attrs={"itemtype": re.compile(r"Product", re.I)})
    if not isinstance(product_scope, Tag):
        return
    offer.extraction_path.append("microdata")
    if not offer.title:
        name_tag = product_scope.find(attrs={"itemprop": "name"})
        if isinstance(name_tag, Tag):
            value = name_tag.get_text(" ", strip=True)
            if value:
                offer.title = value
                _add_evidence(offer, "title", value, "microdata", 0.8)
    if not offer.price_current:
        price_tag = product_scope.find(attrs={"itemprop": "price"})
        if isinstance(price_tag, Tag):
            value = price_tag.get("content") or price_tag.get_text(" ", strip=True)
            price = _parse_price(value)
            if price:
                offer.price_current = price
                _add_evidence(offer, "price", price, "microdata", 0.85)
    if not offer.currency:
        currency_tag = product_scope.find(attrs={"itemprop": "priceCurrency"})
        if isinstance(currency_tag, Tag):
            value = currency_tag.get("content") or currency_tag.get_text(" ", strip=True)
            if value:
                offer.currency = value.strip()
    if not offer.availability:
        availability_tag = product_scope.find(attrs={"itemprop": "availability"})
        if isinstance(availability_tag, Tag):
            value = availability_tag.get("href") or availability_tag.get("content") or availability_tag.get_text(" ", strip=True)
            if value:
                offer.availability = _normalize_availability(value)
                _add_evidence(offer, "availability", offer.availability, "microdata", 0.8)
    if not offer.sku:
        sku_tag = product_scope.find(attrs={"itemprop": "sku"})
        if isinstance(sku_tag, Tag):
            value = sku_tag.get("content") or sku_tag.get_text(" ", strip=True)
            if value:
                offer.sku = value.strip()
                _add_evidence(offer, "sku", offer.sku, "microdata", 0.85)
    if not offer.images:
        image_tag = product_scope.find(attrs={"itemprop": "image"})
        if isinstance(image_tag, Tag):
            value = image_tag.get("src") or image_tag.get("content")
            if value:
                offer.images = [urljoin(base_url, value)]


def _apply_open_graph(offer: ExtractedOffer, soup: BeautifulSoup, base_url: str) -> None:
    used = False
    if not offer.title:
        title = _meta_content(soup, "property", "og:title") or _meta_content(soup, "name", "twitter:title")
        if title:
            offer.title = title.strip()
            _add_evidence(offer, "title", offer.title, "open_graph", 0.7)
            used = True
    if not offer.price_current:
        price_text = _meta_content(soup, "property", "product:price:amount") or _meta_content(soup, "property", "og:price:amount")
        price = _parse_price(price_text)
        if price:
            offer.price_current = price
            _add_evidence(offer, "price", price, "open_graph", 0.75)
            used = True
    if not offer.currency:
        currency = _meta_content(soup, "property", "product:price:currency") or _meta_content(soup, "property", "og:price:currency")
        if currency:
            offer.currency = currency.strip()
            used = True
    if not offer.availability:
        availability = _meta_content(soup, "property", "product:availability") or _meta_content(soup, "property", "og:availability")
        if availability:
            offer.availability = _normalize_availability(availability)
            used = True
    if not offer.images:
        image = _meta_content(soup, "property", "og:image") or _meta_content(soup, "name", "twitter:image")
        if image:
            offer.images = [urljoin(base_url, image)]
            used = True
    if used and "open_graph" not in offer.extraction_path:
        offer.extraction_path.append("open_graph")


def _apply_hydration(offer: ExtractedOffer, soup: BeautifulSoup, base_url: str) -> None:
    hydration_scripts: list[tuple[str, Any]] = []
    for script in soup.find_all("script"):
        script_id = (script.get("id") or "").strip().lower()
        text = (script.string or script.get_text() or "").strip()
        if not text:
            continue
        if script_id == "__next_data__" or (script.get("type") == "application/json" and script_id):
            try:
                decoded = json.loads(text)
                hydration_scripts.append((script_id or "application/json", decoded))
            except json.JSONDecodeError:
                continue
            continue
        for marker in ("window.__INITIAL_STATE__", "window.__NUXT__", "window.__APP_STATE__"):
            if marker in text:
                match = re.search(r"=\s*(\{.*?\})\s*;?\s*$", text, flags=re.S)
                if not match:
                    continue
                try:
                    decoded = json.loads(match.group(1))
                    hydration_scripts.append((marker, decoded))
                except json.JSONDecodeError:
                    continue
    if not hydration_scripts:
        return
    offer.extraction_path.append("hydration")
    payload = hydration_scripts[0][1]
    candidates = _search_hydration(payload)
    for candidate in candidates:
        if not offer.title and candidate.get("title"):
            offer.title = _string(candidate["title"])
            _add_evidence(offer, "title", offer.title, "internal_json", 0.85)
        if not offer.price_current and candidate.get("price") is not None:
            price = _parse_price(candidate["price"])
            if price:
                offer.price_current = price
                _add_evidence(offer, "price", price, "internal_json", 0.85)
        if not offer.currency and candidate.get("currency"):
            offer.currency = _string(candidate["currency"])
        if not offer.availability and candidate.get("availability"):
            offer.availability = _normalize_availability(_string(candidate["availability"]))
        if not offer.sku and candidate.get("sku"):
            offer.sku = _string(candidate["sku"])
        if not offer.brand and candidate.get("brand"):
            offer.brand = _string(candidate["brand"])
        if not offer.images and candidate.get("image"):
            image_value = candidate["image"]
            if isinstance(image_value, list):
                offer.images = [urljoin(base_url, str(item)) for item in image_value if item]
            elif isinstance(image_value, str):
                offer.images = [urljoin(base_url, image_value)]


def _search_hydration(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8 or value is None:
        return []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_search_hydration(item, depth + 1))
        return out
    if not isinstance(value, dict):
        return []
    keys = {key.lower() for key in value.keys()}
    relevant = {"title", "name", "price", "currency", "sku", "vendorcode", "brand", "availability", "image", "images", "thumbnail"}
    if keys & relevant:
        candidate = {
            "title": value.get("name") or value.get("title"),
            "price": value.get("price") or value.get("salePrice") or value.get("originalPrice") or value.get("amount"),
            "currency": value.get("currency") or value.get("priceCurrency"),
            "availability": value.get("availability") or value.get("inStock") or value.get("status"),
            "sku": value.get("sku") or value.get("vendorCode") or value.get("article"),
            "brand": value.get("brand") if isinstance(value.get("brand"), str) else (value.get("brand", {}) or {}).get("name") if isinstance(value.get("brand"), dict) else None,
            "image": value.get("image") or value.get("images") or value.get("thumbnail"),
        }
        return [candidate]
    results: list[dict[str, Any]] = []
    for inner in value.values():
        results.extend(_search_hydration(inner, depth + 1))
    return results


def _apply_recipe(offer: ExtractedOffer, soup: BeautifulSoup, recipe: dict[str, Any]) -> None:
    selectors = recipe.get("selectors") or {}
    if not isinstance(selectors, dict):
        return
    used = False
    for field_name in ("title", "price", "availability"):
        css = selectors.get(field_name)
        if not css:
            continue
        node = soup.select_one(css)
        if not node:
            continue
        value = node.get("content") if isinstance(node, Tag) and node.get("content") else node.get_text(" ", strip=True)
        if not value:
            continue
        if field_name == "title" and not offer.title:
            offer.title = value
            _add_evidence(offer, "title", value, "html", 0.75, detail="recipe")
            used = True
        elif field_name == "price" and not offer.price_current:
            price = _parse_price(value)
            if price:
                offer.price_current = price
                _add_evidence(offer, "price", price, "html", 0.75, detail="recipe")
                used = True
        elif field_name == "availability" and not offer.availability:
            offer.availability = _normalize_availability(value)
            _add_evidence(offer, "availability", offer.availability, "html", 0.75, detail="recipe")
            used = True
    if used and "html_recipe" not in offer.extraction_path:
        offer.extraction_path.append("html_recipe")


def _apply_html_fallback(offer: ExtractedOffer, soup: BeautifulSoup, base_url: str) -> None:
    used = False
    if not offer.title:
        for candidate in (soup.find("h1"), soup.find("title")):
            if not candidate:
                continue
            value = candidate.get_text(" ", strip=True)
            if value:
                offer.title = value
                _add_evidence(offer, "title", value, "html", 0.55)
                used = True
                break
    if not offer.price_current:
        text = soup.get_text(" ", strip=True)
        for pattern in PRICE_PATTERNS:
            match = pattern.search(text)
            if match:
                price = _parse_price(match.group(1))
                if price:
                    offer.price_current = price
                    _add_evidence(offer, "price", price, "html", 0.45)
                    used = True
                    break
    if not offer.availability:
        text_lower = soup.get_text(" ", strip=True).lower()
        for needle, status in AVAILABILITY_KEYWORDS:
            if needle in text_lower:
                offer.availability = status
                _add_evidence(offer, "availability", status, "html", 0.5)
                used = True
                break
    if not offer.images:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            if src.lower().endswith(".svg"):
                continue
            offer.images.append(urljoin(base_url, src))
            if len(offer.images) >= 3:
                break
    if not offer.attributes:
        offer.attributes.update(_extract_html_attributes(soup))
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        offer.canonical_url = urljoin(base_url, canonical_tag["href"])
    if used and "html" not in offer.extraction_path:
        offer.extraction_path.append("html")


def _extract_html_attributes(soup: BeautifulSoup) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for table in soup.find_all("table")[:6]:
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(" ", strip=True)
                value = cells[1].get_text(" ", strip=True)
                if key and value and len(key) <= 80 and len(value) <= 200:
                    attrs.setdefault(key, value)
    for dl in soup.find_all("dl")[:6]:
        dts = [dt.get_text(" ", strip=True) for dt in dl.find_all("dt")]
        dds = [dd.get_text(" ", strip=True) for dd in dl.find_all("dd")]
        for key, value in zip(dts, dds, strict=False):
            if key and value and len(key) <= 80 and len(value) <= 200:
                attrs.setdefault(key, value)
    return attrs


def _normalize(offer: ExtractedOffer) -> None:
    if offer.currency:
        offer.currency = offer.currency.upper().strip()
        if offer.currency in {"RUB", "RUR", "₽", "RU"}:
            offer.currency = "RUB"
    if offer.availability:
        offer.availability = offer.availability.lower()
    if offer.title:
        offer.title = " ".join(offer.title.split())
    if offer.images:
        deduped: list[str] = []
        seen: set[str] = set()
        for image in offer.images:
            if image and image not in seen:
                seen.add(image)
                deduped.append(image)
        offer.images = deduped


def _add_evidence(offer: ExtractedOffer, field_name: str, value: Any, source: str, confidence: float, detail: str | None = None) -> None:
    offer.evidence.append(Evidence(field=field_name, value=value, source=source, confidence=confidence, detail=detail))


def _normalize_availability(value: Any) -> str | None:
    text = (str(value or "")).strip().lower()
    if not text:
        return None
    if "schema.org/instock" in text or text in {"instock", "in_stock", "true"}:
        return "in_stock"
    if "schema.org/outofstock" in text or text in {"outofstock", "out_of_stock", "false"}:
        return "out_of_stock"
    if "preorder" in text or "под заказ" in text:
        return "preorder"
    if "limited" in text or "limitedavailability" in text:
        return "limited"
    if "discontinued" in text or "снят" in text:
        return "discontinued"
    for needle, status in AVAILABILITY_KEYWORDS:
        if needle in text:
            return status
    return text


def _parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[\s ]", "", text).replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
        return float(cleaned)
    for pattern in PRICE_PATTERNS:
        match = pattern.search(text)
        if match:
            number = match.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
            try:
                return float(number)
            except ValueError:
                continue
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _meta_content(soup: BeautifulSoup, attr: str, value: str) -> str | None:
    tag = soup.find("meta", attrs={attr: value})
    if not isinstance(tag, Tag):
        return None
    content = tag.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def _image_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, dict):
        candidate = value.get("url") or value.get("contentUrl")
        if candidate:
            return [str(candidate)]
    return []
