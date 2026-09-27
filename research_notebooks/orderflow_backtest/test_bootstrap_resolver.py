from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orderflow_backtest.bootstrap_resolver import (
    BOOTSTRAP_PARTIAL,
    BOOTSTRAP_QUALIFIED,
    BOOTSTRAP_SEQUENCE_BROKEN,
    BOOTSTRAP_UNAVAILABLE,
    BootstrapResolver,
    expected_minute_frames,
)
from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.multi_symbol_pipeline import MultiSymbolResearchPipeline
from orderflow_backtest.scanner_selection_manifest import ScannerSelectionManifest


START = "2026-06-23T00:00:00Z"
END = "2026-06-23T02:59:59.999Z"


def event(kind, timestamp, last=None, first=None, final=None, prev=None):
    return {
        "event_type": kind, "event_time": timestamp, "last_update_id": last,
        "first_update_id": first, "final_update_id": final, "prev_final_update_id": prev,
    }


def test_valid_pre_window_snapshot_and_continuous_bridge():
    events = [
        event("snapshot", "2026-06-22T23:00:00Z", last=100),
        event("update", "2026-06-22T23:10:00Z", first=101, final=105, prev=100),
        event("update", "2026-06-23T00:10:00Z", first=106, final=110, prev=105),
    ]
    result = BootstrapResolver().resolve("ZECUSDT", events, START, END)
    assert result.status == BOOTSTRAP_QUALIFIED
    assert result.window.causal_coverage == 100.0
    assert result.window.expected_frames_for_qualified_window == 180


def test_snapshot_exists_but_bridge_is_invalid():
    events = [
        event("snapshot", "2026-06-22T23:00:00Z", last=100),
        event("update", "2026-06-23T00:10:00Z", first=120, final=125, prev=119),
    ]
    result = BootstrapResolver().resolve("SUIUSDT", events, START, END)
    assert result.status == BOOTSTRAP_SEQUENCE_BROKEN
    assert result.bridge["status"] == "FAIL"
    assert result.acquisition_request["status"] == "BOOTSTRAP_ACQUISITION_REQUIRED"


def test_no_pre_window_snapshot_is_unavailable():
    result = BootstrapResolver().resolve("WLDUSDT", [], START, END)
    assert result.status == BOOTSTRAP_UNAVAILABLE
    assert result.window.qualified_start is None
    assert result.window.expected_frames_for_qualified_window == 0


def test_first_snapshot_inside_window_creates_partial_window_without_retroactive_frames():
    events = [event("snapshot", "2026-06-23T01:00:00Z", last=100)]
    result = BootstrapResolver().resolve("SUIUSDT", events, START, END)
    assert result.status == BOOTSTRAP_PARTIAL
    assert result.window.qualified_start == "2026-06-23T01:00:00Z"
    assert result.window.excluded_duration_seconds == 3600
    assert result.window.expected_frames_for_qualified_window == 120
    assert result.window.excluded_intervals[0]["start"] == START


def test_partial_window_frame_count_is_authoritative():
    assert expected_minute_frames(datetime(2026, 6, 23, 1, tzinfo=timezone.utc), datetime(2026, 6, 23, 2, 59, 59, 999000, tzinfo=timezone.utc)) == 120


def test_future_snapshot_cannot_initialize_earlier_state():
    result = BootstrapResolver().resolve(
        "WLDUSDT", [event("snapshot", "2026-06-23T01:00:00Z", last=100)], START, END
    )
    assert result.window.qualified_start != START
    assert result.window.excluded_intervals


def test_per_symbol_isolation_allows_qualified_symbol_to_continue():
    original_registry = dict(InstrumentSpec._REGISTRY)
    for symbol in ("ZECUSDT", "SUIUSDT"):
        InstrumentSpec.register(InstrumentSpec(
            symbol=symbol, venue="fixture", market_type="USD-M Futures",
            tick_size=Decimal("0.01"), step_size=Decimal("0.001"),
            price_scale=100, quantity_scale=1000, price_precision=2, quantity_precision=3,
            metadata_source="test fixture", metadata_timestamp="2026-09-27T00:00:00Z",
            metadata_hash="fixture", base_asset=symbol[:-4], quote_asset="USDT", settlement_asset="USDT",
        ))
    manifest = ScannerSelectionManifest(
        selection_mode="FIXED_UNIVERSE", selection_timestamp="2026-06-22T23:00:00Z",
        universe="fixed", selection_rule_version="test", selected_symbols=["ZECUSDT", "SUIUSDT"],
    )
    try:
        pipeline = MultiSymbolResearchPipeline("/tmp", START, END, manifest)
        pipeline.register_symbol("ZECUSDT")
        pipeline.register_symbol("SUIUSDT")
        zec = pipeline.resolve_bootstrap("ZECUSDT", [event("snapshot", "2026-06-22T23:00:00Z", last=100), event("update", "2026-06-23T00:01:00Z", first=101, final=101, prev=100)])
        sui = pipeline.resolve_bootstrap("SUIUSDT", [event("snapshot", "2026-06-23T01:00:00Z", last=100)])
        assert zec.status == BOOTSTRAP_QUALIFIED
        assert sui.status == BOOTSTRAP_PARTIAL
        assert pipeline.symbol_states["ZECUSDT"].bootstrap_status == BOOTSTRAP_QUALIFIED
        assert pipeline.symbol_states["SUIUSDT"].bootstrap_status == BOOTSTRAP_PARTIAL
    finally:
        InstrumentSpec._REGISTRY.clear()
        InstrumentSpec._REGISTRY.update(original_registry)


def test_btc_regression_contract_unchanged():
    assert MultiSymbolResearchPipeline.verify_btc_regression(
        "fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf"
    )["status"] == "PASS"
