from decimal import Decimal
import pytest
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from orderflow_backtest.canonical_strategy_replay import CanonicalStrategyReplay, STRATEGY_MAP
from orderflow_backtest.real_absorption_replay import ReplayConfig

def frame(ts: int, price: str, buy: str = "10", sell: str = "1") -> OrderFlowFrameV1:
    return OrderFlowFrameV1(ts, Decimal(price), Decimal(buy), Decimal(sell), Decimal("0.2"), Decimal("1"))

def test_canonical_momentum_replay_rejects_oos(tmp_path):
    config = ReplayConfig(run_id="mom-boundary", strategy_id="orderflow.momentum.aggression", is_start_ms=0, is_end_ms=1000)
    replay = CanonicalStrategyReplay(config)
    with pytest.raises(ValueError, match="OOS frame rejected"):
        replay.run([frame(0, "100"), frame(1001, "101")], tmp_path)

def test_canonical_divergence_replay_rejects_oos(tmp_path):
    config = ReplayConfig(run_id="div-boundary", strategy_id="orderflow.cvd.divergence.reversal", is_start_ms=0, is_end_ms=1000)
    replay = CanonicalStrategyReplay(config)
    with pytest.raises(ValueError, match="OOS frame rejected"):
        replay.run([frame(0, "100"), frame(1001, "101")], tmp_path)

def test_canonical_strategy_replay_determinism_and_ledgers(tmp_path):
    config = ReplayConfig(run_id="mom-stable", strategy_id="orderflow.momentum.aggression", is_start_ms=0, is_end_ms=10000)
    frames = [frame(i * 1000, "100") for i in range(8)]
    replay = CanonicalStrategyReplay(config)
    r1 = replay.run(frames, tmp_path / "run-1")
    r2 = replay.run(frames, tmp_path / "run-2")
    assert r1["hashes"] == r2["hashes"]
    assert (tmp_path / "run-1" / "signal_ledger.json").exists()
    assert (tmp_path / "run-1" / "order_ledger.json").exists()
    assert (tmp_path / "run-1" / "fill_ledger.json").exists()
    assert (tmp_path / "run-1" / "trade_ledger.json").exists()
    assert (tmp_path / "run-1" / "aggregate.json").exists()
    assert (tmp_path / "run-1" / "run_manifest.json").exists()
