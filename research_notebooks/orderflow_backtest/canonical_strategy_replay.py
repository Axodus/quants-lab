import json, hashlib
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
getcontext().prec = 50
from pathlib import Path
from typing import Sequence, Iterable, Callable
from datetime import datetime, timezone

from .orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1, SourceSignalV1
from .source_equivalent_adapters import momentum_observe, absorption_observe, divergence_observe
from .real_absorption_replay import (
    ReplayConfig, SignalRecord, OrderRecord, FillRecord, TradeRecord,
    artifact_hash, assert_is_frame, _dump, _as_strings,
    _fee, _signed_gross, DECIMAL_ZERO
)

def aggregate_trades(trades: Sequence[TradeRecord]) -> dict[str, object]:
    values = [Decimal(t.net_pnl) for t in trades]
    gross = sum((Decimal(t.gross_pnl) for t in trades), DECIMAL_ZERO)
    entry_fees = sum((Decimal(t.entry_fee) for t in trades), DECIMAL_ZERO)
    exit_fees = sum((Decimal(t.exit_fee) for t in trades), DECIMAL_ZERO)
    slippage = sum((Decimal(t.slippage_cost) for t in trades), DECIMAL_ZERO)
    funding = sum((Decimal(t.funding_pnl) for t in trades), DECIMAL_ZERO)
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
        day = str(datetime.fromtimestamp(trade.exit_time_ms / 1000, tz=timezone.utc).date())
        daily[day] = daily.get(day, DECIMAL_ZERO) + net_trade
    net = sum(daily.values(), DECIMAL_ZERO)
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

STRATEGY_MAP = {
    "orderflow.momentum.aggression": {
        "observe_fn": momentum_observe,
        "is_maker": False,
        "stop_bps": Decimal("20"),
        "tp_bps": Decimal("35"),
        "time_exit": 60,
        "default_config": SourceStrategyConfigV1(window=20, slope_z=Decimal("1.5"), imbalance=Decimal("0.65"), max_spread_ticks=Decimal("1")),
        "limitation": "TAKER_FRICTION_DOMINATED",
    },
    "orderflow.absorption.fade": {
        "observe_fn": absorption_observe,
        "is_maker": True,
        "stop_bps": Decimal("18"),
        "tp_bps": Decimal("40"),
        "time_exit": 60,
        "default_config": SourceStrategyConfigV1(window=20, volume_multiple=Decimal("2.5"), aggression_fraction=Decimal("0.7"), absorption_move_ticks=Decimal("1"), confirmation_delta=Decimal("1"), tick_size=Decimal("0.01"), max_spread_ticks=Decimal("1")),
        "limitation": "MAKER_QUEUE_NOT_HISTORICALLY_PROVEN",
    },
    "orderflow.cvd.divergence.reversal": {
        "observe_fn": divergence_observe,
        "is_maker": False,
        "stop_bps": Decimal("25"),
        "tp_bps": Decimal("50"),
        "time_exit": 60,
        "default_config": SourceStrategyConfigV1(window=20, divergence_price_ticks=Decimal("1"), divergence_delta=Decimal("1"), tick_size=Decimal("0.01"), max_spread_ticks=Decimal("1")),
        "limitation": "TAKER_FRICTION_DOMINATED",
    }
}

