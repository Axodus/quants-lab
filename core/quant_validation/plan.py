"""Deterministic executor for Holdout and Walk-Forward validation plans."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import ExperimentDefinition, ExperimentReference
from core.quant_simulation.engine import SimulationEngine
from core.quant_simulation.execution import DeterministicExecutionModel, ExecutionModel
from core.quant_simulation.models import ExecutionAssumptionProfile, StrategySimulator, text
from .models import (
    DatasetSegment,
    FoldResult,
    MetricComparison,
    OOSResult,
    SelectionSnapshot,
    ValidationPlan,
    ValidationResult,
)
from .statistics import StatisticalAnalyzer


class TemporalLeakageError(ValueError):
    """Raised when test/OOS temporal data leaks into training or selection."""


class ValidationPlanExecutor:
    """Coordinates temporal splitting, frozen selection, and execution via REQ-QUANT-03 SimulationEngine."""

    def __init__(self, simulation_engine: SimulationEngine | None = None):
        self.engine = simulation_engine or SimulationEngine()

    def execute_holdout(
        self,
        plan: ValidationPlan,
        experiment_def: ExperimentDefinition,
        market_states: Sequence[Mapping[str, Any]],
        selection_snapshot: SelectionSnapshot,
        strategy_factory: Callable[[Mapping[str, Any]], StrategySimulator],
        execution_model: ExecutionModel | None = None,
        *,
        split_index: int | None = None,
    ) -> ValidationResult:
        self._validate_leakage_holdout(market_states, split_index, selection_snapshot)
        states_list = list(market_states)
        n = len(states_list)
        idx = split_index if split_index is not None else int(n * Decimal(plan.holdout_is_ratio or "0.7"))

        if idx <= 0 or idx >= n:
            raise ValueError(f"invalid holdout split index: {idx} for total states {n}")

        is_states = states_list[:idx]
        oos_states = states_list[idx:]

        if is_states[-1]["marketTime"] >= oos_states[0]["marketTime"]:
            raise TemporalLeakageError("IS temporal bounds overlap or exceed OOS temporal bounds")

        is_seg = DatasetSegment(
            segment_id=f"seg:is:{plan.plan_id}",
            dataset_id=plan.dataset_reference["datasetId"],
            dataset_version=plan.dataset_reference["datasetVersion"],
            role="IN_SAMPLE",
            temporal_from=str(is_states[0]["marketTime"]),
            temporal_to=str(is_states[-1]["marketTime"]),
            record_count=len(is_states),
        )
        oos_seg = DatasetSegment(
            segment_id=f"seg:oos:{plan.plan_id}",
            dataset_id=plan.dataset_reference["datasetId"],
            dataset_version=plan.dataset_reference["datasetVersion"],
            role="OUT_OF_SAMPLE",
            temporal_from=str(oos_states[0]["marketTime"]),
            temporal_to=str(oos_states[-1]["marketTime"]),
            record_count=len(oos_states),
        )

        strategy = strategy_factory(selection_snapshot.selected_parameters)
        strategy_rev = {"revisionId": selection_snapshot.strategy_revision_id}
        exp_ref = experiment_def.to_reference()

        sim_result = self.engine.run(
            experiment=experiment_def,
            experiment_ref=exp_ref,
            dataset_reference=plan.dataset_reference,
            market_states=oos_states,
            strategy_revision=strategy_rev,
            strategy=strategy,
            execution_model=execution_model,
            run_id=f"oos:{plan.plan_id}",
        )

        comparisons: dict[str, MetricComparison] = {}
        for k, is_v_str in selection_snapshot.metrics_at_selection.items():
            if k in sim_result.metrics:
                oos_v_str = sim_result.metrics[k]
                is_dec = Decimal(is_v_str)
                oos_dec = Decimal(oos_v_str)
                deg = StatisticalAnalyzer.compute_degradation(is_dec, oos_dec)
                comparisons[k] = MetricComparison(is_value=is_v_str, oos_value=oos_v_str, degradation_pct=deg)

        oos_result = OOSResult(
            result_id=f"oos_res:{plan.plan_id}",
            plan_id=plan.plan_id,
            strategy_revision_id=plan.strategy_revision_id,
            selection_snapshot=selection_snapshot,
            oos_segment=oos_seg,
            simulation_result_digest=sim_result.result_digest,
            metrics=dict(sim_result.metrics),
            comparisons=comparisons,
            limitations=sim_result.limitations,
        )

        limitations = list(sim_result.limitations)
        limitations.append("HOLDOUT_ONLY: temporal validation on a single holdout does not prove regime invariance")
        selection_bias_warning = selection_snapshot.candidate_population_size > 10
        if selection_bias_warning:
            limitations.append(f"MULTIPLE_TESTING_WARNING: candidate selected from population of {selection_snapshot.candidate_population_size}")

        val_id = f"val:{sha256_digest({'plan': plan.to_canonical_dict(), 'oos': oos_result.to_canonical_dict()})}"
        return ValidationResult(
            validation_id=val_id,
            plan_id=plan.plan_id,
            strategy_revision_id=plan.strategy_revision_id,
            dataset_reference=plan.dataset_reference,
            status="COMPLETED",
            oos_result=oos_result,
            selection_bias_warning=selection_bias_warning,
            candidate_population_size=selection_snapshot.candidate_population_size,
            limitations=tuple(limitations),
            provenance={"planDigest": plan.plan_digest, "snapshotDigest": selection_snapshot.snapshot_digest},
        )

    def execute_walk_forward(
        self,
        plan: ValidationPlan,
        experiment_def: ExperimentDefinition,
        market_states: Sequence[Mapping[str, Any]],
        reselection_fn: Callable[[Sequence[Mapping[str, Any]], int], SelectionSnapshot],
        strategy_factory: Callable[[Mapping[str, Any]], StrategySimulator],
        execution_model: ExecutionModel | None = None,
    ) -> ValidationResult:
        wf = plan.walk_forward_plan
        if not wf:
            raise ValueError("ValidationPlan missing walk_forward_plan")

        states = list(market_states)
        n = len(states)
        fold_results: list[FoldResult] = []
        fold_ordinal = 0
        start_idx = 0

        while True:
            train_start = 0 if wf.mode == "EXPANDING" else start_idx
            train_end = start_idx + wf.train_window_steps
            test_start = train_end + wf.purge_steps + wf.embargo_steps
            test_end = test_start + wf.test_window_steps

            if test_end > n:
                break

            train_states = states[train_start:train_end]
            test_states = states[test_start:test_end]

            if train_states[-1]["marketTime"] >= test_states[0]["marketTime"]:
                raise TemporalLeakageError(f"Fold {fold_ordinal}: Train bounds overlap or exceed test bounds")

            train_seg = DatasetSegment(
                segment_id=f"seg:wf:train:{fold_ordinal}:{plan.plan_id}",
                dataset_id=plan.dataset_reference["datasetId"],
                dataset_version=plan.dataset_reference["datasetVersion"],
                role="IN_SAMPLE",
                temporal_from=str(train_states[0]["marketTime"]),
                temporal_to=str(train_states[-1]["marketTime"]),
                record_count=len(train_states),
            )
            test_seg = DatasetSegment(
                segment_id=f"seg:wf:test:{fold_ordinal}:{plan.plan_id}",
                dataset_id=plan.dataset_reference["datasetId"],
                dataset_version=plan.dataset_reference["datasetVersion"],
                role="OUT_OF_SAMPLE",
                temporal_from=str(test_states[0]["marketTime"]),
                temporal_to=str(test_states[-1]["marketTime"]),
                record_count=len(test_states),
            )

            snapshot = reselection_fn(train_states, fold_ordinal)
            try:
                strategy = strategy_factory(snapshot.selected_parameters)
                strategy_rev = {"revisionId": snapshot.strategy_revision_id}
                exp_ref = experiment_def.to_reference()

                sim_res = self.engine.run(
                    experiment=experiment_def,
                    experiment_ref=exp_ref,
                    dataset_reference=plan.dataset_reference,
                    market_states=test_states,
                    strategy_revision=strategy_rev,
                    strategy=strategy,
                    execution_model=execution_model,
                    run_id=f"wf:{fold_ordinal}:{plan.plan_id}",
                )
                fold_res = FoldResult(
                    fold_ordinal=fold_ordinal,
                    train_segment=train_seg,
                    test_segment=test_seg,
                    selection_snapshot=snapshot,
                    simulation_result_digest=sim_res.result_digest,
                    metrics=dict(sim_res.metrics),
                    status="COMPLETED",
                )
            except Exception as exc:
                fold_res = FoldResult(
                    fold_ordinal=fold_ordinal,
                    train_segment=train_seg,
                    test_segment=test_seg,
                    selection_snapshot=snapshot,
                    simulation_result_digest="failed",
                    metrics={},
                    status="FAILED",
                    error_reason=str(exc),
                )

            fold_results.append(fold_res)
            fold_ordinal += 1
            start_idx += wf.step_size

        if not fold_results:
            raise ValueError("Insufficient states to generate at least one walk-forward fold")

        failed_folds = sum(1 for f in fold_results if f.status != "COMPLETED")
        status = "COMPLETED" if failed_folds == 0 else "PARTIAL"
        max_pop = max((f.selection_snapshot.candidate_population_size for f in fold_results), default=1)
        bias_warn = max_pop > 10

        limitations = [
            "WALK_FORWARD_FOUNDATION: parameters re-selected per fold on training window only",
            "NO_MONTE_CARLO: does not simulate resampled market return paths",
        ]
        if failed_folds > 0:
            limitations.append(f"FAILED_FOLDS_PRESENT: {failed_folds} folds failed during simulation; aggregated statistical diagnostics withheld (fail-closed)")
            statistical_diagnostics: dict[str, Any] = {}
        else:
            pnl_vals = [Decimal(f.metrics["netPnl"]) for f in fold_results if f.status == "COMPLETED" and "netPnl" in f.metrics]
            pnl_diag = StatisticalAnalyzer.analyze_series("netPnl", pnl_vals)
            statistical_diagnostics = {"netPnl": pnl_diag}

        val_id = f"val:wf:{sha256_digest({'plan': plan.to_canonical_dict(), 'folds': len(fold_results)})}"
        return ValidationResult(
            validation_id=val_id,
            plan_id=plan.plan_id,
            strategy_revision_id=plan.strategy_revision_id,
            dataset_reference=plan.dataset_reference,
            status=status,
            fold_results=tuple(fold_results),
            statistical_diagnostics=statistical_diagnostics,
            selection_bias_warning=bias_warn,
            candidate_population_size=max_pop,
            limitations=tuple(limitations),
            provenance={"planDigest": plan.plan_digest, "foldCount": len(fold_results)},
        )

    @staticmethod
    def _validate_leakage_holdout(states: Sequence[Mapping[str, Any]], split_index: int | None, snapshot: SelectionSnapshot) -> None:
        if not states:
            raise ValueError("market_states cannot be empty")
        if split_index is not None and split_index < len(states):
            split_time = states[split_index]["marketTime"]
            if snapshot.is_segment_ref > str(split_time):
                raise TemporalLeakageError(f"SelectionSnapshot reference ({snapshot.is_segment_ref}) extends into OOS window ({split_time})")
