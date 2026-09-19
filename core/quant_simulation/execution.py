"""Deterministic simulated execution; no exchange or credential surface."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol

from .models import ExecutionAssumptionProfile, SimulatedDecision, SimulatedFill, _market_price


class ExecutionModel(Protocol):
    def execute(self, order_id: str, decision: SimulatedDecision, market_state: Mapping[str, Any]) -> tuple[SimulatedFill, ...]: ...


class DeterministicExecutionModel:
    def __init__(self, profile: ExecutionAssumptionProfile):
        self.profile = profile

    def execute(self, order_id: str, decision: SimulatedDecision, market_state: Mapping[str, Any]) -> tuple[SimulatedFill, ...]:
        if decision.action == "NO_ACTION":
            return ()
        mid = _market_price(market_state)
        sign = Decimal("1") if decision.side == "BUY" else Decimal("-1")
        spread = mid * self.profile.spread_bps / Decimal("20000")
        slippage = mid * self.profile.slippage_bps / Decimal("10000")
        price = mid + sign * (spread + slippage)
        quantity = decision.quantity * self.profile.fill_ratio
        notional = quantity * price
        fee = notional * self.profile.fee_bps / Decimal("10000")
        if quantity == 0:
            return ()
        return (SimulatedFill(
            fill_id=f"fill:{order_id}", order_id=order_id, event_time=str(market_state["marketTime"]), side=decision.side,
            quantity=quantity, price=price, fee=fee, spread_cost=quantity * spread, slippage_cost=quantity * slippage,
        ),)