class CanonicalStrategyReplay:
    def __init__(self, config: ReplayConfig, strategy_config: SourceStrategyConfigV1 | None = None):
        self.config = config
        strat_info = STRATEGY_MAP[config.strategy_id]
        self.observe_fn = strat_info["observe_fn"]
        self.is_maker = strat_info["is_maker"]
        self.stop_bps = strat_info["stop_bps"]
        self.tp_bps = strat_info["tp_bps"]
        self.time_exit = strat_info["time_exit"]
        self.strategy_config = strategy_config or strat_info["default_config"]
        self.limitation = strat_info["limitation"]

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

        fee_rate = self.config.maker_fee_rate if self.is_maker else self.config.taker_fee_rate
        order_type = "LIMIT" if self.is_maker else "MARKET"
        role = "MAKER_ASSUMED" if self.is_maker else "TAKER"
        stop_mult = self.stop_bps / Decimal("10000")
        tp_mult = self.tp_bps / Decimal("10000")

        for index, frame in enumerate(frame_list):
            cvd += frame.delta
            sig = self.observe_fn(frame, history, cvds, cvd, self.strategy_config)
            if sig.side != "NO_SIGNAL":
                signal_id = f"{self.config.run_id}:signal:{len(signals) + 1}"
                record = SignalRecord(
                    signal_id, self.config.run_id, frame.timestamp_ms, self.config.strategy_id,
                    sig.side, sig.reason, str(frame.price), str(frame.delta), str(frame.volume),
                    str(frame.obi), str(frame.spread_ticks)
                )
                signals.append(record)
                if open_trade is None and index + 1 < len(frame_list):
                    entry = frame_list[index + 1]
                    assert_is_frame(entry, self.config)
                    quantity = self.config.notional / entry.price
                    order_id = f"{self.config.run_id}:order:{len(orders) + 1}"
                    order = OrderRecord(
                        order_id, signal_id, frame.timestamp_ms, entry.timestamp_ms, sig.side,
                        str(quantity), str(self.config.notional), order_type, role
                    )
                    fill_id = f"{self.config.run_id}:fill:{len(fills) + 1}"
                    fee = _fee(self.config.notional, fee_rate)
                    fill = FillRecord(
                        fill_id, order_id, entry.timestamp_ms, str(entry.price), str(quantity),
                        role, str(fee), "0"
                    )
                    orders.append(order)
                    fills.append(fill)
                    open_trade = (record, order, fill)
                    entry_index = index + 1

            if open_trade is not None and entry_index is not None and index > entry_index:
                signal_record, order, fill = open_trade
                entry_price = Decimal(fill.price)
                stop = entry_price * (Decimal("1") - stop_mult) if order.side == "LONG" else entry_price * (Decimal("1") + stop_mult)
                take_profit = entry_price * (Decimal("1") + tp_mult) if order.side == "LONG" else entry_price * (Decimal("1") - tp_mult)
                stop_hit = frame.price <= stop if order.side == "LONG" else frame.price >= stop
                take_profit_hit = frame.price >= take_profit if order.side == "LONG" else frame.price <= take_profit
                timed_out = index - entry_index >= self.time_exit or index == len(frame_list) - 1
                if not (stop_hit or take_profit_hit or timed_out):
                    history.append(frame)
                    cvds.append(cvd)
                    continue
                exit_frame = frame
                exit_reason = "STOP" if stop_hit else ("TAKE_PROFIT" if take_profit_hit else "TIME_EXIT")
                exit_price = exit_frame.price
                quantity = Decimal(fill.quantity)
                gross = _signed_gross(order.side, entry_price, exit_price, quantity)
                exit_fee = _fee(self.config.notional, fee_rate)
                funding = sum((rate * self.config.notional * (Decimal("-1") if order.side == "LONG" else Decimal("1"))
                               for ts, rate in self.config.funding_events if fill.fill_time_ms < ts <= exit_frame.timestamp_ms), DECIMAL_ZERO)
                net = gross - Decimal(fill.fee) - exit_fee + funding
                trade = TradeRecord(
                    f"{self.config.run_id}:trade:{len(trades) + 1}", self.config.run_id, self.config.strategy_id,
                    self.config.strategy_revision, self.config.dataset_id, self.config.dataset_revision,
                    self.config.symbol, signal_record.frame_time_ms, order.decision_time_ms, fill.fill_time_ms,
                    exit_frame.timestamp_ms, order.side, str(quantity), order.notional, fill.price, str(exit_price),
                    fill.liquidity_role, role, str(gross), fill.fee, str(exit_fee), fill.slippage,
                    str(funding), str(net), signal_record.reason, exit_reason
                )
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
            "runnerRevision": "canonical-strategy-replay-v1",
            "parameterFingerprint": artifact_hash(asdict(self.strategy_config)),
            "adapterRevision": self.config.strategy_revision,
            "frameStreamHash": self.config.frame_stream_hash,
            "oosEventsConsumed": 0,
            "executionModelLimitation": self.limitation,
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
