from decimal import Decimal

from core.quant_optimization.strategy import default_registry
from core.quant_simulation import DeterministicExecutionModel, ExecutionAssumptionProfile, SimulatedDecision, SimulationEngine
from core.quant_strategies.microstructure.microprice_displacement import (
    BASE_REVISION,
    STRATEGY_ID,
    MicropriceDisplacementConfig,
    MicropriceDisplacementStrategy,
)
from core.quant_foundations.models import ExperimentDefinition


DATASET = {
    "datasetId": "fixture:microprice-baseline",
    "datasetVersion": "v1",
    "contentDigest": "a" * 64,
    "qualityStatus": "verified",
}


def _state(timestamp_ms, *, displacement="0", imbalance="0", spread="2", valid="True", bid="99", ask="101", book="VALID"):
    features = [
        {"featureId": "microprice_displacement_bps", "value": displacement},
        {"featureId": "signed_queue_imbalance_L1", "value": imbalance},
        {"featureId": "spread_bps", "value": spread},
        {"featureId": "microprice_valid", "value": valid},
        {"featureId": "book_validity", "value": book},
        {"featureId": "best_bid", "value": bid},
        {"featureId": "best_ask", "value": ask},
    ]
    return {
        "marketStateId": f"state:{timestamp_ms}",
        "marketPrice": str((Decimal(bid) + Decimal(ask)) / Decimal("2")),
        "marketTime": f"2024-01-01T00:00:{timestamp_ms // 1000:02d}.{timestamp_ms % 1000:03d}Z",
        "observedAt": f"2024-01-01T00:00:{timestamp_ms // 1000:02d}.{timestamp_ms % 1000:03d}Z",
        "validityContext": "historical",
        "features": features,
    }


def _experiment():
    return ExperimentDefinition(
        "microprice-baseline",
        "1",
        f"{STRATEGY_ID}:{BASE_REVISION}",
        "b" * 64,
        (DATASET,),
        (),
        {},
        {"execution": "taker-only"},
        {"methodology_ref": "deterministic-simulation-v1"},
        {"seed": 0},
        {"code_ref": "fixture"},
    )


def _strategy(segment_length=None):
    return MicropriceDisplacementStrategy(MicropriceDisplacementConfig(
        long_displacement_threshold_bps=Decimal("1"),
        short_displacement_threshold_bps=Decimal("-1"),
        long_queue_imbalance_threshold=Decimal("0.2"),
        short_queue_imbalance_threshold=Decimal("-0.2"),
        max_spread_bps=Decimal("5"),
        quantity_or_notional=Decimal("1"),
    ), segment_length=segment_length)


def test_long_short_and_flat_semantics():
    strategy = _strategy()
    assert strategy.decide(_state(0, displacement="1", imbalance="0.2"), Decimal("0")).side == "BUY"
    assert strategy.decide(_state(1_000, displacement="0", imbalance="0"), Decimal("1")).action == "NO_ACTION"
    assert strategy.decide(_state(2_000, displacement="-1", imbalance="-0.2"), Decimal("1")).side == "SELL"


def test_invalid_state_and_spread_guard_are_flat():
    strategy = _strategy()
    assert strategy.decide(_state(0, displacement="2", imbalance="0.5", valid="False"), Decimal("0")).action == "NO_ACTION"
    assert strategy.decide(_state(1_000, displacement="2", imbalance="0.5", spread="6"), Decimal("0")).action == "NO_ACTION"
    assert strategy.decide(_state(2_000, displacement="2", imbalance="0.5", book="INVALID"), Decimal("0")).action == "NO_ACTION"


def test_threshold_boundaries():
    assert _strategy().decide(_state(0, displacement="1", imbalance="0.2"), Decimal("0")).side == "BUY"
    assert _strategy().decide(_state(100, displacement="0.999", imbalance="0.2"), Decimal("0")).action == "NO_ACTION"
    assert _strategy().decide(_state(200, displacement="1", imbalance="0.199"), Decimal("0")).action == "NO_ACTION"
    assert _strategy().decide(_state(300, displacement="-1", imbalance="-0.2"), Decimal("0")).side == "SELL"
    assert _strategy().decide(_state(400, displacement="-0.999", imbalance="-0.2"), Decimal("0")).action == "NO_ACTION"
    assert _strategy().decide(_state(500, displacement="-1", imbalance="-0.199"), Decimal("0")).action == "NO_ACTION"


def test_single_position_and_close_before_reverse():
    strategy = _strategy()
    entry_long = strategy.decide(_state(0, displacement="2", imbalance="0.5"), Decimal("0"))
    assert entry_long.action == "ENTER" and entry_long.side == "BUY"
    while_pending = strategy.decide(_state(100, displacement="2", imbalance="0.5"), Decimal("0"))
    assert while_pending.action == "NO_ACTION"
    in_position = strategy.decide(_state(200, displacement="2", imbalance="0.5"), Decimal("1"))
    assert in_position.action == "NO_ACTION"
    reversal_exit = strategy.decide(_state(300, displacement="-2", imbalance="-0.5"), Decimal("1"))
    assert reversal_exit.action == "EXIT" and reversal_exit.side == "SELL"


