from __future__ import annotations

import html
import json
import re
from typing import Any


JSON_LD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

DATA_STATE_RE = re.compile(
    r'<div\s+[^>]*id=["\']state-(?P<wid>[^"\']+)["\'][^>]*data-state=(?P<quote>["\'])(?P<state>.*?)(?P=quote)',
    re.IGNORECASE | re.DOTALL,
)

WIDGET_LINK_RE = re.compile(
    r'id=["\']state-(?P<wid>[^"\']+)["\']',
    re.IGNORECASE,
)


def extract_payloads_from_html(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    payloads: list[dict[str, Any]] = []

    json_ld_items = _extract_json_ld(text)
    if json_ld_items:
        payloads.append({"jsonLd": json_ld_items})

    widget_states = _extract_widget_states(text)
    if widget_states:
        payloads.append({"widgetStates": widget_states})

    return payloads


def has_ozon_html_markers(text: str | None) -> bool:
    if not text:
        return False
    return ("data-widget" in text or "data-state=" in text or "ozone.ru" in text)


def _extract_json_ld(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in JSON_LD_SCRIPT_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(html.unescape(raw))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    items.append(entry)
        elif isinstance(data, dict):
            items.append(data)
    return items


def _extract_widget_states(text: str) -> dict[str, Any]:
    states: dict[str, Any] = {}
    for match in DATA_STATE_RE.finditer(text):
        widget_id = match.group("wid")
        raw = match.group("state")
        decoded = html.unescape(raw)
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if not data:
            continue
        states[widget_id] = data
    return states
