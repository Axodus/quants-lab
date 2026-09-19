"""Canonical Out-of-Sample, Walk-Forward, and Statistical Validation foundations."""

from .models import (
    DatasetSegment,
    FoldResult,
    MetricComparison,
    OOSResult,
    SegmentRole,
    SelectionSnapshot,
    StatisticalDiagnostics,
    ValidationPlan,
    ValidationResult,
    WalkForwardPlan,
)
from .plan import ValidationPlanExecutor, TemporalLeakageError
from .statistics import StatisticalAnalyzer

__all__ = [
    "DatasetSegment",
    "FoldResult",
    "MetricComparison",
    "OOSResult",
    "SegmentRole",
    "SelectionSnapshot",
    "StatisticalAnalyzer",
    "StatisticalDiagnostics",
    "TemporalLeakageError",
    "ValidationPlan",
    "ValidationPlanExecutor",
    "ValidationResult",
    "WalkForwardPlan",
]
