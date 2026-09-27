"""
Tests for multi-symbol causal research pipeline and bounded execution.

Validates:
  1. InstrumentSpec exact scale preservation and consistency checks.
  2. Fail-closed behavior on missing or unvalidated instrument metadata.
  3. ScannerSelectionManifest temporal causality guards.
  4. Detection and rejection of TEMPORAL_SELECTION_LEAKAGE.
  5. ResearchCapabilityBoundary enforcement (rejection of arbitrary shell).
  6. MultiSymbolResearchPipeline state progression and BTC regression invariant.
  7. Forward vs Historical cohort separation.
"""
from decimal import Decimal
import json
import pytest

from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.scanner_selection_manifest import ScannerSelectionManifest
from orderflow_backtest.research_capabilities import (
    ResearchCapabilityBoundary,
    ResearchActionKind,
    ALLOWED_BACKTEST_STRATEGIES,
)
from orderflow_backtest.multi_symbol_pipeline import (
    MultiSymbolResearchPipeline,
    BTC_CANONICAL_FRAME_HASH,
    DatasetQualificationRegistry,
    ForwardResearchCohort,
)

FREEZE = "/opt/Axodus/Trading/.instructions/reports/historical_orderflow_dataset_freeze.json"


def _registry():
    return DatasetQualificationRegistry.from_freeze(FREEZE)


def test_btc_instrument_spec_invariants():
    spec = InstrumentSpec.from_registry("BTCUSDT")
    spec.validate()
    assert spec.tick_size == Decimal("0.10")
    assert spec.step_size == Decimal("0.001")
    assert spec.price_scale == 10
    assert spec.quantity_scale == 1000
    assert spec.price_from_ticks(773128) == Decimal("77312.8")
    assert spec.quantity_from_steps(537) == Decimal("0.537")
    assert spec.spread_ticks(Decimal("77300.0"), Decimal("77300.5")) == Decimal("5")


def test_eth_instrument_spec_invariants():
    spec = InstrumentSpec.from_registry("ETHUSDC")
    spec.validate()
    assert spec.tick_size == Decimal("0.01")
    assert spec.step_size == Decimal("0.001")
    assert spec.price_scale == 100
    assert spec.quantity_scale == 1000
    assert spec.spread_ticks(Decimal("2500.00"), Decimal("2500.05")) == Decimal("5")


def test_invalid_scale_in_instrument_spec_raises():
    bad = InstrumentSpec(
        symbol="BADUSDT",
        venue="Binance",
        market_type="Futures",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        price_scale=100,  # should be 10!
        quantity_scale=1000,
        price_precision=2,
        quantity_precision=3,
        metadata_source="test",
        metadata_timestamp="2026-06-23T00:00:00Z",
        metadata_hash="hash",
    )
    with pytest.raises(ValueError, match="price_scale 100 inconsistent"):
        bad.validate()


def test_non_unit_fraction_increment_uses_exact_decimal_scale():
    spec = InstrumentSpec(
        symbol="QUARTERUSDT", venue="test", market_type="futures",
        tick_size=Decimal("0.25"), step_size=Decimal("0.005"),
        price_scale=100, quantity_scale=1000,
        price_precision=2, quantity_precision=3,
        metadata_source="fixture", metadata_timestamp="2026-09-27T00:00:00Z",
        metadata_hash="0" * 64,
    )
    spec.validate()
    assert spec.price_to_ticks("10.50") == 1050
    assert spec.price_from_ticks(1050) == Decimal("10.5")
    assert spec.quantity_to_steps("1.235") == 1235
    with pytest.raises(ValueError, match="not aligned"):
        spec.price_to_ticks("10.40")


def test_unregistered_symbol_raises_key_error():
    with pytest.raises(KeyError, match="not found in registry"):
        InstrumentSpec.from_registry("NOT_A_REAL_PAIR")


def test_temporal_selection_leakage_rejected():
    # Selection timestamp AFTER the IS period must fail causality validation.
    manifest = ScannerSelectionManifest(
        selection_mode="FIXED_UNIVERSE",
        selection_timestamp="2026-09-27T00:00:00Z",
        universe="Binance Futures top-100",
        selection_rule_version="scanner-v1",
        selected_symbols=["ZECUSDT", "SUIUSDT"],
    )
    assert not manifest.is_usable_for_historical_is("2026-06-23T00:00:00Z")

    with pytest.raises(ValueError, match="TEMPORAL_SELECTION_LEAKAGE"):
        MultiSymbolResearchPipeline(
            data_root="/tmp",
            is_start="2026-06-23T00:00:00Z",
            is_end="2026-07-02T23:59:59.999Z",
            selection_manifest=manifest,
            qualification_registry=_registry(),
        )


