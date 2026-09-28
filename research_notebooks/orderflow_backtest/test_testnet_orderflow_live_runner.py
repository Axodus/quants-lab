"""Unit and integration tests for Dedicated 24h Testnet Runner."""
from __future__ import annotations

from pathlib import Path
import time
import pytest

from orderflow_backtest.testnet_orderflow_live_runner import (
    CellRole,
    Dedicated24hTestnetRunner,
    MainnetCredentialRejected,
    MainnetEndpointRejected,
    RunnerState,
    SUPPORTED_STRATEGIES,
    SUPPORTED_SYMBOLS,
    TestnetCredentialRequired,
    TestnetCredentialStatus,
    TestnetEndpointConfig,
)


def test_mainnet_endpoints_strictly_rejected():
    for bad in (
        "https://fapi.binance.com",
        "https://api.binance.com",
        "wss://fstream.binance.com",
        "wss://stream.binance.com",
        "https://binance.com/fapi/v1",
        "wss://fstream.binancefuture.com?",
        "https://arbitrary.host.com",
    ):
        with pytest.raises(MainnetEndpointRejected):
            if bad.startswith("ws"):
                TestnetEndpointConfig(ws_url=bad)
            else:
                TestnetEndpointConfig(rest_url=bad)


def test_testnet_endpoint_accepted():
    cfg = TestnetEndpointConfig()
    assert cfg.rest_url == "https://testnet.binancefuture.com"
    assert cfg.ws_url == "wss://fstream.binancefuture.com"


def test_mainnet_credentials_strictly_rejected():
    for key in (
        "TRADING_BINANCE_API_KEY",
        "TRADING_BINANCE_API_SECRET",
        "BINANCE_API_KEY",
        "BINANCE_MAINNET_API_KEY",
    ):
        with pytest.raises(MainnetCredentialRejected):
            TestnetCredentialStatus.resolve({key: "real_secret_key"})


def test_testnet_credentials_resolution():
    # Incomplete
    with pytest.raises(TestnetCredentialRequired):
        TestnetCredentialStatus.resolve({"BINANCE_FUTURES_TESTNET_API_KEY": "abc"})

    # Absent
    status = TestnetCredentialStatus.resolve({})
    assert not status.available

    # Valid explicit Testnet
    valid = TestnetCredentialStatus.resolve({
        "BINANCE_FUTURES_TESTNET_API_KEY": "test_key",
        "BINANCE_FUTURES_TESTNET_SECRET_KEY": "test_secret",
    })
    assert valid.available
    assert valid.source == "BINANCE_FUTURES_TESTNET_API_KEY"


def test_twelve_cells_orchestration_and_btc_control(tmp_path: Path):
    runner = Dedicated24hTestnetRunner(output_root=tmp_path)
    assert runner.cell_count == 12
    for sym in SUPPORTED_SYMBOLS:
        for strat in SUPPORTED_STRATEGIES:
            key = f"{sym}:{strat}:freeze-2026-09-24-adapter-v1"
            assert key in runner.cells
            cell = runner.cells[key]
            if sym == "BTCUSDT":
                assert cell.key.role == CellRole.CONTROL_SAMPLE
            else:
                assert cell.key.role == CellRole.ACTIVE_CANDIDATE


def test_cell_lifecycle_persistence_and_isolation(tmp_path: Path):
    runner = Dedicated24hTestnetRunner(output_root=tmp_path)
    runner.state = RunnerState.RUNNING
    now_s = time.time()
    runner.started_at = now_s
    now_ms = int(now_s * 1000)

    # Ingest on ZEC Momentum
    runner.ingest_event("ZECUSDT", "orderflow.momentum.aggression", "SIGNAL", {"side": "LONG", "price": "35.5"}, timestamp_ms=now_ms)
    runner.ingest_event("ZECUSDT", "orderflow.momentum.aggression", "ORDER", {"orderId": "o1", "side": "BUY", "qty": "1.0"}, timestamp_ms=now_ms + 10)
    runner.ingest_event("ZECUSDT", "orderflow.momentum.aggression", "ACK", {"orderId": "o1"}, timestamp_ms=now_ms + 15)
    runner.ingest_event("ZECUSDT", "orderflow.momentum.aggression", "FILL", {"fillId": "f1", "price": "35.5", "qty": "1.0"}, timestamp_ms=now_ms + 20)
    runner.ingest_event("ZECUSDT", "orderflow.momentum.aggression", "TRADE", {"tradeId": "t1", "netPnl": "0.50"}, timestamp_ms=now_ms + 1000)

    target_cell = runner.cells["ZECUSDT:orderflow.momentum.aggression:freeze-2026-09-24-adapter-v1"]
    summary = target_cell.summary()
    assert summary["signals"] == 1
    assert summary["orders"] == 2
    assert summary["fills"] == 1
    assert summary["trades"] == 1

    # Verify isolation: other cells must be untouched
    btc_cell = runner.cells["BTCUSDT:orderflow.momentum.aggression:freeze-2026-09-24-adapter-v1"]
    assert btc_cell.summary()["signals"] == 0
    assert btc_cell.summary()["trades"] == 0

    # Verify persistent jsonl files
    assert (target_cell.output_dir / "signals.jsonl").is_file()
    assert (target_cell.output_dir / "orders.jsonl").is_file()
    assert (target_cell.output_dir / "fills.jsonl").is_file()
    assert (target_cell.output_dir / "trades.jsonl").is_file()


