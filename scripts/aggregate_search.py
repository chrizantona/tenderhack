from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from search_quality import build_log_features, build_query_plan, best_query_level, score_candidate
from wb_common import DEFAULT_DEST
from wb_parser import WildberriesClient, WildberriesSearchParser
from ozon_parser import OzonClient, OzonSearchParser
from runet_parser import RunetOpenWebClient, RunetOpenWebSearchParser
from ym_parser import YandexMarketClient, YandexMarketSearchParser


AdapterSearch = Callable[[str, int], dict[str, Any]]


class SearchAggregator:
    def __init__(
        self,
        *,
        marketplaces: list[str],
        region_name: str,
        dest: str,
        sort: str,
        enrich: bool,
        enrich_limit: int | None,
        adapter_timeout: float,
        runet_discovery: bool,
        runet_max_search_pages: int,
        runet_max_product_pages: int,
    ) -> None:
        self.marketplaces = marketplaces
        self.region_name = region_name
        self.dest = dest
        self.sort = sort
        self.enrich = enrich
        self.enrich_limit = enrich_limit
        self.adapter_timeout = adapter_timeout
        self.runet_discovery = runet_discovery
        self.runet_max_search_pages = runet_max_search_pages
        self.runet_max_product_pages = runet_max_product_pages
        self._parsers: dict[str, Any] = {}

    def search(
        self,
        query: str,
        *,
        page: int,
        limit: int,
        candidate_limit: int,
        min_good_candidates: int,
        good_score: float,
        max_variants: int | None,
    ) -> dict[str, Any]:
        plan = build_query_plan(query)
        variants = plan.variants[:max_variants] if max_variants else plan.variants
        seen: dict[str, dict[str, Any]] = {}
        attempts = []
        diagnostics = []

        for variant in variants:
            attempt = {
                "level": variant.level,
                "query": variant.query,
                "marketplaces": [],
            }
            for marketplace in self.marketplaces:
                result = self._search_marketplace(
                    marketplace,
                    variant.query,
                    page=page,
                    limit=candidate_limit,
                )
                items = result.get("items") or []
                attempt["marketplaces"].append(
                    {
                        "marketplace": marketplace,
                        "items": len(items),
                        "diagnostics": (result.get("source_meta") or {}).get("diagnostics") or [],
                    }
                )
                diagnostics.extend(
                    {
                        "marketplace": marketplace,
                        "query_level": variant.level,
                        "query": variant.query,
                        **diagnostic,
                    }
                    for diagnostic in ((result.get("source_meta") or {}).get("diagnostics") or [])
                )

                for item in items:
                    key = dedup_key(item)
                    if not key:
                        key = f"{marketplace}:unknown:{len(seen)}"
                    candidate = dict(item)
                    candidate["_aggregate_meta"] = {
                        "source_query": variant.query,
                        "source_query_level": variant.level,
                    }
                    if key not in seen:
                        seen[key] = candidate
                    else:
                        seen[key] = choose_better_candidate(seen[key], candidate)

            scored_items = score_items(list(seen.values()), plan)
            good_candidates = [item for item in scored_items if item["search_quality"]["score"] >= good_score]
            attempt["unique_items_after_attempt"] = len(scored_items)
            attempt["good_candidates_after_attempt"] = len(good_candidates)
            attempts.append(attempt)

            if len(good_candidates) >= min_good_candidates:
                break

        scored_items = score_items(list(seen.values()), plan)
        grouped = group_by_marketplace(scored_items, per_group_limit=limit)
        flat_items = sorted(scored_items, key=lambda item: item["search_quality"]["score"], reverse=True)[: max(1, limit)]

        return {
            "schema_version": "0.2",
            "query": query,
            "normalized_query": plan.normalized_query,
            "page": max(1, page),
            "limit": max(1, limit),
            "total_unique": len(scored_items),
            "items": strip_aggregate_meta(flat_items),
            "groups": {key: strip_aggregate_meta(value) for key, value in grouped.items()},
            "source_meta": {
                "adapter": "search_aggregator",
                "query_plan": asdict(plan),
                "attempts": attempts,
                "marketplaces": self.marketplaces,
                "ranking": {
                    "good_score": good_score,
                    "min_good_candidates": min_good_candidates,
                    "strategy": "strict/core/broad query relaxation + dedup + search_quality scoring",
                },
                "diagnostics": diagnostics,
            },
        }

    def _search_marketplace(self, marketplace: str, query: str, *, page: int, limit: int) -> dict[str, Any]:
        try:
            if marketplace == "wb":
                parser = self._parsers.get("wb")
                if parser is None:
                    parser = WildberriesSearchParser(client=WildberriesClient(timeout=self.adapter_timeout))
                    self._parsers["wb"] = parser
                return parser.search(
                    query,
                    page=page,
                    limit=limit,
                    sort=self.sort,
                    dest=self.dest,
                    region_name=self.region_name,
                    enrich=self.enrich,
                    enrich_limit=self.enrich_limit,
                )
            if marketplace == "ozon":
                parser = self._parsers.get("ozon")
                if parser is None:
                    parser = OzonSearchParser(client=OzonClient(timeout=self.adapter_timeout))
                    self._parsers["ozon"] = parser
                return parser.search(
                    query,
                    page=page,
                    limit=limit,
                    region_name=self.region_name,
                    enrich=self.enrich,
                    enrich_limit=self.enrich_limit,
                )
            if marketplace == "ym":
                parser = self._parsers.get("ym")
                if parser is None:
                    parser = YandexMarketSearchParser(client=YandexMarketClient(timeout=self.adapter_timeout))
                    self._parsers["ym"] = parser
                return parser.search(
                    query,
                    page=page,
                    limit=limit,
                    region_name=self.region_name,
                    enrich=self.enrich,
                    enrich_limit=self.enrich_limit,
                )
            if marketplace == "runet":
                parser = self._parsers.get("runet")
                if parser is None:
                    parser = RunetOpenWebSearchParser(client=RunetOpenWebClient(timeout=self.adapter_timeout))
                    self._parsers["runet"] = parser
                return parser.search(
                    query,
                    page=page,
                    limit=limit,
                    region_name=self.region_name,
                    use_discovery=self.runet_discovery,
                    max_search_pages=self.runet_max_search_pages,
                    max_product_pages=self.runet_max_product_pages,
                )
            raise ValueError(f"Unsupported marketplace: {marketplace}")
        except Exception as exc:
            return empty_marketplace_result(marketplace, query, self.region_name, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate marketplace search with query relaxation and ranking.")
    parser.add_argument("query")
    parser.add_argument("--marketplaces", default="wb,ozon,ym,runet", help="Comma-separated: wb,ozon,ym,runet")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10, help="Max products per output group and flat list.")
    parser.add_argument("--candidate-limit", type=int, default=10, help="Products requested per marketplace attempt.")
    parser.add_argument("--region-name", default="RU/Moscow")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="WB destination region.")
    parser.add_argument("--sort", default="popular", help="WB sort.")
    parser.add_argument("--enrich-details", action="store_true")
    parser.add_argument("--enrich-limit", type=int)
    parser.add_argument("--adapter-timeout", type=float, default=8)
    parser.add_argument("--runet-discovery", action="store_true", help="Also use HTML search discovery for Runet.")
    parser.add_argument("--runet-max-search-pages", type=int, default=4)
    parser.add_argument("--runet-max-product-pages", type=int, default=8)
    parser.add_argument("--min-good-candidates", type=int, default=5)
    parser.add_argument("--good-score", type=float, default=60)
    parser.add_argument("--max-variants", type=int, help="Limit query plan attempts for quick checks.")
    parser.add_argument("--output")
    args = parser.parse_args()

    aggregator = SearchAggregator(
        marketplaces=parse_marketplaces(args.marketplaces),
        region_name=args.region_name,
        dest=args.dest,
        sort=args.sort,
        enrich=args.enrich_details,
        enrich_limit=args.enrich_limit,
        adapter_timeout=args.adapter_timeout,
        runet_discovery=args.runet_discovery,
        runet_max_search_pages=args.runet_max_search_pages,
        runet_max_product_pages=args.runet_max_product_pages,
    )
    result = aggregator.search(
        args.query,
        page=args.page,
        limit=args.limit,
        candidate_limit=args.candidate_limit,
        min_good_candidates=args.min_good_candidates,
        good_score=args.good_score,
        max_variants=args.max_variants,
    )

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def parse_marketplaces(value: str) -> list[str]:
    aliases = {
        "wildberries": "wb",
        "wb": "wb",
        "ozon": "ozon",
        "yandex_market": "ym",
        "yandex": "ym",
        "ym": "ym",
        "runet": "runet",
        "runet_open_web": "runet",
        "open_web": "runet",
        "4": "runet",
    }
    marketplaces = []
    for item in value.split(","):
        normalized = aliases.get(item.strip().lower())
        if not normalized:
            raise ValueError(f"Unsupported marketplace in --marketplaces: {item!r}")
        if normalized not in marketplaces:
            marketplaces.append(normalized)
    return marketplaces


