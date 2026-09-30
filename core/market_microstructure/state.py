"""Read-only view of the canonical native book."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OrderBookState:
    bid_levels: tuple = ()
    ask_levels: tuple = ()
    timestamp: int | None = None
    sequence: int | None = None
    validity_state: str = 'INVALID_BOOTSTRAP'

    @property
    def best_bid(self):
        return self.bid_levels[0][0] if self.bid_levels else None

    @property
    def best_ask(self):
        return self.ask_levels[0][0] if self.ask_levels else None

    @property
    def spread(self):
        return self.best_ask - self.best_bid if self.bid_levels and self.ask_levels else None

    @property
    def mid_price(self):
        return (self.best_ask + self.best_bid) / Decimal(2) if self.bid_levels and self.ask_levels else None
