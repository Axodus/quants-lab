"""Launcher for EMA 5/9 Pullback Scalper Systematic Optimization Study.

Uses the canonical core/quant_optimization trial engine in quants-lab.
Preserves OOS boundary, validates candidate parameter sets across BTC, ZEC, SUI, WLD.
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from decimal import Decimal

from core.quant_optimization.ema import get_search_space, source_hash, STRATEGY_ID, BASE_REVISION
from core.quant_optimization.models import DatasetRef, Window, OptimizationSpec, Objective, Constraint, canonical
from core.quant_optimization.provenance import file_hash, atomic_json
from core.quant_optimization.runner import CanonicalOptimizationEngine
from core.quant_optimization.governance import ResearchLedger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical-root", type=Path, default=Path("/opt/Axodus/Trading/ema_strategy_dev/data"))
    parser.add_argument("--store", type=Path, default=Path("/opt/Axodus/Trading/quants-lab/research_store"))
    parser.add_argument("--study-id", default="ema-v2-systematic-study-01")
    parser.add_argument("--n-trials", type=int, default=100)
    args = parser.parse_args()

    symbols = ("BTCUSDT", "ZECUSDT", "SUIUSDT", "WLDUSDT")
    refs = []
    for symbol in symbols:
        path = args.historical_root / f"{symbol}_1m_20260623_20260706.json"
        if not path.exists():
            raise FileNotFoundError(f"Missing dataset at {path}")
        refs.append(DatasetRef(
            dataset_id=f"known-ema-candles:{symbol}",
            revision="1",
            symbol=symbol,
            path=str(path.resolve()),
            sha256=file_hash(path),
            provenance="KNOWN_HISTORICAL",
            oos_eligible=False
        ))

    space = get_search_space()
    base = {p.name: p.default for p in space.parameters}

    # Initial seeds to guarantee testing the base v2 and reasonable neighboring sets
    initial = [
        base,
        dict(base, take_profit_bps="25", stop_loss_bps="15"),
        dict(base, take_profit_bps="30", stop_loss_bps="15", cooldown_bars=2),
        dict(base, take_profit_bps="40", stop_loss_bps="20", cooldown_bars=3),
        dict(base, ema_fast=4, ema_slow=12, take_profit_bps="30", stop_loss_bps="15"),
        dict(base, ema_fast=6, ema_slow=15, take_profit_bps="35", stop_loss_bps="20"),
    ]

    # Windows: Partition historical dataset into 2 Search windows and 1 Validation window.
    # Dataset range: 2026-06-23T19:00:00Z to 2026-07-06T23:59:00Z
    windows = (
        Window("search-window-1", "2026-06-23T19:00:00Z", "2026-06-28T00:00:00Z", "SEARCH"),
        Window("search-window-2", "2026-06-28T00:00:00Z", "2026-07-02T00:00:00Z", "SEARCH"),
        Window("validation-window", "2026-07-02T00:00:00Z", "2026-07-06T23:59:00Z", "VALIDATION"),
    )

    spec = OptimizationSpec(
        strategy_id=STRATEGY_ID,
        base_revision=BASE_REVISION,
        strategy_source_hash=source_hash(),
        dataset_refs=tuple(refs),
        symbols=symbols,
        windows=windows,
        search_space=space,
        objective=Objective(name="NET_EXPECTANCY", direction="MAXIMIZE"),
        constraints=(
            Constraint("trade_count", minimum="5"),
        ),
        n_trials=args.n_trials,
        seed=2718,
        code_ref="git:quants-lab@d398189",
        sampler="TPE",
        validation_candidates=5,
        initial_parameter_sets=tuple(initial)
    )

    store = args.store.resolve()
    store.mkdir(parents=True, exist_ok=True)
    
    atomic_json(store / f"{args.study_id}_spec.json", spec)
    print(f"Spec registered with spec_hash={spec.spec_hash}")
    print(f"Launching study {args.study_id} with {args.n_trials} trials across {symbols}...")

    engine = CanonicalOptimizationEngine(store, ResearchLedger(store / "governance"))
    
    # Run full optimization
    manifest, results = engine.optimize(args.study_id, spec)
    print("\n=== STUDY COMPLETED ===")
    print(f"Status: {manifest.get('status')}")
    print(f"Completed Trials: {manifest.get('n_trials_completed')}")
    print(f"Unique Parameter Sets: {manifest.get('unique_parameter_sets')}")
    print(f"Candidates Selected for Evaluation: {len(results)}")

    report = {
        "study_id": args.study_id,
        "manifest": manifest,
        "candidates": results,
    }
    atomic_json(store / f"{args.study_id}_report.json", report)
    print(f"Saved report to {store / f'{args.study_id}_report.json'}")


if __name__ == "__main__":
    main()
