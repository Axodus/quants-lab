"""Canonical historical L2/tape contracts and fail-closed qualification helpers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping


class GapClassification(StrEnum):
    NO_GAP = "NO_GAP"
    RECOVERABLE_GAP = "RECOVERABLE_GAP"
    UNRECOVERABLE_GAP = "UNRECOVERABLE_GAP"
    SEQUENCE_RESET = "SEQUENCE_RESET"
    UNKNOWN_SEQUENCE_STATE = "UNKNOWN_SEQUENCE_STATE"


class QualificationState(StrEnum):
    QUALIFIED = "QUALIFIED"
    QUARANTINED = "QUARANTINED"
    PARTIAL = "PARTIAL"
    UNUSABLE = "UNUSABLE"


@dataclass(frozen=True)
class LevelV1:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class OrderBookUpdateV1:
    provider: str
    venue: str
    symbol: str
    event_time_ms: int
    sequence: int | None
    bids: tuple[LevelV1, ...]
    asks: tuple[LevelV1, ...]
    update_type: str
    source_file: str
    previous_sequence: int | None = None


@dataclass(frozen=True)
class TradePrintV1:
    provider: str
    venue: str
    symbol: str
    event_time_ms: int
    trade_id: str | None
    price: Decimal
    quantity: Decimal
    aggressor_side: str
    source_file: str


@dataclass(frozen=True)
class FundingEventV1:
    provider: str
    venue: str
    symbol: str
    event_time_ms: int
    rate: Decimal
    source_file: str


@dataclass(frozen=True)
class InstrumentMetadataV1:
    provider: str
    venue: str
    symbol: str
    effective_from_ms: int
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal | None
    min_notional: Decimal | None
    contract_type: str
    quote_asset: str
    settlement_asset: str
    source_file: str


@dataclass(frozen=True)
class GapRecordV1:
    symbol: str
    start_time_ms: int
    end_time_ms: int
    expected_sequence: int | None
    observed_sequence: int | None
    classification: GapClassification
    recovery_action: str


@dataclass(frozen=True)
class HistoricalReplayReadinessV1:
    dataset_accepted: bool
    l2_qualified: bool
    tape_qualified: bool
    funding_qualified: bool
    metadata_qualified: bool
    causality_qualified: bool

    @property
    def qualified(self) -> bool:
        return all(asdict(self).values())


@dataclass(frozen=True)
class BookStateV1:
    symbol: str
    event_time_ms: int
    sequence: int | None
    bids: tuple[LevelV1, ...]
    asks: tuple[LevelV1, ...]

    @property
    def best_bid(self) -> LevelV1:
        return self.bids[0]

    @property
    def best_ask(self) -> LevelV1:
        return self.asks[0]

    @property
    def spread(self) -> Decimal:
        return self.best_ask.price - self.best_bid.price

    @property
    def valid(self) -> bool:
        return bool(self.bids and self.asks and self.best_bid.price < self.best_ask.price and all(x.quantity > 0 for x in (*self.bids, *self.asks)))


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_fingerprint(manifest: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(manifest).encode()).hexdigest()


def detect_sequence_gap(previous: int | None, current: int | None) -> GapClassification:
    if previous is None or current is None:
        return GapClassification.UNKNOWN_SEQUENCE_STATE
    if current == previous + 1:
        return GapClassification.NO_GAP
    if current <= previous:
        return GapClassification.SEQUENCE_RESET if current == 0 else GapClassification.UNRECOVERABLE_GAP
    return GapClassification.UNRECOVERABLE_GAP


def reconstruct_book(updates: Iterable[OrderBookUpdateV1]) -> tuple[BookStateV1, ...]:
    books: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    result: list[BookStateV1] = []
    previous_sequence: int | None = None
    for update in updates:
        gap = detect_sequence_gap(previous_sequence, update.sequence) if previous_sequence is not None else GapClassification.NO_GAP
        if gap not in {GapClassification.NO_GAP}:
            raise ValueError(f"cannot reconstruct across {gap}")
        if update.update_type == "snapshot":
            books = {level.price: level.quantity for level in update.bids}
            asks = {level.price: level.quantity for level in update.asks}
        elif update.update_type == "delta":
            for level in update.bids:
                if level.quantity == 0:
                    books.pop(level.price, None)
                else:
                    books[level.price] = level.quantity
            for level in update.asks:
                if level.quantity == 0:
                    asks.pop(level.price, None)
                else:
                    asks[level.price] = level.quantity
        else:
            raise ValueError(f"unsupported update type: {update.update_type}")
        state = BookStateV1(update.symbol, update.event_time_ms, update.sequence,
                            tuple(sorted((LevelV1(p, q) for p, q in books.items()), key=lambda x: x.price, reverse=True)),
                            tuple(sorted((LevelV1(p, q) for p, q in asks.items()), key=lambda x: x.price)))
        if not state.valid:
            raise ValueError("invalid, locked, or crossed book")
        result.append(state)
        previous_sequence = update.sequence
    return tuple(result)


def signed_trade_volume(trades: Iterable[TradePrintV1]) -> Decimal:
    total = Decimal(0)
    for trade in trades:
        if trade.aggressor_side == "BUY":
            total += trade.quantity
        elif trade.aggressor_side == "SELL":
            total -= trade.quantity
        else:
            raise ValueError("unknown aggressor side")
    return total


def align_tape_to_book(book: BookStateV1, trades: Iterable[TradePrintV1]) -> tuple[TradePrintV1, ...]:
    return tuple(trade for trade in trades if trade.symbol == book.symbol and trade.event_time_ms <= book.event_time_ms)
