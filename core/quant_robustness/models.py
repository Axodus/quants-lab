"""Canonical parameter-space, trial-budget, trial-result and robustness models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import ExperimentReference
from core.quant_simulation.models import SimulationResult, text

ParameterType = Literal["integer", "decimal", "boolean", "categorical"]
TrialStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "INVALID", "CANCELLED"]
SearchMethod = Literal["GRID", "RANDOM_SEEDED", "REFERENCE_ONLY"]


class ParameterSpecificationError(ValueError):
    """Raised when parameter space or trial definitions violate constraints."""


class TrialBudgetExceededError(ValueError):
    """Raised when trial space exceeds the explicit experiment budget."""


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ParameterSpecificationError(f"{field_name} must be numeric") from exc
    if not dec.is_finite():
        raise ParameterSpecificationError(f"{field_name} must be finite")
    return dec


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    param_type: ParameterType
    allowed_values: tuple[Any, ...]
    unit: str | None = None
    default_value: Any | None = None
    semantic_description: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ParameterSpecificationError("parameter name must be non-empty")
        if not self.allowed_values:
            raise ParameterSpecificationError(f"parameter '{self.name}' must have at least one allowed value")
        if self.param_type not in {"integer", "decimal", "boolean", "categorical"}:
            raise ParameterSpecificationError(f"unsupported parameter type: {self.param_type}")
        normalized_values = []
        for raw in self.allowed_values:
            normalized_values.append(self._normalize_value(raw))
        object.__setattr__(self, "allowed_values", tuple(normalized_values))
        if self.default_value is not None:
            norm_default = self._normalize_value(self.default_value)
            if norm_default not in self.allowed_values:
                raise ParameterSpecificationError(f"default value for '{self.name}' must be within allowed values")
            object.__setattr__(self, "default_value", norm_default)

    def _normalize_value(self, raw: Any) -> Any:
        if self.param_type == "integer":
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise ParameterSpecificationError(f"invalid integer for '{self.name}': {raw}")
            return int(str(raw))
        if self.param_type == "decimal":
            return text(_decimal(raw, self.name))
        if self.param_type == "boolean":
            if isinstance(raw, str):
                lower = raw.lower()
                if lower in {"true", "1"}:
                    return True
                if lower in {"false", "0"}:
                    return False
                raise ParameterSpecificationError(f"invalid boolean for '{self.name}': {raw}")
            return bool(raw)
        return str(raw)

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "paramType": self.param_type,
            "allowedValues": list(self.allowed_values),
            "unit": self.unit,
            "defaultValue": self.default_value,
            "semanticDescription": self.semantic_description,
        }


@dataclass(frozen=True)
class ParameterConstraint:
    constraint_id: str
    description: str
    predicate: Callable[[Mapping[str, Any]], bool] = field(compare=False)

    def validate(self, assignment: Mapping[str, Any]) -> bool:
        try:
            return bool(self.predicate(assignment))
        except Exception:
            return False

    def to_canonical_dict(self) -> dict[str, Any]:
        return {"constraintId": self.constraint_id, "description": self.description}


@dataclass(frozen=True)
class ParameterSpace:
    parameters: tuple[ParameterDefinition, ...]
    constraints: tuple[ParameterConstraint, ...] = ()

    def __post_init__(self) -> None:
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ParameterSpecificationError("duplicate parameter names in parameter space")

    @property
    def cardinality(self) -> int:
        count = 1
        for param in self.parameters:
            count *= len(param.allowed_values)
        return count

    def get(self, name: str) -> ParameterDefinition | None:
        for p in self.parameters:
            if p.name == name:
                return p
        return None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "parameters": [p.to_canonical_dict() for p in self.parameters],
            "constraints": [c.to_canonical_dict() for c in self.constraints],
            "cardinality": self.cardinality,
        }


@dataclass(frozen=True)
class TrialBudget:
    max_generated_trials: int = 1000
    max_executed_trials: int = 1000
    max_failed_trials: int = 100

    def __post_init__(self) -> None:
        if self.max_generated_trials <= 0 or self.max_executed_trials <= 0 or self.max_failed_trials < 0:
            raise ParameterSpecificationError("invalid trial budget thresholds")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "maxGeneratedTrials": self.max_generated_trials,
            "maxExecutedTrials": self.max_executed_trials,
            "maxFailedTrials": self.max_failed_trials,
        }


@dataclass(frozen=True)
class TrialResultRecord:
    trial_id: str
    experiment_id: str
    experiment_revision: str
    strategy_revision_id: str
    dataset_reference: Mapping[str, Any]
    execution_profile: Mapping[str, Any]
    parameters: Mapping[str, Any]
    status: TrialStatus
    metrics: Mapping[str, str]
    simulation_result_digest: str | None = None
    error_reason: str | None = None
    limitations: tuple[str, ...] = ()

    @classmethod
    def from_simulation(
        cls,
        trial_id: str,
        simulation: SimulationResult,
        parameters: Mapping[str, Any],
    ) -> "TrialResultRecord":
        status: TrialStatus = "COMPLETED" if simulation.status == "COMPLETED" else "FAILED"
        return cls(
            trial_id=trial_id,
            experiment_id=simulation.experiment_id,
            experiment_revision=simulation.experiment_revision,
            strategy_revision_id=simulation.strategy_revision_id,
            dataset_reference=simulation.dataset_reference,
            execution_profile=simulation.execution_profile,
            parameters=dict(parameters),
            status=status,
            metrics=dict(simulation.metrics),
            simulation_result_digest=simulation.result_digest,
            limitations=simulation.limitations,
        )

    @classmethod
    def failed(
        cls,
        trial_id: str,
        experiment_ref: ExperimentReference,
        dataset_ref: Mapping[str, Any],
        execution_profile: Mapping[str, Any],
        parameters: Mapping[str, Any],
        reason: str,
    ) -> "TrialResultRecord":
        return cls(
            trial_id=trial_id,
            experiment_id=experiment_ref.experiment_id,
            experiment_revision=experiment_ref.experiment_revision,
            strategy_revision_id=experiment_ref.strategy_revision_id,
            dataset_reference=dict(dataset_ref),
            execution_profile=dict(execution_profile),
            parameters=dict(parameters),
            status="FAILED",
            metrics={},
            error_reason=reason,
            limitations=(f"error:{reason}",),
        )

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "trialId": self.trial_id,
            "experimentId": self.experiment_id,
            "experimentRevision": self.experiment_revision,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetReference": dict(self.dataset_reference),
            "executionProfile": dict(self.execution_profile),
            "parameters": dict(self.parameters),
            "status": self.status,
            "metrics": dict(self.metrics),
            "simulationResultDigest": self.simulation_result_digest,
            "errorReason": self.error_reason,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class MetricSummary:
    min: str
    max: str
    median: str
    q25: str
    q75: str
    count: int

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "min": self.min,
            "max": self.max,
            "median": self.median,
            "q25": self.q25,
            "q75": self.q75,
            "count": self.count,
        }


@dataclass(frozen=True)
class CandidateRegion:
    region_id: str
    center_trial_id: str
    center_parameters: Mapping[str, Any]
    neighborhood_trial_ids: tuple[str, ...]
    classification: Literal["STABLE_REGION", "ISOLATED_PEAK", "BOUNDARY_SENSITIVE", "DEGRADED"]
    diagnostic_summary: str

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "regionId": self.region_id,
            "centerTrialId": self.center_trial_id,
            "centerParameters": dict(self.center_parameters),
            "neighborhoodTrialIds": list(self.neighborhood_trial_ids),
            "classification": self.classification,
            "diagnosticSummary": self.diagnostic_summary,
        }


@dataclass(frozen=True)
class ParameterRobustnessReport:
    report_id: str
    experiment_id: str
    experiment_revision: str
    strategy_revision_id: str
    dataset_reference: Mapping[str, Any]
    total_attempted_trials: int
    completed_trials_count: int
    failed_trials_count: int
    invalid_combinations_count: int
    search_method: SearchMethod
    tested_parameter_space: Mapping[str, Any]
    metric_distributions: Mapping[str, MetricSummary]
    highest_metric_trial_id: str | None
    reference_trial_id: str | None
    candidate_regions: tuple[CandidateRegion, ...]
    boundary_optimum_detected: bool
    isolated_peaks_detected: tuple[str, ...]
    cost_sensitivity_findings: tuple[str, ...]
    limitations: tuple[str, ...]
    runtime_provenance: Mapping[str, Any]

    @property
    def report_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "reportId": self.report_id,
            "experimentId": self.experiment_id,
            "experimentRevision": self.experiment_revision,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetReference": dict(self.dataset_reference),
            "totalAttemptedTrials": self.total_attempted_trials,
            "completedTrialsCount": self.completed_trials_count,
            "failedTrialsCount": self.failed_trials_count,
            "invalidCombinationsCount": self.invalid_combinations_count,
            "searchMethod": self.search_method,
            "testedParameterSpace": dict(self.tested_parameter_space),
            "metricDistributions": {k: v.to_canonical_dict() for k, v in self.metric_distributions.items()},
            "highestMetricTrialId": self.highest_metric_trial_id,
            "referenceTrialId": self.reference_trial_id,
            "candidateRegions": [r.to_canonical_dict() for r in self.candidate_regions],
            "boundaryOptimumDetected": self.boundary_optimum_detected,
            "isolatedPeaksDetected": list(self.isolated_peaks_detected),
            "costSensitivityFindings": list(self.cost_sensitivity_findings),
            "limitations": list(self.limitations),
            "runtimeProvenance": dict(self.runtime_provenance),
        }

    def to_acs_evidence(self, created_at_epoch_ms: int) -> dict[str, Any]:
        """Project into canonical ACS EvidenceRecordV2 envelope."""
        return {
            "schema_version": "1.0",
            "evidence_id": self.report_id,
            "kind": "artifact",
            "subject_ref": {"kind": "parameter-robustness-report", "id": self.report_id},
            "event_ref": {"kind": "experiment", "id": self.experiment_id},
            "source": "executor",
            "classification": "internal",
            "payload_digest": self.report_digest,
            "provenance": {
                "domain": "axodus-trading-quant",
                "strategy_revision_id": self.strategy_revision_id,
                "experiment_id": self.experiment_id,
                "payload_schema": "parameter-robustness-v1",
            },
            "created_at": created_at_epoch_ms,
        }
