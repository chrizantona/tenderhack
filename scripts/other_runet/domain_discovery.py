from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .http_client import RateLimitedHttpClient, FetchError, now_iso, root_host
from .local_index import DomainRecord, StoreTrust


LOGGER = logging.getLogger("other_runet.discovery")

TYPICAL_FEED_PATHS: tuple[str, ...] = (
    "/yandex.xml",
    "/yandex_market.xml",
    "/market.xml",
    "/feed.xml",
    "/products.xml",
    "/export.xml",
    "/google.xml",
    "/merchant.xml",
    "/catalog_export.xml",
    "/yml.xml",
    "/yml",
    "/sitemap_products.xml",
)

ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
INN_RE = re.compile(r"\bИНН\s*[:#№]?\s*(\d{10}|\d{12})\b", re.I)
OGRN_RE = re.compile(r"\bОГРН(?:ИП)?\s*[:#№]?\s*(\d{13}|\d{15})\b", re.I)
PRICE_RUB_RE = re.compile(r"(\d+[\s ]?\d*\s*(?:₽|руб|р\b))", re.I)
COMMERCE_WORD_RE = re.compile(r"купить|корзин|оформить заказ|в наличии|оплат|доставк|каталог|цена", re.I)
CART_URL_RE = re.compile(r"href=\"[^\"]*?/(cart|basket|checkout)", re.I)
CONTACT_URL_RE = re.compile(r"href=\"[^\"]*?/(contact|contacts|kontakt)", re.I)


def inspect_domain(domain: str, *, client: RateLimitedHttpClient) -> tuple[DomainRecord, StoreTrust]:
    host = root_host(domain)
    record = DomainRecord(domain=host)
    trust = StoreTrust(domain=host)
    homepage_url = f"https://{host}/"
    homepage_html, used_https = _fetch_homepage(client, host)
    record.has_https = used_https
    trust.https = used_https
    if homepage_html:
        signals = _scan_homepage_signals(homepage_html, base_url=homepage_url)
        record.has_json_ld = signals["has_json_ld"]
        if signals["inn"]:
            trust.inn = signals["inn"]
        if signals["ogrn"]:
            trust.ogrn = signals["ogrn"]
        if signals["name"]:
            trust.name = signals["name"]
        trust.contacts_found = signals["contacts"]
        record.notes = (record.notes + " " + signals.get("note", "")).strip()
    else:
        signals = {
            "has_json_ld": False,
            "commerce_word_score": 0.0,
            "price_rub_score": 0.0,
            "cart_signal": 0.0,
            "contacts": False,
            "category_url_signal": 0.0,
            "inn": None,
            "ogrn": None,
            "name": None,
        }

    robots_text = _fetch_robots(client, host)
    sitemap_urls = _parse_sitemaps(robots_text, host) if robots_text else []
    if not sitemap_urls:
        for guess in ("/sitemap.xml", "/sitemap_index.xml"):
            url = f"https://{host}{guess}"
            if _head_ok(client, url):
                sitemap_urls.append(url)
                break
    record.has_sitemap = bool(sitemap_urls)
    record.sitemap_urls = sitemap_urls[:10]

    feed_urls = _probe_feed_urls(client, host)
    record.feed_urls = feed_urls[:10]
    record.has_feed = bool(feed_urls)

    commerce_score = _compute_commerce_score(
        json_ld=record.has_json_ld,
        feed=record.has_feed,
        price_rub=signals["price_rub_score"],
        cart=signals["cart_signal"],
        contacts=trust.contacts_found,
        commerce_word=signals["commerce_word_score"],
        category_url=signals["category_url_signal"],
    )
    record.commerce_score = round(commerce_score, 3)

    trust.trust_score = _compute_trust_score(
        commerce_score=commerce_score,
        https=trust.https,
        contacts=trust.contacts_found,
        inn=trust.inn,
        ogrn=trust.ogrn,
    )
    if not trust.https:
        trust.risk_flags.append("no_https")
    if not trust.contacts_found:
        trust.risk_flags.append("no_contacts")
    if not trust.inn and not trust.ogrn:
        trust.risk_flags.append("missing_legal_info")
    if commerce_score < 0.35:
        trust.risk_flags.append("low_commerce_signals")
    record.last_checked_at = now_iso()
    trust.last_checked_at = now_iso()
    return record, trust


