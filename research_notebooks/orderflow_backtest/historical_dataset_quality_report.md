# Historical Dataset Quality Report

Dataset: orderflow-binance-futures-14d-20260623-20260706-v1
Window: 2026-06-23T00:00:00Z -> 2026-07-06T23:59:59.999Z
IS: 2026-06-23T00:00:00Z -> 2026-07-02T23:59:59.999Z
OOS: 2026-07-03T00:00:00Z -> 2026-07-06T23:59:59.999Z
Provider: CryptoHFTData / Binance USD-M Futures / hourly Parquet Zstandard

## Completeness

- Core partitions: 1344/1344 accounted
- Raw SHA-256 valid: 1344/1344
- Schemas valid: 1344/1344
- Missing: 0
- Quarantined: 0
- Download failures: 0

## Sequence and coverage

| Symbol | Logical events | Snapshot rows/events | True gaps | Qualified coverage | IS | OOS |
|---|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 44,877,401 | 50,000 / 25 | 0 | 100.0000% | 100.0000% | 100.0000% |
| ETHUSDC | 33,358,480 | 49,000 / 25 | 9 | 67.9255% | 61.8059% | 83.2245% |

Row-level findings were 76; 65 were removed as grouping artifacts. The remaining logical findings were one BTC pre-window bootstrap finding and ten ETH findings, of which the pre-window finding was resolved and nine true in-window gaps remain.

## Causal qualification

- BTCUSDT pre-window bootstrap: PASS, 2026-06-22/22Z snapshot 10869949249143.
- ETHUSDC pre-window bootstrap: PASS, 2026-06-22/14Z snapshot 10867286016586.
- Duplicate events: 0.
- Out-of-order events: 0.
- Representative OrderFlowFrameV1 causal sample for both symbols: PASS.
- Future L2 exclusion: PASS on representative sample.
- Future tape exclusion: PASS on representative sample.
- Aggressor mapping: PASS.
- ETHUSDC intervals after each true gap are excluded until recovery snapshot.

## Reconstruction and auxiliary data

- BTCUSDT reconstruction: PASS for qualified segment evidence; full bounded replay remains incomplete.
- ETHUSDC reconstruction: qualified segments identified; full bounded replay and all-segment snapshot reconciliation remain incomplete.
- Funding and mark-price coverage: PASS, 1,208,380 rows per symbol.
- Metadata: PASS for tick size, step size, min quantity, and perpetual contract fields.
- Fees remain conservative assumptions, not historical fee facts.

## Regression and safety

- Full Quants-Lab suite: 79 passed / 0 failed.
- Orderflow focused suite: 34 passed / 0 failed.
- Total: 113 passed / 0 failed.
- Backtest: NOT RUN.
- OOS strategy access: NOT RUN.
- Trading mutations: 0.
- Jev calls: 0.
- R3E/R3H/B1 isolation: PASS.
- Secrets exposed: false.

## Disposition

HISTORICAL_DATASET_PARTIAL

ETHUSDC has nine true sequence gaps, producing 67.9255% qualified coverage and preventing full continuous two-symbol acceptance. The artifacts preserve invalid intervals and do not claim HISTORICAL_DATASET_ACCEPTED.
