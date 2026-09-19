"""Canonical Mass Trials and Parameter Robustness foundations."""

from .generator import GridTrialGenerator, TrialGenerationError
from .models import (
    CandidateRegion,
    MetricSummary,
    ParameterConstraint,
    ParameterDefinition,
    ParameterRobustnessReport,
    ParameterSpace,
    TrialBudget,
    TrialBudgetExceededError,
    TrialResultRecord,
)
from .robustness import ParameterRobustnessAnalyzer
from .runner import MassTrialRunner

__all__ = [
    "CandidateRegion",
    "GridTrialGenerator",
    "MassTrialRunner",
    "MetricSummary",
    "ParameterConstraint",
    "ParameterDefinition",
    "ParameterRobustnessAnalyzer",
    "ParameterRobustnessReport",
    "ParameterSpace",
    "TrialBudget",
    "TrialBudgetExceededError",
    "TrialGenerationError",
    "TrialResultRecord",
]
