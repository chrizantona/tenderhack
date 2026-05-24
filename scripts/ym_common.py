from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import requests


DEFAULT_REGION_NAME = "RU/Moscow"
DEFAULT_CURRENCY = "RUB"
MARKET_HOST = "https://market.yandex.ru"
DEFAULT_TRANSPORT = "requests"
DEFAULT_CURL_IMPERSONATE = "chrome124"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://market.yandex.ru/",
}

DOCUMENT_HEADERS = {
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

FINGERPRINT_PROFILES: dict[str, dict[str, Any]] = {
    "chrome124_macos": {
        "transport": "curl_cffi",
        "impersonate": "chrome124",
        "headers": {
            **DOCUMENT_HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"macOS"',
        },
    },
    "chrome124_windows": {
        "transport": "curl_cffi",
        "impersonate": "chrome124",
        "headers": {
            **DOCUMENT_HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
    },
    "chrome120_windows": {
        "transport": "curl_cffi",
        "impersonate": "chrome120",
        "headers": {
            **DOCUMENT_HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
    },
    "safari180_macos": {
        "transport": "curl_cffi",
        "impersonate": "safari180",
        "headers": {
            **DOCUMENT_HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.0 Safari/605.1.15"
            ),
        },
    },
}


class YandexMarketHTTPError(RuntimeError):
    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"GET {url} failed: {status_code} {body[:300]}")


class CurlCffiSession:
    def __init__(self, impersonate: str) -> None:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise RuntimeError(
                "curl_cffi transport requested, but curl_cffi is not installed. "
                "Install it or use --transport requests."
            ) from exc

        self._session = curl_requests.Session()
        self.headers = self._session.headers
        self.impersonate = impersonate

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("impersonate", self.impersonate)
        return self._session.get(*args, **kwargs)


def normalize_transport(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"requests", "curl_cffi"}:
        raise ValueError(f"Unsupported Yandex Market transport: {value!r}")
    return normalized


def fingerprint_profile_names() -> tuple[str, ...]:
    return tuple(sorted(FINGERPRINT_PROFILES))


def resolve_fingerprint_config(
    transport: str = DEFAULT_TRANSPORT,
    *,
    impersonate: str = DEFAULT_CURL_IMPERSONATE,
    fingerprint_profile: str | None = None,
) -> dict[str, Any]:
    if not fingerprint_profile:
        return {
            "name": None,
            "transport": normalize_transport(transport),
            "impersonate": impersonate,
            "headers": {},
        }

    try:
        profile = FINGERPRINT_PROFILES[fingerprint_profile]
    except KeyError as exc:
        raise ValueError(f"Unsupported Yandex Market fingerprint profile: {fingerprint_profile!r}") from exc

    return {
        "name": fingerprint_profile,
        "transport": normalize_transport(profile.get("transport", transport)),
        "impersonate": profile.get("impersonate", impersonate),
        "headers": dict(profile.get("headers", {})),
    }


def make_session(
    transport: str = DEFAULT_TRANSPORT,
    *,
    impersonate: str = DEFAULT_CURL_IMPERSONATE,
    fingerprint_profile: str | None = None,
    cookie_header: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    config = resolve_fingerprint_config(
        transport,
        impersonate=impersonate,
        fingerprint_profile=fingerprint_profile,
    )
    if config["transport"] == "curl_cffi":
        session = CurlCffiSession(impersonate=config["impersonate"])
    else:
        session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.headers.update(config["headers"])
    if extra_headers:
        session.headers.update(extra_headers)
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    return session


def cookie_header_from_file(path: str, *, host: str = "market.yandex.ru") -> str:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Cookie file is empty: {path}")

    if text.startswith("{") or text.startswith("["):
        cookies = cookies_from_json(text, host=host)
        if cookies:
            return cookies

    if "\t" in text:
        cookies = cookies_from_netscape(text, host=host)
        if cookies:
            return cookies

    if "=" not in text:
        raise ValueError(f"Cookie file does not look like JSON, Netscape cookies.txt, or raw Cookie header: {path}")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def cookies_from_json(text: str, *, host: str) -> str:
    payload = json.loads(text)
    if isinstance(payload, dict):
        cookies = payload.get("cookies", [])
    elif isinstance(payload, list):
        cookies = payload
    else:
        cookies = []

    parts = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        domain = str(cookie.get("domain") or host)
        if name and value is not None and domain_matches(host, domain):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def cookies_from_netscape(text: str, *, host: str) -> str:
    parts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) < 7:
            continue
        domain, name, value = columns[0], columns[5], columns[6]
        if name and domain_matches(host, domain):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def domain_matches(host: str, domain: str) -> bool:
    domain = domain.lstrip(".")
    return host == domain or host.endswith("." + domain)


def get_text(
    session: Any,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> str:
    response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location", "")
        raise YandexMarketHTTPError(response.status_code, response.url, f"redirect to {location}")
    if response.status_code != 200:
        raise YandexMarketHTTPError(response.status_code, response.url, response_error_summary(response))
    if not response.content:
        raise RuntimeError(f"GET {response.url} returned an empty response")
    return response.text


def fetch_market_page(
    page_url: str,
    *,
    timeout: float = 15,
    session: Any | None = None,
) -> tuple[str, str]:
    session = session or make_session()
    url = absolute_market_url(page_url) or MARKET_HOST
    return get_text(session, url, timeout=timeout), url


def search_page_path(query: str, page: int, *, region_id: int | None = None) -> str:
    params: dict[str, Any] = {"text": query, "page": max(1, page)}
    if region_id is not None:
        params["lr"] = region_id
    query_string = urlencode(params)
    return f"/search?{query_string}"


def product_page_path(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Yandex Market product id or URL is empty")
    if "market.yandex" in value or value.startswith("/"):
        path = market_page_path(value)
        if "/product" in path or "/card/" in path:
            return path
        product_id = parse_market_id(path)
        if product_id:
            return f"/product/{product_id}"
        raise ValueError(f"Cannot find Yandex Market product path in: {value!r}")
    return f"/product/{parse_market_id(value)}"


def parse_market_id(value: str) -> str:
    for pattern in (
        r"/product--[^/?#]+/(\d+)",
        r"/product/(\d+)",
        r"/card/[^/?#]+/(\d+)",
        r"[?&](?:modelid|modelId|sku|skuId)=(\d+)",
        r"\b(\d{5,})\b",
    ):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise ValueError(f"Cannot find Yandex Market product/model id in: {value!r}")


def market_page_path(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return path
    if not value.startswith("/"):
        return "/" + value
    return value


def absolute_market_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value
    if value.startswith("//"):
        return "https:" + value
    if not value.startswith("/"):
        value = "/" + value
    return MARKET_HOST + value


def add_query_params(path: str, **params: str) -> str:
    parsed = urlparse(path)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(("", "", parsed.path, "", urlencode(query), ""))


def response_error_summary(response: Any) -> str:
    text = response.content[:2000].decode("utf-8", errors="replace").replace("\n", " ").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if message:
                return str(message)[:300]
    normalized = text.lower()
    if "captcha" in normalized or "showcaptcha" in normalized:
        return "Yandex captcha/challenge page"
    if "похоже" in normalized and "vpn" in normalized:
        return "Yandex VPN/region block page"
    if "доступ ограничен" in normalized:
        return "Yandex access restricted page"
    return text[:300]


def unique(values: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
