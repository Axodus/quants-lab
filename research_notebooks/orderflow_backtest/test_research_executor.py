"""Tests for BoundedResearchExecutor — Trinity capability boundary and runtime preflight."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from orderflow_backtest.research_capabilities import (
    ResearchActionKind,
    ResearchCapabilityBoundary,
)
from orderflow_backtest.research_executor import BoundedResearchExecutor, RuntimePreflight
from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.multi_symbol_pipeline import DatasetQualificationRegistry
from orderflow_backtest.pipeline_operations import AxodusResearchOperations, DATASET_ID, BTC_IS_START, BTC_IS_END


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _executor(tmp_path: Path, handlers=None) -> BoundedResearchExecutor:
    return BoundedResearchExecutor(
        data_root=tmp_path,
        runtime=Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/venv/bin/python"),
        state_root=tmp_path / "receipts",
        boundary=ResearchCapabilityBoundary(DatasetQualificationRegistry.from_freeze(
            Path("/opt/Axodus/Trading/.instructions/reports/historical_orderflow_dataset_freeze.json")
        )),
        handlers=handlers or {},
    )


# ------------------------------------------------------------------ #
# Preflight                                                            #
# ------------------------------------------------------------------ #

def test_preflight_reports_data_root_and_runtime(tmp_path):
    exe = _executor(tmp_path)
    pf = exe.preflight()
    assert isinstance(pf, RuntimePreflight)
    assert pf.data_root == str(tmp_path.resolve())
    assert isinstance(pf.available_capacity_bytes, int)
    assert isinstance(pf.pyarrow_available, bool)


def test_preflight_missing_data_root_returns_unavailable():
    bad_root = Path("/run/axodus-nonexistent-path-xyz")
    exe = BoundedResearchExecutor(data_root=bad_root)
    pf = exe.preflight()
    assert pf.available is False


# ------------------------------------------------------------------ #
# Authorization gate                                                   #
# ------------------------------------------------------------------ #

def test_arbitrary_shell_is_rejected(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="ARBITRARY_EXECUTION_REJECTED"):
        exe.execute("run_shell", {"cmd": "ls"})


def test_unknown_action_is_rejected(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="UNKNOWN_ACTION"):
        exe.execute("do_magic", {})


def test_oos_backtest_is_rejected(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="OOS_SEALED"):
        exe.execute(
            ResearchActionKind.RUN_FROZEN_BACKTEST,
            {"strategy_id": "orderflow.momentum.aggression", "symbol": "BTCUSDT", "period": "OOS", "dataset_id": "dataset", "dataset_revision": "v1"},
        )


def test_unqualified_symbol_backtest_is_rejected(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="SYMBOL_NOT_QUALIFIED"):
        exe.execute(
            ResearchActionKind.RUN_FROZEN_BACKTEST,
            {"strategy_id": "orderflow.momentum.aggression", "symbol": "ZECUSDT", "period": "IS", "dataset_id": "dataset", "dataset_revision": "v1"},
        )


def test_unregistered_instrument_acquisition_is_rejected(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="MISSING_INSTRUMENT_SPEC"):
        exe.execute(
            ResearchActionKind.ACQUIRE_DATASET,
            {
                "symbol": "NOT_A_PAIR", "selection_mode": "FIXED_UNIVERSE",
                "selection_timestamp": "2026-06-22T00:00:00Z", "selected_symbols": ["NOT_A_PAIR"],
                "requested_start": "2026-06-23T00:00:00Z", "requested_end": "2026-06-24T00:00:00Z",
            },
        )


def test_path_outside_data_root_is_rejected(tmp_path):
    exe = _executor(tmp_path, handlers={
        ResearchActionKind.BUILD_FRAMES: lambda kw: {"frames": 0},
    })
    with pytest.raises(PermissionError, match="PATH_OUTSIDE_APPROVED_DATA_ROOT"):
        exe.execute(
            ResearchActionKind.BUILD_FRAMES,
            {
                "symbol": "BTCUSDT", "data_root": "/tmp/outside",
                "dataset_id": "orderflow-binance-futures-14d-20260623-20260706-v1", "dataset_revision": "v1", "period": "IS",
            },
        )


# ------------------------------------------------------------------ #
# Successful bounded execution                                         #
# ------------------------------------------------------------------ #

def test_authorized_read_action_passes_and_persists_receipt(tmp_path):
    payload_returned = {"status": "scanner_ok", "symbols": ["BTCUSDT"]}

    def scan_handler(kw):
        return payload_returned

    exe = _executor(tmp_path, handlers={
        ResearchActionKind.SCAN_MARKET: scan_handler,
    })
    args = {"selection_mode": "FORWARD_SELECTED", "universe": "test", "selection_rule_version": "v1"}
    result = exe.execute(ResearchActionKind.SCAN_MARKET, args)
    assert result == payload_returned
    # The executor must persist a receipt file.
    receipts = list((tmp_path / "receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "COMPLETE"
    assert receipt["exchangeOrderMutations"] == 0
    assert receipt["secretsExposed"] is False


def test_inspect_symbol_passes_for_registered_instrument(tmp_path):
    def inspect_handler(kw):
        spec = InstrumentSpec.from_registry(kw["symbol"])
        return {"tick_size": str(spec.tick_size), "step_size": str(spec.step_size)}

    exe = _executor(tmp_path, handlers={
        ResearchActionKind.INSPECT_SYMBOL: inspect_handler,
    })
    result = exe.execute(ResearchActionKind.INSPECT_SYMBOL, {"symbol": "BTCUSDT"})
    assert result["tick_size"] == "0.10"


def test_rejected_action_persists_receipt_with_rejected_status(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError):
        exe.execute("run_shell", {"cmd": "ls"})
    receipts = list((tmp_path / "receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "REJECTED"
    assert receipt["exchangeOrderMutations"] == 0


def test_rejected_secret_is_redacted_from_receipt(tmp_path):
    exe = _executor(tmp_path)
    with pytest.raises(PermissionError, match="CREDENTIAL_ARGUMENT_REJECTED"):
        exe.execute(
            ResearchActionKind.SCAN_MARKET,
            {"selection_mode": "FORWARD_SELECTED", "universe": "test", "selection_rule_version": "v1", "api_key": "must-not-persist"},
        )
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["arguments"]["api_key"] == "[REDACTED]"
    assert "must-not-persist" not in json.dumps(receipt)


def test_missing_handler_returns_blocked(tmp_path):
    # action passes authorization but has no injected handler
    exe = _executor(tmp_path, handlers={})
    with pytest.raises(RuntimeError, match="ACTION_HANDLER_UNAVAILABLE"):
        exe.execute(ResearchActionKind.SCAN_MARKET, {"selection_mode": "FORWARD_SELECTED", "universe": "test", "selection_rule_version": "v1"})


# ------------------------------------------------------------------ #
# Determinism                                                          #
# ------------------------------------------------------------------ #

def test_same_authorized_action_produces_stable_receipt_id(tmp_path):
    """Two identical requests produce the same receipt id (content-addressed)."""
    call_count = 0

    def scan_handler(kw):
        nonlocal call_count
        call_count += 1
        return {"symbols": ["BTCUSDT"]}

    exe = _executor(tmp_path, handlers={
        ResearchActionKind.SCAN_MARKET: scan_handler,
    })
    args = {"selection_mode": "FORWARD_SELECTED", "universe": "test", "selection_rule_version": "v1"}
    exe.execute(ResearchActionKind.SCAN_MARKET, args)
    exe.execute(ResearchActionKind.SCAN_MARKET, args)
    assert call_count == 2
    receipts = list((tmp_path / "receipts").glob("*.json"))
    # Both calls produce the same content-addressed file path.
    assert len(receipts) == 1


# ------------------------------------------------------------------ #
# Safety                                                               #
# ------------------------------------------------------------------ #

def test_zero_mutation_counters_on_every_receipt(tmp_path):
    def get_status_handler(kw):
        return {"state": "COMPLETE"}

    exe = _executor(tmp_path, handlers={
        ResearchActionKind.GET_RUN_STATUS: get_status_handler,
    })
    exe.execute(ResearchActionKind.GET_RUN_STATUS, {})
    for receipt_file in (tmp_path / "receipts").glob("*.json"):
        receipt = json.loads(receipt_file.read_text())
        assert receipt["exchangeOrderMutations"] == 0
        assert receipt["testnetMutations"] == 0
        assert receipt["mainnetMutations"] == 0
        assert receipt["realCapital"] == 0
        assert receipt["secretsExposed"] is False


def test_concrete_operations_resolve_btc_without_fallback(tmp_path):
    freeze = Path("/opt/Axodus/Trading/.instructions/reports/historical_orderflow_dataset_freeze.json")
    registry = DatasetQualificationRegistry.from_freeze(freeze)
    operations = AxodusResearchOperations(
        tmp_path, registry, freeze,
        Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/tmp/aees-closure/full-1"),
        Path("research_notebooks/orderflow_backtest/runs/canonical_historical_is_final"),
    )
    acquired = operations.acquire_historical_dataset({
        "symbol": "BTCUSDT", "dataset_id": DATASET_ID, "dataset_revision": "v1",
        "selection_mode": "FIXED_UNIVERSE", "selection_timestamp": "2026-06-22T23:00:00Z",
        "selected_symbols": ["BTCUSDT"],
        "requested_start": BTC_IS_START, "requested_end": BTC_IS_END,
    })
    assert acquired["status"] == "PREEXISTING_DATASET_RESOLVED"
    with pytest.raises(RuntimeError, match="no fallback was used"):
        operations.acquire_historical_dataset({
            "symbol": "BTCUSDT", "dataset_id": DATASET_ID, "dataset_revision": "v1",
            "selection_mode": "FIXED_UNIVERSE", "selection_timestamp": "2026-06-22T23:00:00Z",
            "selected_symbols": ["BTCUSDT"],
            "requested_start": "2026-06-24T00:00:00Z", "requested_end": BTC_IS_END,
        })


def test_concrete_operations_block_partial_non_btc_replay(tmp_path):
    freeze = Path("/opt/Axodus/Trading/.instructions/reports/historical_orderflow_dataset_freeze.json")
    registry = DatasetQualificationRegistry.from_freeze(freeze)
    operations = AxodusResearchOperations(tmp_path, registry, freeze, tmp_path, tmp_path)
    qualification = operations.qualify_historical_dataset({"symbol": "ETHUSDC", "dataset_id": DATASET_ID, "dataset_revision": "v1"})
    assert qualification["status"] == "PARTIAL"
    assert qualification["replayAuthorized"] is False
    with pytest.raises(ValueError, match="ETHUSDC is PARTIAL"):
        operations.build_orderflow_frames({"symbol": "ETHUSDC", "dataset_id": DATASET_ID, "dataset_revision": "v1", "period": "IS"})


def test_canonical_result_read_never_falls_back_to_btc(tmp_path):
    freeze = Path("/opt/Axodus/Trading/.instructions/reports/historical_orderflow_dataset_freeze.json")
    registry = DatasetQualificationRegistry.from_freeze(freeze)
    operations = AxodusResearchOperations(
        tmp_path, registry, freeze, tmp_path,
        Path("research_notebooks/orderflow_backtest/runs/canonical_historical_is_final"),
    )
    with pytest.raises(ValueError, match="ETHUSDC is PARTIAL"):
        operations.read_canonical_results({"symbol": "ETHUSDC", "dataset_id": DATASET_ID, "dataset_revision": "v1"})

    boundary = ResearchCapabilityBoundary(registry)
    rejected = boundary.authorize(
        ResearchActionKind.READ_CANONICAL_RESULTS,
        {"symbol": "ETHUSDC", "dataset_id": DATASET_ID, "dataset_revision": "v1"},
    )
    assert not rejected.authorized
    assert "ETHUSDC is PARTIAL" in rejected.reason
