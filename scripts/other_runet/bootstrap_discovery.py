from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .http_client import FetchError, RateLimitedHttpClient, root_host
from .marketplace_exclusion import is_excluded_marketplace
from .query_swarm import SwarmQuery, build_discovery_query


LOGGER = logging.getLogger("other_runet.bootstrap")


BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


@dataclass
class SearchEndpoint:
    name: str
    base_url: str
    method: str = "GET"
    query_param: str = "q"
    referrer: str | None = None
    result_selectors: tuple[str, ...] = ("a.result__a", "a[data-testid='result-title-a']", "a.result-link", "h3.result-title a")


DEFAULT_ENDPOINTS: tuple[SearchEndpoint, ...] = (
    SearchEndpoint(
        name="yandex",
        base_url="https://yandex.ru/search/",
        query_param="text",
        referrer="https://yandex.ru/",
        result_selectors=(
            "a.OrganicTitle-Link",
            "a.organic__url",
            "h2.OrganicTitle a",
            "li.serp-item a.link",
            "a[href^='http']",
        ),
    ),
    SearchEndpoint(
        name="google",
        base_url="https://www.google.com/search",
        referrer="https://www.google.com/",
        result_selectors=(
            "div.yuRUbf > a",
            "h3.LC20lb",
            "a[jsname='UWckNb']",
            "a.l",
            "a[href^='/url?q=']",
        ),
    ),
    SearchEndpoint(
        name="bing",
        base_url="https://www.bing.com/search",
        referrer="https://www.bing.com/",
        result_selectors=(
            "li.b_algo h2 a",
            "div.b_title a",
            "h2 a[href^='http']",
        ),
    ),
    SearchEndpoint(
        name="startpage",
        base_url="https://www.startpage.com/sp/search",
        query_param="query",
        referrer="https://www.startpage.com/",
        result_selectors=(
            "a.w-gl__result-title",
            "a.result-title",
            "h3.w-gl__result-title a",
            "section.w-gl a[href^='http']",
        ),
    ),
    SearchEndpoint(
        name="searx_tiekoetter",
        base_url="https://searx.tiekoetter.com/search",
        result_selectors=(
            "article.result h3 a",
            "article.result a.url_wrapper",
            "h3 > a",
        ),
    ),
)


def get_default_endpoints() -> tuple[SearchEndpoint, ...]:
    override = (os.environ.get("OTHER_RUNET_SE_OVERRIDE") or "").strip()
    if not override:
        return DEFAULT_ENDPOINTS
    selected = {token.strip() for token in override.split(",") if token.strip()}
    filtered = tuple(endpoint for endpoint in DEFAULT_ENDPOINTS if endpoint.name in selected)
    return filtered or DEFAULT_ENDPOINTS


