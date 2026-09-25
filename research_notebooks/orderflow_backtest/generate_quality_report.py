import json, time
from pathlib import Path

dataset_id = "orderflow-binance-futures-14d-20260623-20260706-v1"
window = {"start": "2026-06-23T00:00:00Z", "end": "2026-07-06T23:59:59.999Z"}
is_split = {"start": "2026-06-23T00:00:00Z", "end": "2026-07-02T23:59:59.999Z", "days": 10}
oos_split = {"start": "2026-07-03T00:00:00Z", "end": "2026-07-06T23:59:59.999Z", "days": 4}

report = {
  "reportId": "historical-dataset-quality-report-14d-v1",
  "datasetId": dataset_id,
  "datasetRevision": "v1",
  "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "window": window,
  "isSplit": is_split,
  "oosSplit": oos_split,
  "provider": {"source": "CryptoHFTData", "product": "Binance USD-M Futures Order Book & Trades (Hourly Parquet/Zstd)", "qualificationStatus": "CRYPTOHFTDATA_QUALIFIED"},
  "corePartitions": {"expected": 1344, "accounted": 1344, "rawSha256Valid": 1344, "schemaValid": 1344, "missing": 0, "quarantined": 0, "downloadFailures": 0, "retries": 0, "btcOrderbookPartitions": 336, "btcTradePartitions": 336, "ethOrderbookPartitions": 336, "ethTradePartitions": 336},
  "storage": {"rawPrimaryBytes": 11896791040, "normalizedAuxiliaryBytes": 55130445, "format": "Parquet + Zstandard", "dataPath": "/run/media/mzfshark/Storage/Axodus/Trading/market-data"},
  "symbols": {
    "BTCUSDT": {"logicalEvents": 44877401, "snapshotRows": 50000, "snapshotEvents": 25, "rowLevelInitialFindings": 37, "rowLevelArtifactsRemoved": 36, "logicalFindings": 1, "trueGaps": 0, "duplicates": 0, "outOfOrder": 0, "qualifiedSeconds": 1209599.999, "invalidSeconds": 0, "qualifiedCoveragePct": 100.0, "isQualifiedCoveragePct": 100.0, "oosQualifiedCoveragePct": 100.0, "segmentCount": 1, "invalidIntervalCount": 0, "largestQualifiedSegmentSec": 1209599.999, "maxInvalidIntervalSec": 0, "preWindowBootstrap": "VALID_PRE_WINDOW_BOOTSTRAP_CONTINUATION via 2026-06-22/22Z snapshot 10869949249143", "reconstruction": "PASS over qualified segment; full bounded replay not completed by existing scripts", "snapshotReconciliation": "PASS by focused qualification suite", "tapeCausality": {"futureL2Exclusion": "PASS", "futureTapeExclusion": "PASS", "causalOrdering": "PASS", "aggressorMapping": "PASS"}, "fundingMarkPrice": "PASS; 1208380 rows", "metadata": "PASS; tickSize 0.10; stepSize 0.001; minQty 0.001"},
    "ETHUSDC": {"logicalEvents": 33358480, "snapshotRows": 49000, "snapshotEvents": 25, "rowLevelInitialFindings": 39, "rowLevelArtifactsRemoved": 29, "logicalFindings": 10, "trueGaps": 9, "duplicates": 0, "outOfOrder": 0, "qualifiedSeconds": 821626.998, "invalidSeconds": 387973.001, "qualifiedCoveragePct": 67.9255, "isQualifiedCoveragePct": 61.8059, "oosQualifiedCoveragePct": 83.2245, "segmentCount": 9, "invalidIntervalCount": 9, "largestQualifiedSegmentSec": 321885.048, "maxInvalidIntervalSec": 85803.467, "preWindowBootstrap": "VALID_PRE_WINDOW_BOOTSTRAP_CONTINUATION via 2026-06-22/14Z snapshot 10867286016586", "reconstruction": "PARTIAL; qualified segments identified, full bounded replay not completed by existing scripts", "snapshotReconciliation": "NOT COMPLETELY EVIDENCED for all qualified segments", "tapeCausality": {"futureL2Exclusion": "PASS on representative 2026-06-23/00 sample", "futureTapeExclusion": "PASS on representative 2026-06-23/00 sample", "causalOrdering": "PASS on representative sample", "aggressorMapping": "PASS"}, "fundingMarkPrice": "PASS; 1208380 rows", "metadata": "PASS; tickSize 0.01; stepSize 0.001; minQty 0.001"}
  },
  "commonQualifiedCoverage": {"contiguousCalendarDays": 14, "btcQualifiedCoveragePct": 100.0, "ethQualifiedCoveragePct": 67.9255, "commonSynchronousQualifiedPct": 67.9255, "isCommonQualifiedPct": 61.8059, "oosCommonQualifiedPct": 83.2245},
  "regressionSuite": {"fullQuantsLab": "79 passed / 0 failed", "orderflowFocused": "34 passed / 0 failed", "total": "113 passed / 0 failed"},
  "isolation": {"r3e": "PASS", "r3h": "PASS", "b1": "PASS"},
  "safety": {"externalTradingMutations": 0, "testnetMutations": 0, "mainnetMutations": 0, "realCapital": 0, "jevCalls": 0, "secretsExposed": False},
  "disposition": "HISTORICAL_DATASET_PARTIAL",
  "dispositionRationale": "BTCUSDT has complete qualified coverage. ETHUSDC has 9 true sequence gaps and 67.9255% qualified coverage; invalid intervals are explicitly excluded. Full continuous two-symbol acceptance is not supported."
}
payload = json.dumps(report, indent=2) + "\n"
for path in [
    Path("/opt/Axodus/Trading/.instructions/reports/historical_dataset_quality_report.json"),
    Path("/opt/Axodus/Trading/quants-lab/research_notebooks/orderflow_backtest/historical_dataset_quality_report.json"),
]:
    path.write_text(payload)

md = f"""# Historical Dataset Quality Report

Dataset: {dataset_id}
Window: {window["start"]} -> {window["end"]}
IS: {is_split["start"]} -> {is_split["end"]}
OOS: {oos_split["start"]} -> {oos_split["end"]}
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
"""
for path in [
    Path("/opt/Axodus/Trading/.instructions/reports/historical_dataset_quality_report.md"),
    Path("/opt/Axodus/Trading/quants-lab/research_notebooks/orderflow_backtest/historical_dataset_quality_report.md"),
]:
    path.write_text(md)
print("quality reports written")
