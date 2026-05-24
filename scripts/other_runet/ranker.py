from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .evidence_fusion import IdentityScore, evidence_strength
from .price_anomaly import PriceFlag
from .product_passport import ProductPassport
from .sold_object_resolver import SoldObjectResolution
from .structured_extractor import ExtractedOffer


GROUP_HIGH = "high_confidence"
GROUP_GOOD = "good_matches"
GROUP_LOW = "low_confidence"
GROUP_SUSPICIOUS = "suspicious_hidden"


@dataclass
class RankedOffer:
    offer: ExtractedOffer
    identity: IdentityScore
    sold_object: SoldObjectResolution
    trust_score: float
    price_score: float
    price_flags: list[PriceFlag]
    availability_score: float
    freshness_score: float
    evidence_score: float
    final_score: float
    group: str
    reasons: list[str] = field(default_factory=list)
    rejected: bool = False
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.offer.url,
            "final_score": round(self.final_score, 3),
            "group": self.group,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "identity": self.identity.to_dict(),
            "sold_object": self.sold_object.to_dict(),
            "trust_score": round(self.trust_score, 3),
            "price_score": round(self.price_score, 3),
            "price_flags": [flag.to_dict() for flag in self.price_flags],
            "availability_score": round(self.availability_score, 3),
            "freshness_score": round(self.freshness_score, 3),
            "evidence_score": round(self.evidence_score, 3),
            "reasons": list(self.reasons),
        }


def rank_offer(
    offer: ExtractedOffer,
    *,
    passport: ProductPassport,
    identity: IdentityScore,
    sold_object: SoldObjectResolution,
    trust_score: float,
    price_score: float,
    price_flags: Iterable[PriceFlag],
    freshness_score: float = 0.0,
    excluded: bool = False,
) -> RankedOffer:
    availability_score = _availability_score(offer.availability)
    evidence_score = evidence_strength(offer.evidence)
    final_score = (
        identity.score * 0.30
        + sold_object.confidence * 0.22
        + trust_score * 0.16
        + price_score * 0.12
        + availability_score * 0.08
        + freshness_score * 0.06
        + evidence_score * 0.06
    )
    final_score = max(0.0, min(1.0, final_score))
    reasons: list[str] = []
    rejected = False
    reject_reason: str | None = None
    group = GROUP_LOW

    if excluded:
        rejected = True
        reject_reason = "excluded_marketplace"
    elif not sold_object.same_sold_object and not sold_object.needs_review:
        rejected = True
        reject_reason = "sold_object_mismatch"
    elif identity.score < 0.45 and sold_object.confidence < 0.6:
        rejected = True
        reject_reason = "identity_too_low"

    price_flag_codes = {flag.flag for flag in price_flags}
    if "suspicious_low_price" in price_flag_codes:
        group = GROUP_SUSPICIOUS
        reasons.append("suspicious_low_price")
    elif rejected:
        group = GROUP_LOW
    elif final_score >= 0.78 and sold_object.same_sold_object and identity.score >= 0.65:
        group = GROUP_HIGH
    elif final_score >= 0.6 and sold_object.same_sold_object:
        group = GROUP_GOOD
    else:
        group = GROUP_LOW

    if sold_object.needs_review:
        reasons.append("sold_object_needs_review")
    if identity.score < 0.65:
        reasons.append("identity_low")
    if offer.availability == "out_of_stock":
        reasons.append("out_of_stock")
    if offer.price_current is None:
        reasons.append("missing_price")
    if "html" in offer.extraction_path and "json_ld" not in offer.extraction_path:
        reasons.append("html_only_extraction")

    return RankedOffer(
        offer=offer,
        identity=identity,
        sold_object=sold_object,
        trust_score=trust_score,
        price_score=price_score,
        price_flags=list(price_flags),
        availability_score=availability_score,
        freshness_score=freshness_score,
        evidence_score=evidence_score,
        final_score=final_score,
        group=group,
        reasons=reasons,
        rejected=rejected,
        reject_reason=reject_reason,
    )


def sort_ranked(items: Iterable[RankedOffer]) -> list[RankedOffer]:
    return sorted(items, key=lambda ranked: (-int(not ranked.rejected), -ranked.final_score))


def _availability_score(value: str | None) -> float:
    if not value:
        return 0.3
    if value == "in_stock":
        return 1.0
    if value == "limited":
        return 0.8
    if value == "preorder":
        return 0.5
    if value == "out_of_stock":
        return 0.1
    return 0.4
