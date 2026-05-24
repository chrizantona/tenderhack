from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from filters_common import normalize_filter
from ozon_html import _extract_widget_states


_FILTER_WIDGET_PREFIXES = (
    "filtersDesktop",
    "filtersMobile",
    "searchResultsFilters",
    "searchFilters",
    "filterPanel",
)


def _is_filter_widget(widget_id: str) -> bool:
    if not widget_id:
        return False
    lower = widget_id.lower()
    return any(prefix.lower() in lower for prefix in _FILTER_WIDGET_PREFIXES)


def _extract_values(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    out: list[dict[str, Any]] = []
    for vk in ("values", "options", "items", "checkboxes", "colors", "categories"):
        items = body.get(vk)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            vname = (
                it.get("name")
                or it.get("title")
                or it.get("text")
                or it.get("label")
                or it.get("displayName")
            )
            if not isinstance(vname, str) or not vname.strip():
                continue
            out.append({"name": vname.strip(), "id": it.get("key") or it.get("id")})
    return out


def _ozon_filter_to_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a single entry from filtersDesktop sections[].filters[] to a normalized filter."""
    ftype = entry.get("type")
    key = entry.get("key") or entry.get("id")
    if not isinstance(ftype, str) or not isinstance(key, (str, int)):
        return None
    body = entry.get(ftype) if isinstance(entry.get(ftype), dict) else {}
    title = None
    if isinstance(body, dict):
        title = body.get("title") or body.get("name") or body.get("text")
    if not title:
        title = entry.get("title") or entry.get("name")
    if not isinstance(title, str) or not title.strip():
        return None
    label = title.replace("\n", " ").strip()
    values = _extract_values(body)
    return normalize_filter(
        source="ozon",
        raw_id=str(key),
        label=label,
        filter_type=ftype,
        values=values,
        raw=entry,
    )


def _parse_filters_desktop(state: Any, sink: list[dict[str, Any]], seen: set[str]) -> None:
    if not isinstance(state, dict):
        return
    sections = state.get("sections") or []
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        entries = section.get("filters") or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            record = _ozon_filter_to_record(entry)
            if record is None:
                continue
            if record["id"] in seen:
                continue
            sink.append(record)
            seen.add(record["id"])


def _walk_generic_filters(node: Any, sink: list[dict[str, Any]], seen: set[str]) -> None:
    """Fallback walker for older or differently-shaped Ozon filter payloads."""
    if isinstance(node, dict):
        key = node.get("key") or node.get("id") or node.get("filterKey")
        name = node.get("name") or node.get("title") or node.get("text")
        values = node.get("values") or node.get("options") or node.get("items")
        type_hint = node.get("type") or node.get("filterType") or node.get("uiType")
        if (
            isinstance(key, (str, int))
            and isinstance(name, str)
            and name.strip()
            and (
                isinstance(values, list)
                or any(k in node for k in ("rangeFrom", "rangeTo", "min", "max"))
                or (type_hint and isinstance(type_hint, str))
            )
        ):
            key_str = str(key)
            if key_str not in seen:
                norm_values: list[dict[str, Any]] = []
                if isinstance(values, list):
                    for v in values:
                        if isinstance(v, dict):
                            vname = v.get("name") or v.get("text") or v.get("title") or v.get("label")
                            if isinstance(vname, str) and vname.strip():
                                norm_values.append({"name": vname.strip(), "id": v.get("key") or v.get("id")})
                ftype = None
                if isinstance(type_hint, str):
                    ftype = type_hint
                elif "rangeFrom" in node or "rangeTo" in node or "min" in node or "max" in node:
                    ftype = "range"
                elif norm_values:
                    ftype = "enum"
                sink.append(
                    normalize_filter(
                        source="ozon",
                        raw_id=key_str,
                        label=name,
                        filter_type=ftype,
                        values=norm_values,
                        raw=node,
                    )
                )
                seen.add(key_str)
        for v in node.values():
            _walk_generic_filters(v, sink, seen)
    elif isinstance(node, list):
        for item in node:
            _walk_generic_filters(item, sink, seen)


def parse_ozon_filters_html(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    states = _extract_widget_states(text)
    sink: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Prefer filtersDesktop and similar dedicated filter widgets.
    for widget_id, state in states.items():
        if _is_filter_widget(widget_id):
            _parse_filters_desktop(state, sink, seen)
    if not sink:
        for widget_id, state in states.items():
            _walk_generic_filters(state, sink, seen)
    sink = [f for f in sink if f["label"]]
    return sink


def parse_ozon_filters_file(path: str | Path) -> list[dict[str, Any]]:
    return parse_ozon_filters_html(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "out/ozon_filters.html"
    if not Path(target).exists():
        print(json.dumps({"error": "fixture_not_found", "path": target}, ensure_ascii=False))
        raise SystemExit(0)
    filters = parse_ozon_filters_file(target)
    print(json.dumps({"count": len(filters), "filters": filters}, ensure_ascii=False, indent=2))
