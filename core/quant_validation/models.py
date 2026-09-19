"""Canonical typed models for OOS, walk-forward, and statistical validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import ExperimentReference
from core.quant_simulation.models import SimulationResult

SegmentRole = Literal["DEVELOPMENT", "IN_SAMPLE", "OUT_OF_SAMPLE", "VALIDATION"]
ValidationMode = Literal["HOLDOUT", "WALK_FORWARD_ROLLING", "WALK_FORWARD_EXPANDING"]
ValidationStatus = Literal["PENDING", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "INVALIDATED"]


@dataclass(frozen=True)
class DatasetSegment:
    segment_id: str
    dataset_id: str
    dataset_version: str
    role: SegmentRole
    temporal_from: str
    temporal_to: str
    record_count: int

    def __post_init__(self) -> None:
        if self.temporal_from >= self.temporal_to:
            raise ValueError("temporal_from must precede temporal_to")
        if self.record_count < 0:
            raise ValueError("record_count must be non-negative")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "segmentId": self.segment_id,
            "datasetId": self.dataset_id,
            "datasetVersion": self.dataset_version,
            "role": self.role,
            "temporalFrom": self.temporal_from,
            "temporalTo": self.temporal_to,
            "recordCount": self.record_count,
        }


@dataclass(frozen=True)
class SelectionSnapshot:
    snapshot_id: str
    strategy_revision_id: str
    selected_parameters: Mapping[str, Any]
    selection_methodology: str
    source_trial_id: str | None
    candidate_population_size: int
    metrics_at_selection: Mapping[str, str]
    is_segment_ref: str

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id,
            "strategyRevisionId": self.strategy_revision_id,
            "selectedParameters": dict(self.selected_parameters),
            "selectionMethodology": self.selection_methodology,
            "sourceTrialId": self.source_trial_id,
            "candidatePopulationSize": self.candidate_population_size,
            "metricsAtSelection": dict(self.metrics_at_selection),
            "isSegmentRef": self.is_segment_ref,
        }

    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())


@dataclass(frozen=True)
class WalkForwardPlan:
    train_window_steps: int
    test_window_steps: int
    step_size: int
    mode: Literal["ROLLING", "EXPANDING"]
    purge_steps: int = 0
    embargo_steps: int = 0

    def __post_init__(self) -> None:
        if self.train_window_steps <= 0 or self.test_window_steps <= 0 or self.step_size <= 0:
            raise ValueError("window steps and step_size must be positive")
        if self.purge_steps < 0 or self.embargo_steps < 0:
            raise ValueError("purge and embargo steps must be non-negative")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "trainWindowSteps": self.train_window_steps,
            "testWindowSteps": self.test_window_steps,
            "stepSize": self.step_size,
            "mode": self.mode,
            "purgeSteps": self.purge_steps,
            "embargoSteps": self.embargo_steps,
        }


@dataclass(frozen=True)
class ValidationPlan:
    plan_id: str
    strategy_revision_id: str
    dataset_reference: Mapping[str, Any]
    validation_mode: ValidationMode
    temporal_coverage: Mapping[str, str]
    holdout_is_ratio: str | None = None
    walk_forward_plan: WalkForwardPlan | None = None
    execution_assumptions_profile: Mapping[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetReference": dict(self.dataset_reference),
            "validationMode": self.validation_mode,
            "temporalCoverage": dict(self.temporal_coverage),
            "holdoutIsRatio": self.holdout_is_ratio,
            "walkForwardPlan": self.walk_forward_plan.to_canonical_dict() if self.walk_forward_plan else None,
            "executionAssumptionsProfile": dict(self.execution_assumptions_profile),
            "version": self.version,
        }

    @property
    def plan_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())


@dataclass(frozen=True)
class MetricComparison:
    is_value: str
    oos_value: str
    degradation_pct: str | None = None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "isValue": self.is_value,
            "oosValue": self.oos_value,
            "degradationPct": self.degradation_pct,
        }


@dataclass(frozen=True)
class OOSResult:
    result_id: str
    plan_id: str
    strategy_revision_id: str
    selection_snapshot: SelectionSnapshot
    oos_segment: DatasetSegment
    simulation_result_digest: str
    metrics: Mapping[str, str]
    comparisons: Mapping[str, MetricComparison]
    limitations: tuple[str, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "resultId": self.result_id,
            "planId": self.plan_id,
            "strategyRevisionId": self.strategy_revision_id,
            "selectionSnapshot": self.selection_snapshot.to_canonical_dict(),
            "oosSegment": self.oos_segment.to_canonical_dict(),
            "simulationResultDigest": self.simulation_result_digest,
            "metrics": dict(self.metrics),
            "comparisons": {k: v.to_canonical_dict() for k, v in self.comparisons.items()},
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class FoldResult:
    fold_ordinal: int
    train_segment: DatasetSegment
    test_segment: DatasetSegment
    selection_snapshot: SelectionSnapshot
    simulation_result_digest: str
    metrics: Mapping[str, str]
    status: Literal["COMPLETED", "FAILED", "INVALIDATED"]
    error_reason: str | None = None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "foldOrdinal": self.fold_ordinal,
            "trainSegment": self.train_segment.to_canonical_dict(),
            "testSegment": self.test_segment.to_canonical_dict(),
            "selectionSnapshot": self.selection_snapshot.to_canonical_dict(),
            "simulationResultDigest": self.simulation_result_digest,
            "metrics": dict(self.metrics),
            "status": self.status,
            "errorReason": self.error_reason,
        }


@dataclass(frozen=True)
class StatisticalDiagnostics:
    metric_name: str
    mean: str
    std_dev: str
    median: str
    positive_fold_ratio: str
    annualized_sharpe_estimate: str | None
    confidence_interval_95: tuple[str, str] | None
    sample_count: int

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "metricName": self.metric_name,
            "mean": self.mean,
            "stdDev": self.std_dev,
            "median": self.median,
            "positiveFoldRatio": self.positive_fold_ratio,
            "annualizedSharpeEstimate": self.annualized_sharpe_estimate,
            "confidenceInterval95": list(self.confidence_interval_95) if self.confidence_interval_95 else None,
            "sampleCount": self.sample_count,
        }


@dataclass(frozen=True)
class ValidationResult:
    validation_id: str
    plan_id: str
    strategy_revision_id: str
    dataset_reference: Mapping[str, Any]
    status: ValidationStatus
    oos_result: OOSResult | None = None
    fold_results: tuple[FoldResult, ...] = ()
    statistical_diagnostics: Mapping[str, StatisticalDiagnostics] = field(default_factory=dict)
    selection_bias_warning: bool = False
    candidate_population_size: int = 1
    limitations: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def result_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "validationId": self.validation_id,
            "planId": self.plan_id,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetReference": dict(self.dataset_reference),
            "status": self.status,
            "oosResult": self.oos_result.to_canonical_dict() if self.oos_result else None,
            "foldResults": [f.to_canonical_dict() for f in self.fold_results],
            "statisticalDiagnostics": {k: v.to_canonical_dict() for k, v in self.statistical_diagnostics.items()},
            "selectionBiasWarning": self.selection_bias_warning,
            "candidatePopulationSize": self.candidate_population_size,
            "limitations": list(self.limitations),
            "provenance": dict(self.provenance),
        }

    def to_acs_evidence(self, created_at_epoch_ms: int) -> dict[str, Any]:
        """Map into canonical ACS EvidenceRecordV2 envelope."""
        return {
            "schema_version": "1.0",
            "evidence_id": self.validation_id,
            "kind": "artifact",
            "subject_ref": {"kind": "quant-validation-result", "id": self.validation_id},
            "event_ref": {"kind": "validation-plan", "id": self.plan_id},
            "source": "executor",
            "classification": "internal",
            "payload_digest": self.result_digest,
            "provenance": {
                "domain": "axodus-trading-quant",
                "strategy_revision_id": self.strategy_revision_id,
                "plan_id": self.plan_id,
                "payload_schema": "quant-validation-result-v1",
            },
            "created_at": created_at_epoch_ms,
        }
