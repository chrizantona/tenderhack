from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .http_client import now_iso, root_host
from .marketplace_exclusion import is_excluded_marketplace


DEFAULT_DATA_DIR = Path(os.environ.get("OTHER_RUNET_DATA_DIR", "data/other_runet"))


DOMAINS_FILE = "discovered_domains.jsonl"
PRODUCTS_FILE = "indexed_products.jsonl"
TRUST_FILE = "store_trust.jsonl"
RECIPES_FILE = "parser_recipes.jsonl"


_TOKEN_RE = re.compile(r"[a-zа-я0-9]+", re.I)


def tokenize(value: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(value or "")]


@dataclass
class IndexedProduct:
    url: str
    domain: str
    title: str
    brand: str | None = None
    model: str | None = None
    category: str | None = None
    price: float | None = None
    currency: str | None = None
    availability: str | None = None
    sku: str | None = None
    mpn: str | None = None
    gtin: str | None = None
    vendor_code: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    source_type: str = "html"
    commerce_score: float = 0.0
    product_url_score: float = 0.0
    last_seen_at: str = field(default_factory=now_iso)
    last_verified_at: str = field(default_factory=now_iso)
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "brand": self.brand,
            "model": self.model,
            "category": self.category,
            "price": self.price,
            "currency": self.currency,
            "availability": self.availability,
            "sku": self.sku,
            "mpn": self.mpn,
            "gtin": self.gtin,
            "vendor_code": self.vendor_code,
            "attributes": self.attributes,
            "source_type": self.source_type,
            "commerce_score": round(self.commerce_score, 3),
            "product_url_score": round(self.product_url_score, 3),
            "last_seen_at": self.last_seen_at,
            "last_verified_at": self.last_verified_at,
            "content_hash": self.content_hash,
        }


@dataclass
class DomainRecord:
    domain: str
    commerce_score: float = 0.0
    has_feed: bool = False
    has_json_ld: bool = False
    has_sitemap: bool = False
    has_https: bool = True
    feed_urls: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    last_checked_at: str = field(default_factory=now_iso)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "commerce_score": round(self.commerce_score, 3),
            "has_feed": self.has_feed,
            "has_json_ld": self.has_json_ld,
            "has_sitemap": self.has_sitemap,
            "has_https": self.has_https,
            "feed_urls": list(self.feed_urls),
            "sitemap_urls": list(self.sitemap_urls),
            "last_checked_at": self.last_checked_at,
            "notes": self.notes,
        }


@dataclass
class StoreTrust:
    domain: str
    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    contacts_found: bool = False
    https: bool = True
    trust_score: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    last_checked_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "name": self.name,
            "inn": self.inn,
            "ogrn": self.ogrn,
            "contacts_found": self.contacts_found,
            "https": self.https,
            "trust_score": round(self.trust_score, 3),
            "risk_flags": list(self.risk_flags),
            "last_checked_at": self.last_checked_at,
        }


@dataclass
class ParserRecipe:
    domain: str
    strategy: str
    selectors: dict[str, str | None] = field(default_factory=dict)
    json_paths: dict[str, str | None] = field(default_factory=dict)
    requires_city: bool = False
    city_strategy: dict[str, Any] | None = None
    notes: str = ""
    confidence: float = 0.0
    created_at: str = field(default_factory=now_iso)
    last_validated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "strategy": self.strategy,
            "selectors": dict(self.selectors),
            "json_paths": dict(self.json_paths),
            "requires_city": self.requires_city,
            "city_strategy": self.city_strategy,
            "notes": self.notes,
            "confidence": round(self.confidence, 3),
            "created_at": self.created_at,
            "last_validated_at": self.last_validated_at,
        }


