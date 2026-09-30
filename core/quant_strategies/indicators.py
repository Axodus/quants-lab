from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Sequence


def to_decimal(value: Any, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def wma(values: Sequence[Any], period: int = 50) -> Decimal:
    """Exact Decimal WMA; latest completed sample receives the largest weight."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        raise ValueError(f"at least {period} values required for WMA{period}")
    window = [to_decimal(value, "wma value") for value in values[-period:]]
    total_weight = Decimal(period * (period + 1) // 2)
    weighted_sum = sum(
        (Decimal(index + 1) * price for index, price in enumerate(window)),
        Decimal("0"),
    )
    return weighted_sum / total_weight


def ema(values: Sequence[Any], period: int) -> Decimal:
    """Exact Decimal EMA initialized from the first-period simple average."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        raise ValueError(f"at least {period} values required for EMA{period}")
    parsed = [to_decimal(value, "ema value") for value in values]
    multiplier = Decimal("2") / Decimal(period + 1)
    current = sum(parsed[:period], Decimal("0")) / Decimal(period)
    for price in parsed[period:]:
        current = price * multiplier + current * (Decimal("1") - multiplier)
    return current


def step_bps(current: Decimal, previous: Decimal) -> Decimal:
    """Percentage change from previous to current MA in basis points."""
    if previous == 0:
        raise ValueError("previous moving average value must be non-zero")
    return (current - previous) / previous * Decimal("10000")


def distance_bps(price: Decimal, reference: Decimal) -> Decimal:
    """Unsigned price-to-moving-average distance in basis points."""
    if reference <= 0:
        raise ValueError("reference moving average must be positive")
    return abs(price - reference) / reference * Decimal("10000")
