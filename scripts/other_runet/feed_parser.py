from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lxml import etree


LOGGER = logging.getLogger("other_runet.feed")


@dataclass
class FeedOffer:
    offer_id: str | None
    name: str | None
    vendor: str | None
    vendor_code: str | None
    model: str | None
    price: float | None
    old_price: float | None
    currency: str | None
    available: bool | None
    url: str | None
    category_id: str | None
    pictures: list[str] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "name": self.name,
            "vendor": self.vendor,
            "vendor_code": self.vendor_code,
            "model": self.model,
            "price": self.price,
            "old_price": self.old_price,
            "currency": self.currency,
            "available": self.available,
            "url": self.url,
            "category_id": self.category_id,
            "pictures": list(self.pictures),
            "params": dict(self.params),
            "description": self.description,
        }


def parse_yml(xml_bytes: bytes | str, *, max_offers: int = 5000) -> dict[str, Any]:
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8", errors="ignore")
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        return {"error": str(exc), "shop_name": None, "categories": {}, "offers": []}
    if root is None:
        return {"error": "empty root", "shop_name": None, "categories": {}, "offers": []}
    shop = root.find(".//shop")
    if shop is None and root.tag.lower() == "shop":
        shop = root
    shop_name = _text(shop.find("name")) if shop is not None else None
    categories = {}
    if shop is not None:
        for category in shop.findall(".//category"):
            cat_id = category.get("id")
            if cat_id:
                categories[cat_id] = {
                    "name": (category.text or "").strip(),
                    "parent_id": category.get("parentId"),
                }
    offers: list[FeedOffer] = []
    nodes = (shop.findall(".//offer") if shop is not None else root.findall(".//offer"))
    for node in nodes[: max(1, max_offers)]:
        offer = _parse_offer_node(node)
        if offer:
            offers.append(offer)
    return {
        "error": None,
        "shop_name": shop_name,
        "categories": categories,
        "offers": [item.to_dict() for item in offers],
    }


def _parse_offer_node(node: Any) -> FeedOffer | None:
    if node is None:
        return None
    available_attr = node.get("available")
    available = None
    if available_attr is not None:
        available = available_attr.strip().lower() in {"true", "1", "yes"}
    price_text = _text(node.find("price"))
    old_price_text = _text(node.find("oldprice")) or _text(node.find("oldPrice"))
    params: dict[str, str] = {}
    for param in node.findall("param"):
        name = (param.get("name") or "").strip()
        value = (param.text or "").strip()
        if name and value:
            params[name] = value
    pictures = [
        (img.text or "").strip()
        for img in node.findall("picture")
        if img is not None and (img.text or "").strip()
    ]
    return FeedOffer(
        offer_id=node.get("id"),
        name=_text(node.find("name")) or _text(node.find("model")),
        vendor=_text(node.find("vendor")),
        vendor_code=_text(node.find("vendorCode")) or _text(node.find("vendor_code")),
        model=_text(node.find("model")),
        price=_to_float(price_text),
        old_price=_to_float(old_price_text),
        currency=_text(node.find("currencyId")) or _text(node.find("currency")),
        available=available,
        url=_text(node.find("url")),
        category_id=_text(node.find("categoryId")) or _text(node.find("category_id")),
        pictures=pictures,
        params=params,
        description=_text(node.find("description")),
    )


def parse_sitemap(xml_bytes: bytes | str) -> list[dict[str, Any]]:
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8", errors="ignore")
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[dict[str, Any]] = []
    for url in root.findall("sm:url", namespace) or root.findall("url"):
        loc = url.find("sm:loc", namespace)
        if loc is None:
            loc = url.find("loc")
        if loc is None or not (loc.text or "").strip():
            continue
        lastmod = url.find("sm:lastmod", namespace) if loc.tag.endswith("loc") else None
        entries.append({
            "url": loc.text.strip(),
            "lastmod": (lastmod.text or "").strip() if lastmod is not None and lastmod.text else None,
        })
    for sitemap in root.findall("sm:sitemap", namespace) or root.findall("sitemap"):
        loc = sitemap.find("sm:loc", namespace) or sitemap.find("loc")
        if loc is None or not (loc.text or "").strip():
            continue
        entries.append({"url": loc.text.strip(), "lastmod": None, "is_index": True})
    return entries


def _text(node: Any) -> str | None:
    if node is None:
        return None
    text = (node.text or "").strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None
