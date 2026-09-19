"""Deterministic generation of TrialDefinitions from ParameterSpace."""

from __future__ import annotations

import itertools
from typing import Any, Mapping, Sequence

from core.quant_foundations.models import ExperimentDefinition, TrialDefinition
from .models import (
    ParameterConstraint,
    ParameterDefinition,
    ParameterSpace,
    TrialBudget,
    TrialBudgetExceededError,
)


class TrialGenerationError(ValueError):
    """Raised when trial generation encounters an invalid specification."""


class GridTrialGenerator:
    """Exhaustive, deterministic Cartesian-product generator with constraint filtering."""

    def __init__(self, parameter_space: ParameterSpace, budget: TrialBudget | None = None):
        self.parameter_space = parameter_space
        self.budget = budget or TrialBudget()

    def generate(
        self,
        experiment: ExperimentDefinition,
        dataset_ref: Mapping[str, Any],
        execution_assumptions_ref: str | None = None,
    ) -> tuple[tuple[TrialDefinition, ...], int]:
        """Generate deterministic list of TrialDefinitions and return (valid_trials, invalid_combinations_count)."""
        expected_raw = self.parameter_space.cardinality
        if expected_raw > self.budget.max_generated_trials:
            raise TrialBudgetExceededError(
                f"Cartesian product ({expected_raw}) exceeds trial budget ({self.budget.max_generated_trials})"
            )

        param_names = [p.name for p in self.parameter_space.parameters]
        param_value_lists = [p.allowed_values for p in self.parameter_space.parameters]

        valid_trials: list[TrialDefinition] = []
        invalid_count = 0
        ordinal = 0

        for combination in itertools.product(*param_value_lists):
            assignment = dict(zip(param_names, combination))
            if not self._check_constraints(assignment, self.parameter_space.constraints):
                invalid_count += 1
                continue

            trial = TrialDefinition(
                experiment_id=experiment.experiment_id,
                strategy_revision_id=experiment.strategy_revision_id,
                dataset_refs=(dataset_ref,),
                parameters=assignment,
                seed=None,
                seed_policy_ref="deterministic-grid",
                execution_assumptions_ref=execution_assumptions_ref or "deterministic-candle-v1",
                ordinal=ordinal,
            )
            valid_trials.append(trial)
            ordinal += 1

        return tuple(valid_trials), invalid_count

    @staticmethod
    def _check_constraints(
        assignment: Mapping[str, Any],
        constraints: Sequence[ParameterConstraint],
    ) -> bool:
        for constraint in constraints:
            if not constraint.validate(assignment):
                return False
        return True
