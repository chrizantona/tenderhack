from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

from .bootstrap_discovery import discover_candidates
from .domain_discovery import inspect_domain
from .evidence_fusion import compute_identity_confidence
from .feed_parser import parse_sitemap, parse_yml
from .http_client import FetchError, RateLimitedHttpClient, now_iso, root_host
from .local_index import IndexedProduct, LocalIndex, tokenize
from .marketplace_exclusion import excluded_reason, is_excluded_marketplace
from .output_schema import build_product_event, build_source_result
from .price_anomaly import PriceContext, PriceFlag, build_price_context, evaluate_price
from .product_passport import ProductPassport, build_passport
from .product_url_detector import score_html_for_product, url_only_product_likelihood
from .query_swarm import generate_swarm
from .qwen_assist import QwenBudget, maybe_assist_parser_recipe, maybe_assist_sold_object
from .qwen_client import QwenClient
from .qwen_extractor import extract_with_qwen
from .ranker import RankedOffer, rank_offer, sort_ranked
from .region import RegionContext, build_region_context, is_region_supported
from .sold_object_resolver import SoldObjectResolution, resolve_sold_object
from .structured_extractor import ExtractedOffer, extract_offer
from .trust_engine import evaluate_store_trust, update_store_trust


LOGGER = logging.getLogger("other_runet.search")


@dataclass
class SearchStats:
    passport_confidence: float = 0.0
    queries_generated: int = 0
    local_index_hits: int = 0
    frontier_candidates: int = 0
    domains_checked: int = 0
    feeds_found: int = 0
    offers_extracted: int = 0
    live_refreshed: int = 0
    rejected_by_sold_object: int = 0
    excluded_marketplaces: int = 0
    low_confidence_results: int = 0
    qwen_calls: int = 0
    qwen_parser_recipes_created: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passport_confidence": round(self.passport_confidence, 3),
            "queries_generated": self.queries_generated,
            "local_index_hits": self.local_index_hits,
            "frontier_candidates": self.frontier_candidates,
            "domains_checked": self.domains_checked,
            "feeds_found": self.feeds_found,
            "offers_extracted": self.offers_extracted,
            "live_refreshed": self.live_refreshed,
            "rejected_by_sold_object": self.rejected_by_sold_object,
            "excluded_marketplaces": self.excluded_marketplaces,
            "low_confidence_results": self.low_confidence_results,
            "qwen_calls": self.qwen_calls,
            "qwen_parser_recipes_created": self.qwen_parser_recipes_created,
        }


@dataclass
class _Candidate:
    url: str
    domain: str
    source: str
    swarm_query: str | None = None
    intent: str | None = None
    url_likelihood: float = 0.0


@dataclass
class _EmitState:
    limit: int
    emitted: int = 0
    accepted_events: list[dict[str, Any]] = field(default_factory=list)
    rejected_count: int = 0
    low_confidence_count: int = 0
    running_prices: list[float] = field(default_factory=list)
    price_context: PriceContext | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _OfferContext:
    candidate: _Candidate
    offer: ExtractedOffer
    sold: SoldObjectResolution
    url_score: float
    html_text: str

    @property
    def trusted(self) -> bool:
        return self.sold.same_sold_object and self.offer.price_current is not None and self.url_score >= 0.45


