import json
import unittest

from core.quant_foundations import canonical_json
from core.quant_foundations.models import (
    DatasetManifest,
    ExperimentDefinition,
    ExperimentResult,
    QuantValidationError,
    StrategyEvidence,
    TrialDefinition,
    TrialIdentity,
)


STRATEGY_DIGEST = "a" * 64


def dataset(version="1.0.0"):
    return DatasetManifest(
        dataset_id="fixture:btc-usdt-1m",
        dataset_version=version,
        source_ref="fixture:synthetic",
        venue_scope=("synthetic",),
        instrument_scope=("BTC-USDT",),
        temporal_from="2024-01-01T00:00:00Z",
        temporal_to="2024-01-01T00:10:00Z",
        resolution="1m",
        schema_version="market-event-1",
        normalization_version="normalization-1",
        quality_status="verified",
        integrity_metadata={"known_gaps": 0},
        creation_metadata={"created_at": "2026-09-19T00:00:00Z"},
        storage_locator="/tmp/fixture.parquet",
    )


def experiment(dataset_ref):
    return ExperimentDefinition(
        experiment_id="experiment:fixture:1",
        experiment_revision="1",
        strategy_revision_id="strategy:fixture:1",
        strategy_revision_digest=STRATEGY_DIGEST,
        dataset_refs=(dataset_ref.to_shared_reference(),),
        feature_definitions=({"featureId": "close", "featureVersion": "1.0.0", "definitionDigest": "b" * 64},),
        parameter_space={"searchType": "fixed_scenario", "parameters": {"window": 3}},
        execution_assumptions={"feeUnit": "bps", "fee": "0"},
        validation_methodology={"methodology_ref": "simple-backtest:v1", "partitions": "train-test"},
        seed_policy={"kind": "deterministic", "baseSeed": 7},
        runtime_provenance={"code_revision": "c" * 40, "dependency_lock_digest": "d" * 64, "provenance_ref": "runtime:fixture:1"},
    )


class QuantFoundationTests(unittest.TestCase):
    def test_dataset_identity_excludes_storage_locator(self):
        first = dataset()
        second = DatasetManifest(**{**first.__dict__, "storage_locator": "/another/location.parquet"})
        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(first.to_shared_reference(), second.to_shared_reference())

    def test_dataset_revision_changes_reference(self):
        self.assertNotEqual(dataset("1.0.0").effective_content_digest, dataset("2.0.0").effective_content_digest)

    def test_experiment_definition_is_deterministic(self):
        first = experiment(dataset())
        second = experiment(dataset())
        self.assertEqual(first.definition_digest, second.definition_digest)
        self.assertEqual(first.to_reference().to_shared_dict(), second.to_reference().to_shared_dict())

    def test_trial_seed_changes_trial_identity_without_strategy_change(self):
        exp = experiment(dataset())
        base = TrialDefinition(exp.experiment_id, exp.strategy_revision_id, exp.dataset_refs, {"window": 3}, seed=1)
        other = TrialDefinition(exp.experiment_id, exp.strategy_revision_id, exp.dataset_refs, {"window": 3}, seed=2)
        self.assertNotEqual(TrialIdentity.from_definition(base).trial_id, TrialIdentity.from_definition(other).trial_id)
        self.assertEqual(base.strategy_revision_id, other.strategy_revision_id)

    def test_complete_evidence_rejects_incomplete_experiment(self):
        exp = experiment(dataset())
        incomplete = exp.to_reference("partial")
        with self.assertRaises(QuantValidationError):
            StrategyEvidence(
                evidence_id="evidence:1",
                strategy_revision_id=exp.strategy_revision_id,
                experiment_refs=(incomplete,),
                dataset_refs=exp.dataset_refs,
                feature_definitions=exp.feature_definitions,
                methodology_ref="simple-backtest:v1",
                metrics={"return": "0"},
                statistical_findings=(),
                robustness_findings=(),
                execution_assumptions=exp.execution_assumptions,
                uncertainty=("fixture",),
                weaknesses=("synthetic-only",),
                runtime_provenance=exp.runtime_provenance,
                status="complete",
                created_at="2026-09-19T00:00:00Z",
            )

    def test_acs_envelope_has_actual_v2_shape_and_digest(self):
        exp = experiment(dataset())
        reference = exp.to_reference("completed")
        evidence = StrategyEvidence(
            evidence_id="evidence:fixture:1",
            strategy_revision_id=exp.strategy_revision_id,
            experiment_refs=(reference,),
            dataset_refs=exp.dataset_refs,
            feature_definitions=exp.feature_definitions,
            methodology_ref="simple-backtest:v1",
            metrics={"return": "0"},
            statistical_findings=(),
            robustness_findings=(),
            execution_assumptions=exp.execution_assumptions,
            uncertainty=("synthetic-only",),
            weaknesses=("not a profitability claim",),
            runtime_provenance=exp.runtime_provenance,
            status="complete",
            created_at="2026-09-19T00:00:00Z",
        )
        envelope = evidence.to_acs_evidence(1_758_240_000_000)
        self.assertEqual(envelope["schema_version"], "1.0")
        self.assertEqual(envelope["kind"], "artifact")
        self.assertEqual(envelope["payload_digest"], evidence.payload_digest)
        self.assertNotIn("storage_locator", json.dumps(envelope))

    def test_canonical_json_matches_sorted_compact_semantics(self):
        self.assertEqual(canonical_json({"b": 2, "a": [1, None]}), '{"a":[1,null],"b":2}')

    def test_invalid_windows_and_missing_datasets_are_rejected(self):
        with self.assertRaises(QuantValidationError):
            DatasetManifest(**{**dataset().__dict__, "temporal_from": "2024-01-01T00:10:00Z"})
        with self.assertRaises(QuantValidationError):
            ExperimentDefinition(
                experiment_id="x",
                experiment_revision="1",
                strategy_revision_id="strategy:fixture:1",
                strategy_revision_digest=STRATEGY_DIGEST,
                dataset_refs=(),
                feature_definitions=(),
                parameter_space={},
                execution_assumptions={},
                validation_methodology={},
                seed_policy={},
                runtime_provenance={},
            )

    def test_partial_result_is_explicit(self):
        exp = experiment(dataset())
        result = ExperimentResult(
            experiment_ref=exp.to_reference("partial"),
            strategy_revision_id=exp.strategy_revision_id,
            completed_trial_count=1,
            failed_trial_count=1,
            trial_refs=({"trialId": "trial:1"},),
            metrics_ref="metrics:1",
            statistical_ref=None,
            robustness_ref=None,
            limitations=("one trial failed",),
            status="partial",
            provenance_ref="result:fixture:1",
        )
        self.assertEqual(result.status, "partial")


if __name__ == "__main__":
    unittest.main()
