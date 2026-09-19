"""Provider-neutral simulation models with deterministic decimal semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping, Protocol

from core.quant_foundations.canonical import sha256_digest

Action = Literal["NO_ACTION", "ENTER", "EXIT", "ADJUST"]
Side = Literal["BUY", "SELL"]
OrderStatus = Literal["FILLED", "PARTIALLY_FILLED", "UNFILLED", "REJECTED"]


def decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class ExecutionAssumptionProfile:
    profile_id: str = "deterministic-candle-v1"
    profile_version: str = "1.0.0"
    fee_bps: Decimal = Decimal("0")
    spread_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    latency_steps: int = 0
    fill_ratio: Decimal = Decimal("1")
    max_leverage: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        for name in ("fee_bps", "spread_bps", "slippage_bps", "fill_ratio", "max_leverage"):
            value = decimal(getattr(self, name), name)
            if value < 0 or (name == "fill_ratio" and value > 1) or (name == "max_leverage" and value == 0):
                raise ValueError(f"invalid {name}")
        if self.latency_steps < 0:
            raise ValueError("latency_steps must be non-negative")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {"profileId": self.profile_id, "profileVersion": self.profile_version,
                "feeBps": text(decimal(self.fee_bps, "fee_bps")), "spreadBps": text(decimal(self.spread_bps, "spread_bps")),
                "slippageBps": text(decimal(self.slippage_bps, "slippage_bps")), "latencySteps": self.latency_steps,
                "fillRatio": text(decimal(self.fill_ratio, "fill_ratio")), "maxLeverage": text(decimal(self.max_leverage, "max_leverage"))}


@dataclass(frozen=True)
class SimulatedDecision:
    action: Action
    side: Side | None = None
    quantity: Decimal = Decimal("0")
    decision_id: str = "decision"
    rationale_ref: str | None = None

    def __post_init__(self) -> None:
        if self.action == "NO_ACTION":
            if self.side is not None or decimal(self.quantity, "quantity") != 0:
                raise ValueError("NO_ACTION must not carry an order quantity or side")
        elif self.side not in {"BUY", "SELL"} or decimal(self.quantity, "quantity") <= 0:
            raise ValueError("an actionable decision requires a positive quantity and side")

    @classmethod
    def no_action(cls, decision_id: str = "decision") -> "SimulatedDecision":
        return cls(action="NO_ACTION", decision_id=decision_id)


@dataclass(frozen=True)
class SimulatedFill:
    fill_id: str
    order_id: str
    event_time: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal

    def to_canonical_dict(self) -> dict[str, Any]:
        return {"fillId": self.fill_id, "orderId": self.order_id, "eventTime": self.event_time, "side": self.side,
                "quantity": text(self.quantity), "price": text(self.price), "fee": text(self.fee),
                "spreadCost": text(self.spread_cost), "slippageCost": text(self.slippage_cost)}


@dataclass(frozen=True)
class SimulationResult:
    run_id: str
    status: Literal["COMPLETED", "PARTIAL", "FAILED", "INVALIDATED"]
    experiment_id: str
    experiment_revision: str
    strategy_revision_id: str
    dataset_reference: Mapping[str, Any]
    execution_profile: Mapping[str, Any]
    orders: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    equity_series: tuple[Mapping[str, str], ...]
    metrics: Mapping[str, str]
    limitations: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def result_digest(self) -> str:
        return sha256_digest(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, Any]:
        return {"runId": self.run_id, "status": self.status, "experimentId": self.experiment_id,
                "experimentRevision": self.experiment_revision, "strategyRevisionId": self.strategy_revision_id,
                "datasetReference": dict(self.dataset_reference), "executionProfile": dict(self.execution_profile),
                "orders": list(self.orders), "fills": list(self.fills), "equitySeries": list(self.equity_series),
                "metrics": dict(self.metrics), "limitations": list(self.limitations), "provenance": dict(self.provenance)}

    def to_experiment_result(self, experiment_reference) -> Any:
        """Project one completed simulation into the canonical Quant result contract."""
        from core.quant_foundations.models import ExperimentResult

        status = "completed" if self.status == "COMPLETED" else "partial" if self.status == "PARTIAL" else "failed"
        return ExperimentResult(
            experiment_ref=experiment_reference,
            strategy_revision_id=self.strategy_revision_id,
            completed_trial_count=1 if status in {"completed", "partial"} else 0,
            failed_trial_count=0 if status in {"completed", "partial"} else 1,
            trial_refs=({"runId": self.run_id, "resultDigest": self.result_digest},),
            metrics_ref=f"metrics:{self.result_digest}",
            statistical_ref=None,
            robustness_ref=None,
            limitations=self.limitations,
            status=status,
            provenance_ref=str(self.provenance.get("experimentDefinitionDigest", self.result_digest)),
        )


class StrategySimulator(Protocol):
    def decide(self, market_state: Mapping[str, Any], position_quantity: Decimal) -> SimulatedDecision: ...


class NoActionStrategy:
    def decide(self, market_state: Mapping[str, Any], position_quantity: Decimal) -> SimulatedDecision:
        return SimulatedDecision.no_action(f"decision:{market_state.get('marketTime', 'unknown')}")


@dataclass
class ReferenceThresholdStrategy:
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal = Decimal("1")
    entered: bool = False

    def __post_init__(self) -> None:
        self.entry_price = decimal(self.entry_price, "entry_price")
        self.exit_price = decimal(self.exit_price, "exit_price")
        self.quantity = decimal(self.quantity, "quantity")

    def decide(self, market_state: Mapping[str, Any], position_quantity: Decimal) -> SimulatedDecision:
        price = _market_price(market_state)
        stamp = str(market_state.get("marketTime", "unknown"))
        if position_quantity == 0 and not self.entered and price <= self.entry_price:
            self.entered = True
            return SimulatedDecision("ENTER", "BUY", self.quantity, f"enter:{stamp}")
        if position_quantity > 0 and price >= self.exit_price:
            return SimulatedDecision("EXIT", "SELL", position_quantity, f"exit:{stamp}")
        return SimulatedDecision.no_action(f"decision:{stamp}")


def _market_price(state: Mapping[str, Any]) -> Decimal:
    if state.get("marketPrice") is not None:
        return decimal(state["marketPrice"], "marketPrice")
    for feature in state.get("features", ()):
        if feature.get("featureId") in {"close", "price", "mid_price"}:
            return decimal(feature["value"], "feature.value")
    raise ValueError("MarketState requires marketPrice or a close/price feature")
