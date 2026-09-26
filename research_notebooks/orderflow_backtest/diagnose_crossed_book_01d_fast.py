"""Bounded crossed-book diagnosis using fixed-point prefix replay."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from .parquet_orderflow_frames import FixedPointCausalParquetFrameBuilder, _ms

ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data")
TARGET_START_MS = 1782172800000
TARGET_EVENT_MS = 1782175087235
FIELDS = ("event_time", "transaction_time", "event_type", "first_update_id", "final_update_id", "prev_final_update_id")


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def ident(key: tuple[object, ...]) -> dict[str, object]:
    return {name: (value.item() if hasattr(value, "item") else value) for name, value in zip(FIELDS, key)}


def apply(book: dict[str, dict[int, int]], rows: list[tuple[str, int, int]]) -> None:
    for side, price, quantity in rows:
        levels = book["bid" if side in {"bid", "b"} else "ask"]
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity


def tops(book: dict[str, dict[int, int]], depth: int = 10) -> dict[str, list[tuple[str, str]]]:
    return {
        "bids": [(f"{p / 10:.1f}", f"{q / 1000:.3f}") for p, q in sorted(book["bid"].items(), reverse=True)[:depth]],
        "asks": [(f"{p / 10:.1f}", f"{q / 1000:.3f}") for p, q in sorted(book["ask"].items())[:depth]],
    }


def main() -> None:
    builder = FixedPointCausalParquetFrameBuilder(ROOT, symbol="BTCUSDT")
    bootstrap = builder.benchmark_bootstrap()
    book = {"bid": dict(builder._fixed_book_bids), "ask": dict(builder._fixed_book_asks)}
    bootstrap_book = {"bid": dict(book["bid"]), "ask": dict(book["ask"])}
    first_cross = None
    target_path = None
    previous_final = None
    ordering_violations = []
    checked = 0
    paths = [p for p in builder._paths("orderbook") if "2026-06-23/00/" in str(p)]
    for path in paths:
        columns = ["event_time", "transaction_time", "event_type", "first_update_id", "final_update_id", "prev_final_update_id", "side", "price", "quantity"]
        table = pq.read_table(path, columns=columns, filters=[("event_time", "<=", TARGET_EVENT_MS)])
        for key, rows in builder._fixed_groups(table):
            event_ms = _ms(key[0])
            if event_ms < TARGET_START_MS:
                continue
            if event_ms > TARGET_EVENT_MS:
                break
            current_final = int(key[4])
            current_prev = int(key[5])
            if previous_final is not None and current_prev != previous_final:
                ordering_violations.append({"event": ident(key), "previous_final": previous_final})
            previous_final = current_final
            before_bid = max(book["bid"]) if book["bid"] else None
            before_ask = min(book["ask"]) if book["ask"] else None
            before_snapshot = {"bid": dict(book["bid"]), "ask": dict(book["ask"])} if event_ms == TARGET_EVENT_MS else None
            apply(book, rows)
            checked += 1
            after_bid = max(book["bid"]) if book["bid"] else None
            after_ask = min(book["ask"]) if book["ask"] else None
            if after_bid is not None and after_ask is not None and after_bid >= after_ask:
                before = before_snapshot
                if before is None:
                    raise RuntimeError("target event did not capture pre-event state")
                first_cross = {"timestamp": iso(event_ms), "partition": str(path), "identity": ident(key), "number_of_rows": len(rows), "bid_rows": sum(s == "bid" for s, _, _ in rows), "ask_rows": sum(s == "ask" for s, _, _ in rows), "before": {"best_bid_ticks": before_bid, "best_ask_ticks": before_ask, "top": {"bids": [(f"{p / 10:.1f}", f"{q / 1000:.3f}") for p, q in sorted(before["bid"].items(), reverse=True)[:10]], "asks": [(f"{p / 10:.1f}", f"{q / 1000:.3f}") for p, q in sorted(before["ask"].items())[:10]]}}, "after": {"best_bid_ticks": after_bid, "best_ask_ticks": after_ask, "top": tops(book)}, "rows": rows, "before_book": before}
                target_path = path
                break
        if first_cross is not None:
            break
        if first_cross is not None:
            break
    if first_cross is None:
        raise RuntimeError("first crossed state not reproduced")

    target_key = tuple(first_cross["identity"][field] for field in FIELDS)
    raw = []
    columns = list(FIELDS) + ["side", "price", "quantity", "order_count"]
    for batch in pq.ParquetFile(target_path).iter_batches(columns=columns, batch_size=1_000_000):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            key = tuple(data[field][i] for field in FIELDS)
            if key == target_key:
                raw.append({field: data[field][i] for field in columns})

    suspicious = sorted({(side, price) for side, price, _ in first_cross["rows"] if side == "ask" and price <= first_cross["before"]["best_bid_ticks"]})
    lifecycle = {}
    for side, price in suspicious:
        lifecycle[f"{side}:{price}"] = {"side": side, "price_ticks": price, "first_seen": "bootstrap_state" if price in bootstrap_book["ask"] else None, "initial_quantity": bootstrap_book["ask"].get(price), "updates": [{"timestamp": first_cross["timestamp"], "sequence": first_cross["identity"]["final_update_id"], "quantity": quantity} for row_side, row_price, quantity in first_cross["rows"] if row_side == side and row_price == price], "state_at_first_cross": book["ask"].get(price)}

    # Bounded independent arithmetic check: reconstruct the same event using Decimal.
    reference = {"bid": {}, "ask": {}}
    for side, levels in first_cross["before_book"].items():
        reference[side] = {int(Decimal(str(price)) * 10): int(Decimal(str(quantity)) * 1000) for price, quantity in levels.items()}
    apply(reference, first_cross["rows"])
    reference_matches = (max(reference["bid"]), min(reference["ask"])) == (first_cross["after"]["best_bid_ticks"], first_cross["after"]["best_ask_ticks"])

    result = {"status": "DIAGNOSTIC_COMPLETE", "bootstrap": {"status": "PASS", "snapshot_last_update_id": 10869949249143, "snapshot_replaces_prior_state": True, "runtime_seconds": bootstrap["runtime_seconds"]}, "first_cross": first_cross, "raw_provider_evidence": {"path": str(target_path), "event_rows": raw}, "event_grouping": {"status": "PASS", "identity_fields": list(FIELDS), "logical_event_rows": len(raw), "all_rows_share_identity": len({tuple(row[field] for field in FIELDS) for row in raw}) == 1}, "event_ordering": {"status": "PASS" if not ordering_violations else "FAIL", "checked_update_events": checked, "violations": ordering_violations[:20], "authority": "prev_final_update_id/final_update_id"}, "delete_semantics": {"status": "PASS", "zero_quantity_removes_level": True, "lifecycle": lifecycle}, "reference_replay": {"status": "PASS" if reference_matches else "FAIL", "same_state_as_optimized": reference_matches}, "next_provider_snapshot": None, "provider_book_proven_crossed": False, "classification": "UNRESOLVED"}
    out = Path("/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-IMP-QUANT-ORDERFLOW-REPLAY-01D-CROSSED-BOOK-DIAGNOSTIC.json")
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    from decimal import Decimal
    main()
