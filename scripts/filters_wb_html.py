"""Parse WB filters from rendered SERP HTML (after clicking "Все фильтры").

WB's JSON filter endpoint (search.wb.ru/...?resultset=filters) rate-limits
non-residential IPs aggressively. Camoufox loads the SERP page, clicks the
"Все фильтры" button to render the sidebar, and we parse `.filters-desktop`
sections from the DOM.

Each section is:
    <div class="filters-desktop__item ... filters-desktop__item--<KEY> ...">
      <div class="...title..."><span>Бренд</span><button>Сбросить</button></div>
      <ul class="filter__list">
        <li>Yokohama</li>
        <li>CORDIANT</li>
        ...
      </ul>
    </div>

Boolean filters live in `.filters-desktop__switch--<KEY>` (no items).
"""

from __future__ import annotations

import re
from typing import Any

from filters_common import normalize_filter


def parse_wb_filters_html(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    sidebar = soup.select_one(".filters-desktop, .filters-desktop__list")
    if sidebar is None:
        return []

    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for section in sidebar.select(".filters-desktop__item, .filters-desktop__switch"):
        classes = " ".join(section.get("class", []))
        key = _extract_key(classes)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        label = _extract_label(section)
        if not label:
            continue
        is_switch = "filters-desktop__switch" in classes
        if is_switch:
            out.append(_normalize(key=key, label=label, items=[], filter_type="boolean"))
            continue
        # Price has min/max inputs instead of a list
        if section.select_one(".filter__price, [class*='filter__min'], [class*='filter__max']"):
            min_val, max_val = _extract_price_range(section)
            out.append(_normalize(
                key=key, label=label, items=[], filter_type="range",
                extra={"min": min_val, "max": max_val},
            ))
            continue
        items = _extract_items(section)
        out.append(_normalize(
            key=key, label=label, items=items,
            filter_type="enum" if items else "boolean",
        ))
    return out


def _extract_key(classes: str) -> str | None:
    """Pick the real filter key, not the `type-N` modifier.

    WB tags each filter twice in its class list: once with the
    presentation type (`filters-desktop__item--type-5`) and once with the
    actual filter key (`filters-desktop__item--priceU`). Without skipping
    `type-*` we collapse Бренд / Категория / etc. into a handful of type
    buckets and the dedup pass drops 15 of 16 sections.
    """
    matches = re.findall(r"filters-desktop__(?:item|switch)--([a-zA-Z][a-zA-Z0-9_-]*)", classes)
    for m in matches:
        if not m.startswith("type-"):
            return m
    return matches[0] if matches else None


_RESET_RE = re.compile(r"сбросить$", re.IGNORECASE | re.UNICODE)


def _extract_label(section: Any) -> str:
    """Get the human-readable filter title.

    WB renders title + a "Сбросить" reset button in the same header element,
    so a direct .get_text() returns e.g. "БрендСбросить". Strip the suffix.
    """
    # Try common title selectors
    title_el = section.select_one(
        ".filters-desktop__item-title, .filters-desktop__title, "
        "[class*='title']:not([class*='filter__list']):not(.filter__price)"
    )
    if title_el is None:
        # Fallback: first child that's not the list/input
        for child in section.find_all(recursive=False):
            classes = " ".join(child.get("class") or [])
            if "filter__list" in classes or "input-search" in classes:
                continue
            title_el = child
            break
    if title_el is None:
        return ""
    text = title_el.get_text(strip=True)
    # Strip trailing "Сбросить"
    text = _RESET_RE.sub("", text).strip()
    # Strip trailing pseudo-counters like " (123)"
    text = re.sub(r"\s*\(\d+\)\s*$", "", text).strip()
    return text


def _extract_items(section: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for li in section.select(".filter__list li, .filter__items li, ul li"):
        text = li.get_text(strip=True)
        if not text or text in seen or len(text) > 100:
            continue
        # Skip the "+N" overflow counters
        if re.fullmatch(r"\+?\d+", text):
            continue
        seen.add(text)
        # WB doesn't expose value IDs in the DOM (they're in JSON state we
        # don't reach without intercepting fetch); keep id=None and rely on
        # label-based matcher to align with API-side filters.
        items.append({"name": text, "id": None})
        if len(items) >= 80:
            break
    return items


def _extract_price_range(section: Any) -> tuple[int | None, int | None]:
    inputs = section.select("input")
    nums: list[int] = []
    for inp in inputs:
        for attr in ("value", "placeholder"):
            val = inp.get(attr)
            if val and re.fullmatch(r"\d+", val.replace(" ", "")):
                nums.append(int(val.replace(" ", "")))
                break
    if len(nums) >= 2:
        return min(nums), max(nums)
    if len(nums) == 1:
        return nums[0], None
    return None, None


def _normalize(
    *,
    key: str,
    label: str,
    items: list[dict[str, Any]],
    filter_type: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = {"key": key, "name": label, "items": items}
    if extra:
        raw.update(extra)
    return normalize_filter(
        source="wildberries",
        raw_id=key,
        label=label,
        filter_type=filter_type,
        values=items,
        raw=raw,
    )