def score_items(items: list[dict[str, Any]], plan: Any) -> list[dict[str, Any]]:
    features_by_index = build_log_features(items)
    scored = []
    for index, item in enumerate(items):
        item_copy = dict(item)
        features = features_by_index[index]
        aggregate_meta = item_copy.get("_aggregate_meta") or {}
        item_copy["search_quality"] = {
            "query_plan_level": best_query_level(plan, item),
            "source_query_level": aggregate_meta.get("source_query_level"),
            "source_query": aggregate_meta.get("source_query"),
            "features": features,
            "score": score_candidate(plan, item, features),
        }
        scored.append(item_copy)
    scored.sort(key=lambda product: product["search_quality"]["score"], reverse=True)
    return scored


def group_by_marketplace(items: list[dict[str, Any]], *, per_group_limit: int) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        marketplace = item.get("marketplace") or "unknown"
        groups.setdefault(marketplace, []).append(item)
    return {key: value[: max(1, per_group_limit)] for key, value in groups.items()}


def dedup_key(item: dict[str, Any]) -> str | None:
    marketplace = item.get("marketplace") or "unknown"
    for field in ("external_id", "offer_id", "variant_id", "url"):
        value = item.get(field)
        if value:
            return f"{marketplace}:{field}:{value}"
    return None


def choose_better_candidate(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_level = (current.get("_aggregate_meta") or {}).get("source_query_level")
    candidate_level = (candidate.get("_aggregate_meta") or {}).get("source_query_level")
    level_rank = {"strict": 3, "core": 2, "broad": 1}
    if level_rank.get(candidate_level, 0) > level_rank.get(current_level, 0):
        return candidate
    return current


def strip_aggregate_meta(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        item_copy = dict(item)
        item_copy.pop("_aggregate_meta", None)
        result.append(item_copy)
    return result


def empty_marketplace_result(marketplace: str, query: str, region_name: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": "0.2",
        "marketplace": marketplace,
        "query": query,
        "page": 1,
        "limit": 0,
        "total": None,
        "items": [],
        "source_meta": {
            "adapter": marketplace,
            "endpoints": [],
            "region_params": {"region": region_name},
            "diagnostics": [{"stage": "search_page", "message": message}],
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
