from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .marketplace_exclusion import CORE_EXCLUDED_SUFFIXES, EXTRA_EXCLUDED_SUFFIXES
from .product_passport import ProductPassport


COMMERCIAL_SUFFIXES = ("купить", "цена", "в наличии")
SHOP_SUFFIX = "магазин"

DEFAULT_DISCOVERY_EXCLUDE: tuple[str, ...] = CORE_EXCLUDED_SUFFIXES + EXTRA_EXCLUDED_SUFFIXES


@dataclass
class SwarmQuery:
    text: str
    intent: str
    priority: int
    use_for: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "intent": self.intent,
            "priority": self.priority,
            "use_for": list(self.use_for),
        }


def generate_swarm(passport: ProductPassport) -> list[SwarmQuery]:
    queries: list[SwarmQuery] = []
    seen: set[str] = set()

    def add(text: str, intent: str, priority: int, use_for: Iterable[str]) -> None:
        normalized = " ".join((text or "").split()).strip()
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(SwarmQuery(text=normalized, intent=intent, priority=priority, use_for=tuple(use_for)))

    canonical = passport.canonical_query
    raw = passport.raw_query
    brand = passport.brand or ""
    model = passport.model or ""
    product_type = passport.product_type or ""

    add(raw, intent="raw", priority=10, use_for=("local_index",))
    add(canonical, intent="canonical", priority=9, use_for=("local_index", "discovery"))

    if brand and model and model != canonical:
        add(f"{brand} {model}", intent="brand_model", priority=8, use_for=("local_index", "discovery"))

    for suffix in COMMERCIAL_SUFFIXES:
        if canonical:
            add(f"{canonical} {suffix}", intent="commercial", priority=7, use_for=("discovery",))

    for identifier_type, values in passport.identifier_candidates.items():
        for value in values:
            priority = 8 if identifier_type in {"gtin", "ean", "mpn"} else 6
            add(f"{value} {SHOP_SUFFIX}", intent=f"identifier_{identifier_type}", priority=priority, use_for=("discovery",))
            add(value, intent=f"identifier_{identifier_type}_raw", priority=priority, use_for=("local_index",))

    for synonym in passport.synonyms:
        if synonym and synonym.lower() != canonical.lower():
            add(synonym, intent="synonym", priority=5, use_for=("local_index",))

    for variant in passport.russian_variants:
        if variant:
            add(f"{variant} {model}".strip(), intent="russian_variant", priority=5, use_for=("discovery",))

    for variant in passport.english_variants:
        if variant and variant.lower() != canonical.lower():
            add(variant, intent="english_variant", priority=5, use_for=("local_index", "discovery"))

    if product_type:
        type_label = passport.russian_variants[0] if passport.russian_variants else product_type
        if brand:
            add(f"{type_label} {brand} {model}".strip(), intent="typed_brand_model", priority=6, use_for=("discovery",))

    queries.sort(key=lambda item: -item.priority)
    return queries


def build_discovery_query(swarm_query: SwarmQuery, *, exclude_suffixes: Iterable[str] = DEFAULT_DISCOVERY_EXCLUDE) -> str:
    base = swarm_query.text.strip()
    if not base:
        return ""
    exclusions = " ".join(f"-site:{suffix}" for suffix in sorted(set(exclude_suffixes)))
    if swarm_query.intent in {"commercial", "typed_brand_model"} or "купить" in base or "цена" in base:
        return f'"{base}" ₽ {exclusions}'.strip()
    return f'"{base}" купить цена ₽ {exclusions}'.strip()


def queries_for_local_index(swarm: list[SwarmQuery]) -> list[SwarmQuery]:
    return [item for item in swarm if "local_index" in item.use_for]


def queries_for_discovery(swarm: list[SwarmQuery]) -> list[SwarmQuery]:
    return [item for item in swarm if "discovery" in item.use_for]
