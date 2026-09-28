"""Dedicated 24h Binance Futures Testnet runner (implementation only).

Reclassified under AXODUS-TRADING-REQ-QUANT-CONDOR-PIPELINE-REALIGNMENT-01:
Quants-Lab is research-only. Direct Binance transport, REST signing, and order
submission are deprecated/removed from this repository. This module provides
offline experiment manifests, telemetry ingestion, and historical-vs-live comparison.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_SYMBOLS = ("BTCUSDT", "ZECUSDT", "SUIUSDT", "WLDUSDT")
SUPPORTED_STRATEGIES = (
    "orderflow.momentum.aggression",
    "orderflow.absorption.fade",
    "orderflow.cvd.divergence.reversal",
)
STRATEGY_REVISION = "freeze-2026-09-24-adapter-v1"


class RunnerState(StrEnum):
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    FLATTENING = "FLATTENING"
    STOPPED_HARD_STOP_24H = "STOPPED_HARD_STOP_24H"
    STOPPED_KILL_SWITCH = "STOPPED_KILL_SWITCH"
    STOPPED_ERROR = "STOPPED_ERROR"


class CellRole(StrEnum):
    CONTROL_SAMPLE = "CONTROL_SAMPLE"
    ACTIVE_CANDIDATE = "ACTIVE_CANDIDATE"


class QuantsLabDirectExecutionBlocked(PermissionError):
    """Raised if any code attempts direct exchange connectivity from Quants-Lab."""
    pass


@dataclass(frozen=True)
class CellKey:
    symbol: str
    strategy_id: str
    strategy_revision: str = STRATEGY_REVISION

    @property
    def value(self) -> str:
        return f"{self.symbol}:{self.strategy_id}:{self.strategy_revision}"

    @property
    def role(self) -> CellRole:
        return CellRole.CONTROL_SAMPLE if self.symbol == "BTCUSDT" else CellRole.ACTIVE_CANDIDATE


@dataclass
class LifecycleRecord:
    event: str
    timestamp_ms: int
    cell_id: str
    payload: dict[str, Any]


class CellState:
    def __init__(self, key: CellKey, output_dir: Path) -> None:
        self.key = key
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.signals: list[LifecycleRecord] = []
        self.orders: list[LifecycleRecord] = []
        self.fills: list[LifecycleRecord] = []
        self.trades: list[LifecycleRecord] = []
        self.active_position = False
        self.open_order = False
        self.seen_event_ids: set[str] = set()

    def record(self, stream: str, event: str, payload: dict[str, Any], timestamp_ms: int | None = None) -> LifecycleRecord:
        record = LifecycleRecord(event, timestamp_ms or int(time.time() * 1000), self.key.value, payload)
        target = getattr(self, stream)
        target.append(record)
        with (self.output_dir / f"{stream}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return record

    def flatten(self, timestamp_ms: int | None = None) -> None:
        now = timestamp_ms or int(time.time() * 1000)
        if self.open_order:
            self.record("orders", "CANCEL", {"reason": "FLATTEN"}, now)
            self.open_order = False
        if self.active_position:
            self.record("trades", "FLATTEN", {"reason": "FLATTEN"}, now)
            self.active_position = False

    def summary(self) -> dict[str, Any]:
        return {
            "cellId": self.key.value,
            "symbol": self.key.symbol,
            "strategyId": self.key.strategy_id,
            "strategyRevision": self.key.strategy_revision,
            "cellRole": self.key.role.value,
            "signals": len(self.signals),
            "orders": len(self.orders),
            "fills": len(self.fills),
            "trades": len(self.trades),
            "openOrder": self.open_order,
            "activePosition": self.active_position,
        }


class Dedicated24hTestnetRunner:
    """Research telemetry ingestion and comparison harness. No exchange mutations."""

    def __init__(
        self,
        output_root: Path | None = None,
        max_duration_seconds: float = 86_400.0,
        symbols: Iterable[str] = SUPPORTED_SYMBOLS,
        strategies: Iterable[str] = SUPPORTED_STRATEGIES,
    ) -> None:
        self.output_root = (output_root or Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/runs/live_24h_testnet")).resolve()
        self.max_duration_seconds = max_duration_seconds
        self.symbols = tuple(symbols)
        self.strategies = tuple(strategies)
        if set(self.symbols) != set(SUPPORTED_SYMBOLS) or set(self.strategies) != set(SUPPORTED_STRATEGIES):
            raise ValueError("runner requires exactly the approved 4 symbols x 3 strategies")
        self.cells = {
            key.value: CellState(key, self.output_root / key.symbol / key.strategy_id.rsplit(".", 1)[-1])
            for symbol in self.symbols
            for strategy in self.strategies
            for key in [CellKey(symbol, strategy)]
        }
        self.state = RunnerState.INITIALIZED
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.kill_switch = False
        self.hard_stop = False
        self.reconnect_count: int = 0
        self.active_subscriptions: set[str] = set()

    def simulate_ws_connect_and_subscribe(self, symbols: Iterable[str] | None = None) -> list[str]:
        target_symbols = tuple(symbols or self.symbols)
        self.active_subscriptions = {f"{sym.lower()}@bookTicker" for sym in target_symbols}
        return sorted(self.active_subscriptions)

    def simulate_ws_disconnect(self) -> None:
        if self.state == RunnerState.RUNNING:
            self.state = RunnerState.RECONNECTING

    def simulate_ws_reconnect_and_restore(self) -> dict[str, Any]:
        if self.state not in (RunnerState.RECONNECTING, RunnerState.RUNNING):
            raise RuntimeError(f"cannot reconnect from state: {self.state}")
        self.reconnect_count += 1
        restored_subs = sorted(self.active_subscriptions)
        self.state = RunnerState.RUNNING
        return {
            "reconnectCount": self.reconnect_count,
            "restoredSubscriptions": restored_subs,
            "cellCount": self.cell_count,
            "state": self.state.value,
        }

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    def start(self) -> None:
        if self.state != RunnerState.INITIALIZED:
            raise RuntimeError(f"cannot start from {self.state}")
        self.started_at = time.time()
        self.state = RunnerState.RUNNING

    def check_hard_stop(self, now: float | None = None) -> bool:
        if self.state != RunnerState.RUNNING or self.started_at is None:
            return False
        current = now if now is not None else time.time()
        if current - self.started_at >= self.max_duration_seconds:
            self.hard_stop = True
            self.state = RunnerState.FLATTENING
            self.flatten_all(int(current * 1000))
            self.state = RunnerState.STOPPED_HARD_STOP_24H
            self.stopped_at = current
            return False
        return True

    def trigger_kill_switch(self, reason: str = "MANUAL_KILL_SWITCH") -> None:
        self.kill_switch = True
        self.state = RunnerState.FLATTENING
        self.flatten_all()
        self.state = RunnerState.STOPPED_KILL_SWITCH
        self.stopped_at = time.time()
        self._write_manifest(reason)

    def flatten_all(self, timestamp_ms: int | None = None) -> None:
        for cell in self.cells.values():
            cell.flatten(timestamp_ms)

    def ingest_event(self, symbol: str, strategy_id: str, event: str, payload: dict[str, Any], timestamp_ms: int | None = None) -> None:
        if not self.check_hard_stop():
            raise RuntimeError(f"runner is not accepting events: {self.state}")
        cell_id = CellKey(symbol, strategy_id).value
        if cell_id not in self.cells:
            raise ValueError("unknown cell")
        cell = self.cells[cell_id]
        event_id = payload.get("eventId") or payload.get("tradeId") or payload.get("orderId") or payload.get("fillId")
        if event_id:
            dedup_key = f"{event}:{event_id}"
            if dedup_key in cell.seen_event_ids:
                return
            cell.seen_event_ids.add(dedup_key)
        stream = {"SIGNAL": "signals", "ORDER": "orders", "ACK": "orders", "FILL": "fills", "PARTIAL_FILL": "fills", "CANCEL": "orders", "TRADE": "trades"}.get(event)
        if stream is None:
            raise ValueError(f"unsupported lifecycle event: {event}")
        cell.record(stream, event, payload, timestamp_ms)
        if event in {"ORDER", "ACK"}:
            cell.open_order = True
        elif event == "FILL":
            cell.active_position = True
            cell.open_order = False
        elif event == "PARTIAL_FILL":
            cell.active_position = True
        elif event == "CANCEL":
            cell.open_order = False
        elif event == "TRADE":
            cell.active_position = False
            cell.open_order = False

    def compare_historical(self, historical: dict[str, Any]) -> dict[str, Any]:
        results = {}
        for cell_id, cell in self.cells.items():
            live = cell.summary()
            hist = historical.get(cell_id, {})
            results[cell_id] = {
                "cellRole": cell.key.role.value,
                "live": live,
                "historical": hist,
                "signalsPerDay": live["signals"] / 1.0,
                "tradesPerDay": live["trades"] / 1.0,
                "classification": "LIVE_SAMPLE_INSUFFICIENT" if live["trades"] == 0 else "LIVE_BEHAVIOR_CONSISTENCY_REVIEW_REQUIRED",
            }
        return {"cells": results, "state": self.state.value}

    def _write_manifest(self, reason: str | None = None) -> Path:
        self.output_root.mkdir(parents=True, exist_ok=True)
        path = self.output_root / "live_24h_runner_manifest.json"
        payload = {
            "runnerRevision": "v1-24h-testnet-dedicated",
            "strategyRevision": STRATEGY_REVISION,
            "symbols": self.symbols,
            "strategies": self.strategies,
            "cellCount": self.cell_count,
            "state": self.state.value,
            "killSwitch": self.kill_switch,
            "hardStop24h": self.hard_stop,
            "stopReason": reason,
            "cells": {key: cell.summary() for key, cell in self.cells.items()},
            "mainnetMutations": 0,
            "testnetMutations": 0,
            "realCapital": 0,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path
