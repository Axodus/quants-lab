"""Deterministic Mass-Trial Runner delegating to REQ-QUANT-03 SimulationEngine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.quant_foundations.models import ExperimentDefinition, ExperimentReference, TrialDefinition, TrialIdentity
from core.quant_simulation.engine import SimulationEngine, SimulationValidationError
from core.quant_simulation.execution import DeterministicExecutionModel, ExecutionModel
from core.quant_simulation.models import ExecutionAssumptionProfile, StrategySimulator
from .models import TrialBudget, TrialBudgetExceededError, TrialResultRecord


class MassTrialRunner:
    """Deterministic runner that executes trials via REQ-QUANT-03 SimulationEngine."""

    def __init__(self, simulation_engine: SimulationEngine | None = None):
        self.engine = simulation_engine or SimulationEngine()

    def run_trials(
        self,
        experiment: ExperimentDefinition,
        experiment_ref: ExperimentReference,
        dataset_ref: Mapping[str, Any],
        market_states: Iterable[Mapping[str, Any]],
        strategy_factory: Callable[[Mapping[str, Any]], StrategySimulator],
        trials: Sequence[TrialDefinition],
        execution_model: ExecutionModel | None = None,
        *,
        budget: TrialBudget | None = None,
        initial_capital: Decimal | str = Decimal("1000"),
        existing_results: Mapping[str, TrialResultRecord] | None = None,
    ) -> tuple[TrialResultRecord, ...]:
        active_budget = budget or TrialBudget()
        if len(trials) > active_budget.max_executed_trials:
            raise TrialBudgetExceededError(
                f"Number of trials to execute ({len(trials)}) exceeds max_executed_trials budget ({active_budget.max_executed_trials})"
            )

        market_states_list = list(market_states)
        completed_records: list[TrialResultRecord] = []
        seen_identities: set[str] = set()
        failed_count = 0
        cached = existing_results or {}

        # Preserved execution assumptions profile
        profile = execution_model.profile if isinstance(execution_model, DeterministicExecutionModel) else ExecutionAssumptionProfile()
        exec_profile_dict = profile.to_canonical_dict()

        for trial in trials:
            trial_identity = TrialIdentity.from_definition(trial)
            trial_id = trial_identity.trial_id

            # Duplicate suppression: identical trial identity within experiment is executed once
            if trial_id in seen_identities:
                continue
            seen_identities.add(trial_id)

            # Resume / idempotency: reuse cached valid result
            if trial_id in cached:
                cached_record = cached[trial_id]
                completed_records.append(cached_record)
                if cached_record.status != "COMPLETED":
                    failed_count += 1
                continue

            try:
                strategy = strategy_factory(trial.parameters)
                strategy_revision = {"revisionId": trial.strategy_revision_id}
                sim_result = self.engine.run(
                    experiment=experiment,
                    experiment_ref=experiment_ref,
                    dataset_reference=dataset_ref,
                    market_states=market_states_list,
                    strategy_revision=strategy_revision,
                    strategy=strategy,
                    execution_model=execution_model,
                    initial_capital=initial_capital,
                    run_id=trial_id,
                )
                record = TrialResultRecord.from_simulation(trial_id, sim_result, trial.parameters)
            except Exception as exc:
                failed_count += 1
                record = TrialResultRecord.failed(
                    trial_id=trial_id,
                    experiment_ref=experiment_ref,
                    dataset_ref=dataset_ref,
                    execution_profile=exec_profile_dict,
                    parameters=trial.parameters,
                    reason=str(exc),
                )

            completed_records.append(record)
            if failed_count > active_budget.max_failed_trials:
                raise TrialBudgetExceededError(f"Failed trials count ({failed_count}) exceeded budget ({active_budget.max_failed_trials})")

        return tuple(completed_records)
