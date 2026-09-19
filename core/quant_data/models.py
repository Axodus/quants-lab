"""Provider-neutral historical records and dataset quality artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import DatasetManifest


@dataclass(frozen=True)
class CanonicalHistoricalRecord:
    observation_id: str
    instrument: str
    venue: str
    observation_type: str
    event_time: str
    observed_at: str
    received_at: str
    value: Mapping[str, str]
    source_ref: str
    schema_version: str
    quality: str = "valid"
    sequence: int | None = None
    sequence_scope: str | None = None

    def semantic_key(self) -> tuple[str, str, str, str, str]:
        return (self.instrument, self.venue, self.observation_type, self.event_time, self.observation_id)

    def cadence_key(self) -> tuple[str, str, str, str]:
        return (self.instrument, self.venue, self.observation_type, self.event_time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observationId": self.observation_id,
            "instrument": self.instrument,
            "venue": self.venue,
            "observationType": self.observation_type,
            "eventTime": self.event_time,
            "observedAt": self.observed_at,
            "receivedAt": self.received_at,
            "value": dict(self.value),
            "sourceRef": self.source_ref,
            "schemaVersion": self.schema_version,
            "quality": self.quality,
            **({"sequence": self.sequence} if self.sequence is not None else {}),
            **({"sequenceScope": self.sequence_scope} if self.sequence_scope is not None else {}),
        }

    @property
    def content_digest(self) -> str:
        return sha256_digest(self.to_dict())


@dataclass(frozen=True)
class DatasetQualityReport:
    observation_count: int
    duplicate_count: int
    invalid_count: int
    normalization_failure_count: int
    schema_violation_count: int
    gaps: tuple[Mapping[str, str], ...]
    source_anomalies: tuple[str, ...]
    completeness: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"VALID", "VALID_WITH_LIMITATIONS", "INCOMPLETE", "INVALID"}:
            raise ValueError("unsupported dataset quality status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observationCount": self.observation_count,
            "duplicateCount": self.duplicate_count,
            "invalidCount": self.invalid_count,
            "normalizationFailureCount": self.normalization_failure_count,
            "schemaViolationCount": self.schema_violation_count,
            "gaps": [dict(gap) for gap in self.gaps],
            "sourceAnomalies": list(self.source_anomalies),
            "completeness": self.completeness,
            "status": self.status,
        }


@dataclass(frozen=True)
class DatasetArtifact:
    manifest: DatasetManifest
    records: tuple[CanonicalHistoricalRecord, ...]
    quality: DatasetQualityReport
    canonical_content: str
    content_digest: str
    storage_locator: str | None = None

    def inspect(self) -> dict[str, Any]:
        return {
            "datasetReference": self.manifest.to_shared_reference(),
            "manifestDigest": self.manifest.manifest_digest,
            "contentDigest": self.content_digest,
            "recordCount": len(self.records),
            "quality": self.quality.to_dict(),
            "storageLocator": self.storage_locator,
        }
