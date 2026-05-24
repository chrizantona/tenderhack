from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http_client import now_iso, root_host
from .local_index import LocalIndex, StoreTrust


@dataclass
class TrustOutcome:
    score: float
    risk_flags: list[str]
    domain: str
    name: str | None
    inn: str | None
    ogrn: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "name": self.name,
            "inn": self.inn,
            "ogrn": self.ogrn,
            "trust_score": round(self.score, 3),
            "risk_flags": list(self.risk_flags),
            "last_checked_at": now_iso(),
        }


def evaluate_store_trust(
    domain: str,
    *,
    index: LocalIndex,
    extraction_path: list[str] | None = None,
    has_price: bool = True,
    has_availability: bool = True,
) -> TrustOutcome:
    host = root_host(domain)
    record = index.get_trust(host)
    base_score = float((record or {}).get("trust_score") or 0.0)
    risk_flags: list[str] = list((record or {}).get("risk_flags") or [])
    if not record:
        risk_flags.append("newly_seen_domain")
        base_score = max(base_score, 0.15)
    if not has_price:
        risk_flags.append("missing_price")
        base_score *= 0.8
    if not has_availability:
        risk_flags.append("unknown_availability")
        base_score *= 0.9
    if extraction_path and "html" in extraction_path and "json_ld" not in extraction_path and "internal_json" not in extraction_path:
        risk_flags.append("html_only_extraction")
        base_score *= 0.85
    final = max(0.0, min(1.0, base_score))
    flags = sorted({flag for flag in risk_flags if flag})
    return TrustOutcome(
        score=final,
        risk_flags=flags,
        domain=host,
        name=(record or {}).get("name"),
        inn=(record or {}).get("inn"),
        ogrn=(record or {}).get("ogrn"),
    )


def update_store_trust(index: LocalIndex, outcome: TrustOutcome) -> None:
    existing = index.get_trust(outcome.domain) or {}
    trust = StoreTrust(
        domain=outcome.domain,
        name=outcome.name or existing.get("name"),
        inn=outcome.inn or existing.get("inn"),
        ogrn=outcome.ogrn or existing.get("ogrn"),
        contacts_found=bool(existing.get("contacts_found")),
        https=bool(existing.get("https", True)),
        trust_score=outcome.score,
        risk_flags=outcome.risk_flags,
    )
    index.upsert_trust(trust)