def test_kill_switch_and_flatten(tmp_path: Path):
    runner = Dedicated24hTestnetRunner(output_root=tmp_path)
    runner.state = RunnerState.RUNNING
    now_s = time.time()
    runner.started_at = now_s
    now_ms = int(now_s * 1000)

    # Create active state in two cells
    runner.ingest_event("WLDUSDT", "orderflow.absorption.fade", "ORDER", {"orderId": "oW"}, timestamp_ms=now_ms)
    runner.ingest_event("SUIUSDT", "orderflow.cvd.divergence.reversal", "FILL", {"fillId": "fS"}, timestamp_ms=now_ms)

    runner.trigger_kill_switch(reason="SAFETY_ALERT")
    assert runner.state == RunnerState.STOPPED_KILL_SWITCH
    assert runner.kill_switch

    # All cells should have flattened active positions and open orders
    for cell in runner.cells.values():
        assert not cell.open_order
        assert not cell.active_position

    manifest_path = runner._write_manifest()
    assert manifest_path.is_file()


def test_hard_stop_24h_automatically_triggered(tmp_path: Path):
    runner = Dedicated24hTestnetRunner(output_root=tmp_path, max_duration_seconds=86_400.0)
    runner.state = RunnerState.RUNNING
    runner.started_at = 1_000.0

    # After 12h: still running
    assert runner.check_hard_stop(now=1_000.0 + 43_200.0)
    assert runner.state == RunnerState.RUNNING

    # At 24h: hard stop triggers and flattens
    assert not runner.check_hard_stop(now=1_000.0 + 86_400.0)
    assert runner.state == RunnerState.STOPPED_HARD_STOP_24H
    assert runner.hard_stop
def test_duplicate_events_suppression_and_state_continuity(tmp_path: Path):
    runner = Dedicated24hTestnetRunner(output_root=tmp_path)
    runner.state = RunnerState.RUNNING
    runner.started_at = time.time()

    subs = runner.simulate_ws_connect_and_subscribe()
    assert len(subs) == 4

    # Ingest unique order
    runner.ingest_event("BTCUSDT", "orderflow.momentum.aggression", "ORDER", {"orderId": "o_unique_1", "side": "BUY"}, timestamp_ms=1000)
    # Duplicate ingest of same order
    runner.ingest_event("BTCUSDT", "orderflow.momentum.aggression", "ORDER", {"orderId": "o_unique_1", "side": "BUY"}, timestamp_ms=1001)

    btc_cell = runner.cells["BTCUSDT:orderflow.momentum.aggression:freeze-2026-09-24-adapter-v1"]
    assert len(btc_cell.orders) == 1

    # Disconnect simulation -> reconnect -> subscriptions restored -> state and counts continue uninterrupted
    runner.simulate_ws_disconnect()
    assert runner.state == RunnerState.RECONNECTING
    recovery = runner.simulate_ws_reconnect_and_restore()
    assert recovery["reconnectCount"] == 1
    assert len(recovery["restoredSubscriptions"]) == 4
    assert runner.state == RunnerState.RUNNING

    runner.ingest_event("BTCUSDT", "orderflow.momentum.aggression", "FILL", {"fillId": "f_1", "orderId": "o_unique_1"}, timestamp_ms=1005)
    assert len(btc_cell.fills) == 1
    assert btc_cell.active_position
    assert len(btc_cell.orders) == 1
