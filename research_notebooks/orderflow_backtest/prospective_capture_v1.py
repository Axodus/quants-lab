"""Axodus Prospective Order Flow Capture Engine & Contracts."""
from __future__ import annotations

import gzip
import hashlib
import json
import socket
import ssl
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from orderflow_backtest.historical_data_v1 import LevelV1, BookStateV1
from orderflow_backtest.historical_l2_parsers import calculate_depth_skew, calculate_obi, calculate_spread_ticks


class ProspectiveCaptureState(StrEnum):
    BOOTSTRAPPING = "BOOTSTRAPPING"
    STREAMING_QUALIFIED = "STREAMING_QUALIFIED"
    LIVE_GAP_DETECTED = "LIVE_GAP_DETECTED"
    RECONNECTING = "RECONNECTING"
    QUARANTINED = "QUARANTINED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class RawMarketEventV1:
    provider: str
    venue: str
    symbol: str
    stream: str
    exchange_time_ms: int
    receive_time_ms: int
    sequence: int | None
    payload: str
    checksum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        h = hashlib.sha256(self.payload.encode("utf-8")).hexdigest()
        object.__setattr__(self, "checksum_sha256", h)


@dataclass(frozen=True)
class SequenceGapV1:
    symbol: str
    gap_time_ms: int
    previous_sequence: int | None
    observed_sequence: int | None
    gap_type: str
    recovery_action: str


@dataclass(frozen=True)
class CaptureHealthV1:
    connected: bool
    state: ProspectiveCaptureState
    uptime_seconds: float
    last_event_age_ms: int | None
    last_sequence: int | None
    events_received: int
    gaps_detected: int
    events_per_second: float
    disk_writer_healthy: bool
    timestamp_utc: str


@dataclass(frozen=True)
class CapturePartitionManifestV1:
    partition_id: str
    provider: str
    venue: str
    symbol: str
    date_utc: str
    hour_utc: str
    first_event_time_ms: int | None
    last_event_time_ms: int | None
    first_sequence: int | None
    last_sequence: int | None
    record_count: int
    gaps: list[SequenceGapV1]
    raw_file_name: str
    checksum_sha256: str
    capture_version: str = "prospective-v1"


class BinanceLiveBookReconstructor:
    """Reconstructs Binance Futures Level-2 book from REST snapshot + WebSocket incremental updates."""

    def __init__(self, symbol: str, tick_size: Decimal = Decimal("0.01")) -> None:
        self.symbol = symbol.upper()
        self.tick_size = tick_size
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_event_time_ms: int | None = None
        self.buffer: list[dict] = []
        self.state = ProspectiveCaptureState.BOOTSTRAPPING
        self.gaps: list[SequenceGapV1] = []

    def apply_snapshot(self, snapshot_payload: Mapping[str, object]) -> None:
        last_update_id = int(snapshot_payload["lastUpdateId"])
        self.bids = {Decimal(p): Decimal(q) for p, q in snapshot_payload.get("bids", [])}
        self.asks = {Decimal(p): Decimal(q) for p, q in snapshot_payload.get("asks", [])}
        self.last_update_id = last_update_id
        self.last_event_time_ms = int(snapshot_payload.get("E", snapshot_payload.get("T", int(time.time() * 1000))))
        self.state = ProspectiveCaptureState.STREAMING_QUALIFIED

        # Process buffered messages
        buffered = list(self.buffer)
        self.buffer = []
        for msg in buffered:
            self.process_depth_update(msg)

    def process_depth_update(self, msg: Mapping[str, object]) -> BookStateV1 | None:
        if self.state == ProspectiveCaptureState.BOOTSTRAPPING:
            self.buffer.append(dict(msg))
            return None

        u = int(msg["u"])
        U = int(msg["U"])
        pu = int(msg["pu"])
        event_time_ms = int(msg["E"])

        # Drop events older than snapshot
        if self.last_update_id is not None and u < self.last_update_id:
            return None

        # First event after snapshot must satisfy U <= lastUpdateId+1 <= u
        if self.last_update_id is not None and self.last_update_id == int(self.last_update_id):
            if not (U <= self.last_update_id + 1 <= u):
                # In subsequent events, pu must match previous u
                if pu != self.last_update_id:
                    gap = SequenceGapV1(
                        symbol=self.symbol,
                        gap_time_ms=event_time_ms,
                        previous_sequence=self.last_update_id,
                        observed_sequence=pu,
                        gap_type="SEQUENCE_GAP",
                        recovery_action="RECONNECT_AND_RESNAPSHOT",
                    )
                    self.gaps.append(gap)
                    self.state = ProspectiveCaptureState.LIVE_GAP_DETECTED
                    raise ValueError(f"Live sequence gap detected for {self.symbol}: expected pu={self.last_update_id}, got pu={pu}")

        # Apply bids and asks
        for p_str, q_str in msg.get("b", []):
            p, q = Decimal(p_str), Decimal(q_str)
            if q == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q

        for p_str, q_str in msg.get("a", []):
            p, q = Decimal(p_str), Decimal(q_str)
            if q == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

        self.last_update_id = u
        self.last_event_time_ms = event_time_ms

        sorted_bids = tuple(sorted((LevelV1(p, q) for p, q in self.bids.items()), key=lambda x: x.price, reverse=True))
        sorted_asks = tuple(sorted((LevelV1(p, q) for p, q in self.asks.items()), key=lambda x: x.price))

        return BookStateV1(
            symbol=self.symbol,
            event_time_ms=event_time_ms,
            sequence=u,
            bids=sorted_bids,
            asks=sorted_asks,
        )


