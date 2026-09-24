"""Provider-neutral, exact-decimal order-flow research contracts.

These contracts describe canonical event-level research state. They do not
submit orders and are deliberately separate from the legacy synthetic
``MarketTick`` fixture used by the engine tests.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple


@dataclass(frozen=True)
class OrderFlowFrameV1:
    timestamp_ms: int
    price: Decimal
    buy: Decimal
    sell: Decimal
    obi: Decimal
    spread_ticks: Decimal

    @property
    def delta(self) -> Decimal:
        return self.buy - self.sell

    @property
    def volume(self) -> Decimal:
        return self.buy + self.sell


@dataclass(frozen=True)
class SourceStrategyConfigV1:
    window: int = 20
    depth: int = 5
    tick_size: Decimal = Decimal("0.01")
    max_gap_ms: int = 60000
    max_book_age_ms: int = 1000
    max_spread_ticks: Decimal = Decimal("1")
    slope_z: Decimal = Decimal("1.5")
    imbalance: Decimal = Decimal("0.65")
    volume_multiple: Decimal = Decimal("2.5")
    aggression_fraction: Decimal = Decimal("0.7")
    absorption_move_ticks: Decimal = Decimal("1")
    confirmation_delta: Decimal = Decimal("1")
    divergence_price_ticks: Decimal = Decimal("1")
    divergence_delta: Decimal = Decimal("1")


@dataclass(frozen=True)
class AbsorptionFrameV1(OrderFlowFrameV1):
    spread_ticks: Decimal


@dataclass(frozen=True)
class SourceSignalV1:
    side: str
    reason: str


def signal(side: str, reason: str) -> SourceSignalV1:
    if side not in {"LONG", "SHORT", "NO_SIGNAL"}:
        raise ValueError("invalid signal side")
    return SourceSignalV1(side, reason)
