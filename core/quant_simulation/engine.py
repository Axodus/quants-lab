"""Single-run deterministic simulation engine for historical MarketState streams."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from core.quant_foundations.canonical import sha256_digest
from core.quant_foundations.models import ExperimentDefinition, ExperimentReference, ExperimentResult

from .execution import DeterministicExecutionModel, ExecutionModel
from .models import (
    ClosedTradeResult,
    ExecutionAssumptionProfile,
    SimulatedDecision,
    SimulationResult,
    StrategySimulator,
    _market_price,
    decimal,
    text,
)


class SimulationValidationError(ValueError):
    pass


class SimulationInvalidatedError(SimulationValidationError):
    pass


@dataclass
class _Position:
    quantity: Decimal = Decimal("0")
    average_entry: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    active_trade_id: str | None = None
    entry_timestamp: str | None = None
    entry_side: str | None = None
    entry_quantity: Decimal = Decimal("0")
    entry_value: Decimal = Decimal("0")
    entry_fees: Decimal = Decimal("0")
    entry_slippage: Decimal = Decimal("0")
    closed_quantity: Decimal = Decimal("0")
    closed_gross_pnl: Decimal = Decimal("0")
    exit_value: Decimal = Decimal("0")
    exit_fees: Decimal = Decimal("0")
    exit_slippage: Decimal = Decimal("0")


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
        closed_trades: list[dict[str, Any]] = []
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
                    closed_trade = self._apply_fill(fill, position, cash_holder := [cash])
                    cash = cash_holder[0]
                    fills.append(fill.to_canonical_dict())
                    if closed_trade is not None:
                        closed_trades.append(closed_trade.to_canonical_dict())
                        callback = getattr(strategy, "on_trade_closed", None)
                        if callable(callback):
                            callback(closed_trade)
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
                   "fillCount": str(len(fills)), "orderCount": str(len(orders)),
                   "closedTradeCount": str(len(closed_trades))}
        identity = run_id or f"run:{sha256_digest({"experiment": experiment.definition_digest, "dataset": dataset_reference, "profile": profile.to_canonical_dict()})}"
        return SimulationResult(
            run_id=identity,
            status="COMPLETED",
            experiment_id=experiment.experiment_id,
            experiment_revision=experiment.experiment_revision,
            strategy_revision_id=experiment.strategy_revision_id,
            dataset_reference=dict(dataset_reference),
            execution_profile=profile.to_canonical_dict(),
            orders=tuple(orders),
            fills=tuple(fills),
            equity_series=tuple(equity_series),
            metrics=metrics,
            limitations=tuple(sorted(set(limitations))),
            provenance={"experimentDefinitionDigest": experiment.definition_digest, "runtime": dict(experiment.runtime_provenance)},
            closed_trades=tuple(closed_trades),
        )

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
    def _apply_fill(fill, position: _Position, cash_holder: list[Decimal]) -> ClosedTradeResult | None:
        signed = fill.quantity if fill.side == "BUY" else -fill.quantity
        old = position.quantity
        cash_holder[0] -= signed * fill.price + fill.fee
        if old == 0 or old * signed > 0:
            total = abs(old) + abs(signed)
            position.average_entry = ((abs(old) * position.average_entry) + (abs(signed) * fill.price)) / total
            position.quantity += signed
            if old == 0:
                position.active_trade_id = f"trade:{fill.fill_id}"
                position.entry_timestamp = fill.event_time
                position.entry_side = fill.side
                position.entry_quantity = fill.quantity
                position.entry_value = fill.quantity * fill.price
            else:
                position.entry_quantity += fill.quantity
                position.entry_value += fill.quantity * fill.price
            position.entry_fees += fill.fee
            position.entry_slippage += fill.slippage_cost
            return None
        else:
            closing = min(abs(old), abs(signed))
            exit_fraction = closing / fill.quantity
            gross = closing * (fill.price - position.average_entry) * (1 if old > 0 else -1)
            position.realized_pnl += gross
            position.closed_quantity += closing
            position.closed_gross_pnl += gross
            position.exit_value += closing * fill.price
            position.exit_fees += fill.fee * exit_fraction
            position.exit_slippage += fill.slippage_cost * exit_fraction
            position.quantity += signed
            if position.quantity != 0 and old * position.quantity > 0:
                return None

            if not position.active_trade_id or not position.entry_timestamp or not position.entry_side:
                raise SimulationInvalidatedError("position closed without an active trade lifecycle")
            if position.closed_quantity <= 0:
                raise SimulationInvalidatedError("closed trade has no closed quantity")
            if position.entry_quantity <= 0:
                raise SimulationInvalidatedError("closed trade has no entry quantity")

            fees = position.entry_fees + position.exit_fees
            gross_pnl = position.closed_gross_pnl
            closed_trade = ClosedTradeResult(
                trade_id=position.active_trade_id,
                side=position.entry_side,
                quantity=position.closed_quantity,
                entry_timestamp=position.entry_timestamp,
                exit_timestamp=fill.event_time,
                entry_price=position.entry_value / position.entry_quantity,
                exit_price=position.exit_value / position.closed_quantity,
                gross_pnl=gross_pnl,
                fees=fees,
                slippage_cost=position.entry_slippage + position.exit_slippage,
                net_pnl=gross_pnl - fees,
            )

            remaining_quantity = abs(position.quantity)
            remaining_fee = fill.fee * (Decimal("1") - exit_fraction)
            remaining_slippage = fill.slippage_cost * (Decimal("1") - exit_fraction)
            new_side = fill.side if remaining_quantity else None
            position.average_entry = fill.price if remaining_quantity else Decimal("0")
            position.active_trade_id = f"trade:{fill.fill_id}:reversal" if remaining_quantity else None
            position.entry_timestamp = fill.event_time if remaining_quantity else None
            position.entry_side = new_side
            position.entry_quantity = remaining_quantity
            position.entry_value = remaining_quantity * fill.price
            position.entry_fees = remaining_fee
            position.entry_slippage = remaining_slippage
            position.closed_quantity = Decimal("0")
            position.closed_gross_pnl = Decimal("0")
            position.exit_value = Decimal("0")
            position.exit_fees = Decimal("0")
            position.exit_slippage = Decimal("0")
            return closed_trade
