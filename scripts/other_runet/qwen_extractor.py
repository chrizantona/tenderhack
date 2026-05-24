from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http_client import root_host
from .qwen_client import QwenClient, QwenResponse
from .structured_extractor import (
    Evidence,
    ExtractedOffer,
    SOURCE_RELIABILITY,
    _add_evidence,
    _normalize,
    _parse_price,
    _normalize_availability,
)


LOGGER = logging.getLogger("other_runet.qwen.extract")

EXTRACTOR_SYSTEM = (
    "You are a Russian e-commerce data extractor. "
    "You receive a URL, a JSON-LD block (if present), meta tags, the page title and a cleaned text excerpt of a product page. "
    "Return a single JSON object with fields: "
    "{\"title\": str|null, "
    "\"brand\": str|null, "
    "\"price\": {\"current\": number|null, \"old\": number|null, \"currency\": str|null}, "
    "\"availability\": \"in_stock\"|\"out_of_stock\"|\"preorder\"|\"limited\"|\"unknown\"|null, "
    "\"sku\": str|null, \"mpn\": str|null, \"gtin\": str|null, "
    "\"description\": str|null, "
    "\"image\": str|null, "
    "\"breadcrumbs\": [str], "
    "\"attributes\": {string: string}, "
    "\"confidence\": number between 0 and 1, "
    "\"is_product_page\": bool, "
    "\"reason_if_not_product\": str|null}. "
    "Use absolute URLs for image. Use RUB for currency if you see ₽ or руб. "
    "Set is_product_page=false when the URL is a category/listing/search page or doesn't sell exactly one product. "
    "Return only JSON, no commentary."
)


def extract_with_qwen(
    html_text: str,
    *,
    url: str,
    client: QwenClient,
    timeout: float = 60.0,
    existing: ExtractedOffer | None = None,
) -> ExtractedOffer | None:
    if not client.is_available():
        return None
    payload = _build_payload(html_text, url)
    user_prompt = json.dumps(payload, ensure_ascii=False)[:7500]
    response = client.generate_json(
        system_prompt=EXTRACTOR_SYSTEM,
        user_prompt=user_prompt,
        max_tokens=900,
        temperature=0.05,
    )
    if response.parsed is None:
        return None
    return _materialize_offer(response.parsed, url=url, existing=existing, qwen_response=response)


def _build_payload(html_text: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    json_ld_blocks: list[Any] = []
    for script in BeautifulSoup(html_text, "html.parser").find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            json_ld_blocks.append(json.loads(html.unescape(raw)))
        except json.JSONDecodeError:
            continue
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    meta_tags: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        name = meta.get("property") or meta.get("name") or meta.get("itemprop")
        content = meta.get("content")
        if isinstance(name, str) and isinstance(content, str):
            key = name.strip().lower()
            if key.startswith(("og:", "product:", "twitter:", "description", "price", "availability", "sku", "mpn")):
                meta_tags[key] = content.strip()
    breadcrumbs_text = []
    for nav in soup.select(".breadcrumbs, nav.breadcrumb, ol.breadcrumb, .crumbs"):
        items = [item.get_text(" ", strip=True) for item in nav.find_all("a")]
        if items:
            breadcrumbs_text.append(" / ".join(items))
    body_text = soup.get_text(" ", strip=True)
    body_text = re.sub(r"\s+", " ", body_text)
    return {
        "url": url,
        "title_tag": (title_tag.get_text(" ", strip=True) if title_tag else None),
        "h1": (h1_tag.get_text(" ", strip=True) if h1_tag else None),
        "meta": meta_tags,
        "json_ld": json_ld_blocks[:2] if json_ld_blocks else None,
        "breadcrumbs": breadcrumbs_text[:2],
        "body_excerpt": body_text[:3500],
    }


def _materialize_offer(
    parsed: dict[str, Any],
    *,
    url: str,
    existing: ExtractedOffer | None,
    qwen_response: QwenResponse,
) -> ExtractedOffer | None:
    if not isinstance(parsed, dict):
        return None
    is_product = parsed.get("is_product_page")
    if is_product is False:
        return None
    offer = existing or ExtractedOffer(url=url)
    confidence_factor = float(parsed.get("confidence") or 0.7)
    qwen_conf = max(0.4, min(0.95, confidence_factor))
    title = _string(parsed.get("title"))
    if title:
        offer.title = title
        _add_evidence(offer, "title", title, "qwen_fallback", qwen_conf, detail="qwen_extractor")
    brand = _string(parsed.get("brand"))
    if brand and not offer.brand:
        offer.brand = brand
        _add_evidence(offer, "brand", brand, "qwen_fallback", qwen_conf)
    price_payload = parsed.get("price") if isinstance(parsed.get("price"), dict) else {}
    current_price = _parse_price(price_payload.get("current"))
    if current_price:
        offer.price_current = current_price
        _add_evidence(offer, "price", current_price, "qwen_fallback", qwen_conf)
    old_price = _parse_price(price_payload.get("old"))
    if old_price:
        offer.price_old = old_price
    currency = _string(price_payload.get("currency"))
    if currency:
        offer.currency = currency
    availability = _string(parsed.get("availability"))
    if availability and availability.lower() != "unknown":
        offer.availability = _normalize_availability(availability)
        _add_evidence(offer, "availability", offer.availability, "qwen_fallback", qwen_conf)
    sku = _string(parsed.get("sku"))
    if sku and not offer.sku:
        offer.sku = sku
        _add_evidence(offer, "sku", sku, "qwen_fallback", qwen_conf)
    mpn = _string(parsed.get("mpn"))
    if mpn and not offer.mpn:
        offer.mpn = mpn
    gtin = _string(parsed.get("gtin"))
    if gtin and not offer.gtin:
        offer.gtin = gtin
    description = _string(parsed.get("description"))
    if description and not offer.description:
        offer.description = description
    image = _string(parsed.get("image"))
    if image:
        absolute = urljoin(url, image)
        if absolute not in offer.images:
            offer.images.insert(0, absolute)
    breadcrumbs_raw = parsed.get("breadcrumbs")
    if isinstance(breadcrumbs_raw, list):
        cleaned = [str(item).strip() for item in breadcrumbs_raw if item]
        if cleaned and not offer.breadcrumbs:
            offer.breadcrumbs = cleaned
    attributes_raw = parsed.get("attributes")
    if isinstance(attributes_raw, dict):
        for key, value in attributes_raw.items():
            if not isinstance(key, str) or not value:
                continue
            text_value = str(value).strip()
            if text_value:
                offer.attributes.setdefault(key, text_value)
    if "qwen_extractor" not in offer.extraction_path:
        offer.extraction_path.append("qwen_extractor")
    _normalize(offer)
    if not offer.title and not offer.price_current:
        return None
    return offer


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text or None
