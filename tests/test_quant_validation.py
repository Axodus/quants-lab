"""Comprehensive unit and integration tests for REQ-QUANT-05 Out-of-Sample, Walk-Forward & Statistical Validation."""

import unittest
from decimal import Decimal

from core.quant_foundations.models import DatasetManifest, ExperimentDefinition
from core.quant_simulation.execution import DeterministicExecutionModel
from core.quant_simulation.models import ExecutionAssumptionProfile, ReferenceThresholdStrategy
from core.quant_validation import (
    DatasetSegment,
    MetricComparison,
    OOSResult,
    SelectionSnapshot,
    StatisticalAnalyzer,
    StatisticalDiagnostics,
    TemporalLeakageError,
    ValidationPlan,
    ValidationPlanExecutor,
    ValidationResult,
    WalkForwardPlan,
)


def _dataset_ref():
    return {
        "datasetId": "fixture:btc-usdt-1m",
        "datasetVersion": "1.0.0",
        "contentDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "sourceRef": "fixture:synthetic",
        "venueScope": ["synthetic"],
        "instrumentScope": ["BTC-USDT"],
        "temporalCoverage": {"from": "2024-01-01T00:00:00Z", "to": "2024-01-01T00:08:00Z"},
        "resolution": "1m",
        "schemaVersion": "market-observation-candle-v1",
        "qualityStatus": "verified",
    }


def _experiment(dataset_ref=None, revision_id="strat:rev-1"):
    ref = dataset_ref or _dataset_ref()
    return ExperimentDefinition(
        experiment_id="exp:quant-05",
        experiment_revision="1.0.0",
        strategy_revision_id=revision_id,
        strategy_revision_digest="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        dataset_refs=(ref,),
        feature_definitions=({"featureId": "close", "featureVersion": "1.0.0", "schema": "scalar"},),
        parameter_space={"entry": [95, 100], "exit": [105, 110]},
        execution_assumptions={"profile": "deterministic-candle-v1"},
        validation_methodology={"methodology_ref": "deterministic-validation-v1"},
        seed_policy={"policy": "deterministic"},
        runtime_provenance={"python": "3.12", "commit": "local-fixture"},
    )


def _market_states(n=8, trend="up"):
    states = []
    for i in range(n):
        price = "100" if i < 2 else ("110" if trend == "up" else "90")
        states.append({
            "marketStateId": f"historical:fixture:{i}",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "marketTime": f"2024-01-01T00:0{i}:00.000Z",
            "observedAt": f"2024-01-01T00:0{i}:00.000Z",
            "validityContext": "historical",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": price, "computedAt": f"2024-01-01T00:0{i}:00.000Z"}],
        })
    return states


