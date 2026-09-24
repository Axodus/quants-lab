# Orderflow Backtest Validation Package

This directory is a reproducible **validation harness package**, not a promotion engine. The current available dataset is synthetic only.

## Validation labels

- `ENGINE_VALIDATION_SYNTHETIC`: deterministic fixture engine validation. Disposition is always `DATASET_INSUFFICIENT / RESEARCH_CONTINUE` and `promotion_allowed=false`.
- `HISTORICAL_VALIDATION`: offline loader path for externally supplied, provenance-bearing historical data. The runner still does not promote; it emits `HISTORICAL_REVIEW_REQUIRED`.

Synthetic PnL is never strategy validation.

## Required historical data

See `dataset_manifest.json`. Historical review requires 90 contiguous UTC days per pair/venue with raw L2 and trade tape provenance, 1m derived rows, explicit gap policy, and a fixed 60d in-sample / 30d out-of-sample split with no parameter changes.

## Frozen strategies

See `strategy_freeze.json` and `strategy_mapping_and_functional_diff.md`. The frozen adapters intentionally correct the prior harness mismatches:

- Momentum uses standardized delta, OBI, spread proxy, and prior breakout.
- Absorption requires two-interval confirmation and makes no institutional proof claim.
- Divergence uses causal trailing bounds and configured tick/delta thresholds.

## Execution model

See `execution_model.md`. Signals enter no earlier than next bar open/snapshot. If TP and SL touch within the same OHLC bar, stop is applied first unless an ordered tick path exists. Maker synthetic fills are marked assumed, not executable. Funding is not modeled.

## Run examples

```bash
python run_validation.py --pair BTCUSDT --strategy momentum --scenario all --data-mode synthetic
python run_validation.py --pair ETHUSDC --strategy absorption --scenario BASE --data-mode synthetic
```

Artifacts are written to `runs/<sha256-derived-run-id>/`:

- `manifest.json`
- `report.json`
- `<strategy>_trades.csv`
- `<strategy>_trades.json`
- `cost_decomposition.json`

Historical mode is offline only and requires a local JSON file already containing all `MarketTick` fields:

```bash
python run_validation.py --pair BTCUSDT --strategy divergence --data-mode historical --historical-file /path/to/prepared_rows.json
```

## Tests

Run from the parent of the package or set `PYTHONPATH=/opt/Axodus/Trading/quants-lab/research_notebooks`:

```bash
python -m pytest orderflow_backtest/test_backtester.py
```

## Promotion policy

`run_validation.py --request-promotion` is refused. Promotion requires a separate governance process after independent historical L2/tape validation; synthetic mode can never emit `PROMOTION_CANDIDATE`.
