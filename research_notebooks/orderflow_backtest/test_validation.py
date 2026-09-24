from orderflow_backtest.data_fixture import MarketTick
from orderflow_backtest.engine import run_backtest
from orderflow_backtest.strategies import AggressionMomentumScalper, InstitutionalAbsorptionFade, SignalType

def tick(i, **kw):
    d=dict(timestamp=i*60.,open=100.,high=101.,low=99.,close=100.,volume=10.,buy_volume=5.,sell_volume=5.,delta=0.,cvd=0.,bid_depth=10.,ask_depth=10.,bid_skew=0.,vwap=100.,order_imbalance=0.,absorption_score=0.)
    d.update(kw)
    return MarketTick(**d)

def test_momentum_requires_standardized_breakout_and_next_bar_entry():
    ts=[tick(i,delta=float(i%5),high=100+i*.01,low=99-i*.01,cvd=i) for i in range(20)]
    ts.append(tick(20,delta=30,order_imbalance=.6,vwap=101.99,close=102,high=102))
    s=AggressionMomentumScalper(); sig=s.generate_signal(ts,20)
    assert sig and sig.signal_type==SignalType.BUY

def test_absorption_needs_two_intervals():
    s=InstitutionalAbsorptionFade(); ts=[tick(i) for i in range(3)]
    ts.append(tick(3,absorption_score=3,order_imbalance=-.4,bid_skew=.5)); assert s.generate_signal(ts,3) is None
    ts.append(tick(4,absorption_score=3,order_imbalance=-.4,bid_skew=.5)); assert s.generate_signal(ts,4).signal_type==SignalType.BUY

def test_synthetic_disposition_and_cost_flags():
    r=run_backtest(pair='BTCUSDT',days=1,strategy_key='momentum',data_mode='synthetic'); rep=next(iter(r.reports.values())); d=rep.to_dict()
    assert d['validation_label']=='ENGINE_VALIDATION_SYNTHETIC'; assert d['promotion_allowed'] is False; assert d['funding_note']=='NOT_MODELED'
