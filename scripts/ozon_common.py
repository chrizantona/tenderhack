from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


DEFAULT_REGION_NAME = "RU/Moscow"
DEFAULT_CURRENCY = "RUB"

COMPOSER_ENDPOINTS = (
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    "https://api.ozon.ru/composer-api.bx/page/json/v2",
    "https://www.ozon.ru/api/composer-api.bx/page/json/v1",
    "https://api.ozon.ru/composer-api.bx/page/json/v1",
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.ozon.ru",
    "Referer": "https://www.ozon.ru/",
    "x-o3-app-name": "dweb_client",
    "x-o3-app-version": "release_20-05-2026",
    "x-o3-device-type": "desktop",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


class OzonHTTPError(RuntimeError):
    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"GET {url} failed: {status_code} {body[:300]}")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> Any:
    response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location", "")
        body = (
            f"redirect to {location}. Ozon often returns repeated 307 redirects "
            "from blocked, non-RU, or not-browser-like clients."
        )
        raise OzonHTTPError(response.status_code, response.url, body)
    if response.status_code != 200:
        raise OzonHTTPError(response.status_code, response.url, response_error_summary(response))
    if not response.content:
        raise RuntimeError(f"GET {response.url} returned an empty response")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"GET {response.url} returned non-JSON response: {response_error_summary(response)}") from exc


def fetch_composer_page(
    page_url: str,
    *,
    timeout: float = 15,
    session: requests.Session | None = None,
) -> tuple[dict[str, Any], str]:
    session = session or make_session()
    normalized_url = ozon_page_path(page_url)
    errors: list[str] = []
    for endpoint in COMPOSER_ENDPOINTS:
        try:
            payload = get_json(session, endpoint, params={"url": normalized_url}, timeout=timeout)
            return payload, endpoint
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("All Ozon composer endpoints failed:\n" + "\n".join(errors))


def parse_ozon_id(value: str) -> str:
    product_id = extract_id_from_url(value) or re.sub(r"\D+", "", value)
    if not product_id:
        raise ValueError(f"Cannot find Ozon product id in: {value!r}")
    return product_id


def search_page_path(query: str, page: int) -> str:
    # Keep the inner Ozon URL readable. requests will encode it exactly once as
    # composer-api's `url` parameter.
    return f"/search/?text={query}&from_global=true&page={page}"


def product_page_path(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Ozon product id or URL is empty")
    if "ozon.ru" in value or value.startswith("/"):
        path = ozon_page_path(value)
        if "/product/" not in path:
            product_id = extract_id_from_url(path)
            if product_id:
                return f"/product/{product_id}/"
            raise ValueError(f"Cannot find Ozon product path in: {value!r}")
        return ensure_trailing_slash_without_query(path)
    return f"/product/{parse_ozon_id(value)}/"


def product_layout_urls(product_url: str, include_extra_layouts: bool) -> list[str]:
    urls = [product_url]
    if include_extra_layouts:
        urls.append(add_query_params(product_url, layout_container="pdppage2copy", layout_page_index="2"))
        urls.append(add_query_params(product_url, layout_container="appPdpPage3", layout_page_index="3"))
    return unique(urls)


def ozon_page_path(value: str | None) -> str:
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


def absolute_ozon_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value
    if value.startswith("//"):
        return "https:" + value
    if not value.startswith("/"):
        value = "/" + value
    return "https://www.ozon.ru" + value


def ensure_trailing_slash_without_query(path: str) -> str:
    parsed = urlparse(path)
    clean_path = parsed.path
    if not clean_path.endswith("/"):
        clean_path += "/"
    if parsed.query:
        return clean_path + "?" + parsed.query
    return clean_path


def add_query_params(path: str, **params: str) -> str:
    parsed = urlparse(path)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(("", "", parsed.path, "", urlencode(query), ""))


def extract_id_from_url(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"/product/[^/?#]*?(\d{5,})(?:[/?#]|$)", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{5,})\b", value)
    if match:
        return match.group(1)
    return None


def response_error_summary(response: requests.Response) -> str:
    text = response.text[:1000].replace("\n", " ").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            incident_id = payload.get("incidentId") or payload.get("incident_id")
            if incident_id:
                return f"Ozon challenge or block response, incidentId={incident_id}"
            message = payload.get("message") or payload.get("error")
            if message:
                return str(message)[:300]
    if "abt-challenge" in text or "challenge" in text.lower():
        return "Ozon challenge page"
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
