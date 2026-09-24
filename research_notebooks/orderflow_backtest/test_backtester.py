"""Validation package tests for causal synthetic-only orderflow harness."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from orderflow_backtest.data_fixture import MarketTick, generate_synthetic_ticks
from orderflow_backtest.engine import BacktestConfig, BacktestRunner, FeeModel, run_backtest, write_artifacts
from orderflow_backtest.strategies import AggressionMomentumScalper, CVDDivergenceReversal, InstitutionalAbsorptionFade, SignalType

ROOT = Path(__file__).resolve().parent

def tick(i, **kw):
    d = dict(timestamp=1704067200.0+i*60, open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0, buy_volume=50.0, sell_volume=50.0, delta=0.0, cvd=0.0, bid_depth=1000.0, ask_depth=1000.0, bid_skew=0.0, vwap=100.0, order_imbalance=0.0, absorption_score=0.0)
    d.update(kw)
    return MarketTick(**d)

def test_fee_schedule_and_taker_slippage():
    assert FeeModel.for_pair("BTCUSDT").taker_fee_rate == 0.0005
    assert FeeModel.for_pair("ETHUSDC").maker_fee_rate == 0.0
    runner = BacktestRunner(BacktestConfig(pair="BTCUSDT", slippage_bps=2.0))
    from orderflow_backtest.strategies import OrderType
    assert pytest.approx(runner._apply_slippage(100.0, OrderType.TAKER, SignalType.BUY)) == 100.02
    assert pytest.approx(runner._apply_slippage(100.0, OrderType.TAKER, SignalType.SELL)) == 99.98
    assert runner._apply_slippage(100.0, OrderType.MAKER, SignalType.BUY) == 100.0

def test_momentum_freeze_standardized_delta_obi_spread_breakout():
    hist = [tick(i, delta=float(i % 5), high=100+i*0.01, low=99-i*0.01, close=100.0, vwap=100.0) for i in range(20)]
    bars = hist + [tick(20, delta=30.0, order_imbalance=0.60, close=102.0, high=102.0, vwap=101.99)]
    sig = AggressionMomentumScalper().generate_signal(bars, 20)
    assert sig is not None
    assert sig.signal_type == SignalType.BUY
    assert "standardized delta" in sig.reason

def test_absorption_requires_two_interval_confirmation_and_no_proof_label():
    s = InstitutionalAbsorptionFade()
    bars = [tick(i) for i in range(3)] + [tick(3, absorption_score=3.0, order_imbalance=-0.4, bid_skew=0.5)]
    assert s.generate_signal(bars, 3) is None
    bars.append(tick(4, absorption_score=3.0, order_imbalance=-0.4, bid_skew=0.5))
    sig = s.generate_signal(bars, 4)
    assert sig.signal_type == SignalType.BUY
    assert "institution" not in sig.reason.lower()

def test_divergence_uses_prior_only_trailing_bounds_and_thresholds():
    bars = [tick(i, low=100.0, high=101.0, cvd=-100.0, delta=1.0) for i in range(15)]
    bars[5] = tick(5, low=99.0, high=101.0, cvd=-200.0, delta=-1.0)
    bars.append(tick(15, low=98.0, high=100.0, cvd=-50.0, delta=1.0))
    sig = CVDDivergenceReversal().generate_signal(bars, 15)
    assert sig is not None
    assert sig.signal_type == SignalType.BUY
    assert "Bullish CVD Divergence" in sig.reason

def test_engine_enters_next_bar_and_stop_first_collision():
    class OneSignal(AggressionMomentumScalper):
        def generate_signal(self, ticks, idx):
            return super().generate_signal(ticks, idx) if idx == 20 else None
    bars = [tick(i, delta=float(i % 5), high=100+i*0.01, low=99-i*0.01, close=100.0, vwap=100.0) for i in range(20)]
    bars += [tick(20, delta=30.0, order_imbalance=.6, close=102.0, high=102.0, vwap=101.99), tick(21, open=100.0, high=101.0, low=99.0, close=100.0), tick(22, high=101.0, low=99.0, close=100.0)]
    rep = BacktestRunner(BacktestConfig(days=1)).evaluate_strategy(OneSignal(), bars)
    assert rep.total_trades == 1
    tr = rep.trades[0]
    assert tr.entry_time == bars[21].timestamp
    assert tr.exit_reason.startswith("Stop Loss Hit")

def test_synthetic_run_disposition_and_required_metrics():
    r = run_backtest(pair="BTCUSDT", days=1, strategy_key="momentum", data_mode="synthetic")
    rep = next(iter(r.reports.values()))
    d = rep.to_dict()
    for k in ["total_trades", "long_trades", "short_trades", "gross_pnl", "net_pnl", "median_trade_pnl", "avg_winner", "avg_loser", "profit_factor", "expectancy", "max_drawdown_pct", "max_drawdown_duration_trades", "sharpe_ratio", "sortino_ratio", "exposure_time_pct", "turnover", "total_fees_paid", "total_slippage_cost", "largest_winner", "largest_loser", "consecutive_wins", "consecutive_losses"]:
        assert k in d
    assert d["validation_label"] == "ENGINE_VALIDATION_SYNTHETIC"
    assert d["promotion_allowed"] is False
    assert d["disposition"] == "DATASET_INSUFFICIENT / RESEARCH_CONTINUE"
    assert d["funding_note"] == "NOT_MODELED"

def test_artifact_writer_outputs_manifest_ledger_and_costs(tmp_path):
    r = run_backtest(pair="ETHUSDC", days=1, strategy_key="absorption", data_mode="synthetic")
    rid = write_artifacts(r, tmp_path, {"data_mode":"synthetic", "promotion_allowed": False})
    assert len(rid) == 16
    for name in ["manifest.json", "report.json", "cost_decomposition.json"]:
        assert (tmp_path / name).exists()
    assert json.loads((tmp_path / "report.json").read_text())["run_id"] == rid

def test_runner_refuses_promotion(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "run_validation.py"), "--pair", "BTCUSDT", "--strategy", "momentum", "--request-promotion", "--output-root", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode != 0
    assert "promotion is refused" in proc.stderr

def test_synthetic_generator_90d_shape_small_sample():
    ticks = generate_synthetic_ticks("BTCUSDT", days=1, interval_seconds=300, seed=123)
    assert len(ticks) == 288
    assert ticks[0].timestamp < ticks[-1].timestamp