def discover_candidates(
    swarm: Iterable[SwarmQuery],
    *,
    client: RateLimitedHttpClient,
    max_per_query: int = 12,
    max_total: int = 40,
    diagnostics: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    raw_query = ""
    for item in swarm:
        if item.intent in {"raw", "canonical"} and item.text:
            raw_query = item.text
            break
    if not raw_query:
        return []
    return discover_for_query(
        raw_query,
        client=client,
        max_per_query=max_per_query,
        max_total=max_total,
        diagnostics=diagnostics,
    )


def discover_for_query(
    query: str,
    *,
    client: RateLimitedHttpClient,
    max_per_query: int = 12,
    max_total: int = 30,
    diagnostics: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    endpoints = get_default_endpoints()
    results: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    excluded_count = 0
    clean_query = " ".join((query or "").split()).strip()
    if not clean_query:
        return []
    for endpoint in endpoints:
        if len(results) >= max_total:
            break
        try:
            html_text = _fetch_endpoint(client, endpoint, clean_query)
        except FetchError as exc:
            if diagnostics is not None:
                diagnostics.append({
                    "stage": "discovery_request",
                    "endpoint": endpoint.name,
                    "query": clean_query,
                    "error": str(exc),
                })
            continue
        extracted = _extract_links(html_text, endpoint=endpoint, limit=max_per_query * 3)
        added_from_endpoint = 0
        for link in extracted:
            if len(results) >= max_total:
                break
            normalized = _strip_tracking(link)
            if not normalized or normalized in seen_urls:
                continue
            if is_excluded_marketplace(normalized):
                excluded_count += 1
                continue
            domain = root_host(normalized)
            if not domain or domain in seen_domains:
                continue
            seen_urls.add(normalized)
            seen_domains.add(domain)
            added_from_endpoint += 1
            results.append({
                "url": normalized,
                "domain": domain,
                "discovered_via": endpoint.name,
                "swarm_query": clean_query,
                "intent": "user_query",
            })
            if added_from_endpoint >= max_per_query:
                break
    if diagnostics is not None and excluded_count:
        diagnostics.append({"stage": "discovery", "message": f"excluded_marketplace_links={excluded_count}"})
    return results[:max_total]


def _fetch_endpoint(client: RateLimitedHttpClient, endpoint: SearchEndpoint, query: str) -> str:
    encoded = quote_plus(query)
    url = f"{endpoint.base_url}?{endpoint.query_param}={encoded}"
    headers = dict(BROWSER_HEADERS)
    if endpoint.referrer:
        headers["Referer"] = endpoint.referrer
    response = client.get(
        url,
        timeout=12.0,
        headers=headers,
        accept_status=(200, 202, 301, 302),
    )
    if response.status_code != 200:
        raise FetchError(url, status=response.status_code, body=response.text[:200] if response.text else "")
    return response.text or ""


def _extract_links(html_text: str, *, endpoint: SearchEndpoint, limit: int) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    candidates: list[str] = []
    seen_local: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        absolute = urljoin(endpoint.base_url, href)
        cleaned = _strip_tracking(absolute)
        if not cleaned or cleaned in seen_local:
            continue
        if not cleaned.startswith(("http://", "https://")):
            continue
        if _is_search_engine_internal_link(cleaned):
            continue
        if _looks_service_link(cleaned):
            continue
        seen_local.add(cleaned)
        candidates.append(cleaned)
        if len(candidates) >= limit:
            break
    return candidates


_SERVICE_PATH_HINTS: tuple[str, ...] = (
    "/help", "/about", "/policies", "/preferences", "/login", "/signup",
    "/auth", "/contact", "/legal", "/terms", "/privacy", "/imghp",
    "/captcha", "/tools", "/advanced_search", "/intl/", "/maps",
    "/settings", "/account",
)

_SERVICE_HOST_PREFIXES: tuple[str, ...] = (
    "support.", "help.", "policies.", "passport.", "company.",
    "images.", "image.", "video.", "videos.", "news.", "maps.",
    "translate.", "scholar.", "books.", "shopping.", "search.",
)


def _looks_service_link(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if any(host.startswith(prefix) for prefix in _SERVICE_HOST_PREFIXES):
        return True
    path = parsed.path.lower()
    if any(path.startswith(hint) for hint in _SERVICE_PATH_HINTS):
        return True
    return False


def _strip_tracking(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)
    if "duckduckgo.com" in host:
        uddg = query.get("uddg")
        if uddg:
            return unquote(uddg[0])
    if "google.com" in host and parsed.path.startswith("/url"):
        target = query.get("q") or query.get("url")
        if target:
            return target[0]
    if "yandex.ru" in host and (parsed.path.startswith("/clck") or parsed.path.startswith("/redir")):
        target = query.get("url") or query.get("u")
        if target:
            return _maybe_b64_url(target[0])
        return None
    if "bing.com" in host and (parsed.path.startswith("/ck/") or parsed.path == "/aclk"):
        for key in ("u", "url"):
            target = query.get(key)
            if target:
                return _maybe_b64_url(target[0])
        return None
    if "r.search.yahoo.com" in host:
        for key in ("RU", "RV", "url"):
            target = query.get(key)
            if target:
                return unquote(target[0])
        return None
    return parsed._replace(fragment="").geturl()


def _maybe_b64_url(value: str) -> str:
    decoded = unquote(value)
    if decoded.startswith(("http://", "https://")):
        return decoded
    import base64

    try:
        padded = decoded + "=" * (-len(decoded) % 4)
        candidate = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        return decoded
    if candidate.startswith(("http://", "https://")):
        return candidate
    return decoded


_DOMAIN_BLACKLIST: frozenset[str] = frozenset({
    "duckduckgo.com",
    "html.duckduckgo.com",
    "searx.be",
    "searx.tiekoetter.com",
    "search.zackptg5.com",
    "search.brave.com",
    "brave.com",
    "mojeek.com",
    "google.com",
    "bing.com",
    "yandex.ru",
    "yandex.com",
    "yabs.yandex.ru",
    "an.yandex.ru",
    "yastatic.net",
    "yastat.net",
    "ya.ru",
    "search.yahoo.com",
    "r.search.yahoo.com",
    "startpage.com",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "stackoverflow.com",
    "stackexchange.com",
    "youtube.com",
    "rutube.ru",
    "vk.com",
    "ok.ru",
    "t.me",
    "telegram.org",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "habr.com",
    "vc.ru",
    "dzen.ru",
    "wikipedia.org",
    "ru.wikipedia.org",
    "pinterest.com",
    "tilda.ws",
    "tinkoff.ru",
    "sberbank.ru",
})


_BLACKLIST_SUFFIXES: tuple[str, ...] = (
    "yandex.ru",
    "yandex.com",
    "yandex.net",
    "google.com",
    "google.ru",
    "bing.com",
    "yahoo.com",
    "yahoo.net",
    "startpage.com",
    "duckduckgo.com",
    "brave.com",
    "mojeek.com",
    "searx.space",
    "searx.be",
    "searx.tiekoetter.com",
    "tiekoetter.com",
    "tiekoetter.cloud",
    "search.zackptg5.com",
    "zackptg5.com",
    "startmail.com",
)


def _is_search_engine_internal_link(url: str) -> bool:
    host = root_host(url)
    if not host:
        return True
    raw_host = urlparse(url).netloc.lower()
    if raw_host.startswith("www."):
        raw_host = raw_host[4:]
    for suffix in _BLACKLIST_SUFFIXES:
        if host == suffix or raw_host == suffix or raw_host.endswith("." + suffix):
            return True
    if host in _DOMAIN_BLACKLIST:
        return True
    if re.search(r"\.(searx|brave|duckduckgo|mojeek)\.", raw_host):
        return True
    if host.endswith(".gov.ru") or host.endswith(".edu.ru"):
        return True
    return False
