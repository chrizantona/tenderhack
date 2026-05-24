from __future__ import annotations

from urllib.parse import urlparse


CORE_EXCLUDED_SUFFIXES: tuple[str, ...] = (
    "ozon.ru",
    "wildberries.ru",
    "wb.ru",
    "market.yandex.ru",
    "pokupki.market.yandex.ru",
    "market-delivery.yandex.ru",
)

EXTRA_EXCLUDED_SUFFIXES: tuple[str, ...] = (
    "aliexpress.ru",
    "aliexpress.com",
    "megamarket.ru",
    "sbermegamarket.ru",
    "avito.ru",
    "youla.ru",
)

ALL_EXCLUDED_SUFFIXES: tuple[str, ...] = CORE_EXCLUDED_SUFFIXES + EXTRA_EXCLUDED_SUFFIXES


def normalize_host(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text and "/" not in text:
        host = text
    else:
        parsed = urlparse(text if "://" in text else "http://" + text)
        host = parsed.hostname or ""
    host = host.split("@")[-1]
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches_suffix(host: str, suffix: str) -> bool:
    if not host or not suffix:
        return False
    host = host.lower()
    suffix = suffix.lower()
    return host == suffix or host.endswith("." + suffix)


def is_excluded_marketplace(url_or_host: str, *, extra: bool = True) -> bool:
    host = normalize_host(url_or_host)
    if not host:
        return False
    suffixes = ALL_EXCLUDED_SUFFIXES if extra else CORE_EXCLUDED_SUFFIXES
    return any(host_matches_suffix(host, suffix) for suffix in suffixes)


def excluded_reason(url_or_host: str, *, extra: bool = True) -> str | None:
    host = normalize_host(url_or_host)
    if not host:
        return None
    suffixes = ALL_EXCLUDED_SUFFIXES if extra else CORE_EXCLUDED_SUFFIXES
    for suffix in suffixes:
        if host_matches_suffix(host, suffix):
            return suffix
    return None
