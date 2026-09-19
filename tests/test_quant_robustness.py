"""Comprehensive unit and integration tests for REQ-QUANT-04 Parameter Robustness & Mass Trials."""

import unittest
from decimal import Decimal

from core.quant_foundations.models import DatasetManifest, ExperimentDefinition
from core.quant_robustness import (
    CandidateRegion,
    GridTrialGenerator,
    MassTrialRunner,
    ParameterConstraint,
    ParameterDefinition,
    ParameterRobustnessAnalyzer,
    ParameterRobustnessReport,
    ParameterSpace,
    TrialBudget,
    TrialBudgetExceededError,
)
from core.quant_simulation.execution import DeterministicExecutionModel
from core.quant_simulation.models import ExecutionAssumptionProfile, ReferenceThresholdStrategy


def _dataset_ref():
    return {
        "datasetId": "fixture:btc-usdt-1m",
        "datasetVersion": "1.0.0",
        "contentDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "sourceRef": "fixture:synthetic",
        "venueScope": ["synthetic"],
        "instrumentScope": ["BTC-USDT"],
        "temporalCoverage": {"from": "2024-01-01T00:00:00Z", "to": "2024-01-01T00:03:00Z"},
        "resolution": "1m",
        "schemaVersion": "market-observation-candle-v1",
        "qualityStatus": "verified",
    }


def _experiment(dataset_ref=None, revision_id="strat:rev-1"):
    ref = dataset_ref or _dataset_ref()
    return ExperimentDefinition(
        experiment_id="exp:quant-04",
        experiment_revision="1.0.0",
        strategy_revision_id=revision_id,
        strategy_revision_digest="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        dataset_refs=(ref,),
        feature_definitions=({"featureId": "close", "featureVersion": "1.0.0", "schema": "scalar"},),
        parameter_space={"entry": [95, 100, 105], "exit": [105, 110, 115]},
        execution_assumptions={"profile": "deterministic-candle-v1"},
        validation_methodology={"methodology_ref": "deterministic-grid-v1"},
        seed_policy={"policy": "deterministic"},
        runtime_provenance={"python": "3.12", "commit": "local-fixture"},
    )


def _market_states():
    return [
        {
            "marketStateId": "historical:fixture:1",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "marketTime": "2024-01-01T00:00:00.000Z",
            "observedAt": "2024-01-01T00:00:00.000Z",
            "validityContext": "historical",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:00:00.000Z"}],
        },
        {
            "marketStateId": "historical:fixture:2",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "marketTime": "2024-01-01T00:01:00.000Z",
            "observedAt": "2024-01-01T00:01:00.000Z",
            "validityContext": "historical",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:01:00.000Z"}],
        },
        {
            "marketStateId": "historical:fixture:3",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "marketTime": "2024-01-01T00:02:00.000Z",
            "observedAt": "2024-01-01T00:02:00.000Z",
            "validityContext": "historical",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "110", "computedAt": "2024-01-01T00:02:00.000Z"}],
        },
        {
            "marketStateId": "historical:fixture:4",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "marketTime": "2024-01-01T00:03:00.000Z",
            "observedAt": "2024-01-01T00:03:00.000Z",
            "validityContext": "historical",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "110", "computedAt": "2024-01-01T00:03:00.000Z"}],
        },
    ]


class QuantRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.param_space = ParameterSpace(
            parameters=(
                ParameterDefinition("entry", "decimal", allowed_values=("95", "100", "105"), default_value="100"),
                ParameterDefinition("exit", "decimal", allowed_values=("105", "110", "115"), default_value="110"),
            ),
            constraints=(
                ParameterConstraint(
                    constraint_id="entry_lt_exit",
                    description="entry must be less than exit",
                    predicate=lambda p: Decimal(str(p["entry"])) < Decimal(str(p["exit"])),
                ),
            ),
        )
        self.dataset_ref = _dataset_ref()
        self.experiment = _experiment(self.dataset_ref)
        self.runner = MassTrialRunner()
        self.analyzer = ParameterRobustnessAnalyzer()

    def test_parameter_space_and_constraint_filtering(self):
        generator = GridTrialGenerator(self.param_space)
        trials, invalid_count = generator.generate(self.experiment, self.dataset_ref)
        self.assertEqual(len(trials), 8)
        self.assertEqual(invalid_count, 1)
        self.assertEqual(self.param_space.cardinality, 9)

    def test_trial_budget_overflow_rejected(self):
        small_budget = TrialBudget(max_generated_trials=5)
        generator = GridTrialGenerator(self.param_space, budget=small_budget)
        with self.assertRaises(TrialBudgetExceededError):
            generator.generate(self.experiment, self.dataset_ref)

    def test_deterministic_mass_trial_execution_and_replay(self):
        generator = GridTrialGenerator(self.param_space)
        trials, invalid_count = generator.generate(self.experiment, self.dataset_ref)

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        exp_ref = self.experiment.to_reference()
        results_first = self.runner.run_trials(self.experiment, exp_ref, self.dataset_ref, _market_states(), factory, trials)
        results_second = self.runner.run_trials(self.experiment, exp_ref, self.dataset_ref, _market_states(), factory, trials)

        self.assertEqual(len(results_first), 8)
        self.assertEqual(tuple(r.trial_id for r in results_first), tuple(r.trial_id for r in results_second))
        self.assertEqual(tuple(r.simulation_result_digest for r in results_first), tuple(r.simulation_result_digest for r in results_second))

    def test_duplicate_trial_suppression(self):
        generator = GridTrialGenerator(self.param_space)
        trials, _ = generator.generate(self.experiment, self.dataset_ref)
        duplicated_trials = trials + trials

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), factory, duplicated_trials)
        self.assertEqual(len(results), 8)

    def test_resume_idempotency(self):
        generator = GridTrialGenerator(self.param_space)
        trials, _ = generator.generate(self.experiment, self.dataset_ref)

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        partial_trials = trials[:4]
        partial_results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), factory, partial_trials)
        cached_map = {r.trial_id: r for r in partial_results}

        full_results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), factory, trials, existing_results=cached_map)
        self.assertEqual(len(full_results), 8)
        for i in range(4):
            self.assertEqual(full_results[i], partial_results[i])

    def test_robustness_report_surfaces_distributions_and_diagnostics(self):
        generator = GridTrialGenerator(self.param_space)
        trials, invalid_count = generator.generate(self.experiment, self.dataset_ref)

        def factory(params):
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), factory, trials)
        report = self.analyzer.analyze(
            experiment_id=self.experiment.experiment_id,
            experiment_revision=self.experiment.experiment_revision,
            strategy_revision_id=self.experiment.strategy_revision_id,
            dataset_reference=self.dataset_ref,
            parameter_space=self.param_space,
            trial_results=results,
            invalid_combinations_count=invalid_count,
            reference_parameters={"entry": "100", "exit": "110"},
        )

        self.assertEqual(report.total_attempted_trials, 9)
        self.assertEqual(report.completed_trials_count, 8)
        self.assertEqual(report.failed_trials_count, 0)
        self.assertEqual(report.invalid_combinations_count, 1)
        self.assertIn("netPnl", report.metric_distributions)
        self.assertTrue(len(report.candidate_regions) > 0)
        acs_evidence = report.to_acs_evidence(1726750000000)
        self.assertEqual(acs_evidence["schema_version"], "1.0")
        self.assertEqual(acs_evidence["subject_ref"]["kind"], "parameter-robustness-report")
        self.assertEqual(acs_evidence["payload_digest"], report.report_digest)

    def test_failed_trial_handling(self):
        generator = GridTrialGenerator(self.param_space)
        trials, invalid_count = generator.generate(self.experiment, self.dataset_ref)

        def faulty_factory(params):
            if str(params["entry"]) == "105":
                raise ValueError("pathological parameter test")
            return ReferenceThresholdStrategy(entry_price=Decimal(str(params["entry"])), exit_price=Decimal(str(params["exit"])))

        results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), faulty_factory, trials)
        failed_records = [r for r in results if r.status == "FAILED"]
        self.assertEqual(len(failed_records), 2)  # Two valid trials had entry=105
        self.assertIn("pathological parameter test", failed_records[0].error_reason or "")

        report = self.analyzer.analyze(
            experiment_id=self.experiment.experiment_id,
            experiment_revision=self.experiment.experiment_revision,
            strategy_revision_id=self.experiment.strategy_revision_id,
            dataset_reference=self.dataset_ref,
            parameter_space=self.param_space,
            trial_results=results,
            invalid_combinations_count=invalid_count,
        )
        self.assertEqual(report.failed_trials_count, 2)
        self.assertTrue(any("FAILED_TRIALS_PRESENT" in lim for lim in report.limitations))

    def test_isolated_peak_detection(self):
        generator = GridTrialGenerator(self.param_space)
        trials, invalid_count = generator.generate(self.experiment, self.dataset_ref)

        # Create isolated spike at (entry=100, exit=110), weak elsewhere
        def peak_factory(params):
            if str(params["entry"]) == "100" and str(params["exit"]) == "110":
                # Triggers big exit gain in our fixture
                return ReferenceThresholdStrategy(entry_price=Decimal("100"), exit_price=Decimal("110"))
            # Won't trigger any trades
            return ReferenceThresholdStrategy(entry_price=Decimal("10"), exit_price=Decimal("20"))

        results = self.runner.run_trials(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), peak_factory, trials)
        report = self.analyzer.analyze(
            experiment_id=self.experiment.experiment_id,
            experiment_revision=self.experiment.experiment_revision,
            strategy_revision_id=self.experiment.strategy_revision_id,
            dataset_reference=self.dataset_ref,
            parameter_space=self.param_space,
            trial_results=results,
            invalid_combinations_count=invalid_count,
        )
        self.assertTrue(len(report.isolated_peaks_detected) > 0)
        self.assertEqual(report.candidate_regions[0].classification, "ISOLATED_PEAK")


if __name__ == "__main__":
    unittest.main()
