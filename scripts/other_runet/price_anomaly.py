from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class PriceContext:
    median: float | None
    average: float | None
    minimum: float | None
    maximum: float | None
    low_threshold: float | None
    high_threshold: float | None
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "median": _round(self.median),
            "average": _round(self.average),
            "min": _round(self.minimum),
            "max": _round(self.maximum),
            "low_threshold": _round(self.low_threshold),
            "high_threshold": _round(self.high_threshold),
            "count": self.count,
        }


@dataclass
class PriceFlag:
    flag: str
    reason: str
    penalty: float

    def to_dict(self) -> dict[str, Any]:
        return {"flag": self.flag, "reason": self.reason, "penalty": round(self.penalty, 3)}


def build_price_context(prices: Iterable[float | None], *, low_factor: float = 0.65, high_factor: float = 1.8) -> PriceContext:
    values = [float(value) for value in prices if value is not None and value > 0]
    if not values:
        return PriceContext(
            median=None,
            average=None,
            minimum=None,
            maximum=None,
            low_threshold=None,
            high_threshold=None,
            count=0,
        )
    sorted_values = sorted(values)
    median_value = statistics.median(sorted_values)
    average_value = statistics.mean(sorted_values)
    return PriceContext(
        median=median_value,
        average=average_value,
        minimum=min(sorted_values),
        maximum=max(sorted_values),
        low_threshold=median_value * low_factor,
        high_threshold=median_value * high_factor,
        count=len(sorted_values),
    )


def evaluate_price(price: float | None, context: PriceContext) -> tuple[float, list[PriceFlag]]:
    flags: list[PriceFlag] = []
    if price is None:
        return 0.3, [PriceFlag("missing_price", "no price extracted", penalty=0.3)]
    if context.count == 0 or context.median is None:
        return 0.7, []
    score = 1.0
    if context.low_threshold is not None and price <= context.low_threshold:
        ratio = max(0.0, 1.0 - price / context.median)
        flags.append(
            PriceFlag(
                flag="suspicious_low_price",
                reason=f"price {price:.0f} is {int(ratio * 100)}% lower than median trusted offers ({context.median:.0f})",
                penalty=0.35,
            )
        )
        score -= 0.35
    if context.high_threshold is not None and price >= context.high_threshold:
        ratio = max(0.0, price / context.median - 1.0)
        flags.append(
            PriceFlag(
                flag="suspicious_high_price",
                reason=f"price {price:.0f} is {int(ratio * 100)}% above median trusted offers ({context.median:.0f})",
                penalty=0.20,
            )
        )
        score -= 0.20
    if context.median:
        deviation = abs(price - context.median) / context.median
        if 0.05 <= deviation <= 0.25:
            score += 0.05
    return max(0.0, min(1.0, score)), flags


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)
