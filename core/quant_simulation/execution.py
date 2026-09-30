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
        bid, ask = _execution_bbo(market_state, mid, self.profile.spread_bps)
        executable = ask if decision.side == "BUY" else bid
        slippage = mid * self.profile.slippage_bps / Decimal("10000")
        price = executable + sign * slippage
        quantity = decision.quantity * self.profile.fill_ratio
        notional = quantity * price
        fee = notional * self.profile.fee_bps / Decimal("10000")
        if quantity == 0:
            return ()
        return (SimulatedFill(
            fill_id=f"fill:{order_id}", order_id=order_id, event_time=str(market_state["marketTime"]), side=decision.side,
            quantity=quantity, price=price, fee=fee, spread_cost=quantity * (ask - mid if decision.side == "BUY" else mid - bid),
            slippage_cost=quantity * slippage,
        ),)


def _feature_value(market_state: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in market_state:
            return market_state[name]
    for feature in market_state.get("features", ()):
        if feature.get("featureId") in names:
            return feature.get("value")
    return None


def _execution_bbo(market_state: Mapping[str, Any], mid: Decimal, configured_spread_bps: Decimal) -> tuple[Decimal, Decimal]:
    bid_value = _feature_value(market_state, "bestBid", "best_bid", "executableBid", "executable_bid")
    ask_value = _feature_value(market_state, "bestAsk", "best_ask", "executableAsk", "executable_ask")
    if bid_value is not None and ask_value is not None:
        bid = Decimal(str(bid_value))
        ask = Decimal(str(ask_value))
        if bid >= ask:
            raise ValueError("execution BBO is crossed")
        return bid, ask

    state_spread = _feature_value(market_state, "spread_bps")
    spread_bps = Decimal(str(state_spread)) if state_spread is not None else configured_spread_bps
    half_spread = mid * spread_bps / Decimal("20000")
    return mid - half_spread, mid + half_spread
