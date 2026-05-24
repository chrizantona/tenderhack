from __future__ import annotations

import re
import unicodedata
from typing import Any


_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


STOP_TOKENS = {
    "и", "или", "на", "в", "с", "из", "по", "для", "от", "до",
    "the", "a", "an", "of", "for", "by",
}

UNIT_TOKENS = {
    "см", "мм", "м", "г", "кг", "мг", "л", "мл",
    "дб", "вт", "квт", "ва", "кв",
    "кгсут", "квтч", "штук", "шт", "штгод", "лгод",
    "год", "сут", "час", "мин",
}

GENERIC_NOISE = {
    "предмета", "товара", "общий",
    "уровень", "значение",
    "тип", "вид", "способ", "категория",
}


def _strip_units(token: str) -> str:
    if not token:
        return token
    if token in UNIT_TOKENS:
        return ""
    return token


def normalize_label(text: str | None) -> str:
    if not text:
        return ""
    s = text.strip().lower()
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def tokens(text: str | None) -> list[str]:
    norm = normalize_label(text)
    if not norm:
        return []
    raw = [t for t in norm.split(" ") if t]
    cleaned: list[str] = []
    for t in raw:
        if t in STOP_TOKENS:
            continue
        if t.isdigit():
            continue
        stripped = _strip_units(t)
        if not stripped:
            continue
        cleaned.append(stripped)
    return cleaned


def content_tokens(text: str | None) -> list[str]:
    base = tokens(text)
    return [t for t in base if t not in GENERIC_NOISE]


def jaccard(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    return len(inter) / len(union)


def overlap(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    if not inter:
        return 0.0
    return len(inter) / min(len(sa), len(sb))


def similarity(label_a: str, label_b: str) -> float:
    base_a = tokens(label_a)
    base_b = tokens(label_b)
    if not base_a or not base_b:
        return 0.0
    content_a = content_tokens(label_a) or base_a
    content_b = content_tokens(label_b) or base_b
    j_base = jaccard(base_a, base_b)
    j_content = jaccard(content_a, content_b)
    o_content = overlap(content_a, content_b)
    score = max(j_content, 0.6 * o_content + 0.4 * j_base)
    if set(content_a) == set(content_b):
        score = max(score, 0.99)
    return min(score, 1.0)


def normalize_filter(
    source: str,
    raw_id: Any,
    label: str | None,
    *,
    filter_type: str | None = None,
    values: list[dict[str, Any]] | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full_values: list[dict[str, Any]] = []
    for v in values or []:
        if not isinstance(v, dict):
            continue
        name = v.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        vid = v.get("id")
        full_values.append({"name": name.strip(), "id": str(vid) if vid is not None else None})
    return {
        "source": source,
        "id": str(raw_id) if raw_id is not None else None,
        "label": (label or "").strip(),
        "label_normalized": normalize_label(label),
        "tokens": tokens(label),
        "content_tokens": content_tokens(label),
        "type": filter_type,
        "values_count": len(full_values),
        "values_preview": [v["name"] for v in full_values[:6]],
        "values": full_values,
        "raw_id": raw_id,
    }
