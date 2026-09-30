import unittest
from decimal import Decimal
from typing import Any

from core.quant_optimization.strategy import default_registry
from core.quant_simulation import (
    ClosedTradeResult,
    DeterministicExecutionModel,
    ExecutionAssumptionProfile,
    SimulatedDecision,
    SimulationEngine,
)
from core.quant_strategies.indicators import (
    distance_bps,
    ema,
    step_bps,
    to_decimal,
    wma,
)
from core.quant_strategies.microtrend import (
    BASE_REVISION,
    STRATEGY_ID,
    JevResult,
    MADirection,
    MicrotrendConfig,
    MicrotrendScalperStrategy,
    MicrotrendState,
    source_hash,
)
from core.quant_strategies.microtrend.microtrend_scalper import _timestamp_ms
from core.quant_strategies.microtrend.microtrend_scalper import _direction


def _config(**overrides: Any) -> MicrotrendConfig:
    params = {
        "ma_flat_epsilon_bps": Decimal("0.5"),
        "ema9_entry_proximity_bps": Decimal("5.0"),
        "ema21_entry_proximity_bps": Decimal("8.0"),
        "ema21_pullback_proximity_bps": Decimal("10.0"),
        "wma50_pullback_proximity_bps": Decimal("15.0"),
        "quantity_or_notional": Decimal("1.0"),
        "holding_period_ms": 1_000,
        "jev_observation_window_ms": 60_000,
        "jev_min_observations": 3,
        "jev_min_persistence_ratio": Decimal("0.6"),
        "jev_aggression_threshold": Decimal("0.2"),
        "jev_ofi_threshold": Decimal("0.1"),
    }
    params.update(overrides)
    return MicrotrendConfig(**params)


