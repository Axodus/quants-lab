"""Storage adapters that keep physical locations outside DatasetReference identity."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import DatasetArtifact


class DatasetStorage(Protocol):
    def put(self, artifact: DatasetArtifact) -> str: ...
    def get(self, dataset_id: str, dataset_version: str) -> DatasetArtifact: ...


class InMemoryDatasetStorage:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], DatasetArtifact] = {}

    def put(self, artifact: DatasetArtifact) -> str:
        key = (artifact.manifest.dataset_id, artifact.manifest.dataset_version)
        if key in self._items and self._items[key].content_digest != artifact.content_digest:
            raise ValueError(f"immutable dataset version already exists: {key}")
        self._items[key] = artifact
        return f"memory:{artifact.manifest.dataset_id}@{artifact.manifest.dataset_version}"

    def get(self, dataset_id: str, dataset_version: str) -> DatasetArtifact:
        return self._items[(dataset_id, dataset_version)]


class JsonlDatasetStorage:
    """Deterministic local fixture adapter; not the canonical identity authority."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, artifact: DatasetArtifact) -> str:
        target = self.root / artifact.manifest.dataset_id.replace("/", "_") / f"{artifact.manifest.dataset_version}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(record.to_dict().__repr__() for record in artifact.records) + "\n", encoding="utf-8")
        return str(target)

    def get(self, dataset_id: str, dataset_version: str) -> DatasetArtifact:
        raise NotImplementedError("JSONL fixture storage is write/inspect only; use registry for exact artifact resolution")
