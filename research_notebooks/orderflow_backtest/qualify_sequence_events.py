"""Classify CryptoHFTData L2 sequence anomalies by logical event identity.

Reads Arrow batches only; price-level rows belonging to one provider event are
collapsed before sequence checks. No strategy or performance code is called.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

DATA_ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical")
START = "2026-06-23"
END = "2026-07-06"
SYMBOLS = ("BTCUSDT", "ETHUSDC")
OUT = Path("/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-SEQUENCE-LEDGER.json")
BATCH_SIZE = 1_000_000


def _arr(values, missing=-1):
    return np.asarray([missing if x is None else x for x in values], dtype=np.int64)


def _event_key(d: dict, i: int, typ: str) -> tuple:
    if typ == "snapshot":
        return (typ, d["event_time"][i], d["transaction_time"][i], d["last_update_id"][i])
    return (
        typ,
        d["event_time"][i],
        d["transaction_time"][i],
        d["first_update_id"][i],
        d["final_update_id"][i],
        d["prev_final_update_id"][i],
    )


def classify(symbol: str) -> dict:
    paths = sorted(DATA_ROOT.glob(f"*/**/{symbol}_orderbook.parquet"))
    paths = [p for p in paths if START <= p.parts[-3] <= END]
    metrics = {
        "symbol": symbol,
        "files": len(paths),
        "rows": 0,
        "logicalEvents": 0,
        "updateEvents": 0,
        "snapshotRows": 0,
        "snapshotEvents": 0,
        "continuousTransitions": 0,
        "snapshotBootstraps": 0,
        "snapshotRebootstraps": 0,
        "sequenceResets": 0,
        "gaps": 0,
        "duplicates": 0,
        "outOfOrder": 0,
        "qualifiedSegments": 0,
        "qualifiedEventCount": 0,
        "invalidEventCount": 0,
        "anomalies": [],
        "segments": [],
    }
    last_final: int | None = None
    previous_event: dict | None = None
    previous_key: tuple | None = None
    segment_start: dict | None = None
    segment_events = 0
    segment_qualified = False
    sequence_event_index = 0
    seen_snapshot_ids: set[int] = set()

    def finish_segment(end_event: dict | None, reason: str | None = None) -> None:
        nonlocal segment_start, segment_events, segment_qualified
        if segment_start is None:
            return
        metrics["segments"].append({
            "start": segment_start,
            "end": end_event,
            "eventCount": segment_events,
            "qualified": segment_qualified,
            "endReason": reason,
        })
        segment_start = None
        segment_events = 0
        segment_qualified = False

    def record_anomaly(classification: str, current: dict, previous: dict | None, boundary: bool, day_boundary: bool, snapshot_boundary: bool, evidence: str) -> None:
        metrics["anomalies"].append({
            "symbol": symbol,
            "partition": current["partition"],
            "timestamp": current["eventTime"],
            "eventIdentity": current["eventIdentity"],
            "previousFinalUpdateId": previous.get("finalUpdateId") if previous else None,
            "currentFirstUpdateId": current.get("firstUpdateId"),
            "currentPrevFinalUpdateId": current.get("prevFinalUpdateId"),
            "currentFinalUpdateId": current.get("finalUpdateId"),
            "previousEventType": previous.get("eventType") if previous else None,
            "currentEventType": current["eventType"],
            "partitionBoundary": boundary,
            "dayBoundary": day_boundary,
            "snapshotBoundary": snapshot_boundary,
            "classification": classification,
            "evidence": evidence,
        })

    for file_index, path in enumerate(paths, 1):
        file_partition = f"{path.parts[-3]}/{path.parts[-2]}"
        file_day = path.parts[-3]
        current_key = None
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(
            columns=["event_type", "event_time", "transaction_time", "first_update_id", "final_update_id", "prev_final_update_id", "last_update_id"],
            batch_size=BATCH_SIZE,
        ):
            d = batch.to_pydict()
            n = len(d["event_type"])
            metrics["rows"] += n
            typ = np.asarray(d["event_type"], dtype=object)
            event_time = _arr(d["event_time"])
            transaction_time = _arr(d["transaction_time"])
            first = _arr(d["first_update_id"])
            final = _arr(d["final_update_id"])
            prev_final = _arr(d["prev_final_update_id"])
            last_id = _arr(d["last_update_id"])
            metrics["snapshotRows"] += int((typ == "snapshot").sum())
            if n == 0:
                continue
            starts = np.r_[0,
                np.flatnonzero(
                    (typ[1:] != typ[:-1])
                    | (event_time[1:] != event_time[:-1])
                    | (transaction_time[1:] != transaction_time[:-1])
                    | (first[1:] != first[:-1])
                    | (final[1:] != final[:-1])
                    | (prev_final[1:] != prev_final[:-1])
                    | (last_id[1:] != last_id[:-1])
                ) + 1,
            ]
            for i in starts:
                event_type = str(typ[i])
                key = _event_key(d, int(i), event_type)
                if key == current_key:
                    continue
                current_key = key
                metrics["logicalEvents"] += 1
                event = {
                    "partition": file_partition,
                    "eventTime": int(event_time[i]) if event_time[i] >= 0 else None,
                    "eventType": event_type,
                    "eventIdentity": [x for x in key],
                    "firstUpdateId": None if first[i] < 0 else int(first[i]),
                    "finalUpdateId": None if final[i] < 0 else int(final[i]),
                    "prevFinalUpdateId": None if prev_final[i] < 0 else int(prev_final[i]),
                    "lastUpdateId": None if last_id[i] < 0 else int(last_id[i]),
                }
                boundary = previous_event is not None and previous_event["partition"] != file_partition
                day_boundary = previous_event is not None and previous_event["partition"].split("/")[0] != file_day
                snapshot_boundary = event_type == "snapshot" or (previous_event is not None and previous_event["eventType"] == "snapshot")
                if event_type == "snapshot":
                    metrics["snapshotEvents"] += 1
                    sid = event["lastUpdateId"]
                    if sid is None:
                        metrics["invalidEventCount"] += 1
                        finish_segment(previous_event, "INVALID_SNAPSHOT")
                    else:
                        if sid in seen_snapshot_ids:
                            metrics["duplicates"] += 1
                            record_anomaly("TRUE_SEQUENCE_GAP", event, previous_event, boundary, day_boundary, True, "duplicate snapshot identity")
                        seen_snapshot_ids.add(sid)
                        if last_final is None:
                            metrics["snapshotBootstraps"] += 1
                            metrics["qualifiedSegments"] += 1
                        else:
                            metrics["snapshotRebootstraps"] += 1
                            finish_segment(previous_event, "VALID_SNAPSHOT_REBOOTSTRAP")
                            metrics["qualifiedSegments"] += 1
                        last_final = sid
                        segment_start = event
                        segment_qualified = True
                        segment_events = 1
                        previous_event = event
                    continue
                metrics["updateEvents"] += 1
                sequence_event_index += 1
                f = event["firstUpdateId"]
                u = event["finalUpdateId"]
                pu = event["prevFinalUpdateId"]
                if f is None or u is None or pu is None:
                    metrics["invalidEventCount"] += 1
                    previous_event = event
                    continue
                if segment_start is None:
                    segment_start = event
                    segment_qualified = last_final is not None
                if last_final is None:
                    metrics["invalidEventCount"] += 1
                    record_anomaly("AMBIGUOUS", event, previous_event, boundary, day_boundary, snapshot_boundary, "update before any valid snapshot")
                    previous_event = event
                    continue
                bootstrap_overlap = previous_event is not None and previous_event["eventType"] == "snapshot" and f <= last_final + 1 <= u
                if bootstrap_overlap:
                    metrics["continuousTransitions"] += 1
                    metrics["qualifiedEventCount"] += 1
                elif u < last_final:
                    metrics["outOfOrder"] += 1
                    record_anomaly("TRUE_OUT_OF_ORDER", event, previous_event, boundary, day_boundary, snapshot_boundary, "logical event final_update_id decreased")
                    finish_segment(previous_event, "TRUE_OUT_OF_ORDER")
                    segment_start = event
                    segment_qualified = False
                    metrics["invalidEventCount"] += 1
                elif u == last_final:
                    metrics["duplicates"] += 1
                    record_anomaly("PARSER_GROUPING_ERROR", event, previous_event, boundary, day_boundary, snapshot_boundary, "logical final_update_id repeated")
                elif pu == last_final:
                    metrics["continuousTransitions"] += 1
                    metrics["qualifiedEventCount"] += 1
                    segment_qualified = True
                else:
                    metrics["gaps"] += 1
                    record_anomaly("TRUE_SEQUENCE_GAP", event, previous_event, boundary, day_boundary, snapshot_boundary, "prev_final_update_id does not equal previous final_update_id")
                    finish_segment(previous_event, "TRUE_SEQUENCE_GAP")
                    segment_start = event
                    segment_qualified = False
                    metrics["invalidEventCount"] += 1
                last_final = u
                segment_events += 1
                previous_event = event
        print(f"{symbol}: file {file_index}/{len(paths)}", file=sys.stderr, flush=True)
    finish_segment(previous_event, "END_OF_WINDOW")
    metrics["anomalyCount"] = len(metrics["anomalies"])
    return metrics


def main() -> None:
    started = time.time()
    result = {
        "datasetId": "orderflow-binance-futures-14d-20260623-20260706-v1",
        "window": {"start": f"{START}T00:00:00Z", "end": f"{END}T23:59:59.999Z"},
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbols": [classify(s) for s in SYMBOLS],
        "durationSeconds": round(time.time() - started, 3),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
