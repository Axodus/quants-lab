"""Comprehensive unit and integration tests for REQ-QUANT-06 Strategy Evidence Qualification Gate."""

import unittest
from decimal import Decimal

from core.quant_foundations.models import ExperimentDefinition, ExperimentReference, StrategyEvidence
from core.quant_promotion import (
    EvidenceVerificationError,
    PromotionEligibilityResult,
    QualificationReasonCode,
    StrategyEvidencePackage,
    StrategyEvidenceQualificationGate,
    StrategyQualificationPolicy,
    TargetStagePolicy,
)
from core.quant_robustness.models import (
    CandidateRegion,
    MetricSummary,
    ParameterRobustnessReport,
)
from core.quant_simulation.models import SimulationResult
from core.quant_validation.models import (
    DatasetSegment,
    FoldResult,
    MetricComparison,
    OOSResult,
    SelectionSnapshot,
    StatisticalDiagnostics,
    ValidationResult,
)


def _dataset_ref(dataset_id="fixture:btc-usdt-1m", version="1.0.0"):
    return {
        "datasetId": dataset_id,
        "datasetVersion": version,
        "contentDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "sourceRef": "fixture:synthetic",
        "venueScope": ["synthetic"],
        "instrumentScope": ["BTC-USDT"],
        "temporalCoverage": {"from": "2024-01-01T00:00:00Z", "to": "2024-01-01T00:08:00Z"},
        "resolution": "1m",
        "schemaVersion": "market-observation-candle-v1",
        "qualityStatus": "verified",
    }


def _experiment_ref(strat_rev="strat:rev-1"):
    return ExperimentReference(
        experiment_id="exp:quant-06",
        experiment_revision="1.0.0",
        strategy_revision_id=strat_rev,
        dataset_refs=(_dataset_ref(),),
        feature_definitions=({"featureId": "close", "featureVersion": "1.0.0"},),
        methodology_ref="test-methodology-v1",
        status="completed",
        provenance_ref="test-provenance-ref",
    )


def _simulation_result(strat_rev="strat:rev-1", status="COMPLETED", latency=1, max_dd="0.05"):
    return SimulationResult(
        run_id="sim:run:1",
        status=status,
        experiment_id="exp:quant-06",
        experiment_revision="1.0.0",
        strategy_revision_id=strat_rev,
        dataset_reference=_dataset_ref(),
        execution_profile={"feeBps": "10.0", "spreadBps": "5.0", "slippageBps": "2.0", "latencySteps": latency},
        orders=(),
        fills=(),
        equity_series=(),
        metrics={"netPnl": "50.0", "maxDrawdown": max_dd, "totalReturn": "0.05"},
        limitations=(),
    )


def _robustness_report(strat_rev="strat:rev-1", classification="STABLE_REGION", failed_trials=0):
    cand = CandidateRegion(
        region_id="reg:1",
        center_trial_id="trial:center",
        center_parameters={"entry": "100", "exit": "110"},
        neighborhood_trial_ids=("trial:n1", "trial:n2"),
        classification=classification,
        diagnostic_summary="Diagnostic test",
    )
    return ParameterRobustnessReport(
        report_id="rob:rep:1",
        experiment_id="exp:quant-06",
        experiment_revision="1.0.0",
        strategy_revision_id=strat_rev,
        dataset_reference=_dataset_ref(),
        total_attempted_trials=10,
        completed_trials_count=10 - failed_trials,
        failed_trials_count=failed_trials,
        invalid_combinations_count=0,
        search_method="GRID",
        tested_parameter_space={"entry": [95, 100], "exit": [105, 110]},
        metric_distributions={},
        highest_metric_trial_id="trial:center",
        reference_trial_id="trial:center",
        candidate_regions=(cand,),
        boundary_optimum_detected=False,
        isolated_peaks_detected=(),
        cost_sensitivity_findings=(),
        limitations=(),
        runtime_provenance={},
    )


