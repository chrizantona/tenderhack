from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


STOPWORDS = {
    "для",
    "и",
    "или",
    "на",
    "в",
    "во",
    "с",
    "со",
    "по",
    "без",
    "купить",
    "цена",
    "оригинал",
    "новый",
    "new",
}

CATEGORY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tires", "шины", ("шина", "шины", "резина", "покрышка", "r16", "r17", "r18", "летние", "зимние")),
    ("office_equipment", "оргтехника", ("принтер", "мфу", "сканер", "картридж", "laserjet", "deskjet", "pixma")),
    ("memory", "оперативная память", ("ddr3", "ddr4", "ddr5", "sodimm", "so-dimm", "dimm", "оперативная", "память")),
    ("clothing", "одежда", ("шорты", "футболка", "куртка", "кроссовки", "брюки", "джинсы", "adidas", "nike")),
    ("supplements", "спортпит", ("креатин", "протеин", "bcaa", "моногидрат", "creatine", "protein")),
)

SYNONYMS = {
    "резина": "шины",
    "покрышка": "шины",
    "мфу": "принтер",
    "lazerjet": "laserjet",
    "лазерджет": "laserjet",
    "оперативка": "оперативная память",
    "содимм": "so-dimm",
    "кроссы": "кроссовки",
}

SPEC_PATTERNS = (
    r"\b\d+(?:[.,]\d+)?\s*(?:gb|гб|tb|тб|mb|мб)\b",
    r"\b\d+(?:[.,]\d+)?\s*(?:kg|кг|g|гр|г|l|л|ml|мл)\b",
    r"\b\d{3,5}\s*(?:mhz|мгц)\b",
    r"\bcl\d+\b",
    r"\b\d{3}/\d{2}\s*r\d{2}\b",
    r"\br\d{2}\b",
    r"\b\d+(?:[.,]\d+)?v\b",
    r"\b[a-zа-я]{1,4}\d{2,}[\w-]*\b",
)


@dataclass
class QueryVariant:
    level: str
    query: str
    required_terms: list[str]
    optional_terms: list[str]
    relaxed_terms: list[str]
    reason: str


@dataclass
class QueryPlan:
    original_query: str
    normalized_query: str
    detected_category: str | None
    category_label: str | None
    variants: list[QueryVariant]


def main() -> int:
    parser = argparse.ArgumentParser(description="Query planning and candidate scoring helpers.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Build strict/core/broad query variants.")
    plan_parser.add_argument("query")

    score_parser = subparsers.add_parser("score", help="Score normalized products from a JSON result.")
    score_parser.add_argument("input", help="JSON search result with an items array, or a raw array of products.")
    score_parser.add_argument("--query", required=True)
    score_parser.add_argument("--limit", type=int, default=20)
    score_parser.add_argument("--output")

    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(asdict(build_query_plan(args.query)), ensure_ascii=False, indent=2))
        return 0

    result = score_json_result(Path(args.input), query=args.query, limit=args.limit)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def build_query_plan(query: str) -> QueryPlan:
    normalized_query = normalize_text(query)
    corrected = apply_synonyms(normalized_query)
    tokens = tokenize(corrected)
    category_id, category_label = detect_category(tokens)

    spec_terms = extract_spec_terms(corrected)
    brand_or_model_terms = [token for token in tokens if token not in STOPWORDS and token not in spec_terms]
    category_terms = tokenize(category_label or "")

    strict_terms = trim_query_terms(tokens, spec_terms=spec_terms, max_specs=4)
    core_terms = choose_core_terms(brand_or_model_terms, spec_terms=spec_terms, category_terms=category_terms)
    broad_terms = unique(category_terms + brand_or_model_terms[:2]) or brand_or_model_terms[:3] or tokens[:3]

    variants = [
        QueryVariant(
            level="strict",
            query=" ".join(strict_terms),
            required_terms=strict_terms[: min(5, len(strict_terms))],
            optional_terms=strict_terms[min(5, len(strict_terms)) :],
            relaxed_terms=[term for term in tokens if term not in strict_terms],
            reason="Максимально точный запрос, но без длинного хвоста вторичных характеристик.",
        ),
        QueryVariant(
            level="core",
            query=" ".join(core_terms),
            required_terms=core_terms[: min(4, len(core_terms))],
            optional_terms=core_terms[min(4, len(core_terms)) :],
            relaxed_terms=[term for term in tokens if term not in core_terms],
            reason="Бренд/модель/ключевой размер, если strict дал мало хороших кандидатов.",
        ),
        QueryVariant(
            level="broad",
            query=" ".join(broad_terms),
            required_terms=broad_terms[: min(3, len(broad_terms))],
            optional_terms=broad_terms[min(3, len(broad_terms)) :],
            relaxed_terms=[term for term in tokens if term not in broad_terms],
            reason="Категорийный fallback для добора кандидатов перед строгим attribute matching.",
        ),
    ]

    return QueryPlan(
        original_query=query,
        normalized_query=corrected,
        detected_category=category_id,
        category_label=category_label,
        variants=dedupe_variants(variants),
    )


