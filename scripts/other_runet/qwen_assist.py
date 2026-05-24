from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .http_client import now_iso, root_host
from .local_index import ParserRecipe
from .product_passport import ProductPassport
from .qwen_client import (
    PARSER_RECIPE_SYSTEM,
    QwenClient,
    SOLD_OBJECT_SYSTEM,
    build_parser_recipe_prompt,
    build_sold_object_prompt,
)
from .sold_object_resolver import SoldObjectResolution
from .structured_extractor import ExtractedOffer, _add_evidence


LOGGER = logging.getLogger("other_runet.qwen.assist")


@dataclass
class QwenBudget:
    max_calls_per_search: int = 6
    used: int = 0
    elapsed_seconds: float = 0.0
    last_error: str | None = None

    def can_call(self) -> bool:
        return self.used < self.max_calls_per_search

    def register(self, elapsed: float, error: str | None) -> None:
        self.used += 1
        self.elapsed_seconds += elapsed
        if error:
            self.last_error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_calls_per_search": self.max_calls_per_search,
            "used": self.used,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "last_error": self.last_error,
        }


def maybe_assist_sold_object(
    base_resolution: SoldObjectResolution,
    *,
    candidate_title: str,
    breadcrumbs: list[str] | None,
    description: str | None,
    passport: ProductPassport,
    client: QwenClient,
    budget: QwenBudget,
) -> SoldObjectResolution:
    if not base_resolution.needs_review:
        return base_resolution
    if not budget.can_call():
        return base_resolution
    if not client.is_available():
        return base_resolution
    prompt = build_sold_object_prompt(
        passport=passport.to_dict(),
        candidate_title=candidate_title,
        breadcrumbs=breadcrumbs,
        description=description,
    )
    response = client.generate_json(system_prompt=SOLD_OBJECT_SYSTEM, user_prompt=prompt, max_tokens=400)
    budget.register(response.elapsed_seconds, response.error)
    if response.parsed is None:
        return base_resolution
    payload = response.parsed
    decision = (payload.get("decision") or "").strip().lower()
    confidence = float(payload.get("confidence") or 0.0)
    resolution_payload = payload.get("sold_object_resolution") or {}
    if decision not in {"accept", "reject", "review"} or confidence < 0.0 or confidence > 1.0:
        return base_resolution
    updated_signals = list(base_resolution.signals) + [f"qwen_decision:{decision}"]
    same_sold_object = base_resolution.same_sold_object
    needs_review = base_resolution.needs_review
    if decision == "accept" and confidence >= 0.75:
        same_sold_object = True
        needs_review = False
    elif decision == "reject" and confidence >= 0.75:
        same_sold_object = False
        needs_review = False
    elif decision == "review":
        same_sold_object = base_resolution.same_sold_object
        needs_review = True
    new_confidence = max(base_resolution.confidence, min(0.95, (base_resolution.confidence + confidence) / 2))
    return SoldObjectResolution(
        same_sold_object=same_sold_object,
        confidence=new_confidence,
        sold_object=str(resolution_payload.get("sold_object") or base_resolution.sold_object),
        reference_object=resolution_payload.get("reference_object") or base_resolution.reference_object,
        relation=resolution_payload.get("relation") or base_resolution.relation,
        product_type_match=base_resolution.product_type_match,
        reason=str(resolution_payload.get("reason") or base_resolution.reason),
        signals=updated_signals,
        needs_review=needs_review,
    )


def maybe_assist_parser_recipe(
    offer: ExtractedOffer,
    *,
    html_text: str,
    url: str,
    client: QwenClient,
    budget: QwenBudget,
) -> ParserRecipe | None:
    missing = []
    if not offer.title:
        missing.append("title")
    if not offer.price_current:
        missing.append("price")
    if not offer.availability:
        missing.append("availability")
    if not missing:
        return None
    if not budget.can_call() or not client.is_available():
        return None
    excerpt = _build_html_excerpt(html_text)
    domain = root_host(url)
    prompt = build_parser_recipe_prompt(url=url, domain=domain, html_excerpt=excerpt, missing_fields=missing)
    response = client.generate_json(system_prompt=PARSER_RECIPE_SYSTEM, user_prompt=prompt, max_tokens=600)
    budget.register(response.elapsed_seconds, response.error)
    if response.parsed is None:
        return None
    payload = response.parsed
    recipe = _coerce_recipe(payload, domain=domain)
    if recipe is None:
        return None
    applied_fields = _apply_recipe_inline(offer, html_text, recipe)
    if not applied_fields:
        return None
    for field_name in applied_fields:
        _add_evidence(offer, field_name, getattr(offer, "price_current" if field_name == "price" else field_name), "qwen_fallback", 0.7, detail="parser_recipe")
    recipe.last_validated_at = now_iso()
    recipe.confidence = max(0.5, min(1.0, recipe.confidence + 0.1))
    if "qwen_recipe" not in offer.extraction_path:
        offer.extraction_path.append("qwen_recipe")
    return recipe


def _build_html_excerpt(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    head_text = ""
    head = soup.find("head")
    if head:
        head_text = str(head)[:2000]
    body = soup.find("body")
    body_text = ""
    if body:
        body_text = str(body)[:3500]
    return head_text + "\n\n" + body_text


def _coerce_recipe(payload: dict[str, Any], *, domain: str) -> ParserRecipe | None:
    strategy = str(payload.get("strategy") or "html_selectors").strip().lower()
    if strategy not in {"json_ld_then_html", "internal_api", "next_data", "nuxt_state", "html_selectors", "playwright_required"}:
        strategy = "html_selectors"
    selectors_raw = payload.get("selectors") or {}
    json_paths_raw = payload.get("json_paths") or {}
    if not isinstance(selectors_raw, dict):
        return None
    cleaned_selectors: dict[str, str | None] = {}
    for key in ("title", "price", "availability", "breadcrumbs"):
        value = selectors_raw.get(key)
        if isinstance(value, str) and 1 <= len(value) <= 200 and "javascript" not in value.lower():
            cleaned_selectors[key] = value.strip()
        else:
            cleaned_selectors[key] = None
    cleaned_json_paths: dict[str, str | None] = {}
    for key in ("title", "price", "availability", "sku", "brand"):
        value = json_paths_raw.get(key) if isinstance(json_paths_raw, dict) else None
        cleaned_json_paths[key] = value if isinstance(value, str) and 1 <= len(value) <= 200 else None
    return ParserRecipe(
        domain=domain,
        strategy=strategy,
        selectors=cleaned_selectors,
        json_paths=cleaned_json_paths,
        requires_city=bool(payload.get("requires_city")),
        city_strategy=payload.get("city_strategy") if isinstance(payload.get("city_strategy"), dict) else None,
        notes=str(payload.get("notes") or "")[:200],
        confidence=float(payload.get("confidence") or 0.5),
    )


def _apply_recipe_inline(offer: ExtractedOffer, html_text: str, recipe: ParserRecipe) -> list[str]:
    from .structured_extractor import _apply_recipe, _normalize, _parse_price

    soup = BeautifulSoup(html_text, "html.parser")
    before_title = offer.title
    before_price = offer.price_current
    before_availability = offer.availability
    _apply_recipe(offer, soup, recipe.to_dict())
    _normalize(offer)
    applied: list[str] = []
    if offer.title and not before_title:
        applied.append("title")
    if offer.price_current and not before_price:
        applied.append("price")
    if offer.availability and not before_availability:
        applied.append("availability")
    return applied