def test_forward_selected_mode_prohibits_historical_acquisition():
    manifest = ScannerSelectionManifest(
        selection_mode="FORWARD_SELECTED",
        selection_timestamp="2026-09-27T00:00:00Z",
        universe="Binance Futures top-100",
        selection_rule_version="scanner-v1",
        selected_symbols=["ZECUSDT"],
    )
    pipeline = MultiSymbolResearchPipeline(
        data_root="/tmp",
        is_start="2026-06-23T00:00:00Z",
        is_end="2026-07-02T23:59:59.999Z",
        selection_manifest=manifest,
        qualification_registry=_registry(),
    )
    # Register an allowed instrument (e.g. BTCUSDT)
    pipeline.register_symbol("BTCUSDT")
    with pytest.raises(ValueError, match="TEMPORAL_SELECTION_LEAKAGE"):
        pipeline.mark_acquired("BTCUSDT", {"file": "test"})


def test_selection_manifest_persists_hash_addressed_evidence(tmp_path):
    manifest = ScannerSelectionManifest(
        selection_mode="FORWARD_SELECTED",
        selection_timestamp="2026-09-27T12:00:00Z",
        universe="Binance Futures top-100",
        selection_rule_version="scanner-v1",
        selected_symbols=["SOLUSDT"],
    )
    target = tmp_path / "selection.json"
    fingerprint = manifest.persist(target)
    payload = json.loads(target.read_text())
    assert payload["selectionFingerprint"] == fingerprint == manifest.fingerprint()


def test_valid_point_in_time_selection_passes():
    manifest = ScannerSelectionManifest(
        selection_mode="POINT_IN_TIME",
        selection_timestamp="2026-06-22T23:00:00Z",
        universe="Binance Futures top-100",
        selection_rule_version="scanner-v1",
        selected_symbols=["BTCUSDT"],
        historical_snapshot_id="cryptohftdata-scanner-20260622T230000Z-v1",
    )
    assert not manifest.is_usable_for_historical_is("2026-06-23T00:00:00Z")
    with pytest.raises(ValueError, match="POINT_IN_TIME_UNAVAILABLE"):
        MultiSymbolResearchPipeline(
            data_root="/tmp",
            is_start="2026-06-23T00:00:00Z",
            is_end="2026-07-02T23:59:59.999Z",
            selection_manifest=manifest,
            qualification_registry=_registry(),
        )


def test_point_in_time_selection_without_snapshot_fails_closed():
    manifest = ScannerSelectionManifest(
        selection_mode="POINT_IN_TIME",
        selection_timestamp="2026-06-22T23:00:00Z",
        universe="Binance Futures top-100",
        selection_rule_version="scanner-v1",
        selected_symbols=["BTCUSDT"],
    )
    with pytest.raises(ValueError, match="POINT_IN_TIME_UNAVAILABLE"):
        MultiSymbolResearchPipeline(
            data_root="/tmp", is_start="2026-06-23T00:00:00Z",
            is_end="2026-07-02T23:59:59.999Z", selection_manifest=manifest,
        )


def test_research_capabilities_block_arbitrary_shell():
    boundary = ResearchCapabilityBoundary()
    res = boundary.authorize("run_shell", {"cmd": "ls"})
    assert not res.authorized
    assert "ARBITRARY_EXECUTION_REJECTED" in res.reason


def test_research_capabilities_block_oos():
    boundary = ResearchCapabilityBoundary(_registry())
    res = boundary.authorize(
        ResearchActionKind.RUN_FROZEN_BACKTEST,
        {"strategy_id": "orderflow.momentum.aggression", "symbol": "BTCUSDT", "period": "OOS", "dataset_id": "dataset", "dataset_revision": "v1"},
    )
    assert not res.authorized
    assert "OOS_SEALED" in res.reason


