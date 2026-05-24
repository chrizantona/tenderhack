from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .marketplace_exclusion import ALL_EXCLUDED_SUFFIXES, is_excluded_marketplace
from .product_passport import ProductPassport
from .qwen_client import QwenClient


LOGGER = logging.getLogger("other_runet.qwen.suggest")


DOMAIN_SUGGESTER_SYSTEM = (
    "You are a Russian e-commerce search planner. "
    "You receive a product passport (canonical query, brand, model, product type, category). "
    "Return a JSON object listing 10 top relevant Russian online stores (real shops, NOT marketplaces) "
    "that most likely sell exactly this product. "
    "Strictly exclude these aggregator marketplaces: ozon.ru, wildberries.ru, wb.ru, market.yandex.ru, "
    "aliexpress.ru, megamarket.ru, sbermegamarket.ru, avito.ru, youla.ru. "
    "Output schema: "
    "{\"domains\": ["
    "{\"domain\": \"example.ru\", \"name\": \"Магазин\", \"reason\": \"why\", \"search_url_pattern\": \"https://example.ru/search/?q={query}\"}"
    "], \"category_keyword\": str|null}. "
    "Use real production search URLs you remember; placeholder {query} marks where the search term goes (URL-encoded). "
    "Return ONLY JSON, no commentary."
)


@dataclass
class DomainSuggestion:
    domain: str
    name: str | None
    reason: str | None
    search_url_pattern: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "name": self.name,
            "reason": self.reason,
            "search_url_pattern": self.search_url_pattern,
        }


def suggest_domains(
    passport: ProductPassport,
    *,
    client: QwenClient,
    max_domains: int = 10,
) -> list[DomainSuggestion]:
    if not client.is_available():
        return []
    payload = {
        "canonical_query": passport.canonical_query,
        "brand": passport.brand,
        "product_family": passport.product_family,
        "model": passport.model,
        "product_type": passport.product_type,
        "category": passport.category,
        "category_label": passport.category_label,
        "required_attributes": passport.required_attributes,
        "excluded_marketplaces": list(ALL_EXCLUDED_SUFFIXES),
        "max_domains": max_domains,
    }
    response = client.generate_json(
        system_prompt=DOMAIN_SUGGESTER_SYSTEM,
        user_prompt=json.dumps(payload, ensure_ascii=False),
        max_tokens=900,
        temperature=0.2,
    )
    if response.parsed is None:
        return []
    raw_domains = response.parsed.get("domains")
    if not isinstance(raw_domains, list):
        return []
    suggestions: list[DomainSuggestion] = []
    seen: set[str] = set()
    for entry in raw_domains:
        if not isinstance(entry, dict):
            continue
        domain = (entry.get("domain") or "").strip().lower()
        if not domain:
            continue
        domain = _normalize_domain(domain)
        if not domain or domain in seen:
            continue
        if is_excluded_marketplace(domain):
            continue
        pattern = entry.get("search_url_pattern")
        if isinstance(pattern, str):
            pattern = pattern.strip()
            if pattern and "{query}" not in pattern and "{q}" not in pattern:
                pattern = None
        else:
            pattern = None
        seen.add(domain)
        suggestions.append(
            DomainSuggestion(
                domain=domain,
                name=_string_or_none(entry.get("name")),
                reason=_string_or_none(entry.get("reason")),
                search_url_pattern=pattern,
            )
        )
        if len(suggestions) >= max_domains:
            break
    return suggestions


def _normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("http://") or value.startswith("https://"):
        from urllib.parse import urlparse

        value = urlparse(value).netloc
    value = value.split("/")[0]
    if value.startswith("www."):
        value = value[4:]
    return value


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
