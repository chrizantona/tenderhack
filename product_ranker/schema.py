from __future__ import annotations

import re
from typing import Any


def get_path(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
        if current is None:
            return default
    return current


def product_title(card: dict[str, Any]) -> str:
    return str(card.get("title") or "").strip()


def product_brand(card: dict[str, Any]) -> str:
    brand = card.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "").strip()
    return str(brand or "").strip()


def product_category(card: dict[str, Any]) -> str:
    category = card.get("category")
    if isinstance(category, dict):
        return str(category.get("name") or "").strip()
    return str(category or "").strip()


def current_price(card: dict[str, Any]) -> float | None:
    value = get_path(card, "price.current")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def availability_status(card: dict[str, Any]) -> str | None:
    value = get_path(card, "availability.status")
    return str(value).strip() if value else None


def normalize_text(value: str | None) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(value: str | None) -> set[str]:
    return {item for item in normalize_text(value).split() if len(item) > 1}


def compact_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "marketplace": card.get("marketplace"),
        "external_id": card.get("external_id"),
        "url": card.get("url"),
        "title": product_title(card),
        "brand": product_brand(card),
        "category": product_category(card),
        "price": current_price(card),
        "rating": get_path(card, "rating.value"),
        "reviews_count": get_path(card, "rating.reviews_count"),
        "availability": availability_status(card),
        "attributes": card.get("attributes") or {},
        "description": str(card.get("description") or "")[:800],
    }

