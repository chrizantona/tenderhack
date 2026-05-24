from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import RequestException as CffiRequestException
    CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
    cffi_requests = None
    CffiRequestException = Exception
    CFFI_AVAILABLE = False

import requests as _stdlib_requests


LOGGER = logging.getLogger("other_runet.http")

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

IMPERSONATE_PROFILE = os.environ.get("OTHER_RUNET_IMPERSONATE", "chrome124")


def root_host(url_or_host: str) -> str:
    if not url_or_host:
        return ""
    text = url_or_host.strip().lower()
    if "://" not in text and "/" not in text:
        host = text
    else:
        parsed = urlparse(text if "://" in text else "http://" + text)
        host = parsed.hostname or ""
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = "") -> None:
        super().__init__(f"GET {url} failed: {status} {body[:200]}")
        self.url = url
        self.status = status
        self.body = body


@dataclass
class _Response:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]
    url: str


def load_proxy_pool() -> list[str]:
    if (os.environ.get("OTHER_RUNET_PROXY_DISABLE") or "").lower() in ("1", "true", "yes"):
        return []
    raw = (os.environ.get("OTHER_RUNET_PROXIES") or os.environ.get("OZON_PROXIES") or "").strip()
    if not raw or raw.lower() in ("none", "off", "disable", "disabled"):
        return []
    user = (os.environ.get("OZON_PROXY_USER") or "").strip()
    password = (os.environ.get("OZON_PROXY_PASS") or "").strip()
    scheme = (os.environ.get("OZON_PROXY_SCHEME") or "http").strip() or "http"
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    proxies: list[str] = []
    for token in tokens:
        if "://" in token:
            proxies.append(token)
            continue
        if user and password:
            proxies.append(f"{scheme}://{user}:{password}@{token}")
        else:
            proxies.append(f"{scheme}://{token}")
    return proxies


class RateLimitedHttpClient:
    def __init__(
        self,
        *,
        timeout: float = 12.0,
        min_domain_delay: float = 0.6,
        max_retries: int = 1,
        cookies: dict[str, dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        proxy_pool: list[str] | None = None,
        impersonate: str = IMPERSONATE_PROFILE,
    ) -> None:
        self.timeout = timeout
        self.min_domain_delay = min_domain_delay
        self.max_retries = max_retries
        self.impersonate = impersonate
        self.default_headers = dict(headers or DEFAULT_HEADERS)
        self._cookies_by_host = cookies or {}
        self._last_request_by_domain: dict[str, float] = {}
        self._lock = threading.Lock()
        self._proxy_pool = proxy_pool if proxy_pool is not None else load_proxy_pool()
        self._proxy_index = 0
        self._proxy_lock = threading.Lock()
        if CFFI_AVAILABLE:
            self.session = cffi_requests.Session(impersonate=impersonate)
        else:  # pragma: no cover
            LOGGER.warning("curl_cffi not available; falling back to requests")
            self.session = _stdlib_requests.Session()
        self.session.headers.update(self.default_headers)

    def set_cookies_for(self, host: str, cookies: dict[str, str]) -> None:
        normalized = root_host(host)
        if not normalized:
            return
        self._cookies_by_host[normalized] = cookies

    def _wait_domain(self, domain: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last_request_by_domain.get(domain, 0.0)
            sleep_for = self.min_domain_delay - (now - last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last_request_by_domain[domain] = time.monotonic()

    def _next_proxy(self) -> str | None:
        if not self._proxy_pool:
            return None
        with self._proxy_lock:
            proxy = self._proxy_pool[self._proxy_index % len(self._proxy_pool)]
            self._proxy_index += 1
        return proxy

    def get(
        self,
        url: str,
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        accept_status: tuple[int, ...] = (200,),
        allow_text_only: bool = True,
        use_proxy: bool | None = None,
    ) -> _Response:
        domain = root_host(url)
        self._wait_domain(domain)
        request_headers = dict(self.default_headers)
        if headers:
            request_headers.update(headers)
        cookies = self._cookies_by_host.get(domain)
        proxies = None
        if use_proxy is not False and self._proxy_pool:
            proxy_url = self._next_proxy()
            if proxy_url:
                proxies = {"http": proxy_url, "https": proxy_url}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._do_get(
                    url,
                    timeout=timeout or self.timeout,
                    headers=request_headers,
                    cookies=cookies,
                    proxies=proxies,
                )
            except (CffiRequestException, _stdlib_requests.RequestException) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise FetchError(url, status=0, body=str(exc)) from exc
                time.sleep(0.3)
                continue
            if response.status_code not in accept_status:
                if attempt < self.max_retries and response.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(0.5)
                    continue
                raise FetchError(url, status=response.status_code, body=response.text[:200] if response.text else "")
            if allow_text_only and not response.text and not response.content:
                raise FetchError(url, status=response.status_code, body="empty body")
            return response
        if last_exc:
            raise FetchError(url, status=0, body=str(last_exc)) from last_exc
        raise FetchError(url, status=0, body="unknown")

    def _do_get(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str],
        cookies: dict[str, str] | None,
        proxies: dict[str, str] | None,
    ) -> _Response:
        if CFFI_AVAILABLE:
            response = self.session.get(
                url,
                timeout=timeout,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                allow_redirects=True,
                impersonate=self.impersonate,
            )
        else:  # pragma: no cover
            response = self.session.get(
                url,
                timeout=timeout,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                allow_redirects=True,
            )
        return _Response(
            status_code=int(response.status_code),
            text=response.text if isinstance(response.text, str) else "",
            content=response.content or b"",
            headers={str(key).lower(): str(value) for key, value in dict(response.headers).items()},
            url=str(response.url),
        )

    def get_text(self, url: str, *, timeout: float | None = None, headers: dict[str, str] | None = None, use_proxy: bool | None = None) -> str:
        response = self.get(url, timeout=timeout, headers=headers, use_proxy=use_proxy)
        return response.text

    def get_bytes(self, url: str, *, timeout: float | None = None, headers: dict[str, str] | None = None, use_proxy: bool | None = None) -> bytes:
        response = self.get(url, timeout=timeout, headers=headers, allow_text_only=False, use_proxy=use_proxy)
        return response.content


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