def _validation_result(strat_rev="strat:rev-1", status="COMPLETED", fold_statuses=("COMPLETED", "COMPLETED", "COMPLETED")):
    folds = []
    for idx, f_stat in enumerate(fold_statuses):
        seg_tr = DatasetSegment(f"seg:tr:{idx}", "fixture:btc-usdt-1m", "1.0.0", "IN_SAMPLE", f"2024-01-01T00:0{idx}:00Z", f"2024-01-01T00:0{idx+1}:00Z", 1)
        seg_te = DatasetSegment(f"seg:te:{idx}", "fixture:btc-usdt-1m", "1.0.0", "OUT_OF_SAMPLE", f"2024-01-01T00:0{idx+1}:00Z", f"2024-01-01T00:0{idx+2}:00Z", 1)
        snap = SelectionSnapshot(f"snap:{idx}", strat_rev, {"entry": "100"}, "method", f"trial:{idx}", 5, {"netPnl": "10"}, "ref")
        folds.append(FoldResult(idx, seg_tr, seg_te, snap, "digest", {"netPnl": "10"}, f_stat))

    oos = OOSResult(
        result_id="oos:res:1",
        plan_id="plan:1",
        strategy_revision_id=strat_rev,
        selection_snapshot=folds[0].selection_snapshot,
        oos_segment=folds[0].test_segment,
        simulation_result_digest="digest",
        metrics={"netPnl": "10"},
        comparisons={"netPnl": MetricComparison("10", "10", "0")},
        limitations=(),
    )
    pnl_diag = StatisticalDiagnostics(
        metric_name="netPnl",
        mean="10",
        std_dev="2",
        median="10",
        positive_fold_ratio="1.0",
        annualized_sharpe_estimate="2.5",
        confidence_interval_95=("8", "12"),
        sample_count=len(folds),
    )
    return ValidationResult(
        validation_id="val:res:1",
        plan_id="plan:1",
        strategy_revision_id=strat_rev,
        dataset_reference=_dataset_ref(),
        status=status,
        oos_result=oos,
        fold_results=tuple(folds),
        statistical_diagnostics={"netPnl": pnl_diag},
        selection_bias_warning=False,
        candidate_population_size=5,
        limitations=(),
    )


def _canonical_policy():
    return StrategyQualificationPolicy(
        policy_id="policy:axodus:canonical-v1",
        policy_version="1.0.0",
        stage_policies=(
            TargetStagePolicy(
                target_state="ROBUSTNESS",
                allowed_source_states=("BACKTEST",),
                require_simulation=True,
                require_robustness=False,
                require_oos=False,
                require_walk_forward=False,
            ),
            TargetStagePolicy(
                target_state="PAPER",
                allowed_source_states=("ROBUSTNESS",),
                require_simulation=True,
                require_robustness=True,
                require_stable_region=True,
                max_drawdown_limit=Decimal("0.20"),
            ),
            TargetStagePolicy(
                target_state="TESTNET",
                allowed_source_states=("PAPER",),
                require_simulation=True,
                require_robustness=True,
                require_oos=True,
                require_walk_forward=True,
                require_stable_region=True,
                disallow_zero_latency=True,
                min_positive_fold_ratio=Decimal("0.60"),
                min_sharpe_estimate=Decimal("1.0"),
                max_drawdown_limit=Decimal("0.15"),
                max_evidence_age_seconds=86400,
            ),
        ),
    )


class QuantPromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.policy = _canonical_policy()
        self.gate = StrategyEvidenceQualificationGate(self.policy)
        self.eval_time = "2024-01-02T00:00:00Z"

    def test_scenario_a_complete_qualifying_evidence(self):
        # Qualifying transition: PAPER -> TESTNET
        strat_rev = "strat:rev-1"
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev, latency=1, max_dd="0.05"),
            robustness_report=_robustness_report(strat_rev, classification="STABLE_REGION"),
            validation_result=_validation_result(strat_rev, status="COMPLETED"),
            provenance_metadata={"createdAt": "2024-01-01T12:00:00Z"},
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "ELIGIBLE")
        self.assertEqual(len(res.violations), 0)
        self.assertEqual(res.current_state, "PAPER")
        self.assertEqual(res.target_state, "TESTNET")
        self.assertTrue(len(res.evidence_refs) >= 4)

        # ACS Evidence Envelope Conformance
        acs = res.to_acs_evidence(1726750000000)
        self.assertEqual(acs["kind"], "artifact")
        self.assertEqual(acs["subject_ref"]["kind"], "promotion-eligibility")
        self.assertEqual(acs["payload_digest"], res.result_digest)

    def test_scenario_b_missing_evidence(self):
        strat_rev = "strat:rev-1"
        # Missing validation_result and robustness_report for TESTNET target
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev),
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.EVIDENCE_MISSING.value, res.violations)

    def test_scenario_c_aud_q01_partial_walk_forward_fails(self):
        # MANDATORY AUD-Q-01 REGRESSION:
        # Fold 1 COMPLETE, Fold 2 FAILED, Fold 3 COMPLETE -> validation status PARTIAL
        strat_rev = "strat:rev-1"
        val_res = _validation_result(strat_rev, status="PARTIAL", fold_statuses=("COMPLETED", "FAILED", "COMPLETED"))
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev, latency=1),
            robustness_report=_robustness_report(strat_rev),
            validation_result=val_res,
            provenance_metadata={"createdAt": "2024-01-01T12:00:00Z"},
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.WALK_FORWARD_INCOMPLETE.value, res.violations)

    def test_scenario_d_failed_robustness_isolated_peak(self):
        strat_rev = "strat:rev-1"
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev, latency=1),
            robustness_report=_robustness_report(strat_rev, classification="ISOLATED_PEAK"),
            validation_result=_validation_result(strat_rev),
            provenance_metadata={"createdAt": "2024-01-01T12:00:00Z"},
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.ROBUSTNESS_INSUFFICIENT.value, res.violations)

    def test_scenario_e_strategy_revision_mismatch(self):
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id="strat:rev-2",  # Package has rev-2
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref("strat:rev-2"),),
            simulation_result=_simulation_result("strat:rev-2"),
        )
        # Request evaluates rev-1
        res = self.gate.evaluate("strat:1", "strat:rev-1", "BACKTEST", "ROBUSTNESS", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value, res.violations)

    def test_scenario_f_stale_evidence(self):
        strat_rev = "strat:rev-1"
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev, latency=1),
            robustness_report=_robustness_report(strat_rev),
            validation_result=_validation_result(strat_rev),
            provenance_metadata={"createdAt": "2023-01-01T00:00:00Z"},  # 1 year old (> 86400s)
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.EVIDENCE_STALE.value, res.violations)

    def test_scenario_g_invalid_transition(self):
        strat_rev = "strat:rev-1"
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev),
        )
        # Disallowed transition: IDEA -> TESTNET
        res = self.gate.evaluate("strat:1", strat_rev, "IDEA", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.TRANSITION_INVALID.value, res.violations)

    def test_zero_latency_rejection_for_higher_stage(self):
        strat_rev = "strat:rev-1"
        pkg = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(_dataset_ref(),),
            experiment_refs=(_experiment_ref(strat_rev),),
            simulation_result=_simulation_result(strat_rev, latency=0),  # Latency 0
            robustness_report=_robustness_report(strat_rev),
            validation_result=_validation_result(strat_rev),
            provenance_metadata={"createdAt": "2024-01-01T12:00:00Z"},
        )
        res = self.gate.evaluate("strat:1", strat_rev, "PAPER", "TESTNET", pkg, self.eval_time)
        self.assertEqual(res.decision, "NOT_ELIGIBLE")
        self.assertIn(QualificationReasonCode.ZERO_LATENCY_DISALLOWED.value, res.violations)

    def test_deterministic_replay_and_permutation_invariance(self):
        strat_rev = "strat:rev-1"
        ds1 = _dataset_ref("ds:1")
        ds2 = _dataset_ref("ds:2")
        exp1 = _experiment_ref(strat_rev)
        pkg1 = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(ds1, ds2),
            experiment_refs=(exp1,),
            simulation_result=_simulation_result(strat_rev, latency=1),
        )
        pkg2 = StrategyEvidencePackage(
            strategy_id="strat:1",
            strategy_revision_id=strat_rev,
            dataset_refs=(ds2, ds1),  # Permuted datasets
            experiment_refs=(exp1,),
            simulation_result=_simulation_result(strat_rev, latency=1),
        )
        res1 = self.gate.evaluate("strat:1", strat_rev, "BACKTEST", "ROBUSTNESS", pkg1, self.eval_time)
        res2 = self.gate.evaluate("strat:1", strat_rev, "BACKTEST", "ROBUSTNESS", pkg2, self.eval_time)
        self.assertEqual(res1.result_digest, res2.result_digest)
        self.assertEqual(res1.eligibility_id, res2.eligibility_id)


if __name__ == "__main__":
    unittest.main()
