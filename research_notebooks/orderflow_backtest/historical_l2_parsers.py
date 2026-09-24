"""Historical Level-2 market data parsers and qualification engines for Order Flow research."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from orderflow_backtest.historical_data_v1 import (
    BookStateV1,
    GapClassification,
    LevelV1,
    OrderBookUpdateV1,
    TradePrintV1,
    detect_sequence_gap,
    reconstruct_book,
    signed_trade_volume,
)
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1, SourceSignalV1
from orderflow_backtest.source_equivalent_adapters import (
    momentum_observe,
    absorption_observe,
    divergence_observe,
)


@dataclass(frozen=True)
class OrderBookSnapshotV1:
    provider: str
    venue: str
    symbol: str
    event_time_ms: int
    sequence: int | None
    bids: tuple[LevelV1, ...]
    asks: tuple[LevelV1, ...]
    source_file: str

    @property
    def book_state(self) -> BookStateV1:
        return BookStateV1(
            symbol=self.symbol,
            event_time_ms=self.event_time_ms,
            sequence=self.sequence,
            bids=self.bids,
            asks=self.asks,
        )


class TardisSnapshot25Parser:
    """Parses Tardis book_snapshot_25 CSV rows."""

    @staticmethod
    def parse_row(row: Mapping[str, str], source_file: str = "tardis_snapshot_25") -> OrderBookSnapshotV1:
        symbol = row["symbol"].upper()
        # timestamp in tardis is in microseconds
        event_time_ms = int(row["timestamp"]) // 1000
        bids: list[LevelV1] = []
        asks: list[LevelV1] = []
        for i in range(25):
            bp_key, ba_key = f"bids[{i}].price", f"bids[{i}].amount"
            ap_key, aa_key = f"asks[{i}].price", f"asks[{i}].amount"
            if bp_key in row and row[bp_key] and ba_key in row and row[ba_key]:
                bids.append(LevelV1(Decimal(row[bp_key]), Decimal(row[ba_key])))
            if ap_key in row and row[ap_key] and aa_key in row and row[aa_key]:
                asks.append(LevelV1(Decimal(row[ap_key]), Decimal(row[aa_key])))
        return OrderBookSnapshotV1(
            provider="tardis",
            venue="binance-futures",
            symbol=symbol,
            event_time_ms=event_time_ms,
            sequence=None,
            bids=tuple(bids),
            asks=tuple(asks),
            source_file=source_file,
        )

    @classmethod
    def parse_csv(cls, text: str, source_file: str = "tardis_snapshot_25") -> tuple[OrderBookSnapshotV1, ...]:
        reader = csv.DictReader(io.StringIO(text.strip()))
        return tuple(cls.parse_row(r, source_file) for r in reader)


class TardisIncrementalL2Parser:
    """Parses Tardis incremental_book_L2 CSV rows."""

    @staticmethod
    def parse_csv(text: str, source_file: str = "tardis_incremental_l2") -> tuple[OrderBookUpdateV1, ...]:
        reader = csv.DictReader(io.StringIO(text.strip()))
        grouped_updates: list[OrderBookUpdateV1] = []
        current_time_ms: int | None = None
        current_is_snapshot: bool = False
        current_symbol: str = ""
        current_bids: list[LevelV1] = []
        current_asks: list[LevelV1] = []

        def flush():
            nonlocal current_time_ms, current_is_snapshot, current_symbol, current_bids, current_asks
            if current_time_ms is not None and (current_bids or current_asks):
                update_type = "snapshot" if current_is_snapshot else "delta"
                grouped_updates.append(
                    OrderBookUpdateV1(
                        provider="tardis",
                        venue="binance-futures",
                        symbol=current_symbol,
                        event_time_ms=current_time_ms,
                        sequence=None,
                        bids=tuple(current_bids),
                        asks=tuple(current_asks),
                        update_type=update_type,
                        source_file=source_file,
                    )
                )
                current_bids = []
                current_asks = []

        for row in reader:
            time_ms = int(row["timestamp"]) // 1000
            is_snap = row["is_snapshot"].strip().lower() in {"true", "1"}
            sym = row["symbol"].upper()
            side = row["side"].strip().lower()
            price = Decimal(row["price"])
            amount = Decimal(row["amount"])
            lvl = LevelV1(price, amount)

            if current_time_ms is not None and (time_ms != current_time_ms or is_snap != current_is_snapshot):
                flush()
            current_time_ms = time_ms
            current_is_snapshot = is_snap
            current_symbol = sym
            if side == "bid":
                current_bids.append(lvl)
            else:
                current_asks.append(lvl)
        flush()
        return tuple(grouped_updates)


class BinanceNativeL2Parser:
    """Parses native Binance WebSocket and REST payloads."""

    @staticmethod
    def parse_rest_snapshot(payload: Mapping[str, object], symbol: str, source_file: str = "binance_rest") -> OrderBookSnapshotV1:
        last_update_id = int(payload["lastUpdateId"])
        event_time_ms = int(payload.get("E", payload.get("T", 0)))
        bids = tuple(LevelV1(Decimal(p), Decimal(q)) for p, q in payload.get("bids", []))
        asks = tuple(LevelV1(Decimal(p), Decimal(q)) for p, q in payload.get("asks", []))
        return OrderBookSnapshotV1(
            provider="binance",
            venue="binance-futures",
            symbol=symbol.upper(),
            event_time_ms=event_time_ms,
            sequence=last_update_id,
            bids=bids,
            asks=asks,
            source_file=source_file,
        )

    @staticmethod
    def parse_ws_depth_update(payload: Mapping[str, object], source_file: str = "binance_ws") -> OrderBookUpdateV1:
        symbol = str(payload["s"]).upper()
        event_time_ms = int(payload["E"])
        u = int(payload["u"])
        pu = int(payload["pu"])
        bids = tuple(LevelV1(Decimal(p), Decimal(q)) for p, q in payload.get("b", []))
        asks = tuple(LevelV1(Decimal(p), Decimal(q)) for p, q in payload.get("a", []))
        return OrderBookUpdateV1(
            provider="binance",
            venue="binance-futures",
            symbol=symbol,
            event_time_ms=event_time_ms,
            sequence=u,
            bids=bids,
            asks=asks,
            update_type="delta",
            source_file=source_file,
            previous_sequence=pu,
        )


def calculate_obi(bids: Sequence[LevelV1], asks: Sequence[LevelV1], depth: int = 5) -> Decimal:
    top_bids = bids[:depth]
    top_asks = asks[:depth]
    bid_vol = sum((lvl.quantity for lvl in top_bids), Decimal(0))
    ask_vol = sum((lvl.quantity for lvl in top_asks), Decimal(0))
    total_vol = bid_vol + ask_vol
    if total_vol == 0:
        return Decimal(0)
    return (bid_vol - ask_vol) / total_vol


def calculate_depth_skew(bids: Sequence[LevelV1], asks: Sequence[LevelV1], depth: int = 5) -> Decimal:
    top_bids = bids[:depth]
    top_asks = asks[:depth]
    bid_vol = sum((lvl.quantity for lvl in top_bids), Decimal(0))
    ask_vol = sum((lvl.quantity for lvl in top_asks), Decimal(0))
    if ask_vol == 0:
        return Decimal(999)
    return bid_vol / ask_vol


def calculate_spread_ticks(best_bid: Decimal, best_ask: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    return (best_ask - best_bid) / tick_size


def reconcile_book_states(reconstructed: BookStateV1, snapshot: OrderBookSnapshotV1, max_levels: int = 25) -> bool:
    """Reconcile an incremental reconstructed book against a fresh snapshot."""
    if reconstructed.symbol != snapshot.symbol:
        return False
    r_bids = {lvl.price: lvl.quantity for lvl in reconstructed.bids[:max_levels]}
    r_asks = {lvl.price: lvl.quantity for lvl in reconstructed.asks[:max_levels]}
    s_bids = {lvl.price: lvl.quantity for lvl in snapshot.bids[:max_levels]}
    s_asks = {lvl.price: lvl.quantity for lvl in snapshot.asks[:max_levels]}
    return r_bids == s_bids and r_asks == s_asks


def compose_orderflow_frame(
    book: BookStateV1,
    trades: Sequence[TradePrintV1],
    tick_size: Decimal = Decimal("0.01"),
    depth: int = 5,
) -> OrderFlowFrameV1:
    """Composes causal L2 book state + contemporaneous trades into an OrderFlowFrameV1."""
    causal_trades = tuple(t for t in trades if t.symbol == book.symbol and t.event_time_ms <= book.event_time_ms)
    buy_vol = sum((t.quantity for t in causal_trades if t.aggressor_side == "BUY"), Decimal(0))
    sell_vol = sum((t.quantity for t in causal_trades if t.aggressor_side == "SELL"), Decimal(0))
    obi = calculate_obi(book.bids, book.asks, depth=depth)
    spread_ticks = calculate_spread_ticks(book.best_bid.price, book.best_ask.price, tick_size=tick_size)
    mid_price = (book.best_bid.price + book.best_ask.price) / Decimal("2")
    return OrderFlowFrameV1(
        timestamp_ms=book.event_time_ms,
        price=mid_price,
        buy=buy_vol,
        sell=sell_vol,
        obi=obi,
        spread_ticks=spread_ticks,
    )


def verify_adapter_compatibility(
    frames: Sequence[OrderFlowFrameV1],
    config: SourceStrategyConfigV1 | None = None,
) -> dict[str, bool]:
    """Verifies semantic input compatibility with all three frozen Order Flow adapters."""
    cfg = config or SourceStrategyConfigV1()
    results = {}
    cvds: list[Decimal] = []
    current_cvd = Decimal(0)
    for f in frames:
        current_cvd += f.delta
        cvds.append(current_cvd)

    if not frames:
        return {"momentum": False, "absorption": False, "divergence": False}

    history = frames[:-1]
    latest = frames[-1]
    prior_cvds = cvds[:-1]
    latest_cvd = cvds[-1]

    # 1. Momentum
    try:
        mom_sig = momentum_observe(latest, history, prior_cvds, latest_cvd, cfg)
        results["momentum"] = isinstance(mom_sig, SourceSignalV1)
    except Exception:
        results["momentum"] = False

    # 2. Absorption
    try:
        abs_sig = absorption_observe(latest, history, prior_cvds, latest_cvd, cfg)
        results["absorption"] = isinstance(abs_sig, SourceSignalV1)
    except Exception:
        results["absorption"] = False

    # 3. Divergence
    try:
        div_sig = divergence_observe(latest, history, prior_cvds, latest_cvd, cfg)
        results["divergence"] = isinstance(div_sig, SourceSignalV1)
    except Exception:
        results["divergence"] = False

    return results