def score_json_result(path: Path, *, query: str, limit: int) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        items = payload
        result: dict[str, Any] = {"items": items}
    else:
        result = payload
        items = list(payload.get("items") or [])

    plan = build_query_plan(query)
    features_by_index = build_log_features(items)
    scored = []
    for index, item in enumerate(items):
        item_copy = dict(item)
        features = features_by_index[index]
        item_copy["search_quality"] = {
            "query_plan_level": best_query_level(plan, item),
            "features": features,
            "score": score_candidate(plan, item, features),
        }
        scored.append(item_copy)

    scored.sort(key=lambda product: product["search_quality"]["score"], reverse=True)
    result["items"] = scored[: max(1, limit)]
    result["source_meta"] = dict(result.get("source_meta") or {})
    result["source_meta"]["query_plan"] = asdict(plan)
    result["source_meta"]["ranking"] = {
        "strategy": "text_overlap + attribute hints + log_price_deviation penalty",
        "log_features": ["log_price", "log_price_per_unit", "log_weight", "log_volume", "log_quantity"],
    }
    return result


def build_log_features(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base_features = [extract_item_features(item) for item in items]
    cohort_medians = cohort_log_price_medians(items, base_features)
    global_logs = [feature["log_price"] for feature in base_features if feature.get("log_price") is not None]
    global_median = statistics.median(global_logs) if global_logs else None

    enriched = []
    for item, feature in zip(items, base_features, strict=False):
        cohort_key = comparable_key(item)
        median_log_price = cohort_medians.get(cohort_key, global_median)
        log_price = feature.get("log_price")
        if log_price is not None and median_log_price is not None:
            feature["median_log_price"] = round(median_log_price, 6)
            feature["log_price_deviation"] = round(abs(log_price - median_log_price), 6)
        else:
            feature["median_log_price"] = None
            feature["log_price_deviation"] = None
        enriched.append(feature)
    return enriched


def extract_item_features(item: dict[str, Any]) -> dict[str, Any]:
    price = number_at(item, ("price", "current"))
    raw_attrs = (item.get("attributes") or {}).get("raw") or {}
    normalized_attrs = (item.get("attributes") or {}).get("normalized") or {}
    title = " ".join(str(value or "") for value in (item.get("title"), item.get("normalized_title")))
    raw_text = json.dumps(raw_attrs, ensure_ascii=False)
    normalized_text = json.dumps(normalized_attrs, ensure_ascii=False)
    text = " ".join([title, normalized_text, raw_text])
    compact_text = " ".join([title, normalized_text])

    weight = first_positive(
        number_at(normalized_attrs, ("weight_g",)),
        parse_measure(compact_text, "weight_g"),
        parse_measure(raw_text, "weight_g"),
    )
    volume = first_positive(
        number_at(normalized_attrs, ("volume_ml",)),
        parse_measure(compact_text, "volume_ml"),
    )
    quantity = first_positive(
        number_at(normalized_attrs, ("quantity",)),
        parse_measure(compact_text, "quantity"),
    )
    length = first_positive(
        number_at(normalized_attrs, ("length_mm",)),
        parse_measure(compact_text, "length_mm"),
    )
    width = first_positive(
        number_at(normalized_attrs, ("width_mm",)),
        parse_measure(compact_text, "width_mm"),
    )
    height = first_positive(
        number_at(normalized_attrs, ("height_mm",)),
        parse_measure(compact_text, "height_mm"),
    )

    unit_value = weight or volume or quantity
    price_per_unit = price / unit_value if price and unit_value else None
    unit_kind = "weight_g" if weight else "volume_ml" if volume else "quantity" if quantity else None

    return {
        "log_price": safe_log(price),
        "price_per_unit": round(price_per_unit, 6) if price_per_unit else None,
        "log_price_per_unit": safe_log(price_per_unit),
        "unit_kind": unit_kind,
        "log_weight": safe_log(weight),
        "log_volume": safe_log(volume),
        "log_length": safe_log(length),
        "log_width": safe_log(width),
        "log_height": safe_log(height),
        "log_quantity": safe_log(quantity),
    }


def cohort_log_price_medians(items: list[dict[str, Any]], features: list[dict[str, Any]]) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for item, feature in zip(items, features, strict=False):
        log_price = feature.get("log_price")
        if log_price is None:
            continue
        groups.setdefault(comparable_key(item), []).append(log_price)
    return {key: statistics.median(values) for key, values in groups.items() if len(values) >= 2}


def comparable_key(item: dict[str, Any]) -> str:
    brand = normalize_text(((item.get("brand") or {}).get("name")) or (item.get("match_keys") or {}).get("brand") or "")
    model = normalize_text((item.get("match_keys") or {}).get("model") or "")
    category = normalize_text(((item.get("category") or {}).get("name")) or "")
    return "|".join(value for value in (category, brand, model) if value) or "global"


def score_candidate(plan: QueryPlan, item: dict[str, Any], features: dict[str, Any]) -> float:
    searchable = item_search_text(item)
    tokens = set(tokenize(searchable))
    best_overlap = 0.0
    for variant in plan.variants:
        required = set(variant.required_terms)
        optional = set(variant.optional_terms)
        required_score = len(required & tokens) / max(1, len(required))
        optional_score = len(optional & tokens) / max(1, len(optional)) if optional else 0.0
        level_weight = {"strict": 1.0, "core": 0.85, "broad": 0.65}.get(variant.level, 0.5)
        best_overlap = max(best_overlap, level_weight * ((required_score * 0.75) + (optional_score * 0.25)))

    score = best_overlap * 100
    if (item.get("media") or {}).get("main_image_url"):
        score += 4
    if number_at(item, ("price", "current")):
        score += 6
    if (item.get("attributes") or {}).get("raw"):
        score += 4
    if features.get("log_price_deviation") is not None:
        score -= min(20.0, features["log_price_deviation"] * 8)
    return round(score, 3)


def best_query_level(plan: QueryPlan, item: dict[str, Any]) -> str:
    tokens = set(tokenize(item_search_text(item)))
    for variant in plan.variants:
        required = set(variant.required_terms)
        if required and len(required & tokens) / len(required) >= 0.6:
            return variant.level
    return "unmatched"


def item_search_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("normalized_title"),
        (item.get("brand") or {}).get("name"),
        (item.get("category") or {}).get("name"),
        (item.get("match_keys") or {}).get("semantic_key"),
        json.dumps((item.get("attributes") or {}).get("normalized") or {}, ensure_ascii=False),
        json.dumps((item.get("attributes") or {}).get("raw") or {}, ensure_ascii=False),
    ]
    return normalize_text(" ".join(str(part or "") for part in parts))