class JsonLines:
    def __init__(self, path: Path, key_field: str) -> None:
        self.path = path
        self.key_field = key_field
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as file:
                    for raw_line in file:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        key = record.get(self.key_field)
                        if key:
                            self._records[str(key)] = record
            self._loaded = True

    def upsert(self, record: dict[str, Any]) -> None:
        self._ensure_loaded()
        key = record.get(self.key_field)
        if not key:
            return
        with self._lock:
            self._records[str(key)] = record
            self._write_all_locked()

    def get(self, key: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        with self._lock:
            value = self._records.get(str(key))
            return dict(value) if value else None

    def all(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        with self._lock:
            return [dict(item) for item in self._records.values()]

    def keys(self) -> list[str]:
        self._ensure_loaded()
        with self._lock:
            return list(self._records.keys())

    def _write_all_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            for record in self._records.values():
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.path)


class LocalIndex:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.products = JsonLines(self.data_dir / PRODUCTS_FILE, key_field="url")
        self.domains = JsonLines(self.data_dir / DOMAINS_FILE, key_field="domain")
        self.trust = JsonLines(self.data_dir / TRUST_FILE, key_field="domain")
        self.recipes = JsonLines(self.data_dir / RECIPES_FILE, key_field="domain")

    def upsert_product(self, product: IndexedProduct) -> None:
        if is_excluded_marketplace(product.url):
            return
        self.products.upsert(product.to_dict())

    def upsert_domain(self, record: DomainRecord) -> None:
        if is_excluded_marketplace(record.domain):
            return
        self.domains.upsert(record.to_dict())

    def upsert_trust(self, trust: StoreTrust) -> None:
        if is_excluded_marketplace(trust.domain):
            return
        self.trust.upsert(trust.to_dict())

    def upsert_recipe(self, recipe: ParserRecipe) -> None:
        if is_excluded_marketplace(recipe.domain):
            return
        self.recipes.upsert(recipe.to_dict())

    def get_domain(self, domain: str) -> dict[str, Any] | None:
        return self.domains.get(root_host(domain))

    def get_recipe(self, domain: str) -> dict[str, Any] | None:
        return self.recipes.get(root_host(domain))

    def get_trust(self, domain: str) -> dict[str, Any] | None:
        return self.trust.get(root_host(domain))

    def all_domains(self) -> list[dict[str, Any]]:
        return self.domains.all()

    def all_products(self) -> list[dict[str, Any]]:
        return self.products.all()

    def search_products(
        self,
        *,
        tokens: Iterable[str],
        identifiers: Iterable[str] = (),
        category: str | None = None,
        limit: int = 50,
    ) -> list[tuple[float, dict[str, Any]]]:
        query_tokens = {token.lower() for token in tokens if token}
        identifier_set = {value.lower() for value in identifiers if value}
        scored: list[tuple[float, dict[str, Any]]] = []
        for product in self.products.all():
            if is_excluded_marketplace(product.get("url", "")):
                continue
            score = 0.0
            haystack_parts = [
                product.get("title") or "",
                product.get("brand") or "",
                product.get("model") or "",
                product.get("vendor_code") or "",
                product.get("mpn") or "",
                product.get("sku") or "",
                product.get("gtin") or "",
            ]
            haystack_tokens = set()
            for part in haystack_parts:
                haystack_tokens.update(tokenize(part))
            overlap = len(query_tokens & haystack_tokens)
            score += overlap * 1.0
            if identifier_set:
                for identifier in identifier_set:
                    if identifier in haystack_tokens or identifier in (product.get("mpn") or "").lower() or identifier in (product.get("sku") or "").lower() or identifier in (product.get("gtin") or "").lower():
                        score += 5.0
            if category and product.get("category") == category:
                score += 0.5
            score += min(1.5, float(product.get("commerce_score") or 0))
            score += min(1.0, float(product.get("product_url_score") or 0))
            freshness_factor = _freshness_factor(product.get("last_verified_at"))
            score += freshness_factor
            if score <= 0:
                continue
            scored.append((score, product))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:limit]


def _freshness_factor(last_verified_at: str | None) -> float:
    if not last_verified_at:
        return 0.0
    try:
        from datetime import datetime

        ts = datetime.fromisoformat(last_verified_at)
    except ValueError:
        return 0.0
    age_seconds = max(0.0, time.time() - ts.timestamp())
    if age_seconds < 3600:
        return 0.8
    if age_seconds < 86400:
        return 0.5
    if age_seconds < 604800:
        return 0.2
    return 0.0