def _fetch_homepage(client: RateLimitedHttpClient, host: str) -> tuple[str | None, bool]:
    for scheme in ("https", "http"):
        url = f"{scheme}://{host}/"
        try:
            response = client.get(url, timeout=12.0, accept_status=(200, 301, 302, 303, 308))
        except FetchError as exc:
            LOGGER.debug("homepage fetch failed for %s: %s", url, exc)
            continue
        if response is None:
            continue
        return response.text, scheme == "https"
    return None, False


def _scan_homepage_signals(html_text: str, *, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(" ", strip=True)
    has_json_ld = bool(soup.find("script", attrs={"type": re.compile(r"ld\+json", re.I)}))
    commerce_hits = len(COMMERCE_WORD_RE.findall(text))
    price_hits = len(PRICE_RUB_RE.findall(text))
    cart_hits = len(CART_URL_RE.findall(html_text))
    contact_hits = len(CONTACT_URL_RE.findall(html_text))
    category_urls = 0
    for anchor in soup.find_all("a", href=True)[:200]:
        path = urlparse(urljoin(base_url, anchor["href"])).path.lower()
        if any(marker in path for marker in ("/catalog/", "/category/", "/categories/")):
            category_urls += 1
    inn_match = INN_RE.search(text)
    ogrn_match = OGRN_RE.search(text)
    name = None
    title_tag = soup.find("title")
    if title_tag:
        name = (title_tag.get_text() or "").strip().split(" | ")[0].strip() or None
    return {
        "has_json_ld": has_json_ld,
        "commerce_word_score": min(1.0, commerce_hits / 6.0),
        "price_rub_score": min(1.0, price_hits / 6.0),
        "cart_signal": min(1.0, cart_hits / 1.0),
        "contacts": contact_hits > 0,
        "category_url_signal": min(1.0, category_urls / 5.0),
        "inn": inn_match.group(1) if inn_match else None,
        "ogrn": ogrn_match.group(1) if ogrn_match else None,
        "name": name,
        "note": "",
    }


def _fetch_robots(client: RateLimitedHttpClient, host: str) -> str | None:
    url = f"https://{host}/robots.txt"
    try:
        response = client.get(url, timeout=10.0, accept_status=(200, 404))
    except FetchError:
        return None
    if response.status_code != 200:
        return None
    return response.text


def _parse_sitemaps(robots_text: str, host: str) -> list[str]:
    sitemaps = []
    for match in ROBOTS_SITEMAP_RE.findall(robots_text):
        url = match.strip()
        if url:
            sitemaps.append(url if url.startswith("http") else urljoin(f"https://{host}/", url))
    return sitemaps


def _probe_feed_urls(client: RateLimitedHttpClient, host: str) -> list[str]:
    found: list[str] = []
    for path in TYPICAL_FEED_PATHS:
        url = f"https://{host}{path}"
        if _head_ok(client, url):
            found.append(url)
        if len(found) >= 4:
            break
    return found


def _head_ok(client: RateLimitedHttpClient, url: str) -> bool:
    try:
        response = client.get(url, timeout=8.0, accept_status=(200,), allow_text_only=False)
    except FetchError:
        return False
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "xml" in content_type or "rss" in content_type or "text" in content_type:
        return True
    return bool(response.content) and len(response.content) > 64


def _compute_commerce_score(
    *,
    json_ld: bool,
    feed: bool,
    price_rub: float,
    cart: float,
    contacts: bool,
    commerce_word: float,
    category_url: float,
) -> float:
    score = 0.0
    score += 0.25 if json_ld else 0.0
    score += 0.25 if feed else 0.0
    score += 0.15 * price_rub
    score += 0.10 * cart
    score += 0.10 * (1.0 if contacts else 0.0)
    score += 0.10 * commerce_word
    score += 0.05 * category_url
    return min(1.0, score)


def _compute_trust_score(
    *,
    commerce_score: float,
    https: bool,
    contacts: bool,
    inn: str | None,
    ogrn: str | None,
) -> float:
    score = 0.0
    score += 0.40 * commerce_score
    score += 0.15 if https else 0.0
    score += 0.15 if contacts else 0.0
    score += 0.15 if inn else 0.0
    score += 0.15 if ogrn else 0.0
    return min(1.0, score)