def choose_core_terms(terms: list[str], *, spec_terms: list[str], category_terms: list[str]) -> list[str]:
    prioritized_specs = [term for term in spec_terms if is_primary_spec(term)]
    core = unique(terms[:3] + prioritized_specs[:2])
    if len(core) < 2:
        core = unique(terms[:2] + category_terms[:2])
    return core or unique(category_terms + spec_terms[:1])


def trim_query_terms(tokens: list[str], *, spec_terms: list[str], max_specs: int) -> list[str]:
    result = []
    used_specs = 0
    for token in tokens:
        if token in STOPWORDS:
            continue
        if token in spec_terms:
            if used_specs >= max_specs:
                continue
            used_specs += 1
        result.append(token)
        if len(result) >= 8:
            break
    return result or tokens[:5]


def detect_category(tokens: list[str]) -> tuple[str | None, str | None]:
    token_set = set(tokens)
    for category_id, label, markers in CATEGORY_RULES:
        if token_set & set(markers):
            return category_id, label
    return None, None


def extract_spec_terms(query: str) -> list[str]:
    specs = []
    for pattern in SPEC_PATTERNS:
        for match in re.finditer(pattern, query, flags=re.I):
            specs.extend(tokenize(match.group(0)))
    return unique(specs)


def is_primary_spec(term: str) -> bool:
    return bool(
        re.search(r"\d", term)
        and not re.fullmatch(r"\d+(?:[.,]\d+)?v", term)
        and not re.fullmatch(r"cl\d+", term)
    )


def apply_synonyms(text: str) -> str:
    tokens = tokenize(text)
    expanded = []
    for token in tokens:
        expanded.append(SYNONYMS.get(token, token))
    return " ".join(expanded)


def normalize_text(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"[^0-9a-zа-я%./+-]+", " ", text)
    return " ".join(text.split())


def tokenize(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token and token not in STOPWORDS]


def dedupe_variants(variants: list[QueryVariant]) -> list[QueryVariant]:
    result = []
    seen = set()
    for variant in variants:
        key = variant.query
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(variant)
    return result


def unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def safe_log(value: float | int | None) -> float | None:
    if value is None or value <= 0:
        return None
    return round(math.log(float(value)), 6)


def first_positive(*values: float | None) -> float | None:
    for value in values:
        if value is not None and value > 0:
            return value
    return None


def number_at(payload: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(" ", ""))
    return float(match.group(0).replace(",", ".")) if match else None


def parse_measure(text: str, kind: str) -> float | None:
    normalized = normalize_text(text)
    if kind == "weight_g":
        return parse_weight_g(normalized)
    if kind == "volume_ml":
        return parse_volume_ml(normalized)
    if kind == "quantity":
        return parse_quantity(normalized)
    if kind in {"length_mm", "width_mm", "height_mm"}:
        return parse_length_mm(normalized)
    return None


def parse_weight_g(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(кг|kg|гр|г|g)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    return value * 1000 if unit in {"кг", "kg"} else value


def parse_volume_ml(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(мл|ml|л|l)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    return value * 1000 if unit in {"л", "l"} else value


def parse_quantity(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(шт|pcs|pieces|штук)\b", text)
    return float(match.group(1)) if match else None


def parse_length_mm(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(мм|mm|см|cm|м|m)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit in {"м", "m"}:
        return value * 1000
    if unit in {"см", "cm"}:
        return value * 10
    return value


if __name__ == "__main__":
    raise SystemExit(main())
