"""Single-run deterministic simulation engine for historical MarketState streams."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import ExperimentDefinition, ExperimentReference, ExperimentResult

from .execution import DeterministicExecutionModel, ExecutionModel
from .models import ExecutionAssumptionProfile, SimulatedDecision, SimulationResult, StrategySimulator, _market_price, decimal, text


class SimulationValidationError(ValueError):
    pass


class SimulationInvalidatedError(SimulationValidationError):
    pass


@dataclass
class _Position:
    quantity: Decimal = Decimal("0")
    average_entry: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class SimulationEngine:
    def run(
        self,
        experiment: ExperimentDefinition,
        experiment_ref: ExperimentReference,
        dataset_reference: Mapping[str, Any],
        market_states: Iterable[Mapping[str, Any]],
        strategy_revision: Mapping[str, Any],
        strategy: StrategySimulator,
        execution_model: ExecutionModel | None = None,
        *,
        initial_capital: Decimal | str = Decimal("1000"),
        run_id: str | None = None,
    ) -> SimulationResult:
        self._validate_lineage(experiment, experiment_ref, dataset_reference, strategy_revision)
        states = list(market_states)
        if not states:
            raise SimulationValidationError("market_states must not be empty")
        profile = execution_model.profile if isinstance(execution_model, DeterministicExecutionModel) else ExecutionAssumptionProfile()
        model = execution_model or DeterministicExecutionModel(profile)
        capital = decimal(initial_capital, "initial_capital")
        if capital <= 0:
            raise SimulationValidationError("initial_capital must be positive")
        position = _Position()
        cash = capital
        orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        equity_series: list[dict[str, str]] = []
        limitations: list[str] = []
        pending: list[tuple[int, str, SimulatedDecision]] = []
        for index, state in enumerate(states):
            self._validate_state(state)
            for due, order_id, decision in [item for item in pending if item[0] <= index]:
                pending.remove((due, order_id, decision))
                fill_items = model.execute(order_id, decision, state)
                if not fill_items:
                    orders.append({"orderId": order_id, "status": "UNFILLED", "decisionId": decision.decision_id})
                    limitations.append(f"unfilled:{order_id}")
                    continue
                for fill in fill_items:
                    self._apply_fill(fill, position, cash_holder := [cash])
                    cash = cash_holder[0]
                    fills.append(fill.to_canonical_dict())
                status = "FILLED" if fill_items[0].quantity == decision.quantity else "PARTIALLY_FILLED"
                orders.append({"orderId": order_id, "status": status, "decisionId": decision.decision_id})
                if status == "PARTIALLY_FILLED":
                    limitations.append(f"partial_fill:{order_id}")
            decision = strategy.decide(state, position.quantity)
            if decision.action != "NO_ACTION":
                due = index + profile.latency_steps
                order_id = f"order:{index}:{len(orders)}"
                if due < len(states):
                    pending.append((due, order_id, decision))
                else:
                    limitations.append(f"latency_expired:{order_id}")
            mark = _market_price(state)
            equity = cash + position.quantity * mark
            equity_series.append({"time": str(state["marketTime"]), "equity": text(equity)})
        if pending:
            limitations.append("pending_orders_at_end")
        equities = [decimal(point["equity"], "equity") for point in equity_series]
        peak = equities[0]
        max_drawdown = Decimal("0")
        for value in equities:
            peak = max(peak, value)
            if peak:
                max_drawdown = max(max_drawdown, (peak - value) / peak)
        final_equity = equities[-1]
        metrics = {"initialEquity": text(capital), "finalEquity": text(final_equity), "netPnl": text(final_equity - capital),
                   "totalReturn": text((final_equity - capital) / capital), "maxDrawdown": text(max_drawdown),
                   "fees": text(sum((decimal(item["fee"], "fee") for item in fills), Decimal("0"))),
                   "fillCount": str(len(fills)), "orderCount": str(len(orders))}
        identity = run_id or f"run:{sha256_digest({"experiment": experiment.definition_digest, "dataset": dataset_reference, "profile": profile.to_canonical_dict()})}"
        return SimulationResult(identity, "COMPLETED", experiment.experiment_id, experiment.experiment_revision,
                                experiment.strategy_revision_id, dict(dataset_reference), profile.to_canonical_dict(),
                                tuple(orders), tuple(fills), tuple(equity_series), metrics, tuple(sorted(set(limitations))),
                                {"experimentDefinitionDigest": experiment.definition_digest, "runtime": dict(experiment.runtime_provenance)})

    @staticmethod
    def _validate_lineage(experiment: ExperimentDefinition, reference: ExperimentReference, dataset: Mapping[str, Any], strategy_revision: Mapping[str, Any]) -> None:
        if reference.strategy_revision_id != experiment.strategy_revision_id:
            raise SimulationValidationError("ExperimentReference strategy revision mismatch")
        if strategy_revision.get("revisionId") != experiment.strategy_revision_id:
            raise SimulationValidationError("strategy revision is not the exact experiment revision")
        if not any(ref.get("datasetId") == dataset.get("datasetId") and ref.get("datasetVersion") == dataset.get("datasetVersion") and ref.get("contentDigest") == dataset.get("contentDigest") for ref in experiment.dataset_refs):
            raise SimulationValidationError("dataset reference is not an exact ExperimentDefinition input")
        if dataset.get("qualityStatus") in {"corrupted", "rejected"}:
            raise SimulationValidationError("dataset quality is not simulatable")

    @staticmethod
    def _validate_state(state: Mapping[str, Any]) -> None:
        if state.get("validityContext") != "historical" or not state.get("marketTime") or not state.get("observedAt"):
            raise SimulationInvalidatedError("simulation requires historical MarketState with temporal context")
        for feature in state.get("features", ()):
            if feature.get("computedAt") and feature["computedAt"] > state["observedAt"]:
                raise SimulationInvalidatedError("future feature value detected")

    @staticmethod
    def _apply_fill(fill, position: _Position, cash_holder: list[Decimal]) -> None:
        signed = fill.quantity if fill.side == "BUY" else -fill.quantity
        old = position.quantity
        cash_holder[0] -= signed * fill.price + fill.fee
        if old == 0 or old * signed > 0:
            total = abs(old) + abs(signed)
            position.average_entry = ((abs(old) * position.average_entry) + (abs(signed) * fill.price)) / total
            position.quantity += signed
        else:
            closing = min(abs(old), abs(signed))
            position.realized_pnl += closing * (fill.price - position.average_entry) * (1 if old > 0 else -1)
            position.quantity += signed
            if position.quantity == 0:
                position.average_entry = Decimal("0")
            elif old * position.quantity < 0:
                position.average_entry = fill.price
