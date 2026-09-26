"""Bounded-memory BTCUSDT Parquet to OrderFlowFrameV1 builder.

The builder is deliberately independent from the legacy MarketTick engine.
It groups provider price-level rows into logical events, carries one book
across hourly partitions, and emits one causal frame per UTC minute.
"""

from __future__ import annotations

import hashlib
import ctypes
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

import pyarrow.parquet as pq
import pyarrow as pa
import pyarrow.compute as pc
import numpy as np

from .orderflow_contracts import OrderFlowFrameV1


UTC = timezone.utc
IS_START_MS = int(datetime(2026, 6, 23, tzinfo=UTC).timestamp() * 1000)
IS_END_MS = int(datetime(2026, 7, 2, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)


def _ms(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    text = str(value)
    if text.isdigit():
        n = int(text)
        return n // 1000 if n > 10**14 else n
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _minute(ms: int) -> int:
    return ms - ms % 60_000


@dataclass(frozen=True)
class FrameBuilderStats:
    files_processed: int
    l2_rows_processed: int
    l2_logical_events: int
    trade_rows_processed: int
    generated_frames: int
    qualified_frames: int
    missing_frames: int
    rejected_frames: int
    crossed_books: int
    empty_books: int
    invalid_levels: int
    future_l2_violations: int
    future_trade_violations: int
    first_frame_ms: int | None
    last_frame_ms: int | None
    runtime_seconds: float
    frame_stream_hash: str


class _Book:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_event_ms: int | None = None

    def apply(self, side: str, price: Decimal, quantity: Decimal) -> None:
        if quantity < 0 or price <= 0:
            raise ValueError("invalid price or quantity")
        levels = self.bids if side in {"bid", "b"} else self.asks
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity

    def top(self, depth: int) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:depth]
        return bids, asks


class CausalParquetFrameBuilder:
    def __init__(
        self,
        data_root: Path,
        symbol: str = "BTCUSDT",
        tick_size: Decimal = Decimal("0.10"),
        depth: int = 5,
        is_start_ms: int = IS_START_MS,
        is_end_ms: int = IS_END_MS,
        revision: str = "parquet-orderflow-frame-builder-v1",
    ) -> None:
        if symbol != "BTCUSDT":
            raise ValueError("AEES permits BTCUSDT only")
        if is_end_ms > IS_END_MS:
            raise ValueError("OOS boundary rejected before partition discovery")
        self.data_root = Path(data_root)
        self.symbol = symbol
        self.tick_size = tick_size
        self.depth = depth
        self.is_start_ms = is_start_ms
        self.is_end_ms = is_end_ms
        self.revision = revision
        self.book = _Book()
        self.trades_by_minute: dict[int, dict[str, Decimal | int]] = defaultdict(lambda: {
            "buy": Decimal(0), "sell": Decimal(0), "count": 0,
        })
        self.stats = {
            "files_processed": 0,
            "l2_rows_processed": 0,
            "l2_logical_events": 0,
            "trade_rows_processed": 0,
            "crossed_books": 0,
            "empty_books": 0,
            "invalid_levels": 0,
            "future_l2_violations": 0,
            "future_trade_violations": 0,
        }

    @property
    def canonical_root(self) -> Path:
        return self.data_root / "normalized" / "canonical"

    def _paths(self, kind: str, start_ms: int | None = None) -> list[Path]:
        start = datetime.fromtimestamp((start_ms or self.is_start_ms) / 1000, tz=UTC).date()
        end = datetime.fromtimestamp(self.is_end_ms / 1000, tz=UTC).date()
        paths: list[Path] = []
        day = start
        while day <= end:
            for hour in range(24):
                p = self.canonical_root / str(day) / f"{hour:02d}" / f"{self.symbol}_{kind}.parquet"
                if p.exists():
                    paths.append(p)
            day = day.fromordinal(day.toordinal() + 1)
        return paths

    def _group_rows(self, batch: object) -> Iterator[tuple[tuple[object, ...], list[tuple[str, Decimal, Decimal]]]]:
        data = batch.to_pydict()
        # ``last_update_id`` is null on ordinary updates. Excluding that
        # nullable field prevents NaN != NaN from splitting one logical
        # provider event into one group per price level.
        fields = ["event_time", "transaction_time", "event_type", "first_update_id", "final_update_id", "prev_final_update_id"]
        current_key: tuple[object, ...] | None = None
        rows: list[tuple[str, Decimal, Decimal]] = []
        for i in range(len(data["event_type"])):
            key = tuple(data.get(field, [None] * len(data["event_type"]))[i] for field in fields)
            row = (str(data["side"][i]), _decimal(data["price"][i]), _decimal(data["quantity"][i]))
            if current_key is not None and key != current_key:
                yield current_key, rows
                rows = []
            current_key = key
            rows.append(row)
        if current_key is not None:
            yield current_key, rows

    def _load_bootstrap(self) -> None:
        candidates = []
        for day_offset in range(1, 2):
            day = datetime.fromtimestamp(self.is_start_ms / 1000, tz=UTC).date()
            day = day.fromordinal(day.toordinal() - day_offset)
            for hour in range(23, -1, -1):
                p = self.canonical_root / str(day) / f"{hour:02d}" / f"{self.symbol}_orderbook.parquet"
                if p.exists():
                    candidates.append(p)
        for path in candidates:
            # Probe only the small event-type column before decoding millions of
            # price-level strings. Most hourly files contain no bootstrap.
            event_type_table = pq.read_table(path, columns=["event_type"])
            event_types = event_type_table.column("event_type").to_pylist()
            try:
                snapshot_row = next(i for i, value in enumerate(event_types) if str(value) == "snapshot")
            except StopIteration:
                continue
            snapshot_seen = False
            row_offset = 0
            for batch in pq.ParquetFile(path).iter_batches(batch_size=250_000):
                batch_end = row_offset + batch.num_rows
                if batch_end <= snapshot_row:
                    row_offset = batch_end
                    continue
                if row_offset < snapshot_row:
                    batch = batch.slice(snapshot_row - row_offset)
                row_offset = batch_end
                for key, rows in self._group_rows(batch):
                    event_ms = _ms(key[0])
                    event_type = str(key[2])
                    if event_type == "snapshot":
                        self.book = _Book()
                        for side, price, quantity in rows:
                            self.book.apply(side, price, quantity)
                        self.book.last_event_ms = event_ms
                        snapshot_seen = True
                    elif snapshot_seen and event_ms < self.is_start_ms:
                        for side, price, quantity in rows:
                            self.book.apply(side, price, quantity)
                        self.book.last_event_ms = event_ms
            if snapshot_seen and self.book.bids and self.book.asks:
                return
        raise RuntimeError("qualified pre-window BTCUSDT snapshot bootstrap not found")

    def _load_trades(self) -> None:
        for path in self._paths("trades"):
            self.stats["files_processed"] += 1
            for batch in pq.ParquetFile(path).iter_batches(columns=["event_time", "trade_time", "price", "quantity", "is_buyer_maker"], batch_size=250_000):
                data = batch.to_pydict()
                for i in range(len(data["event_time"])):
                    event_ms = _ms(data["event_time"][i])
                    trade_ms = _ms(data["trade_time"][i])
                    if trade_ms < self.is_start_ms or trade_ms > self.is_end_ms:
                        continue
                    if trade_ms > self.is_end_ms:
                        self.stats["future_trade_violations"] += 1
                    side = "SELL" if bool(data["is_buyer_maker"][i]) else "BUY"
                    bucket = self.trades_by_minute[_minute(trade_ms)]
                    bucket["buy" if side == "BUY" else "sell"] += _decimal(data["quantity"][i])
                    bucket["count"] += 1
                    self.stats["trade_rows_processed"] += 1

    def _emit_frame(self, minute_ms: int, cvd: Decimal, previous_price: Decimal | None) -> OrderFlowFrameV1 | None:
        bids, asks = self.book.top(self.depth)
        if not bids or not asks:
            self.stats["empty_books"] += 1
            return None
        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_bid >= best_ask:
            self.stats["crossed_books"] += 1
            return None
        bid_depth = sum((q for _, q in bids), Decimal(0))
        ask_depth = sum((q for _, q in asks), Decimal(0))
        total = bid_depth + ask_depth
        obi = (bid_depth - ask_depth) / total if total else Decimal(0)
        depth_skew = bid_depth / ask_depth if ask_depth else Decimal(0)
        bucket = self.trades_by_minute.get(minute_ms, {"buy": Decimal(0), "sell": Decimal(0), "count": 0})
        buy = Decimal(bucket["buy"])
        sell = Decimal(bucket["sell"])
        mid = (best_bid + best_ask) / Decimal(2)
        displacement = Decimal(0) if previous_price is None else (mid - previous_price) / self.tick_size
        return OrderFlowFrameV1(
            minute_ms + 59_999, mid, buy, sell, obi, (best_ask - best_bid) / self.tick_size,
            best_bid, best_ask, bid_depth, ask_depth, depth_skew, abs(buy - sell), cvd,
            displacement, mid, mid,
        )

    def build(self) -> tuple[list[OrderFlowFrameV1], FrameBuilderStats]:
        started = time.monotonic()
        self._load_bootstrap()
        self._load_trades()
        frames: list[OrderFlowFrameV1] = []
        frame_hash = hashlib.sha256()
        cvd = Decimal(0)
        previous_price: Decimal | None = None
        current_minute: int | None = _minute(self.is_start_ms)
        for path in self._paths("orderbook"):
            self.stats["files_processed"] += 1
            for batch in pq.ParquetFile(path).iter_batches(batch_size=250_000):
                for key, rows in self._group_rows(batch):
                    event_ms = _ms(key[0])
                    if event_ms < self.is_start_ms:
                        continue
                    if event_ms > self.is_end_ms:
                        raise ValueError("OOS event encountered while building IS frames")
                    minute = _minute(event_ms)
                    while current_minute < minute:
                        bucket = self.trades_by_minute.get(current_minute, {"buy": Decimal(0), "sell": Decimal(0)})
                        cvd += Decimal(bucket["buy"]) - Decimal(bucket["sell"])
                        frame = self._emit_frame(current_minute, cvd, previous_price)
                        if frame is not None:
                            frames.append(frame)
                            previous_price = frame.price
                            frame_hash.update(json.dumps(frame.__dict__, sort_keys=True, default=str, separators=(",", ":")).encode())
                        current_minute += 60_000
                    event_type = str(key[2])
                    for side, price, quantity in rows:
                        try:
                            self.book.apply(side, price, quantity)
                        except ValueError:
                            self.stats["invalid_levels"] += 1
                    self.book.last_event_ms = event_ms
                    self.stats["l2_rows_processed"] += len(rows)
                    self.stats["l2_logical_events"] += 1
        if current_minute is not None:
            while current_minute <= self.is_end_ms:
                bucket = self.trades_by_minute.get(current_minute, {"buy": Decimal(0), "sell": Decimal(0)})
                cvd += Decimal(bucket["buy"]) - Decimal(bucket["sell"])
                frame = self._emit_frame(current_minute, cvd, previous_price)
                if frame is not None:
                    frames.append(frame)
                    previous_price = frame.price
                    frame_hash.update(json.dumps(frame.__dict__, sort_keys=True, default=str, separators=(",", ":")).encode())
                current_minute += 60_000
        expected = ((self.is_end_ms - self.is_start_ms) // 60_000) + 1
        stats = FrameBuilderStats(
            self.stats["files_processed"], self.stats["l2_rows_processed"], self.stats["l2_logical_events"],
            self.stats["trade_rows_processed"], len(frames), len(frames), max(expected - len(frames), 0),
            max(len(frames) - expected, 0), self.stats["crossed_books"], self.stats["empty_books"],
            self.stats["invalid_levels"], self.stats["future_l2_violations"], self.stats["future_trade_violations"],
            frames[0].timestamp_ms if frames else None, frames[-1].timestamp_ms if frames else None,
            time.monotonic() - started, frame_hash.hexdigest(),
        )
        return frames, stats


class FixedPointCausalParquetFrameBuilder(CausalParquetFrameBuilder):
    """PyArrow-vectorized equivalent using integer tick/step units."""

    PRICE_SCALE = 10
    QUANTITY_SCALE = 1000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fixed_book_bids: dict[int, int] = {}
        self._fixed_book_asks: dict[int, int] = {}
        self._trade_steps_by_minute: dict[int, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0, "count": 0})
        self.crossed_frame_times: list[int] = []

    @staticmethod
    def _fixed_array(array: object, scale: int) -> list[int]:
        decimal_array = pc.cast(array, pa.decimal128(18, 6))
        return pc.cast(pc.multiply(decimal_array, pa.scalar(scale, type=pa.int64())), pa.int64(), safe=True).to_pylist()

    def _fixed_groups(self, batch: object, row_start: int = 0) -> Iterator[tuple[tuple[object, ...], list[tuple[str, int, int]]]]:
        n = batch.num_rows
        columns = {name: batch.column(batch.schema.get_field_index(name)) for name in [
            "event_time", "transaction_time", "event_type", "first_update_id", "final_update_id",
            "prev_final_update_id", "last_update_id", "side",
        ]}
        arrays = {name: column.to_numpy(zero_copy_only=False) for name, column in columns.items()}
        prices = self._fixed_array(batch.column(batch.schema.get_field_index("price")), self.PRICE_SCALE)
        quantities = self._fixed_array(batch.column(batch.schema.get_field_index("quantity")), self.QUANTITY_SCALE)
        fields = ["event_time", "transaction_time", "event_type", "first_update_id", "final_update_id", "prev_final_update_id"]
        boundaries = np.zeros(n, dtype=np.bool_)
        boundaries[0] = True
        for field in fields:
            values = arrays[field]
            boundaries[1:] |= values[1:] != values[:-1]
        starts = np.flatnonzero(boundaries)
        ends = np.concatenate((starts[1:], np.array([n])))
        for start, end in zip(starts.tolist(), ends.tolist()):
            key = tuple(arrays[field][start].item() if hasattr(arrays[field][start], "item") else arrays[field][start] for field in fields)
            rows = [(str(arrays["side"][i]), int(prices[i]), int(quantities[i])) for i in range(start, end)]
            yield key, rows

    def _fixed_event_stream(self, batches: Iterable[object]) -> Iterator[tuple[tuple[object, ...], list[tuple[str, int, int]]]]:
        """Group logical provider events across Arrow batch boundaries."""
        pending_key: tuple[object, ...] | None = None
        pending_rows: list[tuple[str, int, int]] = []
        for batch in batches:
            for key, rows in self._fixed_groups(batch):
                if pending_key is not None and key == pending_key:
                    pending_rows.extend(rows)
                    continue
                if pending_key is not None:
                    yield pending_key, pending_rows
                pending_key, pending_rows = key, rows
        if pending_key is not None:
            yield pending_key, pending_rows

    @staticmethod
    def _kernel_path(data_root: Path) -> Path:
        override = os.environ.get("AXODUS_ORDERBOOK_KERNEL")
        if override:
            return Path(override)
        return Path(data_root) / "tmp" / "aees-closure" / "book.so"

    @staticmethod
    def _primitive_fixed(column, scale):
        decimal = pc.cast(column, pa.decimal128(18, 6))
        values = pc.cast(pc.multiply(decimal, pa.scalar(scale, type=pa.int64())), pa.int64(), safe=True)
        return values.to_numpy(zero_copy_only=False)

    @staticmethod
    def _kernel_rows(batch: object) -> np.ndarray:
        def ints(name: str) -> np.ndarray:
            column = batch.column(batch.schema.get_field_index(name))
            if column.null_count:
                column = pc.fill_null(column, 0)
            return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int64)

        event_type = np.asarray(pc.cast(pc.equal(batch.column(batch.schema.get_field_index("event_type")), "snapshot"), pa.int8()).to_numpy(), dtype=np.int64)
        side = np.asarray(pc.cast(pc.equal(batch.column(batch.schema.get_field_index("side")), "ask"), pa.int8()).to_numpy(), dtype=np.int64)
        price = FixedPointCausalParquetFrameBuilder._primitive_fixed(batch.column(batch.schema.get_field_index("price")), 10)
        quantity = FixedPointCausalParquetFrameBuilder._primitive_fixed(batch.column(batch.schema.get_field_index("quantity")), 1000)
        return np.column_stack((
            ints("event_time"), ints("transaction_time"), event_type,
            ints("first_update_id"), ints("final_update_id"), ints("prev_final_update_id"),
            ints("last_update_id"), side, price, quantity,
        ))

    def _load_kernel(self):
        import subprocess
        source = Path(__file__).with_name("orderbook_kernel.cpp")
        target = self._kernel_path(self.data_root)
        if not os.environ.get("AXODUS_ORDERBOOK_KERNEL"):
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            target = target.with_name(f"book-{digest}.so")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(f".{os.getpid()}.tmp.so")
                subprocess.run(["g++", "-O3", "-std=c++17", "-shared", "-fPIC",
                                str(source), "-o", str(temporary)], check=True)
                temporary.replace(target)
        library = ctypes.CDLL(str(target))
        c_i64_p = ctypes.POINTER(ctypes.c_int64)
        library.ob_new.argtypes = [ctypes.c_int64, ctypes.c_int64, ctypes.c_int64]
        library.ob_new.restype = ctypes.c_void_p
        library.ob_free.argtypes = [ctypes.c_void_p]
        library.ob_push.argtypes = [ctypes.c_void_p, c_i64_p, ctypes.c_int64]
        library.ob_push.restype = ctypes.c_int64
        library.ob_finish.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        library.ob_finish.restype = ctypes.c_int64
        library.ob_frames.argtypes = [ctypes.c_void_p, c_i64_p]
        library.ob_frames.restype = ctypes.c_int64
        library.ob_info.argtypes = [ctypes.c_void_p, c_i64_p]
        return library

    def build_compiled(self, max_orderbook_files: int | None = None, max_trade_files: int | None = None) -> tuple[list[OrderFlowFrameV1], FrameBuilderStats]:
        """Compiled, bounded-memory builder preserving provider event atomicity."""
        started = time.monotonic()
        library = self._load_kernel()
        handle = library.ob_new(self.is_start_ms, self.is_end_ms, self.depth)
        columns = ["event_time", "transaction_time", "event_type", "first_update_id", "final_update_id",
                   "prev_final_update_id", "last_update_id", "side", "price", "quantity"]
        files = 0
        rows_processed = 0
        try:
            # Feed the qualified snapshot lineage and every subsequent pre-window update.
            day = datetime.fromtimestamp(self.is_start_ms / 1000, tz=UTC).date()
            prewindow = [self.canonical_root / str(day.fromordinal(day.toordinal() - 1)) / f"{hour:02d}" /
                         f"{self.symbol}_orderbook.parquet" for hour in range(24)]
            prewindow = [path for path in prewindow if path.exists()]
            selected = self._paths("orderbook")
            if max_orderbook_files is not None:
                selected = selected[:max_orderbook_files]
            orderbook_paths = prewindow + selected
            for index, path in enumerate(orderbook_paths):
                if index >= len(prewindow) and max_orderbook_files is not None and index - len(prewindow) >= max_orderbook_files:
                    break
                files += 1
                if os.environ.get("AXODUS_REPLAY_PROGRESS"):
                    print(json.dumps({"file": str(path.relative_to(self.canonical_root)), "files": files, "elapsed": time.monotonic()-started}), flush=True)
                for batch in pq.ParquetFile(path).iter_batches(columns=columns, batch_size=1_000_000):
                    rows = self._kernel_rows(batch)
                    rows_processed += len(rows)
                    error = library.ob_push(handle, rows.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), len(rows))
                    if error:
                        info = np.zeros(16, dtype=np.int64)
                        library.ob_info(handle, info.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
                        raise RuntimeError(f"compiled order-book kernel error {error}: {info.tolist()}")
            error = library.ob_finish(handle, 1)
            if error:
                info = np.zeros(16, dtype=np.int64)
                library.ob_info(handle, info.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
                raise RuntimeError(f"compiled order-book kernel finish error {error}: {info.tolist()}")
            count = library.ob_frames(handle, None)
            raw_frames = np.empty((count, 7), dtype=np.int64)
            library.ob_frames(handle, raw_frames.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
            info = np.zeros(16, dtype=np.int64)
            library.ob_info(handle, info.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
        finally:
            library.ob_free(handle)

        self._load_fixed_trades(max_trade_files)
        cvd_steps = 0
        previous_mid: Decimal | None = None
        frames: list[OrderFlowFrameV1] = []
        digest = hashlib.sha256()
        for row in raw_frames:
            timestamp, bid_ticks, ask_ticks, bid_depth_steps, ask_depth_steps, _, _ = map(int, row)
            minute = timestamp - 59_999
            bucket = self._trade_steps_by_minute.get(minute, {"buy": 0, "sell": 0})
            cvd_steps += int(bucket["buy"]) - int(bucket["sell"])
            best_bid = Decimal(bid_ticks) / self.PRICE_SCALE
            best_ask = Decimal(ask_ticks) / self.PRICE_SCALE
            mid = (best_bid + best_ask) / 2
            bid_depth = Decimal(bid_depth_steps) / self.QUANTITY_SCALE
            ask_depth = Decimal(ask_depth_steps) / self.QUANTITY_SCALE
            total = bid_depth + ask_depth
            buy = Decimal(int(bucket["buy"])) / self.QUANTITY_SCALE
            sell = Decimal(int(bucket["sell"])) / self.QUANTITY_SCALE
            frame = OrderFlowFrameV1(
                timestamp, mid, buy, sell, (bid_depth - ask_depth) / total if total else Decimal(0),
                Decimal(ask_ticks - bid_ticks), best_bid, best_ask, bid_depth, ask_depth,
                bid_depth / ask_depth if ask_depth else Decimal(0), abs(buy - sell),
                Decimal(cvd_steps) / self.QUANTITY_SCALE,
                Decimal(0) if previous_mid is None else (mid - previous_mid) / self.tick_size,
                mid, mid,
            )
            frames.append(frame)
            previous_mid = mid
            digest.update(json.dumps(frame.__dict__, sort_keys=True, default=str, separators=(",", ":")).encode())
        expected = ((self.is_end_ms - self.is_start_ms) // 60_000) + 1
        stats = FrameBuilderStats(
            files + self.stats["files_processed"], rows_processed, int(info[13]), self.stats["trade_rows_processed"],
            len(frames), len(frames), max(expected - len(frames), 0), max(len(frames) - expected, 0),
            int(info[0] == 6), int(info[0] == 5), int(info[0] == 3), 0, 0,
            frames[0].timestamp_ms if frames else None, frames[-1].timestamp_ms if frames else None,
            time.monotonic() - started, digest.hexdigest(),
        )
        return frames, stats

    def _fixed_apply(self, side: str, price_ticks: int, quantity_steps: int) -> None:
        if price_ticks <= 0 or quantity_steps < 0:
            raise ValueError("invalid fixed-point price or quantity")
        levels = self._fixed_book_bids if side in {"bid", "b"} else self._fixed_book_asks
        if quantity_steps == 0:
            levels.pop(price_ticks, None)
        else:
            levels[price_ticks] = quantity_steps

    def benchmark_bootstrap(self) -> dict[str, float | int | str]:
        started = time.monotonic()
        self._load_fixed_bootstrap()
        return {
            "runtime_seconds": time.monotonic() - started,
            "bid_levels": len(self._fixed_book_bids),
            "ask_levels": len(self._fixed_book_asks),
            "best_bid_ticks": max(self._fixed_book_bids) if self._fixed_book_bids else None,
            "best_ask_ticks": min(self._fixed_book_asks) if self._fixed_book_asks else None,
        }

    def _load_fixed_bootstrap(self) -> None:
        candidates = []
        day = datetime.fromtimestamp(self.is_start_ms / 1000, tz=UTC).date()
        for hour in range(23, -1, -1):
            p = self.canonical_root / str(day.fromordinal(day.toordinal() - 1)) / f"{hour:02d}" / f"{self.symbol}_orderbook.parquet"
            if p.exists():
                candidates.append(p)
        candidates = list(reversed(candidates))
        snapshot_index: int | None = None
        snapshot_row: int | None = None
        for index, path in enumerate(candidates):
            types = pq.read_table(path, columns=["event_type"]).column("event_type").to_pylist()
            try:
                snapshot_row = next(i for i, value in enumerate(types) if str(value) == "snapshot")
            except StopIteration:
                continue
            snapshot_index = index
            break
        if snapshot_index is None or snapshot_row is None:
            raise RuntimeError("qualified pre-window BTCUSDT fixed-point bootstrap not found")

        for path_index, path in enumerate(candidates[snapshot_index:], start=snapshot_index):
            offset = 0
            for batch in pq.ParquetFile(path).iter_batches(batch_size=1_000_000):
                end = offset + batch.num_rows
                if path_index == snapshot_index and end <= snapshot_row:
                    offset = end
                    continue
                if path_index == snapshot_index and offset < snapshot_row:
                    batch = batch.slice(snapshot_row - offset)
                offset = end
                for key, rows in self._fixed_groups(batch):
                    event_type = str(key[2])
                    event_ms = _ms(key[0])
                    if event_ms >= self.is_start_ms:
                        continue
                    if event_type == "snapshot":
                        self._fixed_book_bids.clear()
                        self._fixed_book_asks.clear()
                    for side, price_ticks, quantity_steps in rows:
                        self._fixed_apply(side, price_ticks, quantity_steps)
        if self._fixed_book_bids and self._fixed_book_asks:
            return
        raise RuntimeError("qualified pre-window BTCUSDT fixed-point bootstrap produced an empty book")

    def _load_fixed_trades(self, max_files: int | None = None) -> None:
        for index, path in enumerate(self._paths("trades")):
            if max_files is not None and index >= max_files:
                break
            self.stats["files_processed"] += 1
            for batch in pq.ParquetFile(path).iter_batches(columns=["trade_time", "quantity", "is_buyer_maker"], batch_size=250_000):
                times = batch.column(0).to_numpy(zero_copy_only=False)
                # Normalized contract uses milliseconds; reject incompatible units.
                if len(times) and times.max() > 10**14:
                    raise ValueError("trade_time unit incompatible with canonical milliseconds")
                makers = batch.column(2).to_numpy(zero_copy_only=False)
                quantities = self._primitive_fixed(batch.column(1), self.QUANTITY_SCALE)
                valid = (times >= self.is_start_ms) & (times <= self.is_end_ms)
                times, makers, quantities = times[valid], makers[valid], quantities[valid]
                minutes = times // 60000 * 60000
                for minute in np.unique(minutes):
                    mask = minutes == minute
                    bucket = self._trade_steps_by_minute[int(minute)]
                    bucket["buy"] += int(quantities[mask & ~makers].sum(dtype=np.int64))
                    bucket["sell"] += int(quantities[mask & makers].sum(dtype=np.int64))
                    bucket["count"] += int(mask.sum())
                self.stats["trade_rows_processed"] += len(times)

    def _fixed_frame(self, minute_ms: int, cvd_steps: int, previous_price_ticks: int | None) -> OrderFlowFrameV1 | None:
        bids = sorted(self._fixed_book_bids.items(), key=lambda x: x[0], reverse=True)[:self.depth]
        asks = sorted(self._fixed_book_asks.items(), key=lambda x: x[0])[:self.depth]
        if not bids or not asks:
            self.stats["empty_books"] += 1
            return None
        best_bid_ticks, best_ask_ticks = bids[0][0], asks[0][0]
        if best_bid_ticks >= best_ask_ticks:
            self.stats["crossed_books"] += 1
            self.crossed_frame_times.append(minute_ms + 59_999)
            return None
        bid_depth_steps = sum(q for _, q in bids)
        ask_depth_steps = sum(q for _, q in asks)
        total_steps = bid_depth_steps + ask_depth_steps
        obi = Decimal(bid_depth_steps - ask_depth_steps) / Decimal(total_steps) if total_steps else Decimal(0)
        depth_skew = Decimal(bid_depth_steps) / Decimal(ask_depth_steps) if ask_depth_steps else Decimal(0)
        bucket = self._trade_steps_by_minute.get(minute_ms, {"buy": 0, "sell": 0, "count": 0})
        buy = Decimal(bucket["buy"]) / self.QUANTITY_SCALE
        sell = Decimal(bucket["sell"]) / self.QUANTITY_SCALE
        best_bid = Decimal(best_bid_ticks) / self.PRICE_SCALE
        best_ask = Decimal(best_ask_ticks) / self.PRICE_SCALE
        price = (best_bid + best_ask) / 2
        displacement = Decimal(0) if previous_price_ticks is None else Decimal(best_bid_ticks + best_ask_ticks - 2 * previous_price_ticks) / (2 * self.PRICE_SCALE)
        return OrderFlowFrameV1(
            minute_ms + 59_999, price, buy, sell, obi, Decimal(best_ask_ticks - best_bid_ticks) / self.PRICE_SCALE,
            best_bid, best_ask, Decimal(bid_depth_steps) / self.QUANTITY_SCALE,
            Decimal(ask_depth_steps) / self.QUANTITY_SCALE, depth_skew,
            Decimal(abs(bucket["buy"] - bucket["sell"])) / self.QUANTITY_SCALE,
            Decimal(cvd_steps) / self.QUANTITY_SCALE, displacement, price, price,
        )

    def build(self, max_orderbook_files: int | None = None, max_trade_files: int | None = None) -> tuple[list[OrderFlowFrameV1], FrameBuilderStats]:
        """Use the event-atomic compiled implementation as the sole fixed-point path."""
        return self.build_compiled(max_orderbook_files, max_trade_files)
