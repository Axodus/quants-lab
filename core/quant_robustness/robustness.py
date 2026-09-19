"""Statistical summary, parameter neighborhood and robustness diagnostics."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_simulation.models import text
from .models import (
    CandidateRegion,
    MetricSummary,
    ParameterDefinition,
    ParameterRobustnessReport,
    ParameterSpace,
    TrialResultRecord,
    _decimal,
)


class ParameterRobustnessAnalyzer:
    """Multidimensional parameter landscape analyzer resisting single-scalar optimization."""

    def analyze(
        self,
        experiment_id: str,
        experiment_revision: str,
        strategy_revision_id: str,
        dataset_reference: Mapping[str, Any],
        parameter_space: ParameterSpace,
        trial_results: Sequence[TrialResultRecord],
        invalid_combinations_count: int,
        *,
        primary_metric: str = "netPnl",
        reference_parameters: Mapping[str, Any] | None = None,
        runtime_provenance: Mapping[str, Any] | None = None,
    ) -> ParameterRobustnessReport:
        total_attempted = len(trial_results) + invalid_combinations_count
        completed = [t for t in trial_results if t.status == "COMPLETED"]
        failed = [t for t in trial_results if t.status != "COMPLETED"]

        # Metric distributions across completed trials
        all_metric_keys: set[str] = set()
        for t in completed:
            all_metric_keys.update(t.metrics.keys())

        distributions: dict[str, MetricSummary] = {}
        for key in sorted(all_metric_keys):
            vals = [Decimal(t.metrics[key]) for t in completed if key in t.metrics]
            if vals:
                vals.sort()
                distributions[key] = MetricSummary(
                    min=text(vals[0]),
                    max=text(vals[-1]),
                    median=text(vals[len(vals) // 2]),
                    q25=text(vals[len(vals) // 4]),
                    q75=text(vals[(len(vals) * 3) // 4]),
                    count=len(vals),
                )

        highest_trial_id: str | None = None
        highest_val: Decimal | None = None
        for t in completed:
            if primary_metric in t.metrics:
                val = Decimal(t.metrics[primary_metric])
                if highest_val is None or val > highest_val:
                    highest_val = val
                    highest_trial_id = t.trial_id

        ref_trial_id: str | None = None
        if reference_parameters:
            for t in trial_results:
                if t.parameters == reference_parameters:
                    ref_trial_id = t.trial_id
                    break

        # Neighborhood & Peak Diagnostics
        candidate_regions: list[CandidateRegion] = []
        isolated_peaks: list[str] = []
        boundary_detected = False

        if highest_trial_id is not None:
            best_trial = next(t for t in completed if t.trial_id == highest_trial_id)
            neighbors = self._find_neighbors(best_trial, completed, parameter_space)
            boundary_detected = self._is_at_boundary(best_trial.parameters, parameter_space)

            if neighbors:
                neighbor_vals = [Decimal(n.metrics[primary_metric]) for n in neighbors if primary_metric in n.metrics]
                if neighbor_vals:
                    avg_neighbor = sum(neighbor_vals) / Decimal(len(neighbor_vals))
                    # If peak is > 50% higher than average of neighbors, flag as isolated peak
                    if highest_val is not None and highest_val > 0 and avg_neighbor < (highest_val * Decimal("0.5")):
                        classification = "ISOLATED_PEAK"
                        isolated_peaks.append(highest_trial_id)
                        diagnostic = f"Isolated peak at {best_trial.parameters}; neighbor avg ({text(avg_neighbor)}) is materially lower than peak ({text(highest_val)})"
                    elif boundary_detected:
                        classification = "BOUNDARY_SENSITIVE"
                        diagnostic = f"Optimum lies at search space boundary: {best_trial.parameters}"
                    else:
                        classification = "STABLE_REGION"
                        diagnostic = f"Stable neighborhood around {best_trial.parameters}; neighbor avg is {text(avg_neighbor)}"
                else:
                    classification = "DEGRADED"
                    diagnostic = "Neighbors have missing metrics"
            else:
                classification = "BOUNDARY_SENSITIVE" if boundary_detected else "DEGRADED"
                diagnostic = "No valid adjacent parameter neighbors found in tested space"

            candidate_regions.append(
                CandidateRegion(
                    region_id=f"region:{best_trial.trial_id}",
                    center_trial_id=best_trial.trial_id,
                    center_parameters=best_trial.parameters,
                    neighborhood_trial_ids=tuple(n.trial_id for n in neighbors),
                    classification=classification,
                    diagnostic_summary=diagnostic,
                )
            )

        limitations = [
            "IN_SAMPLE_ONLY: parameter robustness measured on same historical sample does not constitute out-of-sample validation",
            "NO_MONTE_CARLO: grid perturbation does not simulate random market return paths",
            "NO_WALK_FORWARD: temporal parameter stability across rolling windows is deferred to REQ-QUANT-05",
        ]
        if failed:
            limitations.append(f"FAILED_TRIALS_PRESENT: {len(failed)} trials failed during simulation")

        report_id = f"robustness:{sha256_digest({'exp': experiment_id, 'params': parameter_space.to_canonical_dict(), 'trials': len(trial_results)})}"

        return ParameterRobustnessReport(
            report_id=report_id,
            experiment_id=experiment_id,
            experiment_revision=experiment_revision,
            strategy_revision_id=strategy_revision_id,
            dataset_reference=dataset_reference,
            total_attempted_trials=total_attempted,
            completed_trials_count=len(completed),
            failed_trials_count=len(failed),
            invalid_combinations_count=invalid_combinations_count,
            search_method="GRID",
            tested_parameter_space=parameter_space.to_canonical_dict(),
            metric_distributions=distributions,
            highest_metric_trial_id=highest_trial_id,
            reference_trial_id=ref_trial_id,
            candidate_regions=tuple(candidate_regions),
            boundary_optimum_detected=boundary_detected,
            isolated_peaks_detected=tuple(isolated_peaks),
            cost_sensitivity_findings=(),
            limitations=tuple(limitations),
            runtime_provenance=dict(runtime_provenance or {}),
        )

    @staticmethod
    def _find_neighbors(
        target: TrialResultRecord,
        population: Sequence[TrialResultRecord],
        parameter_space: ParameterSpace,
    ) -> list[TrialResultRecord]:
        """Find adjacent grid neighbors (difference of exactly 1 step on at most 1 parameter)."""
        neighbors: list[TrialResultRecord] = []
        for candidate in population:
            if candidate.trial_id == target.trial_id:
                continue
            diff_count = 0
            is_neighbor = True
            for param in parameter_space.parameters:
                val_target = target.parameters.get(param.name)
                val_cand = candidate.parameters.get(param.name)
                if val_target != val_cand:
                    diff_count += 1
                    if diff_count > 1:
                        is_neighbor = False
                        break
                    # Check if adjacency in allowed_values index is 1
                    try:
                        idx_target = param.allowed_values.index(val_target)
                        idx_cand = param.allowed_values.index(val_cand)
                        if abs(idx_target - idx_cand) != 1:
                            is_neighbor = False
                            break
                    except ValueError:
                        is_neighbor = False
                        break
            if is_neighbor and diff_count == 1:
                neighbors.append(candidate)
        return neighbors

    @staticmethod
    def _is_at_boundary(
        parameters: Mapping[str, Any],
        parameter_space: ParameterSpace,
    ) -> bool:
        for param in parameter_space.parameters:
            val = parameters.get(param.name)
            if val is not None and len(param.allowed_values) > 1:
                if val == param.allowed_values[0] or val == param.allowed_values[-1]:
                    return True
        return False
