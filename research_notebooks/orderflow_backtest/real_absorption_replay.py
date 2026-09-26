"""Canonical, ledger-first replay primitives for the frozen Absorption BASE run.

This module deliberately accepts canonical ``OrderFlowFrameV1`` frames rather
than legacy ``MarketTick`` rows.  The caller is responsible for supplying
frames produced by the qualified L2+tape builder.  OOS frames are rejected
before signal generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, getcontext
getcontext().prec = 50
from pathlib import Path
from typing import Iterable, Sequence

from .orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from .source_equivalent_adapters import absorption_observe


DECIMAL_ZERO = Decimal("0")


@dataclass(frozen=True)
class ReplayConfig:
    run_id: str
    strategy_id: str = "orderflow.absorption.fade"
    strategy_revision: str = "freeze-2026-09-24-adapter-v1"
    dataset_id: str = "orderflow-binance-futures-14d-20260623-20260706-v1"
    dataset_revision: str = "v1"
    symbol: str = "BTCUSDT"
    is_start_ms: int = 1782172800000
    is_end_ms: int = 1783036799999
    frame_builder_revision: str = "OrderFlowFrameV1-qualified-v1"
    execution_model_revision: str = "execution-model-v1-conservative"
    fee_model: str = "BTCUSDT maker=0.0002 taker=0.0005"
    slippage_model: str = "BASE"
    funding_model: str = "qualified-btcusdt-funding-v1"
    funding_events: tuple[tuple[int, Decimal], ...] = ()
    position_sizing_model: str = "fixed-notional-1000-usd"
    notional: Decimal = Decimal("1000")
    maker_fee_rate: Decimal = Decimal("0.0002")
    taker_fee_rate: Decimal = Decimal("0.0005")
    leverage: Decimal = Decimal("1.0")
    stop_first_rule: str = "STOP_FIRST_CONSERVATIVE"
    frame_stream_hash: str = ""
    maker_queue_limitation: str = "MAKER_QUEUE_NOT_HISTORICALLY_PROVEN"


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    run_id: str
    frame_time_ms: int
    strategy_id: str
    side: str
    reason: str
    price: str
    delta: str
    volume: str
    obi: str
    spread_ticks: str


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    signal_id: str
    decision_time_ms: int
    intended_entry_time_ms: int
    side: str
    quantity: str
    notional: str
    order_type: str
    liquidity_assumption: str


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    order_id: str
    fill_time_ms: int
    price: str
    quantity: str
    liquidity_role: str
    fee: str
    slippage: str


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    run_id: str
    strategy_id: str
    strategy_revision: str
    dataset_id: str
    dataset_revision: str
    symbol: str
    signal_time_ms: int
    decision_time_ms: int
    entry_time_ms: int
    exit_time_ms: int
    side: str
    quantity: str
    notional: str
    entry_price: str
    exit_price: str
    entry_liquidity_role: str
    exit_liquidity_role: str
    gross_pnl: str
    entry_fee: str
    exit_fee: str
    slippage_cost: str
    funding_pnl: str
    net_pnl: str
    entry_reason: str
    exit_reason: str


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()


def artifact_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def assert_is_frame(frame: OrderFlowFrameV1, config: ReplayConfig) -> None:
    if frame.timestamp_ms < config.is_start_ms:
        raise ValueError("frame precedes IS start")
    if frame.timestamp_ms > config.is_end_ms:
        raise ValueError("OOS frame rejected: frame_time > IS_END")


def _dump(path: Path, value: object) -> str:
    path.write_bytes(_canonical_bytes(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _as_strings(record: object) -> dict[str, object]:
    return asdict(record)


def _fee(notional: Decimal, rate: Decimal) -> Decimal:
    return notional * rate


def _signed_gross(side: str, entry: Decimal, exit_price: Decimal, quantity: Decimal) -> Decimal:
    return (exit_price - entry) * quantity if side == "LONG" else (entry - exit_price) * quantity


def aggregate_trades(trades: Sequence[TradeRecord]) -> dict[str, object]:
    values = [Decimal(t.net_pnl) for t in trades]
    gross = sum((Decimal(t.gross_pnl) for t in trades), DECIMAL_ZERO)
    entry_fees = sum((Decimal(t.entry_fee) for t in trades), DECIMAL_ZERO)
    exit_fees = sum((Decimal(t.exit_fee) for t in trades), DECIMAL_ZERO)
    slippage = sum((Decimal(t.slippage_cost) for t in trades), DECIMAL_ZERO)
    funding = sum((Decimal(t.funding_pnl) for t in trades), DECIMAL_ZERO)
    net = sum(values, DECIMAL_ZERO)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    gross_profit = sum((Decimal(t.gross_pnl) for t in trades if Decimal(t.gross_pnl) > 0), DECIMAL_ZERO)
    gross_loss = sum((Decimal(t.gross_pnl) for t in trades if Decimal(t.gross_pnl) < 0), DECIMAL_ZERO)
    equity = DECIMAL_ZERO
    peak = DECIMAL_ZERO
    max_drawdown = DECIMAL_ZERO
    daily: dict[str, Decimal] = {}
    for trade in trades:
        net_trade = Decimal(trade.net_pnl)
        equity += net_trade
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        day = str(__import__("datetime").datetime.fromtimestamp(trade.exit_time_ms / 1000, tz=__import__("datetime").timezone.utc).date())
        daily[day] = daily.get(day, DECIMAL_ZERO) + net_trade
    return {
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(values) - len(wins) - len(losses),
        "long_trades": sum(1 for t in trades if t.side == "LONG"),
        "short_trades": sum(1 for t in trades if t.side == "SHORT"),
        "gross_pnl": str(gross),
        "entry_fees": str(entry_fees),
        "exit_fees": str(exit_fees),
        "fees": str(entry_fees + exit_fees),
        "slippage": str(slippage),
        "funding": str(funding),
        "net_pnl": str(net),
        "expectancy": str(net / len(values)) if values else "0",
        "profit_factor": str(gross_profit / abs(gross_loss)) if gross_loss else ("Infinity" if gross_profit else "0"),
        "max_drawdown": str(max_drawdown),
        "daily_net_pnl": {day: str(value) for day, value in sorted(daily.items())},
    }


class AbsorptionReplay:
    """Replay canonical frames and persist ledgers before aggregation."""

    def __init__(self, config: ReplayConfig, strategy_config: SourceStrategyConfigV1 | None = None):
        self.config = config
        self.strategy_config = strategy_config or SourceStrategyConfigV1()

    def run(self, frames: Iterable[OrderFlowFrameV1], output_dir: Path) -> dict[str, object]:
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_list = list(frames)
        if any(frame_list[i].timestamp_ms > frame_list[i + 1].timestamp_ms for i in range(len(frame_list) - 1)):
            raise ValueError("frames must be chronological")
        for frame in frame_list:
            assert_is_frame(frame, self.config)

        signals: list[SignalRecord] = []
        orders: list[OrderRecord] = []
        fills: list[FillRecord] = []
        trades: list[TradeRecord] = []
        history: list[OrderFlowFrameV1] = []
        cvds: list[Decimal] = []
        cvd = DECIMAL_ZERO
        open_trade: tuple[SignalRecord, OrderRecord, FillRecord] | None = None
        entry_index: int | None = None

        for index, frame in enumerate(frame_list):
            cvd += frame.delta
            signal = absorption_observe(frame, history, cvds, cvd, self.strategy_config)
            if signal.side != "NO_SIGNAL":
                signal_id = f"{self.config.run_id}:signal:{len(signals) + 1}"
                record = SignalRecord(signal_id, self.config.run_id, frame.timestamp_ms, self.config.strategy_id,
                                      signal.side, signal.reason, str(frame.price), str(frame.delta), str(frame.volume),
                                      str(frame.obi), str(frame.spread_ticks))
                signals.append(record)
                if open_trade is None and index + 1 < len(frame_list):
                    entry = frame_list[index + 1]
                    assert_is_frame(entry, self.config)
                    quantity = self.config.notional / entry.price
                    order_id = f"{self.config.run_id}:order:{len(orders) + 1}"
                    order = OrderRecord(order_id, signal_id, frame.timestamp_ms, entry.timestamp_ms, signal.side,
                                        str(quantity), str(self.config.notional), "LIMIT", "ASSUMED_MAKER")
                    fill_id = f"{self.config.run_id}:fill:{len(fills) + 1}"
                    fee = _fee(self.config.notional, self.config.maker_fee_rate)
                    fill = FillRecord(fill_id, order_id, entry.timestamp_ms, str(entry.price), str(quantity),
                                      "MAKER_ASSUMED", str(fee), "0")
                    orders.append(order)
                    fills.append(fill)
                    if open_trade is None:
                        open_trade = (record, order, fill)
                        entry_index = index + 1

            if open_trade is not None and entry_index is not None and index > entry_index:
                signal_record, order, fill = open_trade
                entry_price = Decimal(fill.price)
                stop = entry_price * (Decimal("0.9982") if order.side == "LONG" else Decimal("1.0018"))
                take_profit = entry_price * (Decimal("1.0040") if order.side == "LONG" else Decimal("0.9960"))
                stop_hit = frame.price <= stop if order.side == "LONG" else frame.price >= stop
                take_profit_hit = frame.price >= take_profit if order.side == "LONG" else frame.price <= take_profit
                timed_out = index - entry_index >= 60 or index == len(frame_list) - 1
                if not (stop_hit or take_profit_hit or timed_out):
                    history.append(frame)
                    cvds.append(cvd)
                    continue
                exit_frame = frame
                exit_reason = "STOP" if stop_hit else ("TAKE_PROFIT" if take_profit_hit else "TIME_EXIT")
                exit_price = exit_frame.price
                quantity = Decimal(fill.quantity)
                gross = _signed_gross(order.side, entry_price, exit_price, quantity)
                exit_fee = _fee(self.config.notional, self.config.maker_fee_rate)
                funding = sum((rate * self.config.notional * (Decimal("-1") if order.side == "LONG" else Decimal("1"))
                               for ts, rate in self.config.funding_events if fill.fill_time_ms < ts <= exit_frame.timestamp_ms), DECIMAL_ZERO)
                net = gross - Decimal(fill.fee) - exit_fee + funding
                trade = TradeRecord(
                    f"{self.config.run_id}:trade:{len(trades) + 1}", self.config.run_id, self.config.strategy_id,
                    self.config.strategy_revision, self.config.dataset_id, self.config.dataset_revision,
                    self.config.symbol, signal_record.frame_time_ms, order.decision_time_ms, fill.fill_time_ms,
                    exit_frame.timestamp_ms, order.side, str(quantity), order.notional, fill.price, str(exit_price),
                    fill.liquidity_role, "MAKER_ASSUMED", str(gross), fill.fee, str(exit_fee), fill.slippage,
                    str(funding), str(net), signal_record.reason, exit_reason)
                trades.append(trade)
                open_trade = None
                entry_index = None

            history.append(frame)
            cvds.append(cvd)

        manifest = {
            "runId": self.config.run_id,
            "strategyId": self.config.strategy_id,
            "strategyRevision": self.config.strategy_revision,
            "datasetId": self.config.dataset_id,
            "datasetRevision": self.config.dataset_revision,
            "symbol": self.config.symbol,
            "isStartMs": self.config.is_start_ms,
            "isEndMs": self.config.is_end_ms,
            "frameBuilderRevision": self.config.frame_builder_revision,
            "executionModelRevision": self.config.execution_model_revision,
            "feeModel": self.config.fee_model,
            "slippageModel": self.config.slippage_model,
            "fundingModel": self.config.funding_model,
            "positionSizingModel": self.config.position_sizing_model,
            "runnerRevision": "real-absorption-replay-v1",
            "parameterFingerprint": artifact_hash(asdict(self.strategy_config)),
            "adapterRevision": self.config.strategy_revision,
            "frameStreamHash": self.config.frame_stream_hash,
            "oosEventsConsumed": 0,
            "executionModelLimitation": self.config.maker_queue_limitation,
        }
        signal_rows = [_as_strings(record) for record in signals]
        order_rows = [_as_strings(record) for record in orders]
        fill_rows = [_as_strings(record) for record in fills]
        trade_rows = [_as_strings(record) for record in trades]
        hashes = {
            "signals": _dump(output_dir / "signal_ledger.json", signal_rows),
            "orders": _dump(output_dir / "order_ledger.json", order_rows),
            "fills": _dump(output_dir / "fill_ledger.json", fill_rows),
            "trades": _dump(output_dir / "trade_ledger.json", trade_rows),
        }
        manifest["artifactHashes"] = hashes
        manifest_hash = _dump(output_dir / "run_manifest.json", manifest)
        persisted_trades = [TradeRecord(**row) for row in json.loads((output_dir / "trade_ledger.json").read_text())]
        aggregate = aggregate_trades(persisted_trades)
        aggregate["artifact_hashes"] = hashes
        aggregate["run_manifest_hash"] = manifest_hash
        _dump(output_dir / "aggregate.json", aggregate)
        return {"manifest": manifest, "manifest_hash": manifest_hash, "hashes": hashes, "aggregate": aggregate}