def test_holding_period_exit_at_1000ms():
    strategy = _strategy()
    strategy.decide(_state(0, displacement="2", imbalance="0.5"), Decimal("0"))
    strategy.decide(_state(100), Decimal("1"))
    strategy.decide(_state(500), Decimal("1"))
    assert strategy.decide(_state(999), Decimal("1")).action == "NO_ACTION"
    exit_decision = strategy.decide(_state(1_500), Decimal("1"))
    assert exit_decision.action == "EXIT" and exit_decision.side == "SELL"


def test_exact_economic_accounting_and_deterministic_replay():
    states = [
        _state(0, displacement="2", imbalance="0.5", bid="100", ask="102"),
        _state(500, bid="101", ask="103"),
        _state(1_500, bid="105", ask="107"),
        _state(2_000, bid="106", ask="108"),
    ]
    model = DeterministicExecutionModel(ExecutionAssumptionProfile(
        profile_id="taker-exact-v1", fee_bps=Decimal("5"), spread_bps=Decimal("0"),
        slippage_bps=Decimal("0"), latency_steps=1,
    ))
    run1 = SimulationEngine().run(
        _experiment(), _experiment().to_reference(), DATASET, states,
        {"revisionId": f"{STRATEGY_ID}:{BASE_REVISION}"}, _strategy(segment_length=len(states)),
        execution_model=model, initial_capital=Decimal("1000"),
    )
    run2 = SimulationEngine().run(
        _experiment(), _experiment().to_reference(), DATASET, states,
        {"revisionId": f"{STRATEGY_ID}:{BASE_REVISION}"}, _strategy(segment_length=len(states)),
        execution_model=model, initial_capital=Decimal("1000"),
    )
    assert run1.result_digest == run2.result_digest
    assert run1.fills[0]["price"] == "103"
    assert run1.fills[0]["fee"] == "0.0515"
    assert run1.fills[1]["price"] == "106"
    assert run1.fills[1]["fee"] == "0.053"
    assert run1.metrics["fees"] == "0.1045"
    assert run1.metrics["netPnl"] == "2.8955"


def test_execution_model_uses_execution_state_bbo_independently_of_strategy():
    model = DeterministicExecutionModel(ExecutionAssumptionProfile(
        profile_id="explicit-bbo-v1", fee_bps=Decimal("5"), latency_steps=1,
    ))
    buy = model.execute(
        "buy",
        SimulatedDecision("ENTER", "BUY", Decimal("1"), "buy-decision"),
        _state(0, bid="100", ask="102"),
    )[0]
    sell = model.execute(
        "sell",
        SimulatedDecision("ENTER", "SELL", Decimal("1"), "sell-decision"),
        _state(0, bid="100", ask="102"),
    )[0]
    assert buy.price == Decimal("102")
    assert sell.price == Decimal("100")
    assert buy.fee == Decimal("0.051")
    assert sell.fee == Decimal("0.05")


def test_segment_boundary_flatten_is_strategy_owned():
    states = [
        _state(0, displacement="2", imbalance="0.5", bid="100", ask="102"),
        _state(100, bid="101", ask="103"),
        _state(200, bid="102", ask="104"),
        _state(300, bid="103", ask="105"),
    ]
    result = SimulationEngine().run(
        _experiment(), _experiment().to_reference(), DATASET, states,
        {"revisionId": f"{STRATEGY_ID}:{BASE_REVISION}"}, _strategy(segment_length=len(states)),
        execution_model=DeterministicExecutionModel(ExecutionAssumptionProfile(
            profile_id="segment-boundary-v1", fee_bps=Decimal("5"), latency_steps=1,
        )),
    )
    assert result.metrics["fillCount"] == "2"
    assert result.fills[-1]["side"] == "SELL"
    assert result.fills[-1]["eventTime"].endswith("00.300Z")
    assert "terminal_force_flatten" not in result.limitations


def test_no_lookahead_guarantee():
    strategy = _strategy()
    state = _state(0, displacement="2", imbalance="0.5")
    decision = strategy.decide(state, Decimal("0"))
    assert decision.action == "ENTER"
    assert all("computedAt" not in f or f["computedAt"] <= state["observedAt"] for f in state["features"])


def test_registry_contains_immutable_strategy_identity():
    registry = default_registry()
    definition = registry[(STRATEGY_ID, BASE_REVISION)]
    assert definition.factory == MicropriceDisplacementStrategy.from_parameters
    assert len(definition.implementation_hash) == 64
    assert definition.strategy_id == STRATEGY_ID
    assert definition.base_revision == BASE_REVISION
