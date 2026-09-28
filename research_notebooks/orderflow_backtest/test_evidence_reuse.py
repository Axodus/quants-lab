"""Tests for Evidence Reuse Registry & Deployment Routing."""
from __future__ import annotations

from pathlib import Path

from orderflow_backtest.evidence_reuse import (
    CurrentMarketInfo,
    DeploymentState,
    EvidenceReuseRegistry,
    OperationalDeploymentContext,
    ReuseClassification,
    ValidationCellRecord,
)
from orderflow_backtest.instrument_spec import InstrumentSpec
from orderflow_backtest.research_capabilities import (
    ResearchActionKind,
    ResearchCapabilityBoundary,
)
from orderflow_backtest.research_executor import BoundedResearchExecutor


def test_registry_lookup_not_validated():
    registry = EvidenceReuseRegistry()
    classification, reason, blockers = registry.classify(
        "ZECUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1"
    )
    assert classification == ReuseClassification.NOT_VALIDATED
    assert "FIRST_TIME_PAIR_OR_STRATEGY" in reason
    assert "NO_PREVIOUS_VALIDATION" in blockers


def test_is_only_evidence_cannot_skip_validation():
    registry = EvidenceReuseRegistry()
    registry.register_cell(ValidationCellRecord(
        symbol="BTCUSDT",
        strategy_id="orderflow.momentum.aggression",
        strategy_revision="freeze-2026-09-24-adapter-v1",
        is_evidence={"trade_count": 139},
        validation_status="ACCEPTED_AS_NEGATIVE_IS",
    ))
    classification, _, blockers = registry.classify(
        "BTCUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1"
    )
    assert classification == ReuseClassification.VALIDATED_BUT_INCOMPATIBLE
    assert "OOS_NOT_VALIDATED" in blockers


def test_validated_reusable_requires_matching_invariants():
    spec = InstrumentSpec.from_registry("BTCUSDT")
    registry = EvidenceReuseRegistry()
    registry.register_cell(ValidationCellRecord(
        symbol="BTCUSDT",
        strategy_id="orderflow.momentum.aggression",
        strategy_revision="freeze-2026-09-24-adapter-v1",
        instrument_spec_hash=spec.canonical_hash(),
        fee_model="BTCUSDT maker=0.0002 taker=0.0005",
        execution_model="execution-model-v1-conservative",
        is_evidence={"trade_count": 139},
        oos_evidence={"trade_count": 50},
        validation_status="ACCEPTED",
    ))
    classification, _, _ = registry.classify(
        "BTCUSDT",
        "orderflow.momentum.aggression",
        "freeze-2026-09-24-adapter-v1",
        current_spec=spec,
        current_fee_model="BTCUSDT maker=0.0002 taker=0.0005",
        current_execution_model="execution-model-v1-conservative",
    )
    assert classification == ReuseClassification.VALIDATED_REUSABLE


def test_stale_and_redesigned_cells_fail_closed():
    registry = EvidenceReuseRegistry()
    registry.register_cell(ValidationCellRecord(
        symbol="BTCUSDT",
        strategy_id="orderflow.momentum.aggression",
        strategy_revision="freeze-2026-09-24-adapter-v1",
        is_evidence={"trade_count": 1},
        oos_evidence={"trade_count": 1},
        validation_status="ACCEPTED",
    ))
    stale, _, _ = registry.classify(
        "BTCUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1",
        is_stale=True,
    )
    redesigned, reason, _ = registry.classify(
        "BTCUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1",
        is_redesigned=True,
    )
    assert stale == ReuseClassification.VALIDATED_BUT_STALE
    assert redesigned == ReuseClassification.VALIDATED_BUT_INCOMPATIBLE
    assert "STRATEGY_REDESIGNED" in reason


def test_current_market_compatibility_fails_closed():
    registry = EvidenceReuseRegistry()
    record = ValidationCellRecord(
        symbol="BTCUSDT",
        strategy_id="orderflow.momentum.aggression",
        strategy_revision="freeze-2026-09-24-adapter-v1",
    )
    registry.register_cell(record)
    result = registry.check_current_compatibility(
        record, CurrentMarketInfo(symbol="BTCUSDT", is_listed=False)
    )
    assert result["compatible"] is False
    assert "SYMBOL_DELISTED_OR_INACTIVE" in result["failures"]


