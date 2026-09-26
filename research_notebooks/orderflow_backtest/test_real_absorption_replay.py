from decimal import Decimal

import pytest

from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1
from orderflow_backtest.real_absorption_replay import AbsorptionReplay, ReplayConfig, aggregate_trades


def frame(ts: int, price: str, buy: str = "10", sell: str = "1") -> OrderFlowFrameV1:
    return OrderFlowFrameV1(ts, Decimal(price), Decimal(buy), Decimal(sell), Decimal("0.2"), Decimal("1"))


def test_is_boundary_rejects_oos_before_signal_generation(tmp_path):
    config = ReplayConfig(run_id="boundary", is_start_ms=0, is_end_ms=1_000)
    replay = AbsorptionReplay(config)
    with pytest.raises(ValueError, match="OOS frame rejected"):
        replay.run([frame(0, "100"), frame(1_001, "101")], tmp_path)


def test_ledger_persistence_precedes_aggregation_and_is_deterministic(tmp_path):
    config = ReplayConfig(run_id="stable", is_start_ms=0, is_end_ms=10_000)
    frames = [frame(i * 1_000, "100") for i in range(8)]
    first = AbsorptionReplay(config).run(frames, tmp_path / "one")
    second = AbsorptionReplay(config).run(frames, tmp_path / "two")
    assert first["hashes"] == second["hashes"]
    assert (tmp_path / "one" / "signal_ledger.json").exists()
    assert (tmp_path / "one" / "order_ledger.json").exists()
    assert (tmp_path / "one" / "fill_ledger.json").exists()
    assert (tmp_path / "one" / "trade_ledger.json").exists()
    assert (tmp_path / "one" / "aggregate.json").exists()


def test_aggregate_is_derived_from_trade_rows():
    from orderflow_backtest.real_absorption_replay import TradeRecord

    trade = TradeRecord("t", "r", "s", "rev", "d", "v", "BTCUSDT", 0, 0, 0, 1_000,
                        "LONG", "1", "100", "100", "101", "MAKER", "MAKER", "1", "0.02", "0.02", "0", "0", "0.96", "signal", "exit")
    result = aggregate_trades([trade])
    assert result["trade_count"] == 1
    assert result["net_pnl"] == "0.96"
    assert result["daily_net_pnl"] == {"1970-01-01": "0.96"}


def test_single_position_stop_and_funding_from_ledger(tmp_path, monkeypatch):
    import json
    import orderflow_backtest.real_absorption_replay as replay_module
    from orderflow_backtest.orderflow_contracts import signal
    monkeypatch.setattr(replay_module, 'absorption_observe', lambda *args: signal('LONG', 'fixture'))
    config = ReplayConfig(run_id='economics', is_start_ms=0, is_end_ms=3000,
                          funding_events=((1500, Decimal('0.0001')),))
    frames = [frame(0, '100'), frame(1000, '100'), frame(2000, '99'), frame(3000, '99')]
    result = AbsorptionReplay(config).run(frames, tmp_path)
    trades = json.loads((tmp_path/'trade_ledger.json').read_text())
    assert len(trades) == 1
    assert trades[0]['exit_reason'] == 'STOP'
    assert Decimal(trades[0]['funding_pnl']) == Decimal('-0.1000')
    assert Decimal(result['aggregate']['net_pnl']) == Decimal('-10.5')
    assert result['manifest']['executionModelLimitation'] == 'MAKER_QUEUE_NOT_HISTORICALLY_PROVEN'
    assert sum(map(Decimal, result['aggregate']['daily_net_pnl'].values())) == Decimal('-10.5')
