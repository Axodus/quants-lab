"""Canonical, provider-neutral foundations for reproducible Quant experiments."""

from .canonical import canonical_json, sha256_digest
from .models import (
    DatasetManifest,
    ExperimentDefinition,
    ExperimentReference,
    ExperimentResult,
    StrategyEvidence,
    TrialDefinition,
    TrialIdentity,
)

__all__ = [
    "DatasetManifest",
    "ExperimentDefinition",
    "ExperimentReference",
    "ExperimentResult",
    "StrategyEvidence",
    "TrialDefinition",
    "TrialIdentity",
    "canonical_json",
    "sha256_digest",
]