def test_reusable_cell_can_reach_testnet_but_not_mainnet_by_default():
    spec = InstrumentSpec.from_registry("BTCUSDT")
    registry = EvidenceReuseRegistry()
    registry.register_cell(ValidationCellRecord(
        symbol="BTCUSDT",
        strategy_id="orderflow.momentum.aggression",
        strategy_revision="freeze-2026-09-24-adapter-v1",
        instrument_spec_hash=spec.canonical_hash(),
        is_evidence={"trade_count": 10},
        oos_evidence={"trade_count": 10},
        validation_status="ACCEPTED",
    ))
    result = registry.evaluate_deployment_readiness(
        "BTCUSDT",
        "orderflow.momentum.aggression",
        "freeze-2026-09-24-adapter-v1",
        operational_context=OperationalDeploymentContext(),
    )
    assert result.reuse_classification == ReuseClassification.VALIDATED_REUSABLE.value
    assert result.deployment_state == DeploymentState.DEPLOYMENT_CANDIDATE_READY.value
    assert result.testnet == "CONDOR_OWNED"
    assert result.mainnet == "CONDOR_OWNED"
    candidate = registry.export_deployment_candidate(
        "BTCUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1"
    )
    assert candidate.deployment_candidate is True
    assert candidate.symbol == "BTCUSDT"


def test_route_hot_asset_is_per_strategy():
    registry = EvidenceReuseRegistry.create_default_registry()
    result = registry.route_hot_asset("ZECUSDT")
    assert set(result) == {
        "orderflow.momentum.aggression",
        "orderflow.absorption.fade",
        "orderflow.cvd.divergence.reversal",
    }
    assert all(
        item.reuse_classification == ReuseClassification.NOT_VALIDATED.value
        for item in result.values()
    )
    assert all(
        item.deployment_state == DeploymentState.HISTORICAL_VALIDATION_REQUIRED.value
        for item in result.values()
    )


def test_capability_boundary_accepts_reuse_actions_and_rejects_unknown_strategy():
    boundary = ResearchCapabilityBoundary()
    args = {
        "symbol": "BTCUSDT",
        "strategy_id": "orderflow.momentum.aggression",
        "strategy_revision": "freeze-2026-09-24-adapter-v1",
    }
    assert boundary.authorize(ResearchActionKind.CHECK_EVIDENCE_REUSE, args).authorized
    assert boundary.authorize(ResearchActionKind.EVALUATE_DEPLOYMENT_ROUTING, args).authorized
    rejected = boundary.authorize(
        ResearchActionKind.CHECK_EVIDENCE_REUSE,
        {**args, "strategy_id": "unknown"},
    )
    assert not rejected.authorized
    assert "UNKNOWN_STRATEGY" in rejected.reason


def test_bounded_executor_persists_read_only_reuse_receipt(tmp_path: Path):
    registry = EvidenceReuseRegistry()
    executor = BoundedResearchExecutor(
        data_root=tmp_path,
        state_root=tmp_path / "state",
        boundary=ResearchCapabilityBoundary(),
        handlers={
            ResearchActionKind.CHECK_EVIDENCE_REUSE: lambda args: {
                "classification": registry.classify(
                    args["symbol"],
                    args["strategy_id"],
                    args["strategy_revision"],
                )[0].value
            }
        },
    )
    result = executor.execute(
        ResearchActionKind.CHECK_EVIDENCE_REUSE,
        {
            "symbol": "ZECUSDT",
            "strategy_id": "orderflow.momentum.aggression",
            "strategy_revision": "freeze-2026-09-24-adapter-v1",
        },
    )
    assert result["classification"] == ReuseClassification.NOT_VALIDATED.value
    receipts = list((tmp_path / "state").glob("*.json"))
    assert len(receipts) == 1


def test_deployment_candidate_contract_validates_and_rejects_secrets():
    registry = EvidenceReuseRegistry.create_default_registry()
    # Negative research cannot be candidate
    btc_mom = registry.lookup("BTCUSDT", "orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1")
    assert btc_mom is not None
    cand_negative = btc_mom.to_deployment_candidate()
    assert cand_negative.deployment_candidate is False
    assert cand_negative.research_status == "VALIDATED_NEGATIVE"

    # Positive record converts
    rec_pos = ValidationCellRecord(
        symbol="WLDUSDT",
        strategy_id="orderflow.absorption.fade",
        strategy_revision="freeze-2026-09-24-adapter-v1",
        validation_status="OOS_EVIDENCE_POSITIVE",
    )
    cand_pos = rec_pos.to_deployment_candidate()
    assert cand_pos.deployment_candidate is True
    assert cand_pos.research_status == "VALIDATED_POSITIVE"

    # Candidate with leaked secret fails
    cand_secret = rec_pos.to_deployment_candidate()
    cand_dict = cand_secret.to_dict()
    cand_dict["execution_requirements"]["api_key"] = "leaked"
    from orderflow_backtest.evidence_reuse import DeploymentCandidate
    import pytest
    with pytest.raises(ValueError, match="DEPLOYMENT_CANDIDATE_SECRET_FIELD"):
        DeploymentCandidate(**cand_dict).validate()
