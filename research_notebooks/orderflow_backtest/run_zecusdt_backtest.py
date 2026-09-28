"""Canonical historical frame generation and backtest replay for ZECUSDT.
Window: 2026-06-23T00:00:00Z to 2026-07-02T23:59:59.999Z (10-day IS window, 14,400 1m frames)
Bootstrap: 2026-06-22 22:00 UTC snapshot
Strategies: Momentum Aggression, Absorption Fade, CVD Divergence Reversal
Friction: 5 bps taker fee, 2 bps maker fee, conservative stop-first execution.
"""
from __future__ import annotations

import sys
import json
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from decimal import Decimal, getcontext
getcontext().prec = 50

RESEARCH_ROOT = Path("/opt/Axodus/Trading/quants-lab/research_notebooks")
if str(RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(RESEARCH_ROOT))

from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.parquet_orderflow_frames import (
    FixedPointCausalParquetFrameBuilder,
    IS_START_MS,
    IS_END_MS,
)
from orderflow_backtest.canonical_strategy_replay import (
    CanonicalStrategyReplay,
    STRATEGY_MAP,
    aggregate_trades,
)
from orderflow_backtest.real_absorption_replay import (
    ReplayConfig,
    _dump,
    artifact_hash,
)
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1

DATA_ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data")
RUN_ROOT = RESEARCH_ROOT / "orderflow_backtest" / "runs" / "zecusdt_historical_is_verified"
RUN_ROOT.mkdir(parents=True, exist_ok=True)

SYMBOL = "ZECUSDT"
STRATEGIES = [
    "orderflow.momentum.aggression",
    "orderflow.absorption.fade",
    "orderflow.cvd.divergence.reversal",
]


def main():
    print(f"=== ZECUSDT CANONICAL HISTORICAL REPLAY ===", flush=True)
    spec_path = RESEARCH_ROOT / "orderflow_backtest" / "instrument-specs" / f"{SYMBOL}.json"
    spec = InstrumentSpec.load(spec_path)
    InstrumentSpec.register(spec)
    print(f"Loaded spec: {spec.symbol} tick={spec.tick_size} step={spec.step_size} price_scale={spec.price_scale} qty_scale={spec.quantity_scale}", flush=True)

    # 1. Build Causal Frames
    frames_file = RUN_ROOT / "frames.jsonl"
    stats_file = RUN_ROOT / "frame_stats.json"
    if frames_file.exists() and stats_file.exists():
        print(f"Reusing existing qualified frames from {frames_file}...", flush=True)
        frames = []
        for line in frames_file.read_text().splitlines():
            data = json.loads(line)
            frames.append(OrderFlowFrameV1(
                timestamp_ms=int(data["timestamp_ms"]),
                price=Decimal(data["price"]), buy=Decimal(data["buy"]), sell=Decimal(data["sell"]),
                obi=Decimal(data["obi"]), spread_ticks=Decimal(data["spread_ticks"]),
                best_bid=Decimal(data.get("best_bid", "0")), best_ask=Decimal(data.get("best_ask", "0")),
                bid_depth=Decimal(data.get("bid_depth", "0")), ask_depth=Decimal(data.get("ask_depth", "0")),
                depth_skew=Decimal(data.get("depth_skew", "0")),
                absolute_delta=Decimal(data.get("absolute_delta", "0")), cvd=Decimal(data.get("cvd", "0")),
                price_displacement_ticks=Decimal(data.get("price_displacement_ticks", "0")),
                causal_trailing_high=Decimal(data.get("causal_trailing_high", "0")),
                causal_trailing_low=Decimal(data.get("causal_trailing_low", "0")),
            ))
        frame_stream_hash = json.loads(stats_file.read_text())["frame_stream_hash"]
    else:
        print(f"Building 14,400 causal 1-minute frames for {SYMBOL}...", flush=True)
        t0 = time.time()
        builder = FixedPointCausalParquetFrameBuilder(
            data_root=DATA_ROOT, symbol=SYMBOL, instrument_spec=spec,
            is_start_ms=IS_START_MS, is_end_ms=IS_END_MS,
        )
        frames, stats = builder.build_compiled()
        print(f"Frame generation complete for {SYMBOL} in {time.time() - t0:.2f}s:", flush=True)
        print(f"  Total generated frames : {len(frames)}", flush=True)
        print(f"  Qualified frames       : {stats.qualified_frames}", flush=True)
        print(f"  Missing frames         : {stats.missing_frames}", flush=True)
        print(f"  Crossed books          : {stats.crossed_books}", flush=True)
        print(f"  Empty books            : {stats.empty_books}", flush=True)
        print(f"  L2 rows processed      : {stats.l2_rows_processed:,}", flush=True)
        print(f"  Trade rows processed   : {stats.trade_rows_processed:,}", flush=True)
        print(f"  Frame stream hash      : {stats.frame_stream_hash}", flush=True)
        with frames_file.open("w") as f:
            for frame in frames:
                f.write(json.dumps(frame.__dict__, default=str, sort_keys=True) + "\n")
        stats_file.write_text(json.dumps(stats.__dict__, indent=2, default=str) + "\n")
        frame_stream_hash = stats.frame_stream_hash

    # 2. Strategy Replays
    results = {}
    for strat_id in STRATEGIES:
        strat_slug = strat_id.split(".")[-1]
        strat_dir = RUN_ROOT / strat_slug
        strat_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n--- Running Strategy: {strat_id} ---", flush=True)
        strat_info = STRATEGY_MAP[strat_id]
        strat_cfg = strat_info["default_config"]
        if hasattr(strat_cfg, "tick_size"):
            strat_cfg = replace(strat_cfg, tick_size=spec.tick_size)

        replay_cfg = ReplayConfig(
            run_id=f"canonical_is_{SYMBOL.lower()}_{strat_slug}",
            strategy_id=strat_id,
            symbol=SYMBOL,
            frame_stream_hash=frame_stream_hash,
            taker_fee_rate=Decimal("0.0005"),  # 5 bps taker fee
            maker_fee_rate=Decimal("0.0002"),  # 2 bps maker fee
            notional=Decimal("100.0"),         # $100 notional per trade
        )

        replay = CanonicalStrategyReplay(config=replay_cfg, strategy_config=strat_cfg)
        summary = replay.run(frames, strat_dir)
        summary["limitation"] = strat_info["limitation"]
        results[strat_id] = summary

        agg = summary.get("aggregate", {})
        trade_count = agg.get("trade_count", 0)
        wins = agg.get("wins", 0)
        win_rate = (wins / trade_count * 100) if trade_count else 0.0
        print(f"  Trades      : {trade_count} (Wins: {wins}, Losses: {agg.get('losses', 0)})", flush=True)
        print(f"  Win Rate    : {win_rate:.2f}%", flush=True)
        print(f"  Gross PnL   : ${float(agg.get('gross_pnl', 0)):+,.4f}", flush=True)
        print(f"  Fees Paid   : ${float(agg.get('fees', 0)):,.4f}", flush=True)
        print(f"  Net PnL     : ${float(agg.get('net_pnl', 0)):+,.4f}", flush=True)
        print(f"  Max DD      : ${float(agg.get('max_drawdown', 0)):,.4f}", flush=True)
        print(f"  Profit Fac  : {agg.get('profit_factor')}", flush=True)

    summary_file = RUN_ROOT / "zecusdt_backtest_summary.json"
    summary_file.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nExecution complete. Summary persisted to {summary_file}", flush=True)


if __name__ == "__main__":
    main()
