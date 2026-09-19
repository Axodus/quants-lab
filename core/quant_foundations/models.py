"""Typed Quant foundations. These models describe research lineage only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .canonical import sha256_digest

QualityStatus = Literal["verified", "degraded", "incomplete", "corrupted", "rejected", "unknown"]
ExperimentStatus = Literal["defined", "queued", "running", "partial", "completed", "failed", "cancelled", "invalidated", "superseded"]
TrialStatus = Literal["pending", "running", "completed", "failed", "cancelled", "invalidated"]
EvidenceStatus = Literal["candidate", "complete", "partial", "superseded", "invalidated"]


class QuantValidationError(ValueError):
    """Raised when a canonical Quant object cannot preserve required lineage."""


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuantValidationError(f"{field_name} must be a non-empty string")
    return value


def _digest(value: str, field_name: str) -> str:
    _required(value, field_name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise QuantValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    dataset_version: str
    source_ref: str
    venue_scope: tuple[str, ...]
    instrument_scope: tuple[str, ...]
    temporal_from: str
    temporal_to: str
    resolution: str
    schema_version: str
    normalization_version: str
    quality_status: QualityStatus
    known_gaps: tuple[str, ...] = ()
    transformation_lineage: tuple[str, ...] = ()
    partitions: tuple[Mapping[str, Any], ...] = ()
    content_digest: str | None = None
    integrity_metadata: Mapping[str, Any] = field(default_factory=dict)
    creation_metadata: Mapping[str, Any] = field(default_factory=dict)
    storage_locator: str | None = None

    def __post_init__(self) -> None:
        for name in ("dataset_id", "dataset_version", "source_ref", "temporal_from", "temporal_to", "resolution", "schema_version", "normalization_version"):
            _required(getattr(self, name), name)
        if not self.venue_scope or not self.instrument_scope:
            raise QuantValidationError("venue_scope and instrument_scope must not be empty")
        if self.temporal_from >= self.temporal_to:
            raise QuantValidationError("temporal_from must precede temporal_to")
        if self.quality_status not in {"verified", "degraded", "incomplete", "corrupted", "rejected", "unknown"}:
            raise QuantValidationError("unsupported quality_status")
        if self.content_digest is not None:
            _digest(self.content_digest, "content_digest")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "source_ref": self.source_ref,
            "venue_scope": list(self.venue_scope),
            "instrument_scope": list(self.instrument_scope),
            "temporal_from": self.temporal_from,
            "temporal_to": self.temporal_to,
            "resolution": self.resolution,
            "schema_version": self.schema_version,
            "normalization_version": self.normalization_version,
            "quality_status": self.quality_status,
            "known_gaps": list(self.known_gaps),
            "transformation_lineage": list(self.transformation_lineage),
            "partitions": list(self.partitions),
            "integrity_metadata": dict(self.integrity_metadata),
            "creation_metadata": dict(self.creation_metadata),
        }

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    @property
    def effective_content_digest(self) -> str:
        return self.content_digest or self.manifest_digest

    def to_shared_reference(self) -> dict[str, Any]:
        """Map to SCF-01 DatasetReference without leaking physical storage."""

        return {
            "datasetId": self.dataset_id,
            "datasetVersion": self.dataset_version,
            "contentDigest": self.effective_content_digest,
            "sourceRef": self.source_ref,
            "venueScope": list(self.venue_scope),
            "instrumentScope": list(self.instrument_scope),
            "temporalCoverage": {"from": self.temporal_from, "to": self.temporal_to},
            "resolution": self.resolution,
            "schemaVersion": self.schema_version,
            "qualityStatus": self.quality_status if self.quality_status in {"verified", "degraded", "incomplete", "unknown"} else "unknown",
        }


@dataclass(frozen=True)
class ExperimentDefinition:
    experiment_id: str
    experiment_revision: str
    strategy_revision_id: str
    strategy_revision_digest: str
    dataset_refs: tuple[Mapping[str, Any], ...]
    feature_definitions: tuple[Mapping[str, Any], ...]
    parameter_space: Mapping[str, Any]
    execution_assumptions: Mapping[str, Any]
    validation_methodology: Mapping[str, Any]
    seed_policy: Mapping[str, Any]
    runtime_provenance: Mapping[str, Any]
    schema_version: str = "1.0.0"
    status: ExperimentStatus = "defined"

    def __post_init__(self) -> None:
        for name in ("experiment_id", "experiment_revision", "strategy_revision_id", "strategy_revision_digest", "schema_version"):
            _required(getattr(self, name), name)
        _digest(self.strategy_revision_digest, "strategy_revision_digest")
        if not self.dataset_refs:
            raise QuantValidationError("dataset_refs must not be empty")
        if self.status not in {"defined", "queued", "running", "partial", "completed", "failed", "cancelled", "invalidated", "superseded"}:
            raise QuantValidationError("unsupported experiment status")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "experiment_revision": self.experiment_revision,
            "strategy_revision_id": self.strategy_revision_id,
            "strategy_revision_digest": self.strategy_revision_digest,
            "dataset_refs": list(self.dataset_refs),
            "feature_definitions": list(self.feature_definitions),
            "parameter_space": dict(self.parameter_space),
            "execution_assumptions": dict(self.execution_assumptions),
            "validation_methodology": dict(self.validation_methodology),
            "seed_policy": dict(self.seed_policy),
            "runtime_provenance": dict(self.runtime_provenance),
            "schema_version": self.schema_version,
        }

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    def to_reference(self, status: ExperimentStatus | None = None) -> "ExperimentReference":
        return ExperimentReference(
            experiment_id=self.experiment_id,
            experiment_revision=self.experiment_revision,
            strategy_revision_id=self.strategy_revision_id,
            dataset_refs=self.dataset_refs,
            feature_definitions=self.feature_definitions,
            methodology_ref=str(self.validation_methodology.get("methodology_ref", "unspecified")),
            status=status or self.status,
            provenance_ref=self.runtime_provenance.get("provenance_ref", self.definition_digest),
        )


@dataclass(frozen=True)
class ExperimentReference:
    experiment_id: str
    experiment_revision: str
    strategy_revision_id: str
    dataset_refs: tuple[Mapping[str, Any], ...]
    feature_definitions: tuple[Mapping[str, Any], ...]
    methodology_ref: str
    status: ExperimentStatus
    provenance_ref: str

    def to_shared_dict(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "experimentRevision": self.experiment_revision,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetRefs": list(self.dataset_refs),
            "featureDefinitions": list(self.feature_definitions),
            "methodologyRef": self.methodology_ref,
            "status": self.status,
            "provenanceRef": self.provenance_ref,
        }


@dataclass(frozen=True)
class TrialDefinition:
    experiment_id: str
    strategy_revision_id: str
    dataset_refs: tuple[Mapping[str, Any], ...]
    parameters: Mapping[str, Any]
    seed: int | None = None
    seed_policy_ref: str | None = None
    execution_assumptions_ref: str | None = None
    ordinal: int | None = None

    def __post_init__(self) -> None:
        _required(self.experiment_id, "experiment_id")
        _required(self.strategy_revision_id, "strategy_revision_id")
        if not self.dataset_refs:
            raise QuantValidationError("trial dataset_refs must not be empty")
        if self.seed is not None and (not isinstance(self.seed, int) or self.seed < 0):
            raise QuantValidationError("seed must be a non-negative integer when supplied")
        if self.ordinal is not None and (not isinstance(self.ordinal, int) or self.ordinal < 0):
            raise QuantValidationError("ordinal must be a non-negative integer when supplied")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "strategy_revision_id": self.strategy_revision_id,
            "dataset_refs": list(self.dataset_refs),
            "parameters": dict(self.parameters),
            "seed": self.seed,
            "seed_policy_ref": self.seed_policy_ref,
            "execution_assumptions_ref": self.execution_assumptions_ref,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class TrialIdentity:
    trial_id: str
    definition_digest: str
    retry_of: str | None = None

    @classmethod
    def from_definition(cls, definition: TrialDefinition) -> "TrialIdentity":
        digest = sha256_digest(definition)
        return cls(trial_id=f"trial:{digest}", definition_digest=digest)


@dataclass(frozen=True)
class ExperimentResult:
    experiment_ref: ExperimentReference
    strategy_revision_id: str
    completed_trial_count: int
    failed_trial_count: int
    trial_refs: tuple[Mapping[str, Any], ...]
    metrics_ref: str | None
    statistical_ref: str | None
    robustness_ref: str | None
    limitations: tuple[str, ...]
    status: ExperimentStatus
    provenance_ref: str

    def __post_init__(self) -> None:
        if self.completed_trial_count < 0 or self.failed_trial_count < 0:
            raise QuantValidationError("trial counts must be non-negative")
        if self.status == "completed" and self.failed_trial_count:
            raise QuantValidationError("completed result cannot contain failed trials; use partial")
        if self.status == "completed" and self.completed_trial_count == 0:
            raise QuantValidationError("completed result requires at least one completed trial")
        if self.status == "partial" and self.completed_trial_count == 0:
            raise QuantValidationError("partial result requires completed trial evidence")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "experiment_ref": self.experiment_ref.to_shared_dict(),
            "strategy_revision_id": self.strategy_revision_id,
            "completed_trial_count": self.completed_trial_count,
            "failed_trial_count": self.failed_trial_count,
            "trial_refs": list(self.trial_refs),
            "metrics_ref": self.metrics_ref,
            "statistical_ref": self.statistical_ref,
            "robustness_ref": self.robustness_ref,
            "limitations": list(self.limitations),
            "status": self.status,
            "provenance_ref": self.provenance_ref,
        }


@dataclass(frozen=True)
class StrategyEvidence:
    evidence_id: str
    strategy_revision_id: str
    experiment_refs: tuple[ExperimentReference, ...]
    dataset_refs: tuple[Mapping[str, Any], ...]
    feature_definitions: tuple[Mapping[str, Any], ...]
    methodology_ref: str
    metrics: Mapping[str, Any]
    statistical_findings: tuple[str, ...]
    robustness_findings: tuple[str, ...]
    execution_assumptions: Mapping[str, Any]
    uncertainty: tuple[str, ...]
    weaknesses: tuple[str, ...]
    runtime_provenance: Mapping[str, Any]
    status: EvidenceStatus
    created_at: str
    supersedes_evidence_id: str | None = None
    acs_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        _required(self.evidence_id, "evidence_id")
        _required(self.strategy_revision_id, "strategy_revision_id")
        if not self.experiment_refs:
            raise QuantValidationError("StrategyEvidence requires experiment_refs")
        if any(ref.strategy_revision_id != self.strategy_revision_id for ref in self.experiment_refs):
            raise QuantValidationError("all experiment references must use the evidence StrategyRevision")
        if self.status == "complete" and any(ref.status != "completed" for ref in self.experiment_refs):
            raise QuantValidationError("complete evidence requires completed experiments")

    def payload(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "strategyRevisionId": self.strategy_revision_id,
            "experimentRefs": [ref.to_shared_dict() for ref in self.experiment_refs],
            "datasetRefs": list(self.dataset_refs),
            "featureDefinitions": list(self.feature_definitions),
            "methodologyRef": self.methodology_ref,
            "metrics": dict(self.metrics),
            "statisticalFindings": list(self.statistical_findings),
            "robustnessFindings": list(self.robustness_findings),
            "executionAssumptions": dict(self.execution_assumptions),
            "uncertainty": list(self.uncertainty),
            "weaknesses": list(self.weaknesses),
            "runtimeProvenance": dict(self.runtime_provenance),
            "status": self.status,
            "createdAt": self.created_at,
            "supersedesEvidenceId": self.supersedes_evidence_id,
        }

    @property
    def payload_digest(self) -> str:
        return sha256_digest(self.payload())

    def to_acs_evidence(self, created_at_epoch_ms: int) -> dict[str, Any]:
        """Build the actual ACS EvidenceRecordV2 envelope used by ACS v2."""

        return {
            "schema_version": "1.0",
            "evidence_id": self.evidence_id,
            "kind": "artifact",
            "subject_ref": {"kind": "strategy-evidence", "id": self.evidence_id},
            "event_ref": {"kind": "experiment", "id": self.experiment_refs[0].experiment_id},
            "source": "executor",
            "classification": "internal",
            "payload_digest": self.payload_digest,
            "provenance": {
                "domain": "axodus-trading-quant",
                "strategy_revision_id": self.strategy_revision_id,
                "experiment_ids": [ref.experiment_id for ref in self.experiment_refs],
                "payload_schema": "strategy-evidence-v1",
            },
            "created_at": created_at_epoch_ms,
            **({"correction_of": self.supersedes_evidence_id} if self.supersedes_evidence_id else {}),
        }
