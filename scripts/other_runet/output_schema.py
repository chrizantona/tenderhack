from __future__ import annotations

import hashlib
from typing import Any

from .evidence_fusion import IdentityScore
from .http_client import now_iso, root_host
from .price_anomaly import PriceFlag
from .product_passport import ProductPassport
from .ranker import RankedOffer
from .sold_object_resolver import SoldObjectResolution
from .structured_extractor import ExtractedOffer


MARKETPLACE_KEY = "other_runet"
ADAPTER_VERSION = "0.1"


def build_product_event(
    *,
    ranked: RankedOffer,
    passport: ProductPassport,
    trust_record: dict[str, Any] | None,
    region_name: str,
    region_supported: bool,
    extra_diagnostics: list[str] | None = None,
) -> dict[str, Any]:
    offer = ranked.offer
    domain = root_host(offer.url)
    stable_external = hashlib.sha1((offer.url or domain or offer.title or "").encode("utf-8")).hexdigest()[:16]
    seller_name = (trust_record or {}).get("name") or domain
    return {
        "schema_version": "0.2",
        "marketplace": MARKETPLACE_KEY,
        "query": passport.raw_query,
        "external_id": stable_external,
        "group_id": stable_external,
        "variant_id": stable_external,
        "offer_id": stable_external,
        "url": offer.url,
        "title": offer.title,
        "normalized_title": (offer.title or "").lower(),
        "description": offer.description,
        "brand": {
            "name": offer.brand or passport.brand,
            "external_id": None,
        },
        "category": {
            "name": offer.category or passport.category_label,
            "external_id": None,
            "parent_name": "Прочие источники Рунета",
            "parent_external_id": None,
            "path": (["Прочие источники Рунета"] + ([offer.category or passport.category_label] if (offer.category or passport.category_label) else [])),
        },
        "price": {
            "current": offer.price_current,
            "old": offer.price_old,
            "currency": offer.currency or "RUB",
            "discount_percent": _discount_percent(offer.price_current, offer.price_old),
            "source": "open_web",
            "updated_at": now_iso(),
        },
        "rating": {"value": None, "reviews_count": None},
        "seller": {
            "id": domain,
            "name": seller_name,
            "rating": None,
        },
        "availability": {
            "status": offer.availability,
            "quantity": None,
            "region": region_name,
            "warehouse_id": None,
        },
        "delivery": {
            "region": region_name,
            "time_min_hours": None,
            "time_max_hours": None,
            "price": None,
            "currency": offer.currency or "RUB",
        },
        "media": {
            "main_image_url": offer.images[0] if offer.images else None,
            "image_urls": list(offer.images),
            "photo_count": len(offer.images) or None,
            "has_video": None,
        },
        "attributes": {
            "raw": dict(offer.attributes),
            "normalized": _normalized_attributes(passport),
        },
        "match_keys": {
            "brand": (offer.brand or passport.brand or "").lower() or None,
            "model": (offer.title or "").lower() or None,
            "manufacturer_part_number": offer.mpn,
            "vendor_code": offer.sku,
            "barcode": offer.gtin,
            "color": passport.required_attributes.get("color"),
            "size": passport.required_attributes.get("size"),
            "semantic_key": " ".join(
                filter(None, [passport.brand, passport.product_family, passport.model])
            ).lower() or None,
        },
        "marketplace_specific": {
            MARKETPLACE_KEY: {
                "domain": domain,
                "source_type": _source_type(offer),
                "confidence": round(ranked.final_score, 3),
                "identity_confidence": round(ranked.identity.score, 3),
                "same_product_confidence": round(ranked.sold_object.confidence, 3),
                "trust_score": round(ranked.trust_score, 3),
                "evidence": [item.to_dict() for item in offer.evidence],
                "risk_flags": list((trust_record or {}).get("risk_flags") or []),
                "sold_object": ranked.sold_object.to_dict(),
                "price_flags": [flag.to_dict() for flag in ranked.price_flags],
                "ranker_reasons": list(ranked.reasons),
                "ranker_group": ranked.group,
                "rejected": ranked.rejected,
                "reject_reason": ranked.reject_reason,
                "extraction_path": list(offer.extraction_path),
                "region_supported": region_supported,
                "diagnostics": list(extra_diagnostics or []),
            }
        },
        "source_meta": {
            "fetched_at": now_iso(),
            "adapter": MARKETPLACE_KEY,
            "adapter_version": ADAPTER_VERSION,
            "endpoints": [offer.url],
            "region_params": {"region": region_name, "currency": offer.currency or "RUB", "region_supported": region_supported},
            "diagnostics": list(extra_diagnostics or []),
        },
        "raw_data": {},
    }


def build_source_result(
    *,
    products: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    stats: dict[str, Any],
    region_name: str,
    region_supported: bool,
    took_seconds: float,
    status_override: str | None = None,
) -> dict[str, Any]:
    if status_override:
        status = status_override
    elif products:
        status = "ok"
    else:
        status = "empty"
    payload = {
        "source": MARKETPLACE_KEY,
        "status": status,
        "count": len(products),
        "products": products,
        "stats": stats,
        "diagnostics": _format_diagnostics(diagnostics),
        "region": region_name,
        "region_supported": region_supported,
        "took_seconds": round(took_seconds, 3),
    }
    return payload


def _format_diagnostics(diagnostics: list[dict[str, Any]]) -> list[str]:
    formatted: list[str] = []
    for item in diagnostics or []:
        if isinstance(item, str):
            formatted.append(item)
        elif isinstance(item, dict):
            stage = item.get("stage") or "info"
            message = item.get("message") or item.get("error") or item.get("status") or "ok"
            url = item.get("url")
            if url:
                formatted.append(f"{stage}:{url}:{message}")
            else:
                formatted.append(f"{stage}:{message}")
    return formatted


def _source_type(offer: ExtractedOffer) -> str:
    if "json_ld" in offer.extraction_path:
        return "json_ld"
    if "internal_json" in offer.extraction_path:
        return "internal_json"
    if "microdata" in offer.extraction_path:
        return "microdata"
    if "open_graph" in offer.extraction_path:
        return "open_graph"
    if "qwen_recipe" in offer.extraction_path:
        return "qwen_fallback"
    return "html"


def _discount_percent(current: float | None, old: float | None) -> int | None:
    if not current or not old or old <= current:
        return None
    return int(round((1.0 - current / old) * 100))


def _normalized_attributes(passport: ProductPassport) -> dict[str, Any]:
    normalized = dict(passport.required_attributes)
    if passport.brand:
        normalized.setdefault("brand", passport.brand)
    if passport.product_type:
        normalized.setdefault("product_type", passport.product_type)
    if passport.product_family:
        normalized.setdefault("family", passport.product_family)
    return normalized
