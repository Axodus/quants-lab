"""Deterministic Strategy Evidence Qualification Gate."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from core.quant_foundations.canonical import sha256_digest
from .models import (
    EvidenceVerificationError,
    PromotionEligibilityResult,
    QualificationReasonCode,
    StrategyEvidencePackage,
    StrategyLifecycleState,
    StrategyQualificationPolicy,
    TargetStagePolicy,
)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


class StrategyEvidenceQualificationGate:
    """Evaluates exact StrategyRevision evidence against versioned target-stage policy."""

    def __init__(self, policy: StrategyQualificationPolicy):
        self.policy = policy

    def evaluate(
        self,
        strategy_id: str,
        strategy_revision_id: str,
        current_state: StrategyLifecycleState,
        target_state: StrategyLifecycleState,
        evidence_package: StrategyEvidencePackage,
        evaluation_time: str,
    ) -> PromotionEligibilityResult:
        checks: list[str] = []
        violations: list[str] = []
        eval_dt = _parse_iso(evaluation_time)

        # 1. Exact StrategyRevision and StrategyIdentity binding
        checks.append("STRATEGY_REVISION_BINDING")
        if evidence_package.strategy_id != strategy_id or evidence_package.strategy_revision_id != strategy_revision_id:
            violations.append(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value)

        # 2. Target Stage Policy Availability
        checks.append("POLICY_LOOKUP")
        stage_policy = self.policy.get_policy(target_state)
        if not stage_policy:
            violations.append(QualificationReasonCode.POLICY_UNAVAILABLE.value)
            return self._build_result(
                strategy_revision_id,
                current_state,
                target_state,
                "NOT_ELIGIBLE",
                evidence_package,
                checks,
                violations,
                evaluation_time,
            )

        # 3. Lifecycle Transition Validity
        checks.append("LIFECYCLE_TRANSITION_VALIDITY")
        if current_state not in stage_policy.allowed_source_states:
            violations.append(QualificationReasonCode.TRANSITION_INVALID.value)

        # 4. Dataset Integrity & Exact Reference
        checks.append("DATASET_INTEGRITY")
        if not evidence_package.dataset_refs:
            violations.append(QualificationReasonCode.DATASET_INVALID.value)
        else:
            for ds in evidence_package.dataset_refs:
                if not ds.get("datasetId") or not ds.get("datasetVersion") or not ds.get("contentDigest"):
                    violations.append(QualificationReasonCode.DATASET_INVALID.value)
                    break
                if ds.get("qualityStatus") in {"corrupted", "rejected"}:
                    violations.append(QualificationReasonCode.DATASET_INVALID.value)
                    break

        # 5. Provenance Integrity across Evidence Items
        checks.append("EVIDENCE_PROVENANCE")
        if evidence_package.strategy_evidence:
            if evidence_package.strategy_evidence.strategy_revision_id != strategy_revision_id:
                violations.append(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value)

        if evidence_package.simulation_result:
            if evidence_package.simulation_result.strategy_revision_id != strategy_revision_id:
                violations.append(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value)

        if evidence_package.robustness_report:
            if evidence_package.robustness_report.strategy_revision_id != strategy_revision_id:
                violations.append(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value)

        if evidence_package.validation_result:
            if evidence_package.validation_result.strategy_revision_id != strategy_revision_id:
                violations.append(QualificationReasonCode.STRATEGY_REVISION_MISMATCH.value)

        # 6. Simulation Evidence Qualification
        if stage_policy.require_simulation:
            checks.append("SIMULATION_REQUIREMENT")
            sim = evidence_package.simulation_result
            if not sim or sim.status != "COMPLETED":
                violations.append(QualificationReasonCode.EVIDENCE_MISSING.value)
            else:
                # Execution assumptions check
                exec_profile = sim.execution_profile
                if stage_policy.disallow_zero_latency and exec_profile.get("latencySteps", 0) == 0:
                    violations.append(QualificationReasonCode.ZERO_LATENCY_DISALLOWED.value)
                if stage_policy.max_drawdown_limit is not None:
                    dd_str = sim.metrics.get("maxDrawdown", "1.0")
                    if Decimal(dd_str) > stage_policy.max_drawdown_limit:
                        violations.append(QualificationReasonCode.DRAWDOWN_LIMIT.value)

        # 7. Parameter Robustness Qualification
        if stage_policy.require_robustness:
            checks.append("ROBUSTNESS_REQUIREMENT")
            rob = evidence_package.robustness_report
            if not rob:
                violations.append(QualificationReasonCode.EVIDENCE_MISSING.value)
            else:
                if rob.failed_trials_count > 0:
                    violations.append(QualificationReasonCode.EVIDENCE_PARTIAL.value)
                if stage_policy.require_stable_region:
                    # Must have at least one candidate region classified as STABLE_REGION
                    has_stable = any(r.classification == "STABLE_REGION" for r in rob.candidate_regions)
                    if not has_stable:
                        violations.append(QualificationReasonCode.ROBUSTNESS_INSUFFICIENT.value)

        # 8. Out-of-Sample (OOS) Qualification
        if stage_policy.require_oos:
            checks.append("OOS_VALIDATION_REQUIREMENT")
            val = evidence_package.validation_result
            if not val or not val.oos_result:
                violations.append(QualificationReasonCode.OOS_REQUIRED.value)
            else:
                if val.status != "COMPLETED":
                    violations.append(QualificationReasonCode.OOS_FAILED.value)

        # 9. Walk-Forward Qualification (AUD-Q-01 Preservation)
        if stage_policy.require_walk_forward:
            checks.append("WALK_FORWARD_REQUIREMENT")
            val = evidence_package.validation_result
            if not val or not val.fold_results:
                violations.append(QualificationReasonCode.WALK_FORWARD_INCOMPLETE.value)
            else:
                # AUD-Q-01 invariant: If ANY fold failed or overall status is not COMPLETED, it cannot qualify
                has_failed_folds = any(f.status != "COMPLETED" for f in val.fold_results)
                if val.status != "COMPLETED" or has_failed_folds:
                    violations.append(QualificationReasonCode.WALK_FORWARD_INCOMPLETE.value)

                # Statistical thresholds across folds
                if stage_policy.min_positive_fold_ratio is not None:
                    diag = val.statistical_diagnostics.get("netPnl")
                    if not diag or Decimal(diag.positive_fold_ratio) < stage_policy.min_positive_fold_ratio:
                        violations.append(QualificationReasonCode.STATISTICS_UNDEFINED.value)

                if stage_policy.min_sharpe_estimate is not None:
                    diag = val.statistical_diagnostics.get("netPnl")
                    if not diag or diag.annualized_sharpe_estimate is None or Decimal(diag.annualized_sharpe_estimate) < stage_policy.min_sharpe_estimate:
                        violations.append(QualificationReasonCode.STATISTICS_UNDEFINED.value)

        # 10. Evidence Freshness
        if stage_policy.max_evidence_age_seconds is not None:
            checks.append("EVIDENCE_FRESHNESS")
            created_at = evidence_package.provenance_metadata.get("createdAt")
            if created_at:
                age_seconds = (eval_dt - _parse_iso(str(created_at))).total_seconds()
                if age_seconds > stage_policy.max_evidence_age_seconds:
                    violations.append(QualificationReasonCode.EVIDENCE_STALE.value)
            else:
                violations.append(QualificationReasonCode.PROVENANCE_INCOMPLETE.value)

        # Deterministic deduplicated and sorted ordering for violations
        unique_violations = tuple(sorted(set(violations)))
        unique_checks = tuple(sorted(set(checks)))

        decision: PromotionEligibilityDecision = "ELIGIBLE" if not unique_violations else "NOT_ELIGIBLE"

        return self._build_result(
            strategy_revision_id,
            current_state,
            target_state,
            decision,
            evidence_package,
            unique_checks,
            unique_violations,
            evaluation_time,
        )

    def _build_result(
        self,
        strategy_revision_id: str,
        current_state: StrategyLifecycleState,
        target_state: StrategyLifecycleState,
        decision: PromotionEligibilityDecision,
        evidence_package: StrategyEvidencePackage,
        checks: tuple[str, ...],
        violations: tuple[str, ...],
        evaluation_time: str,
    ) -> PromotionEligibilityResult:
        evidence_refs = self._extract_evidence_refs(evidence_package)
        eligibility_id = f"eligibility:{sha256_digest({'strat': strategy_revision_id, 'target': target_state, 'policy': self.policy.policy_digest, 'ev': evidence_package.package_digest, 'time': evaluation_time})}"
        return PromotionEligibilityResult(
            eligibility_id=eligibility_id,
            strategy_revision_id=strategy_revision_id,
            target_state=target_state,
            decision=decision,
            evidence_refs=evidence_refs,
            policy_version=self.policy.policy_version,
            checks=checks,
            violations=violations,
            decided_at=evaluation_time,
            authority_ref=self.policy.authority_ref,
            current_state=current_state,
            policy_digest=self.policy.policy_digest,
            evidence_package_digest=evidence_package.package_digest,
        )

    @staticmethod
    def _extract_evidence_refs(pkg: StrategyEvidencePackage) -> tuple[str, ...]:
        refs: list[str] = []
        if pkg.strategy_evidence:
            refs.append(f"evidence:{pkg.strategy_evidence.evidence_id}")
        if pkg.simulation_result:
            refs.append(f"simulation:{pkg.simulation_result.run_id}")
        if pkg.robustness_report:
            refs.append(f"robustness:{pkg.robustness_report.report_id}")
        if pkg.validation_result:
            refs.append(f"validation:{pkg.validation_result.validation_id}")
        for exp in pkg.experiment_refs:
            refs.append(f"experiment:{exp.experiment_id}@{exp.experiment_revision}")
        for ds in pkg.dataset_refs:
            refs.append(f"dataset:{ds.get('datasetId')}@{ds.get('datasetVersion')}")
        return tuple(sorted(set(refs)))
