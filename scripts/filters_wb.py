from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from filters_common import normalize_filter


WB_FILTERS_ENDPOINTS = (
    "https://u-search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
)

WB_DEFAULT_DEST = -1257786


def fetch_wildberries_filters(
    query: str,
    *,
    dest: int = WB_DEFAULT_DEST,
    timeout: float = 15.0,
    wb_client: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull WB filters via the shared WildberriesClient when provided.

    The shared client already has curl_cffi chrome120 impersonation, the
    Sec-CH-UA / Sec-Fetch-* headers, warmup cookies, and proxy rotation —
    everything needed to dodge the 403/429 cascade that plain urllib hits.
    Falls back to a bare urllib call only when no client is supplied (CLI use).
    """
    params = {
        "ab_testid": "fttp",
        "appType": "1",
        "curr": "rub",
        "dest": str(dest),
        "lang": "ru",
        "query": query,
        "resultset": "filters",
        "spp": "30",
        "suppressSpellcheck": "false",
    }
    last_error: str | None = None

    if wb_client is not None:
        for endpoint in WB_FILTERS_ENDPOINTS:
            try:
                data = wb_client.get_json(endpoint, params=params, endpoint_kind="search", timeout=timeout)
            except Exception as exc:
                last_error = f"{endpoint}: {exc}"
                continue
            filters_raw = (
                data.get("filters")
                or (data.get("data") or {}).get("filters")
                or []
            )
            filters = [_normalize_wb_filter(entry) for entry in filters_raw]
            filters = [f for f in filters if f["label"]]
            meta = {
                "endpoint": endpoint,
                "raw_count": len(filters_raw),
                "kept_count": len(filters),
            }
            return filters, meta
        return [], {"error": last_error or "no_endpoints_worked"}

    # Standalone CLI fallback (no shared client): plain urllib. Likely to
    # be 403/429-blocked but kept for `python3 scripts/filters_wb.py X`.
    headers = {
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    for endpoint in WB_FILTERS_ENDPOINTS:
        url = endpoint + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    last_error = f"{endpoint}: HTTP {response.status}"
                    continue
                raw = response.read()
        except Exception as exc:
            last_error = f"{endpoint}: {exc!r}"
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"{endpoint}: invalid JSON ({exc})"
            continue
        filters_raw = (
            data.get("filters")
            or (data.get("data") or {}).get("filters")
            or []
        )
        filters = [_normalize_wb_filter(entry) for entry in filters_raw]
        filters = [f for f in filters if f["label"]]
        meta = {
            "endpoint": endpoint,
            "raw_count": len(filters_raw),
            "kept_count": len(filters),
        }
        return filters, meta
    return [], {"error": last_error or "no_endpoints_worked"}


def _normalize_wb_filter(entry: dict[str, Any]) -> dict[str, Any]:
    label = entry.get("name")
    items = entry.get("items") or []
    values = [{"name": item.get("name"), "id": item.get("id")} for item in items if isinstance(item, dict)]
    ftype = entry.get("filterType")
    if not ftype:
        if entry.get("min") is not None or entry.get("max") is not None:
            ftype = "range"
        elif items:
            ftype = "enum"
        else:
            ftype = "boolean"
    return normalize_filter(
        source="wildberries",
        raw_id=entry.get("key"),
        label=label,
        filter_type=ftype,
        values=values,
        raw=entry,
    )


if __name__ == "__main__":
    import sys

    query = sys.argv[1] if len(sys.argv) > 1 else "холодильник"
    filters, meta = fetch_wildberries_filters(query)
    print(json.dumps({"meta": meta, "count": len(filters), "filters": filters}, ensure_ascii=False, indent=2))
