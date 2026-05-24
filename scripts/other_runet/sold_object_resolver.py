from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .product_passport import NEGATIVE_BY_TYPE, ProductPassport


RELATION_MARKERS_RU: tuple[str, ...] = (
    "для", "под", "на", "к ", "к ",
    "совместим с", "совместимая с", "совместимый с", "совместимые с",
    "подходит для", "подходит к", "предназначен для", "пригоден для",
)

RELATION_MARKERS_EN: tuple[str, ...] = (
    "for ", "fits ", "compatible with", "designed for", "suitable for",
)

ALL_RELATION_MARKERS: tuple[str, ...] = RELATION_MARKERS_RU + RELATION_MARKERS_EN

ACCESSORY_NEGATIVE_TOKENS: tuple[str, ...] = (
    "чехол", "стекло", "плёнка", "пленка", "ремешок", "браслет", "адаптер",
    "зарядное", "кабель", "стилус", "наклейка", "магнит", "крышка",
    "крепление", "стенд", "док", "док-станция", "силикон", "бампер",
    "сумка", "вешалка", "плечики", "ниппель", "колпак", "колпачок",
    "грузик", "балансировочный", "переходник", "удлинитель",
    "ремкомплект", "вал", "фильтр", "насадка", "щётка", "щетка",
    "мешок", "запчасть", "запчасти",
)


@dataclass
class SoldObjectResolution:
    same_sold_object: bool
    confidence: float
    sold_object: str
    reference_object: str | None
    relation: str | None
    product_type_match: float
    reason: str
    signals: list[str] = field(default_factory=list)
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "same_sold_object": self.same_sold_object,
            "confidence": round(self.confidence, 3),
            "sold_object": self.sold_object,
            "reference_object": self.reference_object,
            "relation": self.relation,
            "product_type_match": round(self.product_type_match, 3),
            "reason": self.reason,
            "signals": list(self.signals),
            "needs_review": self.needs_review,
        }


def resolve_sold_object(
    title: str,
    *,
    passport: ProductPassport,
    breadcrumbs: Iterable[str] = (),
    description: str | None = None,
    url: str | None = None,
) -> SoldObjectResolution:
    title_clean = " ".join((title or "").split())
    title_lower = title_clean.lower()
    breadcrumbs_clean = [crumb for crumb in breadcrumbs if crumb]
    description_lower = (description or "").lower()

    signals: list[str] = []

    relation_match = _find_relation_marker(title_lower)
    has_accessory_negative = _has_accessory_negative(title_lower, passport)

    sold_object_text = title_clean
    reference_object_text: str | None = None
    relation_text: str | None = None
    if relation_match:
        marker_token, position = relation_match
        sold_object_text = title_clean[:position].strip(" ,;–-")
        reference_object_text = title_clean[position + len(marker_token):].strip()
        relation_text = marker_token.strip()
        signals.append(f"relation_marker:{marker_token.strip()}")

    if has_accessory_negative:
        signals.append("accessory_negative_in_title")

    target_brand = (passport.brand or "").lower()
    target_model = (passport.model or "").lower()
    target_family = (passport.product_family or "").lower()
    type_keywords = _product_type_keywords(passport.product_type)

    product_type_match = _scoring_product_type_match(title_lower, type_keywords, passport.product_type)
    if product_type_match >= 0.6:
        signals.append("product_type_match")
    elif type_keywords:
        signals.append("product_type_mismatch")

    sold_object_similarity = _scoring_text_match(
        sold_object=sold_object_text.lower(),
        passport=passport,
    )
    required_attrs_match = _scoring_required_attributes(title_clean, description or "", passport)
    category_match = _scoring_category_match(breadcrumbs_clean, passport)

    compatibility_penalty = 0.0
    if relation_match and reference_object_text:
        if _references_target(reference_object_text.lower(), target_brand, target_model, target_family):
            compatibility_penalty = 1.0
            signals.append("reference_targets_passport")
        elif has_accessory_negative:
            compatibility_penalty = 0.5
    elif has_accessory_negative:
        compatibility_penalty = 0.7
        signals.append("accessory_without_marker")

    score = (
        sold_object_similarity * 0.40
        + product_type_match * 0.25
        + required_attrs_match * 0.20
        + category_match * 0.10
        - compatibility_penalty * 0.35
    )
    score = max(0.0, min(1.0, score))

    same_sold_object = score >= 0.72
    needs_review = 0.55 <= score < 0.72
    reason_parts: list[str] = []
    if compatibility_penalty > 0:
        reason_parts.append("accessory or compatibility item")
    if product_type_match < 0.4 and type_keywords:
        reason_parts.append("product_type mismatch")
    if required_attrs_match < 0.4 and passport.required_attributes:
        reason_parts.append("required attributes mismatch")
    if not reason_parts:
        reason_parts.append("title matches passport")
    reason = "; ".join(reason_parts)

    return SoldObjectResolution(
        same_sold_object=same_sold_object,
        confidence=score,
        sold_object=sold_object_text or title_clean,
        reference_object=reference_object_text,
        relation=relation_text,
        product_type_match=product_type_match,
        reason=reason,
        signals=signals,
        needs_review=needs_review,
    )


