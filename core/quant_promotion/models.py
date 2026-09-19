"""Canonical models for Strategy Evidence Qualification and Promotion Gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import DatasetManifest, ExperimentReference, StrategyEvidence
from core.quant_robustness.models import ParameterRobustnessReport
from core.quant_simulation.models import SimulationResult
from core.quant_validation.models import ValidationResult

StrategyLifecycleState = Literal[
    "IDEA",
    "RESEARCH",
    "BACKTEST",
    "ROBUSTNESS",
    "PAPER",
    "TESTNET",
    "LIMITED_LIVE",
    "PRODUCTION",
    "MONITOR",
    "REVALIDATION",
    "DEMOTED",
    "RETIRED",
]

PromotionEligibilityDecision = Literal[
    "ELIGIBLE",
    "NOT_ELIGIBLE",
    "REVALIDATION_REQUIRED",
    "DISQUALIFIED",
]


class QualificationReasonCode(str, Enum):
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    DATASET_INVALID = "DATASET_INVALID"
    METHODOLOGY_INVALID = "METHODOLOGY_INVALID"
    ROBUSTNESS_INSUFFICIENT = "ROBUSTNESS_INSUFFICIENT"
    OOS_REQUIRED = "OOS_REQUIRED"
    OOS_FAILED = "OOS_FAILED"
    WALK_FORWARD_INCOMPLETE = "WALK_FORWARD_INCOMPLETE"
    STATISTICS_UNDEFINED = "STATISTICS_UNDEFINED"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    COST_ASSUMPTION_INVALID = "COST_ASSUMPTION_INVALID"
    PROVENANCE_INCOMPLETE = "PROVENANCE_INCOMPLETE"
    STRATEGY_REVISION_MISMATCH = "STRATEGY_REVISION_MISMATCH"
    TRANSITION_INVALID = "TRANSITION_INVALID"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    ZERO_LATENCY_DISALLOWED = "ZERO_LATENCY_DISALLOWED"
    SELECTION_BIAS_EXCESSIVE = "SELECTION_BIAS_EXCESSIVE"


class EvidenceVerificationError(ValueError):
    """Raised when evidence authenticity, integrity, or provenance fails."""


@dataclass(frozen=True)
class StrategyEvidencePackage:
    strategy_id: str
    strategy_revision_id: str
    dataset_refs: tuple[Mapping[str, Any], ...]
    experiment_refs: tuple[ExperimentReference, ...]
    strategy_evidence: StrategyEvidence | None = None
    simulation_result: SimulationResult | None = None
    robustness_report: ParameterRobustnessReport | None = None
    validation_result: ValidationResult | None = None
    provenance_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_revision_id:
            raise ValueError("strategy_id and strategy_revision_id must be non-empty")

    def to_canonical_dict(self) -> dict[str, Any]:
        # Canonical representation with stable sorted arrays
        sorted_ds = sorted(self.dataset_refs, key=lambda d: (str(d.get("datasetId")), str(d.get("datasetVersion"))))
        sorted_exp = sorted(self.experiment_refs, key=lambda e: (e.experiment_id, e.experiment_revision))
        return {
            "strategyId": self.strategy_id,
            "strategyRevisionId": self.strategy_revision_id,
            "datasetRefs": sorted_ds,
            "experimentRefs": [e.to_shared_dict() for e in sorted_exp],
            "strategyEvidenceDigest": self.strategy_evidence.payload_digest if self.strategy_evidence else None,
            "simulationResultDigest": self.simulation_result.result_digest if self.simulation_result else None,
            "robustnessReportDigest": self.robustness_report.report_digest if self.robustness_report else None,
            "validationResultDigest": self.validation_result.result_digest if self.validation_result else None,
            "provenanceMetadata": dict(self.provenance_metadata),
        }

    @property
    def package_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())


@dataclass(frozen=True)
class TargetStagePolicy:
    target_state: StrategyLifecycleState
    allowed_source_states: tuple[StrategyLifecycleState, ...]
    require_simulation: bool = True
    require_robustness: bool = False
    require_oos: bool = False
    require_walk_forward: bool = False
    require_stable_region: bool = False
    disallow_zero_latency: bool = False
    max_drawdown_limit: Decimal | None = None
    min_positive_fold_ratio: Decimal | None = None
    min_sharpe_estimate: Decimal | None = None
    max_evidence_age_seconds: int | None = None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "targetState": self.target_state,
            "allowedSourceStates": list(self.allowed_source_states),
            "requireSimulation": self.require_simulation,
            "requireRobustness": self.require_robustness,
            "requireOOS": self.require_oos,
            "requireWalkForward": self.require_walk_forward,
            "requireStableRegion": self.require_stable_region,
            "disallowZeroLatency": self.disallow_zero_latency,
            "maxDrawdownLimit": str(self.max_drawdown_limit) if self.max_drawdown_limit is not None else None,
            "minPositiveFoldRatio": str(self.min_positive_fold_ratio) if self.min_positive_fold_ratio is not None else None,
            "minSharpeEstimate": str(self.min_sharpe_estimate) if self.min_sharpe_estimate is not None else None,
            "maxEvidenceAgeSeconds": self.max_evidence_age_seconds,
        }


@dataclass(frozen=True)
class StrategyQualificationPolicy:
    policy_id: str
    policy_version: str
    stage_policies: tuple[TargetStagePolicy, ...]
    authority_ref: str = "axodus:quant:qualification-gate-v1"

    def get_policy(self, target_state: StrategyLifecycleState) -> TargetStagePolicy | None:
        for sp in self.stage_policies:
            if sp.target_state == target_state:
                return sp
        return None

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "stagePolicies": [sp.to_canonical_dict() for sp in self.stage_policies],
            "authorityRef": self.authority_ref,
        }

    @property
    def policy_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())


@dataclass(frozen=True)
class PromotionEligibilityResult:
    eligibility_id: str
    strategy_revision_id: str
    target_state: StrategyLifecycleState
    decision: PromotionEligibilityDecision
    evidence_refs: tuple[str, ...]
    policy_version: str
    checks: tuple[str, ...]
    violations: tuple[str, ...]
    decided_at: str
    authority_ref: str
    current_state: StrategyLifecycleState
    policy_digest: str
    evidence_package_digest: str
    expires_at: str | None = None

    def to_shared_dict(self) -> dict[str, Any]:
        """Exact mapping to SCF-01 PromotionEligibility contract."""
        d = {
            "eligibilityId": self.eligibility_id,
            "strategyRevisionId": self.strategy_revision_id,
            "targetState": self.target_state,
            "decision": self.decision,
            "evidenceRefs": list(self.evidence_refs),
            "policyVersion": self.policy_version,
            "checks": list(self.checks),
            "violations": list(self.violations),
            "decidedAt": self.decided_at,
            "authorityRef": self.authority_ref,
        }
        if self.expires_at is not None:
            d["expiresAt"] = self.expires_at
        return d

    def to_canonical_dict(self) -> dict[str, Any]:
        res = self.to_shared_dict()
        res["currentState"] = self.current_state
        res["policyDigest"] = self.policy_digest
        res["evidencePackageDigest"] = self.evidence_package_digest
        return res

    @property
    def result_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    def to_acs_evidence(self, created_at_epoch_ms: int) -> dict[str, Any]:
        """Canonical ACS EvidenceRecordV2 envelope."""
        return {
            "schema_version": "1.0",
            "evidence_id": self.eligibility_id,
            "kind": "artifact",
            "subject_ref": {"kind": "promotion-eligibility", "id": self.eligibility_id},
            "event_ref": {"kind": "strategy-qualification", "id": self.strategy_revision_id},
            "source": "executor",
            "classification": "internal",
            "payload_digest": self.result_digest,
            "provenance": {
                "domain": "axodus-trading-quant",
                "strategy_revision_id": self.strategy_revision_id,
                "target_state": self.target_state,
                "decision": self.decision,
                "policy_version": self.policy_version,
                "payload_schema": "promotion-eligibility-v1",
            },
            "created_at": created_at_epoch_ms,
        }
