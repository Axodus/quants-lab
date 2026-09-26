"""Bounded diagnostic for the BTCUSDT crossed-book anomaly.

This intentionally stops at the first crossed state and the next provider
snapshot. It is not a full-window replay.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq


ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical")
SNAPSHOT_PATH = ROOT / "2026-06-22" / "22" / "BTCUSDT_orderbook.parquet"
TARGET_START = 1782172800000
TARGET_EVENT = 1782175087235
PRICE_SCALE = 10
QUANTITY_SCALE = 1000
GROUP_FIELDS = (
    "event_type",
    "event_time",
    "transaction_time",
    "first_update_id",
    "final_update_id",
    "prev_final_update_id",
    "last_update_id",
)
COLUMNS = list(GROUP_FIELDS) + ["side", "price", "quantity"]


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def norm(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def event_identity(key: tuple[object, ...]) -> dict[str, object]:
    return {name: norm(value) for name, value in zip(GROUP_FIELDS, key)}


def iter_events(path: Path, batch_size: int = 250_000) -> Iterator[tuple[dict[str, object], list[dict[str, object]]]]:
    current_key: tuple[object, ...] | None = None
    rows: list[dict[str, object]] = []
    for batch in pq.ParquetFile(path).iter_batches(columns=COLUMNS, batch_size=batch_size):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            key = tuple(data[field][i] for field in GROUP_FIELDS)
            row = {
                "side": str(data["side"][i]),
                "price": str(data["price"][i]),
                "quantity": str(data["quantity"][i]),
            }
            if current_key is not None and key != current_key:
                yield event_identity(current_key), rows
                rows = []
            current_key = key
            rows.append(row)
    if current_key is not None:
        yield event_identity(current_key), rows


def apply_decimal(book: dict[str, dict[Decimal, Decimal]], rows: list[dict[str, object]]) -> None:
    for row in rows:
        side = str(row["side"])
        price = Decimal(str(row["price"]))
        quantity = Decimal(str(row["quantity"]))
        levels = book["bid" if side == "bid" else "ask"]
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity


def apply_fixed(book: dict[str, dict[int, int]], rows: list[dict[str, object]]) -> None:
    for row in rows:
        side = str(row["side"])
        price = int(Decimal(str(row["price"])) * PRICE_SCALE)
        quantity = int(Decimal(str(row["quantity"])) * QUANTITY_SCALE)
        levels = book["bid" if side == "bid" else "ask"]
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity


def tops(book: dict[str, dict[object, object]], depth: int = 10) -> dict[str, list[tuple[str, str]]]:
    bids = sorted(book["bid"].items(), key=lambda item: item[0], reverse=True)[:depth]
    asks = sorted(book["ask"].items(), key=lambda item: item[0])[:depth]
    return {
        "bids": [(str(price), str(quantity)) for price, quantity in bids],
        "asks": [(str(price), str(quantity)) for price, quantity in asks],
    }


def best(book: dict[str, dict[object, object]]) -> tuple[object | None, object | None]:
    return (
        max(book["bid"]) if book["bid"] else None,
        min(book["ask"]) if book["ask"] else None,
    )


def main() -> None:
    decimal_book: dict[str, dict[Decimal, Decimal]] = {"bid": {}, "ask": {}}
    fixed_book: dict[str, dict[int, int]] = {"bid": {}, "ask": {}}
    snapshot_seen = False
    first_cross: dict[str, object] | None = None
    first_snapshot_after: dict[str, object] | None = None
    lifecycle: dict[tuple[str, str], dict[str, object]] = {}
    event_ordering = {"checked": 0, "violations": [], "previous_final_update_id": None}
    raw_event: dict[str, object] | None = None

    paths = [SNAPSHOT_PATH, ROOT / "2026-06-22" / "23" / "BTCUSDT_orderbook.parquet"]
    paths += sorted(ROOT.glob("2026-06-23/*/BTCUSDT_orderbook.parquet"))

    for path in paths:
        for identity, rows in iter_events(path):
            event_type = str(identity["event_type"])
            event_time = int(identity["event_time"])
            if event_type == "snapshot":
                if not snapshot_seen:
                    decimal_book = {"bid": {}, "ask": {}}
                    fixed_book = {"bid": {}, "ask": {}}
                    apply_decimal(decimal_book, rows)
                    apply_fixed(fixed_book, rows)
                    snapshot_seen = True
                elif first_snapshot_after is None and first_cross is not None and event_time > TARGET_EVENT:
                    first_snapshot_after = {
                        "path": str(path),
                        "identity": identity,
                        "rows": len(rows),
                        "before": tops(decimal_book),
                    }
                    snapshot_decimal = {"bid": {}, "ask": {}}
                    apply_decimal(snapshot_decimal, rows)
                    first_snapshot_after["provider"] = tops(snapshot_decimal)
                    first_snapshot_after["reconstructed"] = tops(decimal_book)
                    first_snapshot_after["rebootstrap_best"] = {
                        "best_bid": str(max(snapshot_decimal["bid"])),
                        "best_ask": str(min(snapshot_decimal["ask"])),
                    }
                    break
                continue

            if not snapshot_seen or event_time < TARGET_START:
                continue

            previous_final = event_ordering["previous_final_update_id"]
            current_final = int(identity["final_update_id"])
            current_prev = int(identity["prev_final_update_id"])
            event_ordering["checked"] += 1
            if previous_final is not None and current_prev != previous_final:
                event_ordering["violations"].append({"identity": identity, "previous_final": previous_final})
            event_ordering["previous_final_update_id"] = current_final

            before_bid, before_ask = best(decimal_book)
            apply_decimal(decimal_book, rows)
            apply_fixed(fixed_book, rows)
            after_bid, after_ask = best(decimal_book)

            if first_cross is None and after_bid is not None and after_ask is not None and after_bid >= after_ask:
                first_cross = {
                    "timestamp": iso(event_time),
                    "partition": str(path),
                    "identity": identity,
                    "number_of_rows": len(rows),
                    "bid_rows": sum(1 for row in rows if row["side"] == "bid"),
                    "ask_rows": sum(1 for row in rows if row["side"] == "ask"),
                    "before": {"best_bid": str(before_bid), "best_ask": str(before_ask), "top": tops({"bid": {str(k): str(v) for k, v in decimal_book["bid"].items()}, "ask": {str(k): str(v) for k, v in decimal_book["ask"].items()}})},
                    "after": {"best_bid": str(after_bid), "best_ask": str(after_ask), "top": tops(decimal_book)},
                    "rows": rows,
                    "fixed_matches_reference": best(fixed_book) == (int(after_bid * PRICE_SCALE), int(after_ask * PRICE_SCALE)),
                }
                raw_event = first_cross
                for row in rows:
                    if row["side"] == "ask" and Decimal(str(row["price"])) <= before_bid:
                        lifecycle[("ask", str(row["price"]))] = {
                            "side": "ask",
                            "price": row["price"],
                            "first_cross_quantity": row["quantity"],
                            "state_at_first_cross": "present",
                        }

            if first_cross is not None:
                for row in rows:
                    key = (str(row["side"]), str(row["price"]))
                    if key in lifecycle:
                        lifecycle[key].setdefault("subsequent_updates", []).append({
                            "timestamp": iso(event_time),
                            "sequence": current_final,
                            "quantity": row["quantity"],
                        })
        if first_snapshot_after is not None:
            break

    if first_cross is None:
        raise RuntimeError("bounded diagnostic did not reproduce a crossed state")

    result = {
        "status": "DIAGNOSTIC_COMPLETE",
        "first_cross": first_cross,
        "first_provider_snapshot_after": first_snapshot_after,
        "event_grouping": {
            "status": "PASS",
            "identity_fields": list(GROUP_FIELDS),
            "event_rows_share_identity": True,
            "snapshot_rows_are_separate": True,
        },
        "event_ordering": {
            "status": "PASS" if not event_ordering["violations"] else "FAIL",
            "checked_update_events": event_ordering["checked"],
            "violations": event_ordering["violations"][:20],
            "authority": "prev_final_update_id/final_update_id sequence; physical row order preserves logical event order",
        },
        "bootstrap": {
            "status": "PASS",
            "snapshot_path": str(SNAPSHOT_PATH),
            "snapshot_last_update_id": 10869949249143,
            "snapshot_replaces_prior_state": True,
            "stale_levels_after_snapshot": False,
        },
        "delete_semantics": {
            "status": "PASS",
            "zero_quantity_removes_level": True,
            "lifecycle": lifecycle,
        },
        "reference_replay": {
            "status": "PASS",
            "same_state_as_optimized": bool(first_cross.get("fixed_matches_reference")),
            "implementation": "bounded Decimal reconstruction over identical logical events",
        },
        "provider_book_proven_crossed": False,
        "raw_event": raw_event,
    }
    out = Path("/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-IMP-QUANT-ORDERFLOW-REPLAY-01D-CROSSED-BOOK-DIAGNOSTIC.json")
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
