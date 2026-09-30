import unittest
from decimal import Decimal

from core.quant_foundations.models import ExperimentDefinition
from core.quant_simulation.engine import SimulationEngine, _Position
from core.quant_simulation.models import ExecutionAssumptionProfile, SimulatedDecision, SimulatedFill


def _fill(fill_id, side, qty, price, fee="0", slip="0"):
    return SimulatedFill(
        fill_id=fill_id,
        order_id=fill_id.replace("fill", "order"),
        event_time="2026-09-30T00:00:00+00:00",
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        spread_cost=Decimal("0"),
        slippage_cost=Decimal(slip),
    )


class PositionPartialCloseCostBasisTests(unittest.TestCase):
    def test_long_partial_reductions_preserve_average_entry_and_close_economics(self):
        position = _Position()
        cash = [Decimal("100000")]
        SimulationEngine._apply_fill(_fill("fill1", "BUY", "10", "100", "1"), position, cash)
        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(cash[0], Decimal("98999"))

        self.assertIsNone(SimulationEngine._apply_fill(_fill("fill2", "SELL", "4", "110", "0.4"), position, cash))
        self.assertEqual(position.quantity, Decimal("6"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(position.realized_pnl, Decimal("40"))
        self.assertEqual(cash[0], Decimal("99438.6"))

        self.assertIsNone(SimulationEngine._apply_fill(_fill("fill3", "SELL", "3", "120", "0.3"), position, cash))
        self.assertEqual(position.quantity, Decimal("3"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(position.realized_pnl, Decimal("100"))

        closed = SimulationEngine._apply_fill(_fill("fill4", "SELL", "3", "130", "0.3"), position, cash)
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.average_entry, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("190"))
        self.assertEqual(cash[0], Decimal("100188.0"))
        self.assertEqual(closed.quantity, Decimal("10"))
        self.assertEqual(closed.entry_price, Decimal("100"))
        self.assertEqual(closed.exit_price, Decimal("119"))
        self.assertEqual(closed.gross_pnl, Decimal("190"))
        self.assertEqual(closed.fees, Decimal("2.0"))
        self.assertEqual(closed.net_pnl, Decimal("188.0"))

    def test_short_partial_reductions_preserve_average_entry(self):
        position = _Position()
        cash = [Decimal("100000")]
        SimulationEngine._apply_fill(_fill("fill1", "SELL", "10", "100", "1"), position, cash)
        self.assertEqual(position.quantity, Decimal("-10"))
        self.assertEqual(position.average_entry, Decimal("100"))

        SimulationEngine._apply_fill(_fill("fill2", "BUY", "4", "90", "0.4"), position, cash)
        self.assertEqual(position.quantity, Decimal("-6"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(position.realized_pnl, Decimal("40"))

        SimulationEngine._apply_fill(_fill("fill3", "BUY", "3", "80", "0.3"), position, cash)
        self.assertEqual(position.quantity, Decimal("-3"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(position.realized_pnl, Decimal("100"))

        closed = SimulationEngine._apply_fill(_fill("fill4", "BUY", "3", "70", "0.3"), position, cash)
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.average_entry, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("190"))
        self.assertEqual(closed.quantity, Decimal("10"))
        self.assertEqual(closed.entry_price, Decimal("100"))
        self.assertEqual(closed.exit_price, Decimal("81"))
        self.assertEqual(closed.gross_pnl, Decimal("190"))
        self.assertEqual(closed.net_pnl, Decimal("188.0"))

    def test_partial_reduction_then_addition_preserves_remaining_cost_basis_and_lifecycle_economics(self):
        position = _Position()
        cash = [Decimal("100000")]

        SimulationEngine._apply_fill(_fill("fill1", "BUY", "10", "100", "1"), position, cash)
        SimulationEngine._apply_fill(_fill("fill2", "SELL", "4", "110", "0.4"), position, cash)

        self.assertEqual(position.quantity, Decimal("6"))
        self.assertEqual(position.average_entry, Decimal("100"))
        self.assertEqual(position.realized_pnl, Decimal("40"))

        SimulationEngine._apply_fill(_fill("fill3", "BUY", "4", "120", "0.48"), position, cash)
        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.average_entry, Decimal("108"))

        closed = SimulationEngine._apply_fill(_fill("fill4", "SELL", "10", "130", "1.3"), position, cash)

        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.average_entry, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("260"))
        self.assertEqual(cash[0], Decimal("100256.82"))
        self.assertEqual(closed.quantity, Decimal("14"))
        self.assertEqual(closed.entry_price, Decimal("1480") / Decimal("14"))
        self.assertEqual(closed.exit_price, Decimal("1740") / Decimal("14"))
        self.assertEqual(closed.gross_pnl, Decimal("260"))
        self.assertEqual(closed.fees, Decimal("3.18"))
        self.assertEqual(closed.net_pnl, Decimal("256.82"))

    def test_reversal_closes_old_trade_and_starts_new_cost_basis(self):
        position = _Position()
        cash = [Decimal("100000")]
        SimulationEngine._apply_fill(_fill("fill1", "BUY", "10", "100", "1"), position, cash)
        closed = SimulationEngine._apply_fill(_fill("fill2", "SELL", "15", "110", "1.5"), position, cash)

        self.assertEqual(closed.side, "BUY")
        self.assertEqual(closed.quantity, Decimal("10"))
        self.assertEqual(closed.gross_pnl, Decimal("100"))
        self.assertEqual(position.quantity, Decimal("-5"))
        self.assertEqual(position.average_entry, Decimal("110"))
        self.assertEqual(position.entry_side, "SELL")
        self.assertEqual(position.entry_quantity, Decimal("5"))


class ClosedTradeLifecycleTests(unittest.TestCase):
    @staticmethod
    def _dataset_ref():
        return {
            "datasetId": "fixture:closed-trade",
            "datasetVersion": "1.0.0",
            "contentDigest": "a" * 64,
            "sourceRef": "fixture:synthetic",
            "venueScope": ["synthetic"],
            "instrumentScope": ["BTC-USDT"],
            "temporalCoverage": {"from": "2026-09-30T00:00:00Z", "to": "2026-09-30T00:03:00Z"},
            "resolution": "1m",
            "schemaVersion": "market-observation-candle-v1",
            "qualityStatus": "verified",
        }

    @classmethod
    def _experiment(cls):
        dataset = cls._dataset_ref()
        return ExperimentDefinition(
            experiment_id="exp:closed-trade",
            experiment_revision="1.0.0",
            strategy_revision_id="strategy:closed-trade-v1",
            strategy_revision_digest="b" * 64,
            dataset_refs=(dataset,),
            feature_definitions=({"featureId": "close", "featureVersion": "1.0.0", "schema": "scalar"},),
            parameter_space={},
            execution_assumptions={"profile": "test"},
            validation_methodology={"methodology_ref": "test"},
            seed_policy={"policy": "deterministic"},
            runtime_provenance={"test": "closed-trade"},
        )

    @staticmethod
    def _states():
        return tuple(
            {
                "marketStateId": f"state:{index}",
                "instrument": "BTC-USDT",
                "venue": "synthetic",
                "marketTime": f"2026-09-30T00:0{index}:00.000Z",
                "observedAt": f"2026-09-30T00:0{index}:00.000Z",
                "validityContext": "historical",
                "marketPrice": price,
                "features": (),
            }
            for index, price in enumerate(("100", "100", "110", "110"))
        )

    def test_engine_notifies_once_after_fee_aware_final_economics_before_next_decision(self):
        class Strategy:
            def __init__(self):
                self.closed = []
                self.decisions_after_close = 0

            def decide(self, state, position_quantity):
                index = int(state["marketStateId"].split(":")[-1])
                if self.closed:
                    self.decisions_after_close += 1
                if index == 0:
                    return SimulatedDecision("ENTER", "BUY", Decimal("1"), "enter")
                if index == 1 and position_quantity > 0:
                    return SimulatedDecision("EXIT", "SELL", Decimal("1"), "exit")
                return SimulatedDecision.no_action(f"no-action:{index}")

            def on_trade_closed(self, closed_trade):
                self.closed.append(closed_trade)

        experiment = self._experiment()
        strategy = Strategy()
        result = SimulationEngine().run(
            experiment,
            experiment.to_reference(),
            self._dataset_ref(),
            self._states(),
            {"revisionId": "strategy:closed-trade-v1"},
            strategy,
            initial_capital=Decimal("1000"),
        )

        self.assertEqual(len(strategy.closed), 1)
        closed = strategy.closed[0]
        self.assertEqual(closed.gross_pnl, Decimal("10"))
        self.assertEqual(closed.fees, Decimal("0"))
        self.assertEqual(closed.net_pnl, Decimal("10"))
        self.assertEqual(result.metrics["closedTradeCount"], "1")
        self.assertEqual(result.closed_trades[0]["netPnl"], "10")
        self.assertGreaterEqual(strategy.decisions_after_close, 1)

    def test_closed_trade_result_includes_entry_and_exit_fees_exactly_once(self):
        position = _Position()
        cash = [Decimal("1000")]
        SimulationEngine._apply_fill(_fill("entry", "BUY", "2", "100", "0.1", "0.2"), position, cash)
        closed = SimulationEngine._apply_fill(_fill("exit", "SELL", "2", "105", "0.2", "0.4"), position, cash)

        self.assertEqual(closed.gross_pnl, Decimal("10"))
        self.assertEqual(closed.fees, Decimal("0.3"))
        self.assertEqual(closed.slippage_cost, Decimal("0.6"))
        self.assertEqual(closed.net_pnl, Decimal("9.7"))


if __name__ == "__main__":
    unittest.main()
