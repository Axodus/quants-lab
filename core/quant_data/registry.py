"""Exact-version dataset registry foundation."""

from __future__ import annotations

from .models import DatasetArtifact
from .storage import DatasetStorage


class DatasetUnavailableError(LookupError):
    pass


class DatasetRegistry:
    def __init__(self, storage: DatasetStorage) -> None:
        self.storage = storage
        self._references: dict[tuple[str, str], DatasetArtifact] = {}

    def register(self, artifact: DatasetArtifact) -> str:
        key = (artifact.manifest.dataset_id, artifact.manifest.dataset_version)
        existing = self._references.get(key)
        if existing is not None and existing.content_digest != artifact.content_digest:
            raise ValueError(f"dataset version is immutable: {key}")
        locator = self.storage.put(artifact)
        self._references[key] = artifact
        return locator

    def resolve(self, dataset_id: str, dataset_version: str) -> DatasetArtifact:
        key = (dataset_id, dataset_version)
        if key not in self._references:
            raise DatasetUnavailableError(f"exact dataset version unavailable: {dataset_id}@{dataset_version}")
        return self._references[key]

    def verify(self, dataset_id: str, dataset_version: str) -> bool:
        artifact = self.resolve(dataset_id, dataset_version)
        return artifact.content_digest == artifact.manifest.effective_content_digest

    def list_references(self) -> tuple[dict[str, str], ...]:
        return tuple({"datasetId": key[0], "datasetVersion": key[1], "contentDigest": artifact.content_digest} for key, artifact in sorted(self._references.items()))
