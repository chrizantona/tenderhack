from __future__ import annotations

import copy
import threading
import time
from typing import Any

import requests


WB_GEO_URL = "https://user-geo-data.wildberries.ru/get-geo-info"
WB_GEO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


class _TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._items[key]
                return None
            return copy.deepcopy(value)

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + self.ttl_seconds, copy.deepcopy(value))


_wb_geo_cache = _TTLCache(ttl_seconds=24 * 60 * 60)


def _round_key(value: float) -> float:
    return round(float(value), 3)


def resolve_wb_dest(latitude: float, longitude: float, *, timeout: float = 5.0) -> str | None:
    if latitude is None or longitude is None:
        return None
    key = (_round_key(latitude), _round_key(longitude))
    cached = _wb_geo_cache.get(key)
    if cached is not None:
        return cached or None

    params = {
        "currency": "RUB",
        "latitude": str(latitude),
        "longitude": str(longitude),
        "locale": "ru",
        "address": "",
    }
    try:
        response = requests.get(WB_GEO_URL, params=params, headers=WB_GEO_HEADERS, timeout=timeout)
    except requests.RequestException:
        _wb_geo_cache.set(key, "")
        return None
    if response.status_code != 200:
        _wb_geo_cache.set(key, "")
        return None
    try:
        data = response.json()
    except ValueError:
        _wb_geo_cache.set(key, "")
        return None

    dest = _extract_dest_from_xinfo(data.get("xinfo"))
    if not dest:
        destinations = data.get("destinations") or []
        if isinstance(destinations, list) and destinations:
            dest = str(destinations[-1])
    if dest:
        _wb_geo_cache.set(key, dest)
        return dest
    _wb_geo_cache.set(key, "")
    return None


def _extract_dest_from_xinfo(xinfo: Any) -> str | None:
    if not isinstance(xinfo, str):
        return None
    for part in xinfo.split("&"):
        if part.startswith("dest="):
            value = part[len("dest=") :].strip()
            return value or None
    return None
