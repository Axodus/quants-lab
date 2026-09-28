"""Full end-to-end historical frame generation and backtest runner for Option A cohort.
Symbols: ZECUSDT, SUIUSDT, WLDUSDT
Window: 2026-06-23 to 2026-07-06 (10-day IS: 2026-06-23 to 2026-07-02 23:59:59.999Z)
Strategies: Momentum, Absorption, Divergence
"""
import sys
import json
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from decimal import Decimal, getcontext
getcontext().prec = 50

# Ensure package root is in path
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
RUN_ROOT = RESEARCH_ROOT / "orderflow_backtest" / "runs" / "option_a_historical_is_verified"
RUN_ROOT.mkdir(parents=True, exist_ok=True)

QUALIFIED_START_SUI_MS = 1782238800000  # 2026-06-23T18:20:00Z
SYMBOLS = {
    "ZECUSDT": {"start_ms": IS_START_MS, "expected_frames": 14400, "causal_coverage_pct": 100.0, "status": "BOOTSTRAP_QUALIFIED"},
    "SUIUSDT": {"start_ms": QUALIFIED_START_SUI_MS, "expected_frames": 13300, "causal_coverage_pct": 92.3641, "status": "BOOTSTRAP_PARTIAL"},
    "WLDUSDT": {"start_ms": QUALIFIED_START_SUI_MS, "expected_frames": 13300, "causal_coverage_pct": 92.3641, "status": "BOOTSTRAP_PARTIAL"},
}
STRATEGIES = [
    "orderflow.momentum.aggression",
    "orderflow.absorption.fade",
    "orderflow.cvd.divergence.reversal",
]


def run():
    summary_results = {}

    for sym, sym_info in SYMBOLS.items():
        print(f"\n=======================================================", flush=True)
        print(f"PROCESSING SYMBOL: {sym}", flush=True)
        print(f"=======================================================", flush=True)

        spec_path = RESEARCH_ROOT / "orderflow_backtest" / "instrument-specs" / f"{sym}.json"
        spec = InstrumentSpec.load(spec_path)
        InstrumentSpec.register(spec)
        print(f"Loaded spec: {spec.symbol} tick={spec.tick_size} step={spec.step_size} price_scale={spec.price_scale} qty_scale={spec.quantity_scale}", flush=True)

        sym_dir = RUN_ROOT / sym
        sym_dir.mkdir(parents=True, exist_ok=True)
        stats_file = sym_dir / "frame_stats.json"
        frames_file = sym_dir / "frames.jsonl"

        # 1. Build causal frames (or load if already materialized and matches expected frame count)
        if frames_file.exists() and stats_file.exists():
            print(f"Reusing existing qualified frames for {sym}...", flush=True)
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
            stats_data = json.loads(stats_file.read_text())
            frame_stream_hash = stats_data["frame_stream_hash"]
        else:
            print(f"Building {sym_info['expected_frames']} causal 1-minute frames for {sym}...", flush=True)
            t0 = time.time()
            builder = FixedPointCausalParquetFrameBuilder(
                data_root=DATA_ROOT,
                symbol=sym,
                instrument_spec=spec,
                is_start_ms=sym_info["start_ms"],
                is_end_ms=IS_END_MS,
            )
            frames, stats = builder.build_compiled()
            elapsed = time.time() - t0
            print(f"Frames generated for {sym} in {elapsed:.2f}s: {len(frames)} (qualified={stats.qualified_frames}, hash={stats.frame_stream_hash[:16]}...)", flush=True)
            if len(frames) != sym_info["expected_frames"]:
                raise RuntimeError(f"FRAME_COUNT_MISMATCH for {sym}: expected {sym_info['expected_frames']}, got {len(frames)}")
            with frames_file.open("w") as f:
                for frame in frames:
                    f.write(json.dumps(frame.__dict__, default=str, sort_keys=True) + "\n")
            stats_file.write_text(json.dumps(stats.__dict__, indent=2, default=str) + "\n")
            frame_stream_hash = stats.frame_stream_hash

        summary_results[sym] = {
            "symbol": sym,
            "bootstrap_status": sym_info["status"],
            "causal_coverage_pct": sym_info["causal_coverage_pct"],
            "frames_count": len(frames),
            "frame_stream_hash": frame_stream_hash,
            "strategies": {}
        }

        # 2. Run Strategy Replays
        for strat_id in STRATEGIES:
            strat_slug = strat_id.split(".")[-1]
            strat_dir = sym_dir / strat_slug
            strat_dir.mkdir(parents=True, exist_ok=True)

            print(f"  -> Running strategy {strat_id} on {sym}...", flush=True)
            strat_info = STRATEGY_MAP[strat_id]
            strat_cfg = strat_info["default_config"]
            if hasattr(strat_cfg, "tick_size"):
                strat_cfg = replace(strat_cfg, tick_size=spec.tick_size)

            replay_cfg = ReplayConfig(
                run_id=f"canonical_is_{sym.lower()}_{strat_slug}",
                strategy_id=strat_id,
                symbol=sym,
                is_start_ms=sym_info["start_ms"],
                frame_stream_hash=frame_stream_hash,
                taker_fee_rate=Decimal("0.0005"), # 5 bps
                maker_fee_rate=Decimal("0.0002"), # 2 bps
                notional=Decimal("100.0"),
            )

            replay = CanonicalStrategyReplay(config=replay_cfg, strategy_config=strat_cfg)
            result = replay.run(frames, strat_dir)

            summary_results[sym]["strategies"][strat_id] = result
            agg = result.get("aggregate", {})
            tc = agg.get("trade_count", 0)
            wr = float(agg.get("wins", 0)) / tc * 100 if tc else 0.0
            print(f"     Trades: {tc}, WinRate: {wr:.1f}%, Net PnL: ${float(agg.get('net_pnl', 0)):+.4f}, Gross: ${float(agg.get('gross_pnl', 0)):+.4f}, Fees: ${float(agg.get('fees', 0)):.4f}", flush=True)

    summary_file = RUN_ROOT / "option_a_backtest_summary.json"
    summary_file.write_text(json.dumps(summary_results, indent=2, default=str) + "\n")
    print(f"\nALL RUNS COMPLETE! Summary saved to {summary_file}", flush=True)


if __name__ == "__main__":
    run()
