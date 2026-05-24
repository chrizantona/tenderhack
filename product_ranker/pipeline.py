from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from .qwen import PASSPORT_SYSTEM, VALIDATION_SYSTEM, QwenClient
from .schema import (
    availability_status,
    compact_card,
    current_price,
    normalize_text,
    product_brand,
    product_category,
    product_title,
    tokens,
)


ACCESSORY_WORDS = {
    "чехол",
    "стекло",
    "пленка",
    "плёнка",
    "кабель",
    "зарядка",
    "ремешок",
    "держатель",
    "адаптер",
}


def rank_products(
    *,
    query: str,
    cards: list[dict[str, Any]],
    qwen: QwenClient | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    passport = build_passport(query, qwen=qwen)
    validated = [validate_card(query, passport, card, qwen=qwen) for card in cards]

    accepted = [item for item in validated if not item["rejected"]]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted:
        groups[item["category"]].append(item)

    grouped_output: list[dict[str, Any]] = []
    all_ranked: list[dict[str, Any]] = []
    for category, items in groups.items():
        ranked = apply_price_outliers_and_rank(query, passport, items)
        all_ranked.extend(ranked)
        grouped_output.append(
            {
                "category": category,
                "count": len(ranked),
                "items": sorted(ranked, key=lambda x: x["final_score"], reverse=True),
            }
        )

    best = [
        item
        for item in sorted(all_ranked, key=lambda x: x["final_score"], reverse=True)
        if not item.get("hidden")
    ][:top_n]

    return {
        "query": query,
        "passport": passport,
        "best": best,
        "groups": sorted(grouped_output, key=lambda g: (-g["count"], g["category"])),
        "rejected": [item for item in validated if item["rejected"]],
        "diagnostics": {
            "cards_in": len(cards),
            "accepted": len(accepted),
            "rejected": len(cards) - len(accepted),
            "qwen_used": bool(qwen and qwen.is_available()),
        },
    }


def build_passport(query: str, *, qwen: QwenClient | None) -> dict[str, Any]:
    if qwen and qwen.is_available():
        result = qwen.json(
            system=PASSPORT_SYSTEM,
            user={"query": query},
            max_tokens=500,
        )
        if result.parsed:
            return result.parsed
    return heuristic_passport(query)


def heuristic_passport(query: str) -> dict[str, Any]:
    text = normalize_text(query)
    words = text.split()
    brand = None
    for candidate in ("apple", "samsung", "xiaomi", "huawei", "honor", "sony", "lg"):
        if candidate in words:
            brand = candidate
            break
    product_type = "unknown"
    if any(w in words for w in ("iphone", "смартфон", "телефон")):
        product_type = "smartphone"
    elif any(w in words for w in ("ноутбук", "laptop", "macbook")):
        product_type = "laptop"
    elif any(w in words for w in ("шины", "резина")):
        product_type = "tires"
    return {
        "canonical_query": query.strip(),
        "product_type": product_type,
        "brand": brand,
        "model": " ".join(words[:5]) or None,
        "required_attributes": {},
        "negative_constraints": list(ACCESSORY_WORDS),
        "category_hint": product_type,
    }


def validate_card(
    query: str,
    passport: dict[str, Any],
    card: dict[str, Any],
    *,
    qwen: QwenClient | None,
) -> dict[str, Any]:
    compact = compact_card(card)
    title = product_title(card)
    structural_flags = []
    if not title:
        structural_flags.append("missing_title")
    if current_price(card) is None:
        structural_flags.append("missing_price")
    if not card.get("url") and not card.get("external_id"):
        structural_flags.append("missing_identity")

    qwen_decision: dict[str, Any] | None = None
    if qwen and qwen.is_available() and title:
        result = qwen.json(
            system=VALIDATION_SYSTEM,
            user={"passport": passport, "candidate": compact},
            max_tokens=500,
        )
        qwen_decision = result.parsed

    if qwen_decision:
        decision = str(qwen_decision.get("decision") or "review").lower()
        confidence = float_or(qwen_decision.get("confidence"), 0.5)
        reason = str(qwen_decision.get("reason") or "")
        category = str(qwen_decision.get("category") or product_category(card) or passport.get("category_hint") or "other")
        qwen_flags = qwen_decision.get("flags") if isinstance(qwen_decision.get("flags"), list) else []
    else:
        decision, confidence, reason, category, qwen_flags = heuristic_validation(query, passport, card)

    rejected = False
    reject_reason = None
    if "missing_title" in structural_flags:
        rejected = True
        reject_reason = "missing_title"
    elif decision == "reject":
        rejected = True
        reject_reason = reason or "qwen_reject"
    elif looks_like_accessory(query, title):
        rejected = True
        reject_reason = "accessory_mismatch"

    return {
        "card": card,
        "compact": compact,
        "category": normalize_category(category),
        "validation": {
            "decision": decision,
            "confidence": round(confidence, 3),
            "reason": reason,
            "flags": list(qwen_flags) + structural_flags,
        },
        "rejected": rejected,
        "reject_reason": reject_reason,
    }


def heuristic_validation(
    query: str,
    passport: dict[str, Any],
    card: dict[str, Any],
) -> tuple[str, float, str, str, list[str]]:
    title = product_title(card)
    query_tokens = tokens(query)
    title_tokens = tokens(title + " " + product_brand(card))
    overlap = len(query_tokens & title_tokens) / max(1, len(query_tokens))
    flags: list[str] = []
    decision = "accept" if overlap >= 0.35 else "review"
    if overlap < 0.15:
        decision = "reject"
        flags.append("low_text_overlap")
    if looks_like_accessory(query, title):
        decision = "reject"
        flags.append("accessory_mismatch")
    category = product_category(card) or str(passport.get("category_hint") or "other")
    return decision, min(1.0, max(0.1, overlap + 0.35)), f"text_overlap={overlap:.2f}", category, flags


def apply_price_outliers_and_rank(
    query: str,
    passport: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prices = [current_price(item["card"]) for item in items]
    clean_prices = [price for price in prices if price is not None and price > 0]
    median_price = statistics.median(clean_prices) if clean_prices else None
    low_threshold = median_price * 0.65 if median_price else None
    high_threshold = median_price * 1.80 if median_price else None

    ranked = []
    for item in items:
        card = item["card"]
        price = current_price(card)
        price_flags: list[str] = []
        hidden = False
        price_score = 0.7
        if price is None:
            price_flags.append("missing_price")
            price_score = 0.3
        elif median_price:
            if low_threshold and price <= low_threshold:
                price_flags.append("suspicious_low_price")
                hidden = True
                price_score = 0.15
            elif high_threshold and price >= high_threshold:
                price_flags.append("suspicious_high_price")
                price_score = 0.45
            else:
                deviation = abs(price - median_price) / median_price
                price_score = max(0.35, 1.0 - deviation)

        score = final_score(
            query=query,
            passport=passport,
            card=card,
            validation_confidence=item["validation"]["confidence"],
            price_score=price_score,
            hidden=hidden,
        )
        ranked.append(
            {
                **item,
                "price_context": {
                    "median": median_price,
                    "low_threshold": low_threshold,
                    "high_threshold": high_threshold,
                },
                "price_flags": price_flags,
                "hidden": hidden,
                "final_score": round(score, 4),
            }
        )
    return ranked


def final_score(
    *,
    query: str,
    passport: dict[str, Any],
    card: dict[str, Any],
    validation_confidence: float,
    price_score: float,
    hidden: bool,
) -> float:
    text_score = text_match_score(query, card)
    availability = availability_score(availability_status(card))
    rating = rating_review_score(card)
    seller = 0.6 if (card.get("seller") or {}).get("name") else 0.4
    source = 0.8 if card.get("marketplace") else 0.5
    score = (
        validation_confidence * 0.35
        + text_score * 0.20
        + price_score * 0.15
        + availability * 0.10
        + rating * 0.10
        + seller * 0.05
        + source * 0.05
    )
    if hidden:
        score *= 0.35
    return max(0.0, min(1.0, score))


def text_match_score(query: str, card: dict[str, Any]) -> float:
    q = tokens(query)
    haystack = tokens(" ".join([product_title(card), product_brand(card), product_category(card)]))
    if not q:
        return 0.0
    return len(q & haystack) / len(q)


def availability_score(status: str | None) -> float:
    if status == "in_stock":
        return 1.0
    if status == "limited":
        return 0.8
    if status == "preorder":
        return 0.5
    if status == "out_of_stock":
        return 0.1
    return 0.4


def rating_review_score(card: dict[str, Any]) -> float:
    rating = float_or((card.get("rating") or {}).get("value"), None)
    reviews = float_or((card.get("rating") or {}).get("reviews_count"), 0.0) or 0.0
    rating_part = 0.5 if rating is None else min(1.0, max(0.0, rating / 5.0))
    review_part = min(1.0, math.log10(reviews + 1) / 4.0)
    return rating_part * 0.7 + review_part * 0.3


def looks_like_accessory(query: str, title: str) -> bool:
    query_norm = normalize_text(query)
    title_tokens = tokens(title)
    query_asks_accessory = bool(tokens(query) & ACCESSORY_WORDS)
    if query_asks_accessory:
        return False
    if not (title_tokens & ACCESSORY_WORDS):
        return False
    accessory_relation = any(marker in normalize_text(title) for marker in (" для ", " compatible ", "for "))
    return accessory_relation or True


def normalize_category(value: str) -> str:
    text = normalize_text(value)
    if not text or text == "unknown":
        return "other"
    mapping = {
        "smartphone": "smartphones",
        "смартфоны": "smartphones",
        "смартфон": "smartphones",
        "телефоны": "smartphones",
        "телефон": "smartphones",
        "мобильные телефоны": "smartphones",
        "мобильный телефон": "smartphones",
        "laptop": "laptops",
        "ноутбуки": "laptops",
        "ноутбук": "laptops",
        "tires": "tires",
        "шины": "tires",
        "шина": "tires",
    }
    return mapping.get(text, text)


def float_or(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
