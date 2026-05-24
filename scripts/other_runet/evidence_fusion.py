from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .product_passport import COLOR_VOCABULARY, ProductPassport
from .structured_extractor import Evidence, ExtractedOffer, SOURCE_RELIABILITY


@dataclass
class IdentityScore:
    score: float
    components: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "components": {key: round(value, 3) for key, value in self.components.items()},
        }


def compute_identity_confidence(offer: ExtractedOffer, passport: ProductPassport) -> IdentityScore:
    components: dict[str, float] = {
        "gtin_exact_match": 0.0,
        "mpn_brand_match": 0.0,
        "required_attrs_match": 0.0,
        "title_semantic_match": 0.0,
        "category_match": 0.0,
    }
    identifiers = passport.identifier_candidates
    gtin_targets = {value.lower() for value in identifiers.get("gtin", []) + identifiers.get("ean", [])}
    mpn_targets = {value.lower() for value in identifiers.get("mpn", []) + identifiers.get("sku", [])}
    if offer.gtin and offer.gtin.lower() in gtin_targets:
        components["gtin_exact_match"] = 1.0
    elif gtin_targets and offer.gtin:
        components["gtin_exact_match"] = 0.0
    if offer.mpn:
        if mpn_targets and offer.mpn.lower() in mpn_targets:
            components["mpn_brand_match"] = 1.0
        elif passport.brand and offer.brand and offer.brand.lower() == passport.brand.lower():
            components["mpn_brand_match"] = 0.6
    elif passport.brand and offer.brand and offer.brand.lower() == passport.brand.lower():
        components["mpn_brand_match"] = 0.4

    components["required_attrs_match"] = _required_attrs_match(offer, passport)
    components["title_semantic_match"] = _title_semantic_match(offer.title or "", passport)
    components["category_match"] = _category_match(offer, passport)
    score = (
        components["gtin_exact_match"] * 0.40
        + components["mpn_brand_match"] * 0.25
        + components["required_attrs_match"] * 0.20
        + components["title_semantic_match"] * 0.10
        + components["category_match"] * 0.05
    )
    return IdentityScore(score=score, components=components)


def evidence_strength(evidence_items: Iterable[Evidence]) -> float:
    if not evidence_items:
        return 0.0
    best_per_field: dict[str, float] = {}
    for item in evidence_items:
        reliability = SOURCE_RELIABILITY.get(item.source, 0.5)
        composite = reliability * float(item.confidence or 0)
        best = best_per_field.get(item.field, 0.0)
        if composite > best:
            best_per_field[item.field] = composite
    if not best_per_field:
        return 0.0
    weight_map = {
        "price": 0.30,
        "title": 0.20,
        "availability": 0.15,
        "brand": 0.10,
        "sku": 0.10,
        "gtin": 0.10,
        "breadcrumbs": 0.05,
    }
    total_weight = 0.0
    weighted = 0.0
    for field_name, score in best_per_field.items():
        weight = weight_map.get(field_name, 0.05)
        total_weight += weight
        weighted += weight * score
    if total_weight == 0:
        return 0.0
    return min(1.0, weighted / total_weight)


def _required_attrs_match(offer: ExtractedOffer, passport: ProductPassport) -> float:
    required = passport.required_attributes or {}
    if not required:
        return 1.0
    haystack = " ".join(
        filter(
            None,
            [
                offer.title,
                offer.description,
                " ".join(offer.attributes.values()),
            ],
        )
    ).lower()
    matched = 0
    total = 0
    for key, value in required.items():
        total += 1
        text_value = str(value).lower()
        if key == "color":
            aliases = [alias.lower() for alias in COLOR_VOCABULARY.get(text_value, [text_value])]
            if any(alias in haystack for alias in aliases):
                matched += 1
        elif key in {"storage_gb", "ram_gb", "tire_width", "tire_profile", "tire_diameter", "frequency_mhz"}:
            if re.search(rf"(?<!\d){re.escape(text_value)}(?!\d)", haystack):
                matched += 1
        else:
            if text_value in haystack:
                matched += 1
    if total == 0:
        return 1.0
    return matched / total


def _title_semantic_match(title: str, passport: ProductPassport) -> float:
    if not title:
        return 0.0
    haystack = title.lower()
    score = 0.0
    if passport.brand and passport.brand.lower() in haystack:
        score += 0.4
    if passport.model:
        model_tokens = [token for token in passport.model.lower().split() if len(token) > 1]
        if model_tokens:
            hits = sum(1 for token in model_tokens if re.search(rf"\b{re.escape(token)}\b", haystack))
            score += min(0.5, 0.5 * hits / max(1, len(model_tokens)))
    if passport.product_family and passport.product_family.lower() in haystack:
        score += 0.1
    return min(1.0, score)


def _category_match(offer: ExtractedOffer, passport: ProductPassport) -> float:
    target = (passport.category_label or "").lower()
    if not target:
        return 0.5
    for crumb in offer.breadcrumbs:
        if crumb and target in crumb.lower():
            return 1.0
    if offer.category and target in offer.category.lower():
        return 1.0
    return 0.3
