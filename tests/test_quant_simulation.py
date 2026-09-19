"""Deterministic validation of backtest foundations without exchange mutation."""

import unittest
from decimal import Decimal

from core.quant_foundations.models import DatasetManifest, ExperimentDefinition
from core.quant_simulation import (
    DeterministicExecutionModel,
    ExecutionAssumptionProfile,
    NoActionStrategy,
    ReferenceThresholdStrategy,
    SimulationEngine,
    SimulationInvalidatedError,
    SimulationValidationError,
)


def _dataset_ref(version="1.0.0", digest="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"):
    return {
        "datasetId": "fixture:btc-usdt-1m",
        "datasetVersion": version,
        "contentDigest": digest,
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
        experiment_id="exp:quant-03",
        experiment_revision="1.0.0",
        strategy_revision_id=revision_id,
        strategy_revision_digest="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        dataset_refs=(ref,),
        feature_definitions=({"featureId": "close", "featureVersion": "1.0.0", "schema": "scalar"},),
        parameter_space={"entry": "100", "exit": "110"},
        execution_assumptions={"profile": "deterministic-candle-v1"},
        validation_methodology={"methodology_ref": "deterministic-simulation-v1"},
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


class QuantSimulationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimulationEngine()
        self.dataset_ref = _dataset_ref()
        self.experiment = _experiment(self.dataset_ref)
        self.strategy_revision = {"revisionId": "strat:rev-1"}

    def test_reproducible_simulation_digest(self):
        exp_ref = self.experiment.to_reference()
        strategy = ReferenceThresholdStrategy(entry_price=Decimal("100"), exit_price=Decimal("110"))
        first = self.engine.run(self.experiment, exp_ref, self.dataset_ref, _market_states(), self.strategy_revision, strategy)
        strategy_again = ReferenceThresholdStrategy(entry_price=Decimal("100"), exit_price=Decimal("110"))
        second = self.engine.run(self.experiment, exp_ref, self.dataset_ref, _market_states(), self.strategy_revision, strategy_again)
        self.assertEqual(first.result_digest, second.result_digest)
        self.assertEqual(first.metrics["fillCount"], "2")
        self.assertEqual(first.metrics["netPnl"], "10")
        self.assertEqual(first.to_experiment_result(exp_ref).completed_trial_count, 1)

    def test_no_action_produces_valid_zero_trade_result(self):
        result = self.engine.run(self.experiment, self.experiment.to_reference(), self.dataset_ref, _market_states(), self.strategy_revision, NoActionStrategy())
        self.assertEqual(result.metrics["fillCount"], "0")
        self.assertEqual(result.metrics["netPnl"], "0")

    def test_friction_materially_impacts_pnl(self):
        exp_ref = self.experiment.to_reference()
        strategy = ReferenceThresholdStrategy(entry_price=Decimal("100"), exit_price=Decimal("110"))
        frictionless = self.engine.run(self.experiment, exp_ref, self.dataset_ref, _market_states(), self.strategy_revision, strategy)
        strategy_frictional = ReferenceThresholdStrategy(entry_price=Decimal("100"), exit_price=Decimal("110"))
        frictional = self.engine.run(
            self.experiment,
            exp_ref,
            self.dataset_ref,
            _market_states(),
            self.strategy_revision,
            strategy_frictional,
            execution_model=DeterministicExecutionModel(ExecutionAssumptionProfile(fee_bps=Decimal("10"), spread_bps=Decimal("20"), slippage_bps=Decimal("5"))),
        )
        self.assertLess(Decimal(frictional.metrics["netPnl"]), Decimal(frictionless.metrics["netPnl"]))

    def test_look_ahead_and_live_state_are_rejected(self):
        exp_ref = self.experiment.to_reference()
        invalid_state = [
            {
                "marketStateId": "historical:fixture:future",
                "instrument": "BTC-USDT",
                "venue": "synthetic",
                "marketTime": "2024-01-01T00:00:00.000Z",
                "observedAt": "2024-01-01T00:00:00.000Z",
                "validityContext": "historical",
                "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:05:00.000Z"}],
            }
        ]
        with self.assertRaises(SimulationInvalidatedError):
            self.engine.run(self.experiment, exp_ref, self.dataset_ref, invalid_state, self.strategy_revision, NoActionStrategy())

    def test_lineage_mismatch_fails_closed(self):
        exp_ref = self.experiment.to_reference()
        other_dataset = _dataset_ref(version="2.0.0", digest="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
        with self.assertRaises(SimulationValidationError):
            self.engine.run(self.experiment, exp_ref, other_dataset, _market_states(), self.strategy_revision, NoActionStrategy())
        with self.assertRaises(SimulationValidationError):
            self.engine.run(self.experiment, exp_ref, self.dataset_ref, _market_states(), {"revisionId": "strat:rev-2"}, NoActionStrategy())


if __name__ == "__main__":
    unittest.main()