def _find_relation_marker(title_lower: str) -> tuple[str, int] | None:
    earliest: tuple[str, int] | None = None
    for marker in ALL_RELATION_MARKERS:
        if marker.endswith(" ") or marker.endswith(" "):
            pattern = re.escape(marker)
            match = re.search(pattern, title_lower)
        else:
            pattern = r"\b" + re.escape(marker.strip()) + r"\b"
            match = re.search(pattern, title_lower)
        if not match:
            continue
        position = match.start()
        if earliest is None or position < earliest[1]:
            earliest = (match.group(0), position)
    return earliest


def _has_accessory_negative(title_lower: str, passport: ProductPassport) -> bool:
    candidates = set(ACCESSORY_NEGATIVE_TOKENS)
    if passport.product_type:
        candidates.update(NEGATIVE_BY_TYPE.get(passport.product_type, ()))
    for token in candidates:
        if not token:
            continue
        if re.search(rf"\b{re.escape(token)}", title_lower):
            return True
    return False


def _references_target(reference_lower: str, brand: str, model: str, family: str) -> bool:
    for token in (model, family, brand):
        token = (token or "").strip().lower()
        if not token:
            continue
        if token and token in reference_lower:
            return True
    return False


def _scoring_product_type_match(title_lower: str, type_keywords: tuple[str, ...], product_type: str | None) -> float:
    if not type_keywords:
        return 0.5
    for keyword in type_keywords:
        if keyword in title_lower:
            return 1.0
    if product_type and product_type in title_lower:
        return 0.6
    return 0.0


def _product_type_keywords(product_type: str | None) -> tuple[str, ...]:
    return {
        "smartphone": ("смартфон", "phone", "iphone", "galaxy", "redmi", "pixel"),
        "laptop": ("ноутбук", "macbook", "laptop", "thinkpad", "ideapad"),
        "tablet": ("планшет", "ipad", "tab"),
        "printer": ("принтер", "мфу", "printer"),
        "scanner": ("сканер", "scanner"),
        "cartridge": ("картридж", "тонер"),
        "monitor": ("монитор", "display"),
        "tires": ("шина", "шины", "резина", "покрышка", "r1", "r2"),
        "clothing": ("шорты", "футболка", "куртка", "брюки", "джинсы", "кроссовки", "пиджак", "толстовка", "худи", "свитер"),
        "vacuum": ("пылесос", "vacuum"),
        "graphics_card": ("видеокарта", "rtx", "gtx", "radeon"),
    }.get(product_type or "", ())


def _scoring_text_match(*, sold_object: str, passport: ProductPassport) -> float:
    if not sold_object:
        return 0.0
    haystack = sold_object.lower()
    score = 0.0
    if passport.brand and passport.brand.lower() in haystack:
        score += 0.4
    if passport.model:
        model_tokens = [token for token in passport.model.lower().split() if len(token) > 1]
        if model_tokens:
            hits = sum(1 for token in model_tokens if re.search(rf"\b{re.escape(token)}\b", haystack))
            score += min(0.4, 0.4 * hits / max(1, len(model_tokens)))
    if passport.product_family and passport.product_family.lower() in haystack:
        score += 0.1
    for variant in passport.synonyms:
        if not variant:
            continue
        if variant.lower() in haystack:
            score += 0.05
            break
    return min(1.0, score)


def _scoring_required_attributes(title: str, description: str, passport: ProductPassport) -> float:
    required = passport.required_attributes or {}
    if not required:
        return 1.0
    haystack = (title + " " + description).lower()
    matched = 0
    total = 0
    for key, value in required.items():
        total += 1
        if value is None:
            continue
        text_value = str(value).lower()
        if key in {"storage_gb", "ram_gb", "tire_width", "tire_profile", "tire_diameter"}:
            if re.search(rf"(?<!\d){re.escape(text_value)}(?!\d)", haystack):
                matched += 1
        elif key == "color":
            from .product_passport import COLOR_VOCABULARY

            aliases = [alias.lower() for alias in COLOR_VOCABULARY.get(text_value, [text_value])]
            if any(alias in haystack for alias in aliases):
                matched += 1
        elif key in {"chip", "chipset", "gpu_series", "gpu_model", "gpu_modifier", "model_modifier", "model_number"}:
            if text_value in haystack:
                matched += 1
        else:
            if text_value in haystack:
                matched += 1
    if total == 0:
        return 1.0
    return matched / total


def _scoring_category_match(breadcrumbs: list[str], passport: ProductPassport) -> float:
    if not breadcrumbs or not passport.category_label:
        return 0.5
    label_lower = passport.category_label.lower()
    for crumb in breadcrumbs:
        if not crumb:
            continue
        if label_lower in crumb.lower():
            return 1.0
    if passport.product_type:
        for crumb in breadcrumbs:
            if passport.product_type in crumb.lower():
                return 0.7
    return 0.3