class OtherRunetSearchParser:
    def __init__(
        self,
        *,
        http_client: RateLimitedHttpClient | None = None,
        qwen_client: QwenClient | None = None,
        index: LocalIndex | None = None,
        max_workers: int = 6,
    ) -> None:
        self.http = http_client or RateLimitedHttpClient(min_domain_delay=0.6, timeout=12.0)
        self.qwen = qwen_client or QwenClient()
        self.index = index or LocalIndex()
        self.max_workers = max_workers

    def search(
        self,
        query: str,
        *,
        region_name: str = "RU/Moscow",
        limit: int = 10,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
        max_discovery_candidates: int = 25,
        max_live_candidates: int = 10,
        max_per_domain: int = 1,
        time_budget: float | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        stats = SearchStats()
        diagnostics: list[dict[str, Any]] = []
        region = build_region_context(region_name)
        passport = build_passport(query)
        stats.passport_confidence = passport.confidence
        if not passport.canonical_query:
            return build_source_result(
                products=[],
                diagnostics=[{"stage": "passport", "message": "empty_canonical_query"}],
                stats=stats.to_dict(),
                region_name=region.canonical_label,
                region_supported=False,
                took_seconds=time.time() - started,
                status_override="empty",
            )

        swarm = generate_swarm(passport)
        stats.queries_generated = len(swarm)
        deadline = (started + time_budget) if time_budget else None

        candidates = self._gather_candidates(
            passport=passport,
            swarm=swarm,
            stats=stats,
            diagnostics=diagnostics,
            max_discovery_candidates=max_discovery_candidates,
            deadline=deadline,
        )
        if not candidates:
            return build_source_result(
                products=[],
                diagnostics=diagnostics + [{"stage": "candidates", "message": "no_candidates"}],
                stats=stats.to_dict(),
                region_name=region.canonical_label,
                region_supported=False,
                took_seconds=time.time() - started,
                status_override="empty",
            )

        candidates = self._filter_and_rank_candidates(candidates, max_live_candidates=max_live_candidates, max_per_domain=max_per_domain)
        stats.frontier_candidates = len(candidates)

        emit_state = _EmitState(limit=limit)
        qwen_lock = None if getattr(self.qwen, "parallel_safe", False) else threading.Lock()

        per_candidate_budget = 22.0
        worker_pool = ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(candidates))))
        try:
            future_map = {
                worker_pool.submit(
                    self._process_candidate,
                    candidate,
                    passport=passport,
                    region=region,
                    qwen_lock=qwen_lock,
                    emit_state=emit_state,
                    item_callback=item_callback,
                    stats=stats,
                    diagnostics=diagnostics,
                ): candidate
                for candidate in candidates
            }
            stage_deadline = time.time() + (per_candidate_budget * max(1, len(candidates)) / max(1, self.max_workers))
            if deadline is not None:
                stage_deadline = min(stage_deadline, deadline)
            try:
                for future in as_completed(list(future_map.keys()), timeout=max(1.0, stage_deadline - time.time())):
                    try:
                        future.result(timeout=1.0)
                    except Exception as exc:
                        candidate = future_map[future]
                        diagnostics.append({"stage": "process", "url": candidate.url, "error": repr(exc)})
            except FuturesTimeoutError:
                diagnostics.append({"stage": "process", "message": "stage_deadline_exceeded"})
        finally:
            worker_pool.shutdown(wait=False, cancel_futures=True)

        diagnostics.append({"stage": "price_context", "message": str(emit_state.price_context.to_dict() if emit_state.price_context else {})})
        if emit_state.rejected_count:
            diagnostics.append({"stage": "ranker", "message": f"rejected_offers={emit_state.rejected_count}"})
        stats.low_confidence_results = emit_state.low_confidence_count

        return build_source_result(
            products=emit_state.accepted_events,
            diagnostics=diagnostics,
            stats=stats.to_dict(),
            region_name=region.canonical_label,
            region_supported=False,
            took_seconds=time.time() - started,
            status_override=None if emit_state.accepted_events else "empty",
        )

    def _gather_candidates(
        self,
        *,
        passport: ProductPassport,
        swarm: list[Any],
        stats: SearchStats,
        diagnostics: list[dict[str, Any]],
        max_discovery_candidates: int,
        deadline: float | None,
    ) -> list[_Candidate]:
        token_query = tokenize(passport.canonical_query)
        identifiers = []
        for values in passport.identifier_candidates.values():
            identifiers.extend(values)
        local_results = self.index.search_products(
            tokens=token_query,
            identifiers=identifiers,
            category=passport.category,
            limit=30,
        )
        stats.local_index_hits = len(local_results)
        candidates: dict[str, _Candidate] = {}
        for score, product in local_results:
            url = product.get("url") or ""
            if not url or is_excluded_marketplace(url):
                continue
            domain = product.get("domain") or root_host(url)
            candidates[url] = _Candidate(
                url=url,
                domain=domain,
                source="local_index",
                url_likelihood=max(0.5, min(1.0, score / 8.0)),
            )

        if deadline is None or time.time() < deadline:
            discovered = discover_candidates(
                swarm,
                client=self.http,
                max_per_query=8,
                max_total=max_discovery_candidates,
                diagnostics=diagnostics,
            )
        else:
            discovered = []
        for entry in discovered:
            url = entry["url"]
            if is_excluded_marketplace(url):
                stats.excluded_marketplaces += 1
                continue
            domain = entry.get("domain") or root_host(url)
            if not domain:
                continue
            existing = candidates.get(url)
            url_likelihood = url_only_product_likelihood(url)
            if existing:
                existing.url_likelihood = max(existing.url_likelihood, url_likelihood)
                continue
            candidates[url] = _Candidate(
                url=url,
                domain=domain,
                source=str(entry.get("discovered_via") or "discovery"),
                swarm_query=str(entry.get("swarm_query") or "") or None,
                intent=str(entry.get("intent") or "") or None,
                url_likelihood=url_likelihood,
            )
        return list(candidates.values())

    def _filter_and_rank_candidates(
        self,
        candidates: Iterable[_Candidate],
        *,
        max_live_candidates: int,
        max_per_domain: int,
    ) -> list[_Candidate]:
        grouped: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            if is_excluded_marketplace(candidate.url):
                continue
            grouped.setdefault(candidate.domain, []).append(candidate)
        ordered_domains = sorted(grouped.keys(), key=lambda domain: -max(item.url_likelihood for item in grouped[domain]))
        result: list[_Candidate] = []
        for domain in ordered_domains:
            items = grouped[domain]
            items.sort(key=lambda candidate: -candidate.url_likelihood)
            for candidate in items[:max_per_domain]:
                result.append(candidate)
                if len(result) >= max_live_candidates:
                    return result
        return result

    def _refresh_domains(
        self,
        domains: list[str],
        *,
        stats: SearchStats,
        diagnostics: list[dict[str, Any]],
        deadline: float | None,
        per_domain_budget: float = 6.0,
    ) -> None:
        new_domains: list[str] = []
        for domain in domains:
            record = self.index.get_domain(domain)
            if not record:
                new_domains.append(domain)
        if not new_domains:
            return
        stats.domains_checked = len(new_domains)
        total_budget = min(20.0, per_domain_budget * len(new_domains))
        start = time.time()
        with ThreadPoolExecutor(max_workers=min(6, len(new_domains))) as pool:
            futures = {pool.submit(inspect_domain, domain, client=self.http): domain for domain in new_domains}
            for future in as_completed(futures, timeout=total_budget if total_budget > 0 else None):
                if deadline and time.time() > deadline:
                    break
                if time.time() - start > total_budget:
                    break
                domain = futures[future]
                try:
                    record, trust = future.result()
                except Exception as exc:
                    diagnostics.append({"stage": "domain_discovery", "url": domain, "error": str(exc)})
                    continue
                self.index.upsert_domain(record)
                self.index.upsert_trust(trust)
                if record.has_feed:
                    stats.feeds_found += len(record.feed_urls)

    def _process_candidate(
        self,
        candidate: _Candidate,
        *,
        passport: ProductPassport,
        region: RegionContext,
        qwen_lock: threading.Lock | None,
        emit_state: "_EmitState",
        item_callback: Callable[[dict[str, Any], int], None] | None,
        stats: SearchStats,
        diagnostics: list[dict[str, Any]],
    ) -> None:
        context = self._fetch_and_extract(candidate, passport, diagnostics, qwen_lock=qwen_lock)
        if context is None:
            return
        with emit_state.lock:
            stats.offers_extracted += 1
            stats.live_refreshed += 1
        if context.sold.needs_review:
            budget = QwenBudget(max_calls_per_search=1)
            if qwen_lock is not None:
                with qwen_lock:
                    upgraded = maybe_assist_sold_object(
                        context.sold,
                        candidate_title=context.offer.title or "",
                        breadcrumbs=context.offer.breadcrumbs,
                        description=context.offer.description,
                        passport=passport,
                        client=self.qwen,
                        budget=budget,
                    )
            else:
                upgraded = maybe_assist_sold_object(
                    context.sold,
                    candidate_title=context.offer.title or "",
                    breadcrumbs=context.offer.breadcrumbs,
                    description=context.offer.description,
                    passport=passport,
                    client=self.qwen,
                    budget=budget,
                )
            if upgraded is not context.sold:
                context.sold = upgraded
            with emit_state.lock:
                stats.qwen_calls += budget.used
        offer = context.offer
        sold = context.sold
        if not sold.same_sold_object and not sold.needs_review:
            with emit_state.lock:
                stats.rejected_by_sold_object += 1
        identity = compute_identity_confidence(offer, passport)
        trust_outcome = evaluate_store_trust(
            context.candidate.domain,
            index=self.index,
            extraction_path=offer.extraction_path,
            has_price=offer.price_current is not None,
            has_availability=bool(offer.availability),
        )
        update_store_trust(self.index, trust_outcome)
        with emit_state.lock:
            if offer.price_current is not None and sold.same_sold_object:
                emit_state.running_prices.append(offer.price_current)
            emit_state.price_context = build_price_context(emit_state.running_prices)
            price_ctx = emit_state.price_context
        price_score, price_flags = evaluate_price(offer.price_current, price_ctx)
        ranked = rank_offer(
            offer,
            passport=passport,
            identity=identity,
            sold_object=sold,
            trust_score=trust_outcome.score,
            price_score=price_score,
            price_flags=price_flags,
            freshness_score=0.7,
            excluded=is_excluded_marketplace(offer.url),
        )
        trust_record = self.index.get_trust(context.candidate.domain) or {}
        self._upsert_indexed_product(context, identity_score=identity.score)
        with emit_state.lock:
            if ranked.rejected:
                emit_state.rejected_count += 1
                return
            if ranked.final_score < 0.45 and ranked.group != "high_confidence":
                emit_state.low_confidence_count += 1
            if emit_state.emitted >= emit_state.limit:
                return
            event = build_product_event(
                ranked=ranked,
                passport=passport,
                trust_record=trust_record,
                region_name=region.canonical_label,
                region_supported=is_region_supported(ranked.offer.url),
                extra_diagnostics=[f"intent:{candidate.intent or candidate.source}"],
            )
            emit_state.accepted_events.append(event)
            current_index = emit_state.emitted
            emit_state.emitted += 1
        if item_callback is not None:
            try:
                item_callback(event, current_index)
            except Exception as exc:
                diagnostics.append({"stage": "callback", "url": candidate.url, "error": repr(exc)})

    def _fetch_and_extract(
        self,
        candidate: _Candidate,
        passport: ProductPassport,
        diagnostics: list[dict[str, Any]],
        *,
        qwen_lock: threading.Lock | None = None,
    ) -> _OfferContext | None:
        try:
            response = self.http.get(
                candidate.url,
                timeout=9.0,
                headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                    "Sec-Fetch-Site": "cross-site",
                },
                accept_status=(200, 301, 302, 303, 308),
            )
        except FetchError as exc:
            diagnostics.append({"stage": "fetch", "url": candidate.url, "error": str(exc)})
            return None
        html_text = response.text or ""
        if not html_text:
            return None
        recipe_record = self.index.get_recipe(candidate.domain)
        offer = extract_offer(html_text, candidate.url, recipe=recipe_record)
        needs_qwen = (
            self.qwen.is_available()
            and (
                not offer.has_minimum_fields()
                or ("json_ld" not in offer.extraction_path and "microdata" not in offer.extraction_path)
            )
        )
        if needs_qwen:
            try:
                if qwen_lock is not None:
                    with qwen_lock:
                        upgraded = extract_with_qwen(
                            html_text,
                            url=candidate.url,
                            client=self.qwen,
                            existing=offer,
                        )
                else:
                    upgraded = extract_with_qwen(
                        html_text,
                        url=candidate.url,
                        client=self.qwen,
                        existing=offer,
                    )
            except Exception as exc:
                diagnostics.append({"stage": "qwen_extractor", "url": candidate.url, "error": repr(exc)})
                upgraded = None
            if upgraded is not None:
                offer = upgraded
            elif not offer.has_minimum_fields():
                qwen_recipe = maybe_assist_parser_recipe(
                    offer,
                    html_text=html_text,
                    url=candidate.url,
                    client=self.qwen,
                    budget=QwenBudget(),
                )
                if qwen_recipe is not None:
                    self.index.upsert_recipe(qwen_recipe)
                    offer.extraction_path.append("qwen_recipe")
        if not offer.has_minimum_fields():
            diagnostics.append({"stage": "extract", "url": candidate.url, "message": "no_minimum_fields"})
            return None
        url_score = score_html_for_product(html_text, candidate.url).get("score", 0.0)
        sold = resolve_sold_object(
            offer.title or candidate.url,
            passport=passport,
            breadcrumbs=offer.breadcrumbs,
            description=offer.description,
            url=candidate.url,
        )
        return _OfferContext(
            candidate=candidate,
            offer=offer,
            sold=sold,
            url_score=url_score,
            html_text=html_text,
        )

    def _upsert_indexed_product(self, context: _OfferContext, *, identity_score: float) -> None:
        offer = context.offer
        document = IndexedProduct(
            url=offer.url,
            domain=context.candidate.domain,
            title=offer.title or "",
            brand=offer.brand,
            model=None,
            category=offer.category,
            price=offer.price_current,
            currency=offer.currency,
            availability=offer.availability,
            sku=offer.sku,
            mpn=offer.mpn,
            gtin=offer.gtin,
            vendor_code=offer.sku,
            attributes=dict(offer.attributes),
            source_type=_index_source_type(offer.extraction_path),
            commerce_score=identity_score,
            product_url_score=context.url_score,
            last_seen_at=now_iso(),
            last_verified_at=now_iso(),
            content_hash=None,
        )
        self.index.upsert_product(document)


def context_intent_for(offer: ExtractedOffer, candidates: list[_Candidate]) -> str:
    for candidate in candidates:
        if candidate.url == offer.url:
            return candidate.intent or candidate.source
    return "unknown"


def _index_source_type(extraction_path: list[str]) -> str:
    if "json_ld" in extraction_path:
        return "json_ld"
    if "internal_json" in extraction_path:
        return "internal_json"
    if "microdata" in extraction_path:
        return "microdata"
    if "open_graph" in extraction_path:
        return "open_graph"
    if "qwen_recipe" in extraction_path:
        return "qwen_recipe"
    return "html"