def _state(
    offset_minutes: int,
    close: str = "100.0",
    *,
    closed: bool = True,
    timeframe: str = "1m",
    snapshots: tuple[dict[str, Any], ...] = (),
    jev_decision: dict[str, Any] | None = None,
    jev_context: dict[str, Any] | None = None,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone, timedelta
    base = datetime(2026, 9, 30, 0, 0, 0, tzinfo=timezone.utc)
    time_iso = (base + timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")
    state: dict[str, Any] = {
        "marketStateId": f"state:{offset_minutes}",
        "marketTime": time_iso,
        "observedAt": time_iso,
        "validityContext": "historical",
        "marketPrice": close,
        "candleClosed": closed,
        "timeframe": timeframe,
        "microstructureSnapshots": snapshots,
    }
    if features is not None:
        state["featureValues"] = features
    if jev_decision is not None:
        state["jevDecision"] = jev_decision
    if jev_context is not None:
        state["jevContext"] = jev_context
    return state


class MicrotrendIndicatorsTests(unittest.TestCase):
    def test_wma_exact_decimal_weights(self):
        prices = [Decimal(str(i)) for i in range(1, 51)]
        val = wma(prices, 50)
        # prices: p_0=1 .. p_49=50
        # weights: w_0=1 .. w_49=50
        # sum( (i+1)*(i+1) for i in 0..49 ) = sum( k^2 for k=1..50 ) = 42925
        # sum( (i+1) for i in 0..49 ) = 1275
        # 42925 / 1275 = 1717 / 51 = 33.66666666666666666666666667
        expected = Decimal("42925") / Decimal("1275")
        self.assertEqual(val, expected)

    def test_wma_warmup_requires_50_candles(self):
        prices = [Decimal("100") for _ in range(49)]
        with self.assertRaises(ValueError):
            wma(prices, 50)
        self.assertEqual(wma(prices + [Decimal("100")], 50), Decimal("100"))

    def test_ema_initialization_and_multiplier(self):
        prices = [Decimal("100") for _ in range(9)]
        self.assertEqual(ema(prices, 9), Decimal("100"))
        # price jump to 110 at step 10:
        # multiplier = 2 / 10 = 0.2
        # current = 110 * 0.2 + 100 * 0.8 = 22 + 80 = 102
        self.assertEqual(ema(prices + [Decimal("110")], 9), Decimal("102"))

    def test_direction_classification(self):
        eps = Decimal("1.0")  # 1 bp
        p1 = Decimal("10000")
        p2_up = Decimal("10002")  # +2 bps
        p2_flat = Decimal("10000.5")  # +0.5 bps
        p2_down = Decimal("9998")  # -2 bps

        self.assertGreater(step_bps(p2_up, p1), eps)
        self.assertLess(step_bps(p2_down, p1), -eps)
        self.assertTrue(abs(step_bps(p2_flat, p1)) <= eps)

    def test_direction_epsilon_boundary_is_inclusive(self):
        previous = Decimal("10000")
        self.assertEqual(_direction(Decimal("10001"), previous, Decimal("1")), MADirection.UP)
        self.assertEqual(_direction(Decimal("9999"), previous, Decimal("1")), MADirection.DOWN)

    def test_zero_direction_epsilon_is_rejected_to_keep_direction_states_disjoint(self):
        with self.assertRaisesRegex(ValueError, "ma_flat_epsilon_bps must be positive"):
            _config(ma_flat_epsilon_bps=Decimal("0"))


class MicrotrendScalperStateTests(unittest.TestCase):
    def test_warmup_requires_50_closed_candles(self):
        strategy = MicrotrendScalperStrategy(_config())
        for i in range(49):
            dec = strategy.decide(_state(i, "100.0"), Decimal("0"))
            self.assertEqual(dec.action, "NO_ACTION")
            self.assertEqual(strategy.state, MicrotrendState.UNARMED)

    def test_three_candle_arming_long(self):
        strategy = MicrotrendScalperStrategy(_config())
        # First 50 flat
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.UNARMED)

        # 3 sharply rising candles
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            dec = strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)
        self.assertEqual(dec.action, "NO_ACTION")

    def test_three_candle_arming_short(self):
        strategy = MicrotrendScalperStrategy(_config())
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.UNARMED)

        for i, p in enumerate(["95.0", "88.0", "80.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_SHORT)

    def test_entry_proximity_and_single_position(self):
        # Configure wide proximity so a close near EMA9 qualifies
        strategy = MicrotrendScalperStrategy(_config(ema9_entry_proximity_bps=Decimal("1500.0")))
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

        # Next candle close near EMA9 -> triggers entry
        dec = strategy.decide(_state(53, "121.0"), Decimal("0"))
        self.assertEqual(dec.action, "ENTER")
        self.assertEqual(dec.side, "BUY")

        # In position: no new entry
        dec_in_pos = strategy.decide(_state(54, "122.0"), Decimal("1.0"))
        self.assertEqual(dec_in_pos.action, "NO_ACTION")

    def test_ema9_weakening_and_realignment(self):
        strategy = MicrotrendScalperStrategy(_config())
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

        # Step opposing the LONG regime: price drop from 120 to 100 turns EMA9 down
        strategy.decide(_state(53, "100.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.WEAKENING_LONG)

        # Immediate bounce -> ARMED_LONG
        strategy.decide(_state(54, "135.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

    def test_structural_invalidation_disarms(self):
        strategy = MicrotrendScalperStrategy(_config())
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

        # Structural invalidation is immediate. Later data may independently
        # qualify a fresh SHORT regime, so assert at the invalidating candle.
        strategy.decide(_state(53, "50.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.UNARMED)



    def test_entry_proximity_rejects_when_close_is_outside_even_if_extremum_touches(self):
        strategy = MicrotrendScalperStrategy(_config(
            ema9_entry_proximity_bps=Decimal("5.0"),
            ema21_entry_proximity_bps=Decimal("5.0"),
        ))
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

        # Close is 135.0, far above EMA9/EMA21. A wick/high of 120 touches EMA9,
        # but completed close must govern entry proximity.
        state = _state(53, "135.0", features={"high": "120.0", "low": "110.0"})
        dec = strategy.decide(state, Decimal("0"))
        self.assertEqual(dec.action, "NO_ACTION")

    def test_ema9_second_opposing_step_triggers_pullback_wait(self):
        strategy = MicrotrendScalperStrategy(_config())
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)

        # First opposing step -> WEAKENING_LONG
        strategy.decide(_state(53, "100.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.WEAKENING_LONG)

        # Second opposing step -> PULLBACK_WAIT_LONG
        strategy.decide(_state(54, "95.0"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.PULLBACK_WAIT_LONG)

    def test_holding_period_exit_uses_simulation_event_time(self):
        strategy = MicrotrendScalperStrategy(
            _config(holding_period_ms=60_000, ema9_entry_proximity_bps=Decimal("1500.0"))
        )
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.decide(_state(53, "121.0"), Decimal("0")).action, "ENTER")

        # The simulated fill is acknowledged at the next market state. Holding
        # time starts from that execution-state timestamp, then exits at 60s.
        self.assertEqual(strategy.decide(_state(54, "121.0"), Decimal("1.0")).action, "NO_ACTION")
        exit_decision = strategy.decide(_state(55, "121.0"), Decimal("1.0"))
        self.assertEqual(exit_decision.action, "EXIT")
        self.assertTrue(exit_decision.decision_id.startswith("exit:holding:"))

    def test_segment_boundary_flattens_open_position(self):
        strategy = MicrotrendScalperStrategy(
            _config(ema9_entry_proximity_bps=Decimal("1500.0")), segment_length=56
        )
        for i in range(50):
            strategy.decide(_state(i, "100.0"), Decimal("0"))
        for i, p in enumerate(["105.0", "112.0", "120.0"], start=50):
            strategy.decide(_state(i, p), Decimal("0"))
        self.assertEqual(strategy.decide(_state(53, "121.0"), Decimal("0")).action, "ENTER")

        exit_decision = strategy.decide(_state(54, "121.0"), Decimal("1.0"))
        self.assertEqual(exit_decision.action, "EXIT")
        self.assertTrue(exit_decision.decision_id.startswith("exit:segment-boundary:"))


class MicrotrendJevAndCooldownTests(unittest.TestCase):
    def test_cooldown_activated_on_two_consecutive_losses(self):
        strategy = MicrotrendScalperStrategy(_config())
        trade1 = ClosedTradeResult(
            trade_id="t1", side="BUY", quantity=Decimal("1"),
            entry_timestamp="2026-09-30T00:00:00Z", exit_timestamp="2026-09-30T00:05:00Z",
            entry_price=Decimal("100"), exit_price=Decimal("95"),
            gross_pnl=Decimal("-5"), fees=Decimal("0.1"), slippage_cost=Decimal("0"), net_pnl=Decimal("-5.1"),
        )
        strategy.on_trade_closed(trade1)
        self.assertEqual(strategy.consecutive_losses, 1)
        self.assertFalse(strategy.cooldown_active)

        # Break-even trade preserves streak
        trade_be = ClosedTradeResult(
            trade_id="t_be", side="BUY", quantity=Decimal("1"),
            entry_timestamp="2026-09-30T00:06:00Z", exit_timestamp="2026-09-30T00:07:00Z",
            entry_price=Decimal("100"), exit_price=Decimal("100.1"),
            gross_pnl=Decimal("0.1"), fees=Decimal("0.1"), slippage_cost=Decimal("0"), net_pnl=Decimal("0.0"),
        )
        strategy.on_trade_closed(trade_be)
        self.assertEqual(strategy.consecutive_losses, 1)
        self.assertFalse(strategy.cooldown_active)

        # Second loss triggers 15m cooldown
        trade2 = ClosedTradeResult(
            trade_id="t2", side="BUY", quantity=Decimal("1"),
            entry_timestamp="2026-09-30T00:08:00Z", exit_timestamp="2026-09-30T00:10:00Z",
            entry_price=Decimal("100"), exit_price=Decimal("98"),
            gross_pnl=Decimal("-2"), fees=Decimal("0.1"), slippage_cost=Decimal("0"), net_pnl=Decimal("-2.1"),
        )
        strategy.on_trade_closed(trade2)
        self.assertEqual(strategy.consecutive_losses, 2)
        self.assertTrue(strategy.cooldown_active)

        # Check that cooldown blocks entry
        now_ms = _timestamp_ms("2026-09-30T00:10:00Z") + 5 * 60_000  # 5 minutes in
        self.assertTrue(strategy._cooldown_is_active(now_ms))

        # Check that expiry clears cooldown
        now_ms_after = _timestamp_ms("2026-09-30T00:10:00Z") + 15 * 60_000 + 1000  # 15m + 1s
        self.assertFalse(strategy._cooldown_is_active(now_ms_after))

    def test_win_resets_loss_streak(self):
        strategy = MicrotrendScalperStrategy(_config())
        loss = ClosedTradeResult(
            trade_id="t1", side="BUY", quantity=Decimal("1"),
            entry_timestamp="2026-09-30T00:00:00Z", exit_timestamp="2026-09-30T00:05:00Z",
            entry_price=Decimal("100"), exit_price=Decimal("95"),
            gross_pnl=Decimal("-5"), fees=Decimal("0.1"), slippage_cost=Decimal("0"), net_pnl=Decimal("-5.1"),
        )
        win = ClosedTradeResult(
            trade_id="t2", side="BUY", quantity=Decimal("1"),
            entry_timestamp="2026-09-30T00:06:00Z", exit_timestamp="2026-09-30T00:08:00Z",
            entry_price=Decimal("100"), exit_price=Decimal("105"),
            gross_pnl=Decimal("5"), fees=Decimal("0.1"), slippage_cost=Decimal("0"), net_pnl=Decimal("4.9"),
        )
        strategy.on_trade_closed(loss)
        self.assertEqual(strategy.consecutive_losses, 1)
        strategy.on_trade_closed(win)
        self.assertEqual(strategy.consecutive_losses, 0)
        self.assertFalse(strategy.cooldown_active)

    def test_registry_resolution(self):
        reg = default_registry()
        self.assertIn((STRATEGY_ID, BASE_REVISION), reg)
        defn = reg[(STRATEGY_ID, BASE_REVISION)]
        self.assertEqual(defn.strategy_id, STRATEGY_ID)
        self.assertEqual(defn.base_revision, BASE_REVISION)
        self.assertEqual(defn.implementation_hash, source_hash())


class MicrotrendJevContractTests(unittest.TestCase):
    @staticmethod
    def _jev_context():
        return {
            "run_id": "run:microtrend",
            "cell_id": "cell:microtrend",
            "symbol": "BTCUSDT",
            "venue": "Binance USD-M Futures",
            "market_type": "USD-M Futures",
            "deployment_candidate_id": "deployment:microtrend",
            "correlation_id": "correlation:microtrend",
        }

    def _armed(self, **overrides):
        strategy = MicrotrendScalperStrategy(_config(**overrides))
        for i in range(50):
            strategy.decide(_state(i, "100"), Decimal("0"))
        for i, close in enumerate(("105", "112", "120"), start=50):
            strategy.decide(_state(i, close), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.ARMED_LONG)
        return strategy

    @staticmethod
    def _snapshots(*, timestamp="2026-09-30T00:55:00Z", values=None):
        features = {
            "book_validity": "VALID",
            "microprice_valid": True,
            "signed_queue_imbalance_L1": "0.5",
            "normalized_OFI_L5": "0.5",
            "normalized_weighted_pressure": "0.5",
            "aggression_imbalance": "0.5",
            "signed_volume_velocity": "0.5",
            "buy_absorption_score": "0.1",
            "sell_absorption_score": "0",
            "buy_sweep_score": "0.2",
            "sell_sweep_score": "0",
        }
        if values:
            features.update(values)
        end = _timestamp_ms(timestamp)
        return tuple({"timestamp": end - offset * 1000, **features} for offset in (2, 1, 0))

    @staticmethod
    def _jev_decision(candidate_id, outcome, decided_at="2026-09-30T00:56:00Z"):
        values = {
            "CONTINUATION": ("SIGNAL_QUALIFIED", "ENTRY", "BUY", "MICROTREND_CONTINUATION"),
            "NEUTRAL": ("NO_ACTION", "NONE", "NONE", "MICROTREND_NEUTRAL"),
            "REVERSAL_RISK": ("SIGNAL_REJECTED", "NONE", "NONE", "MICROTREND_REVERSAL_RISK"),
            "INVALID": ("SIGNAL_REJECTED", "NONE", "NONE", "MICROTREND_INVALID"),
        }
        decision, action, side, reason = values[outcome]
        return {
            "schema_version": "v1",
            "decision_id": f"decision:{candidate_id}",
            "run_id": "run:microtrend",
            "cell_id": "cell:microtrend",
            "candidate_trigger_id": candidate_id,
            "correlation_id": "correlation:microtrend",
            "strategy_id": STRATEGY_ID,
            "strategy_revision": BASE_REVISION,
            "decision": decision,
            "action": action,
            "side": side,
            "reason_codes": [reason],
            "decided_at_ms": _timestamp_ms(decided_at),
        }

    def test_jev_decision_requires_exact_schema_and_request_identity(self):
        mismatches = (
            {"schema_version": "v2"},
            {"decision_id": ""},
            {"run_id": "another-run"},
            {"cell_id": "another-cell"},
            {"correlation_id": "another-correlation"},
            {"strategy_id": "another-strategy"},
            {"strategy_revision": "another-revision"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                strategy = self._armed(
                    ema21_pullback_proximity_bps=Decimal("5000"),
                    wma50_pullback_proximity_bps=Decimal("5000"),
                )
                self._enter_pullback(strategy)
                request = strategy.pending_jev_request
                response = self._jev_decision(request["candidate_trigger_id"], "CONTINUATION")
                response.update(mismatch)

                result = strategy.decide(_state(56, "95", jev_decision=response), Decimal("0"))

                self.assertEqual(strategy.last_jev_result, JevResult.INVALID)
                self.assertEqual(strategy.state, MicrotrendState.PULLBACK_WAIT_LONG)
                self.assertEqual(result.action, "NO_ACTION")
                self.assertIsNone(strategy.pending_jev_request)

    def _enter_pullback(self, strategy, snapshots=None, jev_context=None):
        strategy.decide(_state(53, "100"), Decimal("0"))
        strategy.decide(_state(54, "95"), Decimal("0"))
        self.assertEqual(strategy.state, MicrotrendState.PULLBACK_WAIT_LONG)
        # The contract triggers Jev only on a causal crossing from outside to
        # inside a pullback band. These focused tests isolate the band-entry
        # transition after the divergence path was established above.
        strategy._zone_entered = False
        strategy.decide(
            _state(
                55,
                "95",
                snapshots=snapshots or self._snapshots(),
                jev_context=self._jev_context() if jev_context is None else jev_context,
            ),
            Decimal("0"),
        )

    def test_jev_continuation_reconfirms_then_allows_directional_entry(self):
        strategy = self._armed(
            ema21_pullback_proximity_bps=Decimal("5000"),
            wma50_pullback_proximity_bps=Decimal("5000"),
        )
        self._enter_pullback(strategy)
        request = strategy.pending_jev_request
        self.assertIsNotNone(request)
        self.assertEqual(request["run_id"], "run:microtrend")
        self.assertEqual(request["cell_id"], "cell:microtrend")
        self.assertEqual(request["symbol"], "BTCUSDT")
        self.assertEqual(request["event_time"], _timestamp_ms("2026-09-30T00:55:00Z") / 1000)
        expected_features = (
            "signed_queue_imbalance_L1",
            "normalized_OFI_L5",
            "normalized_weighted_pressure",
            "aggression_imbalance",
            "signed_volume_velocity",
            "buy_absorption_score",
            "sell_absorption_score",
            "buy_sweep_score",
            "sell_sweep_score",
        )
        expected_observations = [
            {
                "timestamp": _timestamp_ms(snapshot["timestamp"]),
                "features": {name: str(snapshot[name]) for name in expected_features},
            }
            for snapshot in sorted(self._snapshots(), key=lambda item: _timestamp_ms(item["timestamp"]))
        ]
        self.assertEqual(request["feature_snapshot"]["observations"], expected_observations)
        self.assertEqual(request["deployment_candidate_id"], "deployment:microtrend")
        result = strategy.decide(
            _state(56, "95", jev_decision=self._jev_decision(request["candidate_trigger_id"], "CONTINUATION")),
            Decimal("0"),
        )
        self.assertEqual(strategy.last_jev_result, JevResult.CONTINUATION)
        self.assertEqual(strategy.state, MicrotrendState.RECONFIRMED_LONG)
        # A validated decision is consumed on the current causal candle and may
        # immediately produce the entry candidate; execution latency remains
        # owned by SimulationEngine.
        self.assertEqual((result.action, result.side), ("ENTER", "BUY"))

    def test_jev_neutral_and_reversal_risk_never_create_orders(self):
        for outcome, expected_state in (
            ("NEUTRAL", MicrotrendState.PULLBACK_WAIT_LONG),
            ("REVERSAL_RISK", MicrotrendState.REVERSAL_RISK_LONG),
        ):
            with self.subTest(outcome=outcome):
                strategy = self._armed(
                    ema21_pullback_proximity_bps=Decimal("5000"),
                    wma50_pullback_proximity_bps=Decimal("5000"),
                )
                snapshots = self._snapshots(values={
                    "signed_queue_imbalance_L1": "-0.8" if outcome == "REVERSAL_RISK" else "0",
                    "normalized_OFI_L5": "-0.8" if outcome == "REVERSAL_RISK" else "0",
                    "normalized_weighted_pressure": "-0.8" if outcome == "REVERSAL_RISK" else "0",
                    "aggression_imbalance": "-0.8" if outcome == "REVERSAL_RISK" else "0",
                    "signed_volume_velocity": "-0.8" if outcome == "REVERSAL_RISK" else "0",
                    "sell_absorption_score": "0.5" if outcome == "REVERSAL_RISK" else "0",
                    "sell_sweep_score": "0.8" if outcome == "REVERSAL_RISK" else "0",
                })
                self._enter_pullback(strategy, snapshots)
                request = strategy.pending_jev_request
                self.assertIsNotNone(request)
                result = strategy.decide(
                    _state(56, "95", jev_decision=self._jev_decision(request["candidate_trigger_id"], outcome)),
                    Decimal("0"),
                )
                self.assertEqual(strategy.last_jev_result, JevResult[outcome])
                self.assertEqual(strategy.state, expected_state)
                self.assertEqual(result.action, "NO_ACTION")
                self.assertEqual(strategy.decide(_state(57, "95"), Decimal("0")).action, "NO_ACTION")

    def test_invalid_stale_gap_and_insufficient_jev_observations_fail_closed(self):
        invalid_sets = (
            self._snapshots(values={"stale_state": True}),
            self._snapshots(values={"sequence_gap": True}),
            self._snapshots()[:2],
        )
        for snapshots in invalid_sets:
            with self.subTest(snapshots=snapshots):
                strategy = self._armed(
                    ema21_pullback_proximity_bps=Decimal("5000"),
                    wma50_pullback_proximity_bps=Decimal("5000"),
                )
                self._enter_pullback(strategy, snapshots)
                self.assertEqual(strategy.last_jev_result, JevResult.INVALID)
                self.assertIsNone(strategy.pending_jev_request)
                self.assertEqual(strategy.decide(_state(56, "95"), Decimal("0")).action, "NO_ACTION")

    def test_future_jev_snapshot_is_rejected(self):
        strategy = self._armed(
            ema21_pullback_proximity_bps=Decimal("5000"),
            wma50_pullback_proximity_bps=Decimal("5000"),
        )
        future = self._snapshots(timestamp="2026-09-30T00:56:00Z")
        self._enter_pullback(strategy, future)
        self.assertEqual(strategy.last_jev_result, JevResult.INVALID)
        self.assertIsNone(strategy.pending_jev_request)

    def test_ema21_zone_has_priority_when_both_pullback_bands_match(self):
        strategy = self._armed(
            ema21_pullback_proximity_bps=Decimal("5000"),
            wma50_pullback_proximity_bps=Decimal("5000"),
        )
        self._enter_pullback(strategy)
        self.assertEqual(strategy._zone_reference, "EMA21")
        request = strategy.pending_jev_request
        strategy.decide(_state(56, "95", snapshots=self._snapshots()), Decimal("0"))
        self.assertEqual(strategy.pending_jev_request, request)

    def test_missing_jev_context_fails_closed_before_external_dispatch(self):
        strategy = self._armed(
            ema21_pullback_proximity_bps=Decimal("5000"),
            wma50_pullback_proximity_bps=Decimal("5000"),
        )
        self._enter_pullback(strategy, jev_context={})
        self.assertEqual(strategy.last_jev_result, JevResult.INVALID)
        self.assertIsNone(strategy.pending_jev_request)


class MicrotrendCooldownBoundaryTests(unittest.TestCase):
    def test_cooldown_resets_on_new_segment_and_preserves_open_position_exit(self):
        strategy = MicrotrendScalperStrategy(_config(holding_period_ms=1000))
        for i in range(50):
            strategy.decide(_state(i, "100"), Decimal("0"))
        trades = (
            ClosedTradeResult("loss-1", "BUY", Decimal("1"), "2026-09-30T00:00:00Z", "2026-09-30T00:05:00Z",
                              Decimal("100"), Decimal("95"), Decimal("-5"), Decimal("0.1"), Decimal("0"), Decimal("-5.1")),
            ClosedTradeResult("loss-2", "BUY", Decimal("1"), "2026-09-30T00:06:00Z", "2026-09-30T00:08:00Z",
                              Decimal("100"), Decimal("95"), Decimal("-5"), Decimal("0.1"), Decimal("0"), Decimal("-5.1")),
        )
        for trade in trades:
            strategy.on_trade_closed(trade)
        self.assertEqual(strategy.cooldown_until_ms, _timestamp_ms(trades[-1].exit_timestamp) + 15 * 60_000)
        strategy._opened_at_ms = _timestamp_ms("2026-09-30T00:48:00Z")
        self.assertEqual(strategy.decide(_state(50, "100"), Decimal("1")).action, "EXIT")
        strategy.reset_segment()
        self.assertEqual(strategy.consecutive_losses, 0)
        self.assertIsNone(strategy.cooldown_until_ms)

    def test_flat_net_trade_preserves_loss_streak(self):
        strategy = MicrotrendScalperStrategy(_config())
        loss = ClosedTradeResult("loss", "BUY", Decimal("1"), "2026-09-30T00:00:00Z", "2026-09-30T00:05:00Z",
                                 Decimal("100"), Decimal("95"), Decimal("-5"), Decimal("5"), Decimal("0"), Decimal("-10"))
        breakeven = ClosedTradeResult("flat", "BUY", Decimal("1"), "2026-09-30T00:06:00Z", "2026-09-30T00:07:00Z",
                                      Decimal("100"), Decimal("100"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        strategy.on_trade_closed(loss)
        strategy.on_trade_closed(breakeven)
        self.assertEqual(strategy.consecutive_losses, 1)

    def test_duplicate_closed_trade_feedback_is_rejected(self):
        strategy = MicrotrendScalperStrategy(_config())
        trade = ClosedTradeResult("same", "BUY", Decimal("1"), "2026-09-30T00:00:00Z", "2026-09-30T00:01:00Z",
                                  Decimal("100"), Decimal("99"), Decimal("-1"), Decimal("0"), Decimal("0"), Decimal("-1"))
        strategy.on_trade_closed(trade)
        with self.assertRaisesRegex(ValueError, "duplicate closed-trade notification"):
            strategy.on_trade_closed(trade)


if __name__ == "__main__":
    unittest.main()