def test_research_capabilities_block_unqualified_symbol_backtest():
    boundary = ResearchCapabilityBoundary(_registry())
    res = boundary.authorize(
        ResearchActionKind.RUN_FROZEN_BACKTEST,
        {"strategy_id": "orderflow.momentum.aggression", "symbol": "ZECUSDT", "period": "IS", "dataset_id": "dataset", "dataset_revision": "v1"},
    )
    assert not res.authorized
    assert "SYMBOL_NOT_QUALIFIED" in res.reason


def test_research_capabilities_allow_typed_read_actions():
    boundary = ResearchCapabilityBoundary(_registry())
    assert boundary.authorize(ResearchActionKind.SCAN_MARKET, {
        "selection_mode": "FORWARD_SELECTED", "universe": "test", "selection_rule_version": "v1",
    }).authorized
    assert boundary.authorize(ResearchActionKind.INSPECT_SYMBOL, {"symbol": "BTCUSDT"}).authorized
    assert boundary.authorize(ResearchActionKind.GET_RUN_STATUS, {}).authorized
    assert boundary.authorize(ResearchActionKind.READ_CANONICAL_RESULTS, {
        "symbol": "BTCUSDT",
        "dataset_id": "orderflow-binance-futures-14d-20260623-20260706-v1",
        "dataset_revision": "v1",
    }).authorized


def test_point_in_time_scan_requires_snapshot_evidence():
    boundary = ResearchCapabilityBoundary()
    rejected = boundary.authorize(ResearchActionKind.SCAN_MARKET, {
        "selection_mode": "POINT_IN_TIME", "universe": "test", "selection_rule_version": "v1",
    })
    assert not rejected.authorized
    assert rejected.reason == "POINT_IN_TIME_UNAVAILABLE: historical scanner reconstruction is not implemented"


def test_btc_regression_hash_verification():
    verification = MultiSymbolResearchPipeline.verify_btc_regression(BTC_CANONICAL_FRAME_HASH)
    assert verification["status"] == "PASS"

    drifted = MultiSymbolResearchPipeline.verify_btc_regression("bad_hash_value")
    assert drifted["status"] == "FAIL"
    assert "SEMANTIC_DRIFT" in drifted["note"]


def test_forward_research_cohort_prevents_historical_leakage():
    cohort = ForwardResearchCohort(
        selection_timestamp="2026-09-27T12:00:00Z",
        symbols=["ZECUSDT", "SUIUSDT", "WLDUSDT", "SOLUSDT"],
        selection_fingerprint="abc123hash",
    )
    # A cohort selected on 2026-09-27 cannot be back-dated to the June IS.
    with pytest.raises(ValueError, match="TEMPORAL_LEAKAGE_GUARD"):
        cohort.assert_not_historical("2026-06-23T00:00:00Z")
    # It is valid only as a prospective cohort after its selection timestamp.
    cohort.assert_not_historical("2026-09-28T00:00:00Z")



def test_frozen_qualification_allows_btc_and_blocks_partial_eth():
    registry = _registry()
    assert registry.require_replay_authority("BTCUSDT").coverage_pct == 100.0
    with pytest.raises(ValueError, match="ETHUSDC is PARTIAL"):
        registry.require_replay_authority("ETHUSDC")
    with pytest.raises(ValueError, match="DATASET_ID_MISMATCH"):
        registry.require_dataset_replay_authority("BTCUSDT", "other-dataset")


def test_build_and_replay_require_frozen_dataset_identity():
    boundary = ResearchCapabilityBoundary(_registry())
    rejected = boundary.authorize(
        ResearchActionKind.BUILD_FRAMES,
        {"symbol": "BTCUSDT", "dataset_id": "wrong-dataset", "dataset_revision": "v1", "period": "IS"},
    )
    assert not rejected.authorized
    assert "DATASET_ID_MISMATCH" in rejected.reason

    rejected = boundary.authorize(
        ResearchActionKind.RUN_FROZEN_BACKTEST,
        {
            "strategy_id": "orderflow.momentum.aggression",
            "symbol": "BTCUSDT",
            "dataset_id": "wrong-dataset", "dataset_revision": "v1",
            "period": "IS",
        },
    )
    assert not rejected.authorized
    assert "DATASET_ID_MISMATCH" in rejected.reason


def test_forward_cohort_before_is_is_not_historical_leakage():
    cohort = ForwardResearchCohort(
        selection_timestamp="2026-06-22T22:00:00Z",
        symbols=["BTCUSDT"],
        selection_fingerprint="pre-is",
    )
    cohort.assert_not_historical("2026-06-23T00:00:00Z")
