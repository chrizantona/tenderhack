from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from filters_common import normalize_filter


_FILTER_BLOCK_RE = re.compile(
    r'<div[^>]*data-filter-(?:id|type)="(?P<a>[^"]+)"[^>]*data-filter-(?:id|type)="(?P<b>[^"]+)"[^>]*>',
)

_LABEL_RE = re.compile(
    r'<span class="mUEru[^"]*"[^>]*>([^<]+)</span>',
)

_VALUE_LABEL_RE = re.compile(
    r'data-zone-name="filterValue"[^>]*>.*?<span[^>]*>([^<]+)</span>',
    re.DOTALL,
)


def _looks_like_id(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_\-]+$", value)) and any(c.isdigit() for c in value) is False or value.isdigit()


def _parse_filter_block(html_src: str, pos: int, length: int = 4000) -> dict[str, Any]:
    chunk = html_src[pos : pos + length]
    return chunk


def _extract_label_near(html_src: str, pos: int, length: int = 4000) -> str | None:
    chunk = html_src[pos : pos + length]
    # Skip past the opening tag (which itself contains data-filter-id and data-filter-type).
    first_close = chunk.find(">")
    search_from = first_close + 1 if first_close >= 0 else 0
    rest = chunk[search_from:]
    next_filter = re.search(r'data-filter-(?:id|type)="', rest)
    if next_filter is not None:
        rest = rest[: next_filter.start()]
    m = _LABEL_RE.search(rest)
    if m:
        return html.unescape(m.group(1))
    return None


def _extract_values_near(html_src: str, pos: int, length: int = 6000) -> list[dict[str, Any]]:
    chunk = html_src[pos : pos + length]
    values: list[dict[str, Any]] = []
    for m in _VALUE_LABEL_RE.finditer(chunk):
        name = html.unescape(m.group(1)).strip()
        if not name:
            continue
        values.append({"name": name})
    return values


def parse_yandex_market_filters_html(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    src = text

    # Two attribute orderings exist in the markup.
    blocks: list[tuple[int, str, str]] = []
    for m in re.finditer(
        r'<div[^>]*data-filter-id="(?P<fid>[^"]+)"[^>]*data-filter-type="(?P<ftype>[^"]+)"[^>]*>',
        src,
    ):
        blocks.append((m.start(), m.group("fid"), m.group("ftype")))
    for m in re.finditer(
        r'<div[^>]*data-filter-type="(?P<ftype>[^"]+)"[^>]*data-filter-id="(?P<fid>[^"]+)"[^>]*>',
        src,
    ):
        blocks.append((m.start(), m.group("fid"), m.group("ftype")))

    blocks.sort()
    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    for pos, fid, ftype in blocks:
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        label = _extract_label_near(src, pos)
        if not label or not label.strip():
            # Booleans without a visible legend pollute the matcher and are not useful.
            continue
        values = _extract_values_near(src, pos)
        results.append(
            normalize_filter(
                source="yandex_market",
                raw_id=fid,
                label=label,
                filter_type=ftype,
                values=values,
            )
        )
    return results


def parse_yandex_market_filters_file(path: str | Path) -> list[dict[str, Any]]:
    return parse_yandex_market_filters_html(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "YM.html"
    filters = parse_yandex_market_filters_file(target)
    print(json.dumps({"count": len(filters), "filters": filters}, ensure_ascii=False, indent=2))