class QuantValidationTests(unittest.TestCase):
    def setUp(self):
        self.dataset_ref = _dataset_ref()
        self.experiment = _experiment(self.dataset_ref)
        self.executor = ValidationPlanExecutor()

    def test_statistical_analyzer_moments_and_sharpe(self):
        vals = [Decimal("8"), Decimal("10"), Decimal("12")]
        diag = StatisticalAnalyzer.analyze_series("netPnl", vals, annualization_factor=252)
        self.assertEqual(diag.mean, "10")
        self.assertEqual(diag.sample_count, 3)
        self.assertEqual(diag.positive_fold_ratio, "1")
        self.assertIsNotNone(diag.annualized_sharpe_estimate)
        self.assertIsNotNone(diag.confidence_interval_95)

    def test_holdout_execution_and_degradation(self):
        plan = ValidationPlan(
            plan_id="plan:holdout:1",
            strategy_revision_id="strat:rev-1",
            dataset_reference=self.dataset_ref,
            validation_mode="HOLDOUT",
            temporal_coverage={"from": "2024-01-01T00:00:00.000Z", "to": "2024-01-01T00:07:00.000Z"},
            holdout_is_ratio="0.5",
        )
        snapshot = SelectionSnapshot(
            snapshot_id="snap:1",
            strategy_revision_id="strat:rev-1",
            selected_parameters={"entry": "100", "exit": "110"},
            selection_methodology="grid-best",
            source_trial_id="trial:1",
            candidate_population_size=100,
            metrics_at_selection={"netPnl": "20"},
            is_segment_ref="2024-01-01T00:03:00.000Z",
        )

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        states = _market_states(8, trend="down")
        val_res = self.executor.execute_holdout(plan, self.experiment, states, snapshot, factory, split_index=4)

        self.assertEqual(val_res.status, "COMPLETED")
        self.assertTrue(val_res.selection_bias_warning)
        self.assertEqual(val_res.candidate_population_size, 100)
        self.assertIsNotNone(val_res.oos_result)
        self.assertEqual(val_res.oos_result.comparisons["netPnl"].degradation_pct, "100")
        acs = val_res.to_acs_evidence(1726750000000)
        self.assertEqual(acs["kind"], "artifact")
        self.assertEqual(acs["payload_digest"], val_res.result_digest)

    def test_holdout_leakage_rejection(self):
        plan = ValidationPlan(
            plan_id="plan:holdout:leak",
            strategy_revision_id="strat:rev-1",
            dataset_reference=self.dataset_ref,
            validation_mode="HOLDOUT",
            temporal_coverage={"from": "2024-01-01T00:00:00.000Z", "to": "2024-01-01T00:07:00.000Z"},
            holdout_is_ratio="0.5",
        )
        leaked_snapshot = SelectionSnapshot(
            snapshot_id="snap:leak",
            strategy_revision_id="strat:rev-1",
            selected_parameters={"entry": "100", "exit": "110"},
            selection_methodology="grid-best",
            source_trial_id="trial:leak",
            candidate_population_size=1,
            metrics_at_selection={"netPnl": "20"},
            is_segment_ref="2024-01-01T00:06:00.000Z",
        )

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        states = _market_states(8)
        with self.assertRaises(TemporalLeakageError):
            self.executor.execute_holdout(plan, self.experiment, states, leaked_snapshot, factory, split_index=4)

    def test_walk_forward_rolling_and_expanding(self):
        for mode in ["ROLLING", "EXPANDING"]:
            wf_plan = WalkForwardPlan(
                train_window_steps=2,
                test_window_steps=2,
                step_size=2,
                mode=mode,
            )
            plan = ValidationPlan(
                plan_id=f"plan:wf:{mode.lower()}",
                strategy_revision_id="strat:rev-1",
                dataset_reference=self.dataset_ref,
                validation_mode=f"WALK_FORWARD_{mode}",
                temporal_coverage={"from": "2024-01-01T00:00:00.000Z", "to": "2024-01-01T00:07:00.000Z"},
                walk_forward_plan=wf_plan,
            )

            def reselection_fn(train_states, fold_ordinal):
                return SelectionSnapshot(
                    snapshot_id=f"snap:fold:{fold_ordinal}",
                    strategy_revision_id="strat:rev-1",
                    selected_parameters={"entry": "100", "exit": "110"},
                    selection_methodology="fold-reselection",
                    source_trial_id=f"trial:fold:{fold_ordinal}",
                    candidate_population_size=5,
                    metrics_at_selection={"netPnl": "10"},
                    is_segment_ref=str(train_states[-1]["marketTime"]),
                )

            def factory(params):
                return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

            states = _market_states(8, trend="up")
            val_res = self.executor.execute_walk_forward(plan, self.experiment, states, reselection_fn, factory)

            self.assertEqual(val_res.status, "COMPLETED")
            self.assertEqual(len(val_res.fold_results), 3)
            self.assertIn("netPnl", val_res.statistical_diagnostics)
            self.assertEqual(val_res.statistical_diagnostics["netPnl"].sample_count, 3)

    def test_walk_forward_preserves_failed_fold(self):
        wf_plan = WalkForwardPlan(
            train_window_steps=2,
            test_window_steps=2,
            step_size=2,
            mode="ROLLING",
        )
        plan = ValidationPlan(
            plan_id="plan:wf:fail",
            strategy_revision_id="strat:rev-1",
            dataset_reference=self.dataset_ref,
            validation_mode="WALK_FORWARD_ROLLING",
            temporal_coverage={"from": "2024-01-01T00:00:00.000Z", "to": "2024-01-01T00:07:00.000Z"},
            walk_forward_plan=wf_plan,
        )

        def reselection_fn(train_states, fold_ordinal):
            return SelectionSnapshot(
                snapshot_id=f"snap:fail:{fold_ordinal}",
                strategy_revision_id="strat:rev-1",
                selected_parameters={"entry": "100", "exit": "110", "fail": fold_ordinal == 1},
                selection_methodology="fold-reselection",
                source_trial_id=f"trial:{fold_ordinal}",
                candidate_population_size=1,
                metrics_at_selection={"netPnl": "10"},
                is_segment_ref=str(train_states[-1]["marketTime"]),
            )

        def factory(params):
            if params.get("fail"):
                raise ValueError("simulated fold failure")
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        states = _market_states(8, trend="up")
        val_res = self.executor.execute_walk_forward(plan, self.experiment, states, reselection_fn, factory)

        self.assertEqual(val_res.status, "PARTIAL")
        self.assertEqual(len(val_res.fold_results), 3)
        self.assertEqual(val_res.fold_results[1].status, "FAILED")
        self.assertIn("simulated fold failure", val_res.fold_results[1].error_reason or "")
        self.assertTrue(any("FAILED_FOLDS_PRESENT" in lim for lim in val_res.limitations))

    def test_reproducibility_deterministic_replay(self):
        wf_plan = WalkForwardPlan(train_window_steps=2, test_window_steps=2, step_size=2, mode="ROLLING")
        plan = ValidationPlan(
            plan_id="plan:wf:replay",
            strategy_revision_id="strat:rev-1",
            dataset_reference=self.dataset_ref,
            validation_mode="WALK_FORWARD_ROLLING",
            temporal_coverage={"from": "2024-01-01T00:00:00.000Z", "to": "2024-01-01T00:07:00.000Z"},
            walk_forward_plan=wf_plan,
        )

        def reselection(train_states, fold_ordinal):
            return SelectionSnapshot(
                snapshot_id=f"snap:{fold_ordinal}",
                strategy_revision_id="strat:rev-1",
                selected_parameters={"entry": "100", "exit": "110"},
                selection_methodology="replay",
                source_trial_id=f"trial:{fold_ordinal}",
                candidate_population_size=1,
                metrics_at_selection={"netPnl": "10"},
                is_segment_ref=str(train_states[-1]["marketTime"]),
            )

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        states = _market_states(8, trend="up")
        res1 = self.executor.execute_walk_forward(plan, self.experiment, states, reselection, factory)
        res2 = self.executor.execute_walk_forward(plan, self.experiment, states, reselection, factory)

        self.assertEqual(res1.result_digest, res2.result_digest)
        self.assertEqual(res1.validation_id, res2.validation_id)


if __name__ == "__main__":
    unittest.main()
