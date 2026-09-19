"""Dataset construction, ordering, quality checks and canonical serialization."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from core.quant_foundations.canonical import canonical_json, sha256_digest
from core.quant_foundations.models import DatasetManifest

from .models import CanonicalHistoricalRecord, DatasetArtifact, DatasetQualityReport
from .normalization import HistoricalSourceAdapter


class DatasetBuildError(ValueError):
    pass


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class DatasetBuilder:
    def __init__(self, normalizer: Any, duplicate_policy: str = "retain-first") -> None:
        if duplicate_policy not in {"retain-first", "reject"}:
            raise ValueError("duplicate_policy must be retain-first or reject")
        self.normalizer = normalizer
        self.duplicate_policy = duplicate_policy

    def build(
        self,
        source: HistoricalSourceAdapter,
        manifest: DatasetManifest,
        *,
        expected_interval: timedelta | None = None,
    ) -> DatasetArtifact:
        normalized: list[CanonicalHistoricalRecord] = []
        invalid = 0
        failures = 0
        anomalies: list[str] = []
        for raw in source.read():
            try:
                normalized.append(self.normalizer.normalize(raw))
            except (ValueError, KeyError, TypeError) as error:
                failures += 1
                anomalies.append(f"normalization:{error}")

        normalized.sort(key=lambda record: record.semantic_key())
        seen: set[tuple[str, str, str, str]] = set()
        unique: list[CanonicalHistoricalRecord] = []
        duplicate_count = 0
        for record in normalized:
            key = record.cadence_key()
            if key in seen:
                duplicate_count += 1
                if self.duplicate_policy == "reject":
                    raise DatasetBuildError(f"duplicate historical record: {key}")
                continue
            seen.add(key)
            unique.append(record)

        gaps: list[Mapping[str, str]] = []
        if expected_interval is not None:
            grouped: dict[tuple[str, str, str], list[CanonicalHistoricalRecord]] = {}
            for record in unique:
                grouped.setdefault((record.instrument, record.venue, record.observation_type), []).append(record)
            for group_key, records in grouped.items():
                records.sort(key=lambda record: record.event_time)
                for previous, current in zip(records, records[1:]):
                    cursor = _parse(previous.event_time) + expected_interval
                    current_time = _parse(current.event_time)
                    while cursor < current_time:
                        gaps.append({"instrument": group_key[0], "venue": group_key[1], "from": cursor.isoformat().replace("+00:00", "Z"), "to": (cursor + expected_interval).isoformat().replace("+00:00", "Z")})
                        cursor += expected_interval

        if not unique:
            status = "INVALID"
        elif failures:
            status = "INVALID" if not unique else "VALID_WITH_LIMITATIONS"
        elif gaps:
            status = "INCOMPLETE"
        elif duplicate_count:
            status = "VALID_WITH_LIMITATIONS"
        else:
            status = "VALID"
        quality = DatasetQualityReport(
            observation_count=len(unique),
            duplicate_count=duplicate_count,
            invalid_count=invalid,
            normalization_failure_count=failures,
            schema_violation_count=0,
            gaps=tuple(gaps),
            source_anomalies=tuple(anomalies),
            completeness="complete" if status == "VALID" else "limited" if status == "VALID_WITH_LIMITATIONS" else "incomplete",
            status=status,
        )
        content = canonical_json([record.to_dict() for record in unique])
        content_digest = sha256_digest([record.to_dict() for record in unique])
        manifest_quality = {"VALID": "verified", "VALID_WITH_LIMITATIONS": "degraded", "INCOMPLETE": "incomplete", "INVALID": "rejected"}[status]
        finalized_manifest = replace(manifest, content_digest=content_digest, quality_status=manifest_quality, integrity_metadata={**manifest.integrity_metadata, "contentDigest": content_digest, "recordCount": len(unique), "quality": quality.to_dict()})
        return DatasetArtifact(finalized_manifest, tuple(unique), quality, content, content_digest)
