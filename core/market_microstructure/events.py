"""Immutable event-atomic research contracts. Times are integer UTC milliseconds."""
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar
from core.quant_optimization.models import decimal


@dataclass(frozen=True, kw_only=True)
class MarketEvent:
    venue: str
    market_type: str
    symbol: str
    event_time: int
    source_ref: str
    receive_time: int | None = None
    transaction_time: int | None = None
    sequence: int | None = None
    effective_resolution_ms: int = 1
    event_type: ClassVar[str] = 'MARKET'

    def __post_init__(self):
        if not all(isinstance(x, str) and x for x in (self.venue, self.market_type, self.symbol, self.source_ref)):
            raise ValueError('event identity/source required')
        for value in (self.event_time, self.receive_time, self.transaction_time, self.sequence):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('timestamps/sequence require nonnegative integers')
        if type(self.effective_resolution_ms) is not int or self.effective_resolution_ms <= 0:
            raise ValueError('positive source resolution required')


def levels(values):
    result = tuple((side, decimal(price), decimal(qty)) for side, price, qty in values)
    if not result or any(side not in {'bid', 'ask'} or p <= 0 or q < 0 for side, p, q in result):
        raise ValueError('invalid book levels')
    if len({(side,p) for side,p,_ in result}) != len(result):
        raise ValueError('duplicate price level inside atomic event')
    return result


@dataclass(frozen=True, kw_only=True)
class BookSnapshotEvent(MarketEvent):
    levels: tuple
    last_update_id: int
    event_type: ClassVar[str] = 'BOOK_SNAPSHOT'

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, 'levels', levels(self.levels))
        if type(self.last_update_id) is not int or self.last_update_id < 0:
            raise ValueError('snapshot sequence required')


@dataclass(frozen=True, kw_only=True)
class BookDeltaEvent(MarketEvent):
    # All levels in one exchange update MUST be applied atomically.
    levels: tuple
    first_update_id: int
    final_update_id: int
    prev_final_update_id: int
    event_type: ClassVar[str] = 'BOOK_DELTA'

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, 'levels', levels(self.levels))
        if any(type(x) is not int or x < 0 for x in (self.first_update_id, self.final_update_id, self.prev_final_update_id)):
            raise ValueError('update sequence required')
        if self.first_update_id > self.final_update_id or self.prev_final_update_id >= self.final_update_id:
            raise ValueError('invalid update interval')


@dataclass(frozen=True, kw_only=True)
class TradeEvent(MarketEvent):
    price: Decimal
    quantity: Decimal
    aggressor: str
    aggressor_provenance: str
    trade_id: str | None = None
    event_type: ClassVar[str] = 'TRADE'

    def __post_init__(self):
        super().__post_init__()
        for name in ('price', 'quantity'):
            object.__setattr__(self, name, decimal(getattr(self, name)))
            if getattr(self, name) <= 0:
                raise ValueError('positive trade price/quantity required')
        if self.aggressor not in {'BUY', 'SELL', 'UNKNOWN'} or self.aggressor_provenance not in {'OBSERVED', 'INFERRED', 'UNKNOWN'}:
            raise ValueError('aggressor semantics required')
        if (self.aggressor == 'UNKNOWN') != (self.aggressor_provenance == 'UNKNOWN'):
            raise ValueError('aggressor provenance mismatch')


# BBO is derived exclusively from CausalOrderBook.state; no external BBO authority.

@dataclass(frozen=True, kw_only=True)
class MarkPriceEvent(MarketEvent):
    """Derivatives context, not an executable book price; Decimal, not tick-rounded.

    Mark/index values can have finer precision than the instrument's order tick.
    Optional provider fields remain None when absent.
    """
    mark_price: Decimal
    provenance: str
    index_price: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: int | None = None
    event_type: ClassVar[str] = 'MARK_PRICE'

    def __post_init__(self):
        super().__post_init__()
        if self.provenance not in {'OBSERVED', 'INFERRED', 'UNKNOWN'}:
            raise ValueError('mark price provenance required')
        for name in ('mark_price', 'index_price', 'funding_rate'):
            value = getattr(self, name)
            if value is not None:
                value = decimal(value)
                object.__setattr__(self, name, value)
                if name != 'funding_rate' and value <= 0:
                    raise ValueError('invalid mark/index price')
        if self.mark_price is None:
            raise ValueError('mark price required')
        if self.next_funding_time is not None and (type(self.next_funding_time) is not int or self.next_funding_time < 0):
            raise ValueError('invalid next funding time')

    def to_dict(self):
        from core.quant_optimization.models import canonical
        return dict(canonical(self), event_type=self.event_type)

    @classmethod
    def from_record(cls, record, *, timestamp_unit='ms'):
        """Normalize explicit timestamp units; never guess units or missing values.

        Canonical timestamps have millisecond resolution. Sub-ms precision is
        truncated; source_ref remains the authority for the original source row.
        """
        scales = {'ms': 1, 'us': 1000, 'ns': 1000000}
        if timestamp_unit not in scales:
            raise ValueError('unsupported timestamp unit')
        data = dict(record)
        if data.pop('event_type', cls.event_type) != cls.event_type:
            raise ValueError('wrong event type')
        for name in ('event_time', 'receive_time', 'transaction_time', 'next_funding_time'):
            if data.get(name) is not None:
                if type(data[name]) is not int or data[name] < 0:
                    raise ValueError('invalid source timestamp')
                data[name] //= scales[timestamp_unit]
        return cls(**data)
