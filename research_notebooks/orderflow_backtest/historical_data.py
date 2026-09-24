"""Validation-only tools for provenance-controlled L2/tape manifests."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DatasetValidationError(ValueError):
    pass


REQUIRED_MANIFEST_FIELDS = {
    "datasetId", "datasetRevision", "provider", "venue", "symbols",
    "startTime", "endTime", "timezone", "raw", "checksums",
    "sequenceCoverage", "gapSummary", "normalizationVersion", "parserVersion", "createdAt",
}


@dataclass(frozen=True)
class DatasetManifestV1:
    value: dict[str, Any]

    @property
    def dataset_id(self) -> str:
        return self.value["datasetId"]

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_manifest(path: str | Path) -> DatasetManifestV1:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DatasetValidationError("manifest must be an object")
    missing = REQUIRED_MANIFEST_FIELDS - payload.keys()
    if missing:
        raise DatasetValidationError("missing manifest fields: " + ", ".join(sorted(missing)))
    if payload["timezone"] != "UTC":
        raise DatasetValidationError("canonical timezone must be UTC")
    if not isinstance(payload["symbols"], list) or not payload["symbols"]:
        raise DatasetValidationError("symbols must be a non-empty list")
    if not isinstance(payload["checksums"], dict) or not payload["checksums"]:
        raise DatasetValidationError("checksums must identify immutable raw artifacts")
    if payload.get("mode") == "synthetic" or payload.get("label") == "ENGINE_VALIDATION_SYNTHETIC":
        raise DatasetValidationError("synthetic dataset cannot satisfy historical acceptance")
    return DatasetManifestV1(payload)


def verify_checksums(manifest: DatasetManifestV1, root: str | Path) -> dict[str, str]:
    root_path = Path(root)
    verified = {}
    for relative, expected in manifest.value["checksums"].items():
        path = root_path / relative
        if not path.is_file():
            raise DatasetValidationError(f"missing raw artifact: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise DatasetValidationError(f"checksum mismatch: {relative}")
        verified[relative] = actual
    return verified


def classify_sequence_quality(sequence_coverage: dict[str, Any]) -> str:
    if sequence_coverage.get("continuity") is True and not sequence_coverage.get("gaps"):
        return "NO_GAP"
    if sequence_coverage.get("continuity") is False:
        return "UNRECOVERABLE_GAP"
    return "UNKNOWN_SEQUENCE_STATE"