class ProspectivePartitionWriter:
    """Writes immutable raw JSONL partitions with SHA-256 verification."""

    def __init__(self, output_dir: Path, provider: str, venue: str, symbol: str) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.venue = venue
        self.symbol = symbol.upper()
        self.events: list[RawMarketEventV1] = []
        self.gaps: list[SequenceGapV1] = []
        self.date_utc = time.strftime("%Y-%m-%d", time.gmtime())
        self.hour_utc = time.strftime("%H", time.gmtime())

    def append_event(self, event: RawMarketEventV1) -> None:
        self.events.append(event)

    def record_gap(self, gap: SequenceGapV1) -> None:
        self.gaps.append(gap)

    def close_and_finalize(self) -> tuple[Path, CapturePartitionManifestV1]:
        timestamp_tag = int(time.time())
        filename = f"{self.provider}_{self.symbol}_{self.date_utc}_H{self.hour_utc}_{timestamp_tag}.raw.jsonl"
        file_path = self.output_dir / filename

        hasher = hashlib.sha256()
        with file_path.open("w", encoding="utf-8") as f:
            for ev in self.events:
                line = json.dumps({
                    "provider": ev.provider,
                    "venue": ev.venue,
                    "symbol": ev.symbol,
                    "stream": ev.stream,
                    "exchange_time_ms": ev.exchange_time_ms,
                    "receive_time_ms": ev.receive_time_ms,
                    "sequence": ev.sequence,
                    "payload": ev.payload,
                    "checksum": ev.checksum_sha256,
                }) + "\n"
                f.write(line)
                hasher.update(line.encode("utf-8"))

        checksum = hasher.hexdigest()
        first_time = self.events[0].exchange_time_ms if self.events else None
        last_time = self.events[-1].exchange_time_ms if self.events else None
        sequences = [e.sequence for e in self.events if e.sequence is not None]
        first_seq = sequences[0] if sequences else None
        last_seq = sequences[-1] if sequences else None

        manifest = CapturePartitionManifestV1(
            partition_id=f"{self.provider}:{self.symbol}:{self.date_utc}:H{self.hour_utc}:{timestamp_tag}",
            provider=self.provider,
            venue=self.venue,
            symbol=self.symbol,
            date_utc=self.date_utc,
            hour_utc=self.hour_utc,
            first_event_time_ms=first_time,
            last_event_time_ms=last_time,
            first_sequence=first_seq,
            last_sequence=last_seq,
            record_count=len(self.events),
            gaps=list(self.gaps),
            raw_file_name=filename,
            checksum_sha256=checksum,
        )

        manifest_path = self.output_dir / f"{filename}.manifest.json"
        manifest_path.write_text(json.dumps(asdict(manifest), indent=2))

        return file_path, manifest
