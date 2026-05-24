"""LLM-based filter matcher stub.

Drop a real implementation here later. The shape is identical to
:func:`filters_match.match_filters` so it can be swapped in by the
service layer with a flag (``USE_LLM_MATCHER=1`` env var, see
:mod:`app.filters_service`).
"""
from __future__ import annotations

import json
import os
from typing import Any

from filters_match import match_filters as _heuristic_match_filters


LLM_MODEL_ENV = "FILTERS_LLM_MODEL"
LLM_API_KEY_ENV = "FILTERS_LLM_API_KEY"


def _has_credentials() -> bool:
    return bool(os.environ.get(LLM_API_KEY_ENV))


def _build_prompt(filters_by_source: dict[str, list[dict[str, Any]]]) -> str:
    """Build a self-contained prompt the LLM can answer."""
    lines = [
        "You are matching marketplace filters between sources.",
        "Group filters that represent the same product characteristic.",
        "Output JSON: {\"groups\": [{\"canonical_label\": str, \"members\": [{\"source\": str, \"id\": str}]}]}",
        "Sources and filters:",
    ]
    for source, filters in filters_by_source.items():
        lines.append(f"\n## {source}")
        for f in filters:
            lines.append(f"- id={f.get('id')!r}  label={f.get('label')!r}  type={f.get('type')!r}")
    return "\n".join(lines)


def _call_llm(prompt: str) -> dict[str, Any] | None:
    """Plug your LLM call here. Return parsed JSON groups or None on failure."""
    # TODO: integrate Anthropic / OpenAI / local LLM. Expected return:
    # {"groups": [{"canonical_label": "Бренд", "members": [{"source": "wildberries", "id": "fbrand"}, ...]}]}
    return None


def match_filters_llm(
    filters_by_source: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """LLM-driven matcher. Falls back to heuristic when no credentials/result.

    The return shape mirrors :func:`filters_match.match_filters` so callers
    can swap implementations transparently.
    """
    if not _has_credentials():
        result = _heuristic_match_filters(filters_by_source)
        result["matcher"] = "heuristic_fallback"
        result["llm_diagnostics"] = ["no_credentials"]
        return result

    prompt = _build_prompt(filters_by_source)
    llm_result = _call_llm(prompt)
    if not isinstance(llm_result, dict) or not isinstance(llm_result.get("groups"), list):
        result = _heuristic_match_filters(filters_by_source)
        result["matcher"] = "heuristic_fallback"
        result["llm_diagnostics"] = ["llm_returned_no_groups"]
        return result

    # Translate LLM groups -> internal shape, then patch on top of the
    # heuristic baseline so unmatched/coverage are populated.
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for source, filters in filters_by_source.items():
        for f in filters:
            by_id[(source, str(f.get("id")))] = f

    matched_groups: list[dict[str, Any]] = []
    used_keys: set[tuple[str, str]] = set()
    for raw_group in llm_result["groups"]:
        members: list[dict[str, Any]] = []
        for m in raw_group.get("members") or []:
            key = (m.get("source"), str(m.get("id")))
            entry = by_id.get(key)
            if entry is not None and key not in used_keys:
                members.append(entry)
                used_keys.add(key)
        if len(members) < 2:
            continue
        sources_present = sorted({m["source"] for m in members})
        matched_groups.append(
            {
                "canonical_label": raw_group.get("canonical_label")
                or members[0]["label"],
                "sources": sources_present,
                "members": members,
                "score": 1.0,
            }
        )

    sources = sorted(filters_by_source.keys())
    coverage: dict[str, dict[str, Any]] = {}
    for source in sources:
        total = len(filters_by_source[source])
        matched = sum(1 for k in used_keys if k[0] == source)
        coverage[source] = {
            "total": total,
            "matched": matched,
            "ratio": round(matched / total, 3) if total else 0.0,
        }
    unmatched: dict[str, list[dict[str, Any]]] = {s: [] for s in sources}
    for source, filters in filters_by_source.items():
        for f in filters:
            if (source, str(f.get("id"))) not in used_keys:
                unmatched[source].append(f)
    total_filters = sum(coverage[s]["total"] for s in sources)
    total_matched = sum(coverage[s]["matched"] for s in sources)
    return {
        "sources": sources,
        "matched_groups": matched_groups,
        "unmatched": unmatched,
        "coverage": coverage,
        "totals": {
            "total_filters": total_filters,
            "matched_filters": total_matched,
            "matched_ratio": round(total_matched / total_filters, 3) if total_filters else 0.0,
            "matched_groups": len(matched_groups),
        },
        "pair_stats": [],
        "threshold": None,
        "matcher": "llm",
    }


__all__ = ["match_filters_llm"]
