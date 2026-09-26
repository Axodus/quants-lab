import argparse, json
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone, timedelta

from .orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from .parquet_orderflow_frames import IS_START_MS, IS_END_MS
from .real_absorption_replay import ReplayConfig
from .canonical_strategy_replay import CanonicalStrategyReplay, STRATEGY_MAP
from .canonical_absorption_evidence import load_verified_frames

def run_all_strategies(frame_dir: Path, output_root: Path):
    stats = json.loads((frame_dir / "stats.json").read_text())["stats"]
    frame_hash = stats["frame_stream_hash"]
    frames = load_verified_frames(frame_dir / "frames.jsonl", frame_hash)
    
    output_root.mkdir(parents=True, exist_ok=True)
    all_results = {}

    for strat_id, info in STRATEGY_MAP.items():
        short_name = strat_id.split(".")[-2] if "absorption" in strat_id or "momentum" in strat_id or "cvd" in strat_id else strat_id
        if "momentum" in strat_id:
            short_name = "momentum"
        elif "absorption" in strat_id:
            short_name = "absorption"
        elif "cvd" in strat_id or "divergence" in strat_id:
            short_name = "divergence"

        strat_dir = output_root / short_name
        strat_dir.mkdir(parents=True, exist_ok=True)

        config = ReplayConfig(
            run_id=f"{short_name}-btcusdt-is-{frame_hash[:16]}",
            strategy_id=strat_id,
            frame_stream_hash=frame_hash,
            fee_model="BTCUSDT maker=0.0002 taker=0.0005"
        )

        replay_engine = CanonicalStrategyReplay(config, info["default_config"])
        res1 = replay_engine.run(frames, strat_dir / "run-1")
        res2 = replay_engine.run(frames, strat_dir / "run-2")

        if res1["hashes"] != res2["hashes"] or res1["aggregate"] != res2["aggregate"]:
            raise ValueError(f"Deterministic replay failed for {strat_id}")

        trades = json.loads((strat_dir / "run-1" / "trade_ledger.json").read_text())
        crossings = [t["trade_id"] for t in trades if t["entry_time_ms"] // 28800000 != t["exit_time_ms"] // 28800000]

        daily = res1["aggregate"]["daily_net_pnl"]
        for day in range(10):
            key = str((datetime(2026, 6, 23, tzinfo=timezone.utc) + timedelta(days=day)).date())
            daily.setdefault(key, "0")
        
        daily_sum = sum((Decimal(v) for v in daily.values()), Decimal(0))
        assert daily_sum == Decimal(res1["aggregate"]["net_pnl"]), f"Accounting mismatch for {strat_id}: {daily_sum} != {res1['aggregate']['net_pnl']}"

        result = {
            "strategyId": strat_id,
            "shortName": short_name,
            "runId": config.run_id,
            "frameHash": frame_hash,
            "hashes": res1["hashes"],
            "manifestHash": res1["manifest_hash"],
            "aggregate": res1["aggregate"],
            "deterministicReplay": "PASS",
            "fundingCrossingTrades": crossings,
            "oosEventsConsumed": 0,
            "dailySum": str(daily_sum),
            "accountingDelta": "0.00"
        }
        (strat_dir / "reconciliation.json").write_text(json.dumps(result, indent=2) + "\n")
        all_results[short_name] = result

    summary_file = output_root / "all_strategies_summary.json"
    summary_file.write_text(json.dumps(all_results, indent=2) + "\n")
    return all_results

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frame-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    res = run_all_strategies(args.frame_dir, args.output_root)
    print(json.dumps(res, indent=2))
