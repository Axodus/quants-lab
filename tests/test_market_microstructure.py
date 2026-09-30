"""Golden, causal, native-kernel and integration acceptance; entirely offline."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import pytest
from research_notebooks.orderflow_backtest.instrument_spec import InstrumentSpec
from core.market_microstructure import CausalOrderBook, FeatureConfig, MicrostructureFeatureEngine
from core.market_microstructure.events import *
from core.market_microstructure.ofi import ofi
from core.market_microstructure.models import digest
from core.market_microstructure.markout import MarkoutEngine, ReferenceEvent
from core.market_microstructure.aggregation import EventBars, ClockBuckets
from core.market_microstructure.provenance import FeatureCache, cache_key
from core.market_microstructure.integration import simulation_market_state, optimization_feature_ref
from core.market_microstructure.registry import catalog
from core.market_microstructure.replay import book_events, receive_ms, replay


@pytest.fixture(scope='session')
def native(tmp_path_factory):
    return tmp_path_factory.mktemp('native')


@pytest.fixture
def spec():
    return InstrumentSpec('BTCUSDT','binance','USD-M Futures',D('.1'),D('.001'),10,1000,1,3,'fixture','2026-06-23T00:00:00Z','f'*64)


@pytest.fixture
def engine(spec,native):
    with CausalOrderBook(spec,native,10) as book:
        yield MicrostructureFeatureEngine(book,'a'*64)


def common(t=0):
    return dict(venue='binance',market_type='USD-M Futures',symbol='BTCUSDT',source_ref='fixture',event_time=t)


def snap(t=0,seq=10,bid='10',ask='10'):
    return BookSnapshotEvent(**common(t),last_update_id=seq,
        levels=tuple([('bid',str(100-i),bid) for i in range(10)]+[('ask',str(101+i),ask) for i in range(10)]))


def update(t,seq,levels,prev=None):
    return BookDeltaEvent(**common(t),first_update_id=seq,final_update_id=seq,prev_final_update_id=seq-1 if prev is None else prev,levels=levels)


def trade(t,side='BUY',qty='1',price='101',identity=None):
    return TradeEvent(**common(t),price=price,quantity=qty,aggressor=side,
                      aggressor_provenance='UNKNOWN' if side=='UNKNOWN' else 'OBSERVED',trade_id=identity)


def warm(engine):
    engine.process(snap())
    engine.process(update(100,11,[('bid','100','10')]))


@pytest.mark.parametrize('bad',[0.1,'NaN','Infinity','-1','0'])
def test_trade_invalid_decimal(bad):
    with pytest.raises(ValueError):
        trade(1,qty=bad)


def test_immutable_event_and_snapshot(engine):
    levels=[['bid','100','10'],['ask','101','10']]
    event=BookSnapshotEvent(**common(),last_update_id=10,levels=levels)
    levels[0][2]='999'
    assert event.levels[0][2]==D(10)
    warm(engine)
    s=engine.snapshot(100)
    with pytest.raises(TypeError):
        s.features['mid']=D(0)


@pytest.mark.parametrize('bid,ask,expected',[('30','10',D('.75')),('10','30',D('.25'))])
def test_queue_and_microprice(engine,bid,ask,expected):
    engine.process(snap(bid=bid,ask=ask))
    engine.process(update(100,11,[('bid','100',bid)]))
    s=engine.snapshot(100)
    for n in (1,5,10):
        assert s.features[f'queue_imbalance_L{n}']==expected
    assert s.features['microprice']==D(100)+expected
    assert s.features['weighted_book_pressure']==sum((D(bid)-D(ask))/D(i) for i in range(1,11))


@pytest.mark.parametrize('side,quantity,expected',[('bid','12',D(2)),('ask','12',D(-2)),('bid','8',D(-2)),('ask','8',D(2))])
def test_ofi_golden(engine,side,quantity,expected):
    engine.process(snap())
    engine.process(update(100,11,[(side,'100' if side=='bid' else '101',quantity)]))
    s=engine.snapshot(100)
    for depth in (1,5,10):
        assert s.features[f'OFI_L{depth}']==expected


def test_atomic_cross_transient(engine):
    warm(engine)
    # A transient row-wise cross disappears within the same atomic event.
    event=update(200,12,[('ask','99.5','2'),('bid','100','0')])
    assert engine.process(event)
    assert engine.book.state.best_bid==D(99)
    assert engine.book.state.validity_state=='VALID'


@pytest.mark.parametrize('levels,state',[
    ([('ask','99','1')],'INVALID_CROSSED'),
    ([("bid",str(100-i),'0') for i in range(10)],'INVALID_EMPTY')])
def test_invalid_book_features_rejected(engine,levels,state):
    warm(engine)
    engine.process(update(200,12,levels))
    assert engine.book.state.validity_state==state
    with pytest.raises(ValueError): engine.snapshot(100)


def test_gap_and_rebootstrap(engine):
    warm(engine)
    engine.process(update(200,15,[('bid','100','12')]))
    assert engine.book.state.validity_state=='INVALID_SEQUENCE'
    assert engine.counters['sequence_gaps']==1
    with pytest.raises(ValueError): engine.snapshot(100)
    engine.process(snap(300,20))
    with pytest.raises(ValueError): engine.snapshot(100)
    engine.process(update(400,21,[('bid','100','11')]))
    assert engine.snapshot(100).features['OFI_L1']==D(1)


def test_missing_bootstrap_and_staleness(engine):
    engine.process(update(0,11,[('bid','100','1')]))
    assert engine.counters['bootstrap_failures']==1
    with pytest.raises(ValueError):engine.snapshot(100)
    engine.process(snap(1))
    with pytest.raises(ValueError,match='STALE'):engine.snapshot(100,11002)


def test_instrument_tick_and_identity(engine):
    with pytest.raises(ValueError):engine.process(replace(snap(),symbol='ZECUSDT'))
    with pytest.raises(ValueError):engine.process(replace(snap(),levels=[('bid','100.01','1'),('ask','101','1')]))


def test_aggression_velocity_acceleration(engine):
    warm(engine)
    engine.process(trade(110,'BUY','3'))
    engine.process(trade(120,'SELL','1','100'))
    engine.process(update(200,12,[('bid','100','10')]))
    s=engine.snapshot(100).features
    assert s['buy_aggressive_volume']==3
    assert s['sell_aggressive_volume']==1
    assert s['aggression_imbalance']==D('.5')
    assert s['trade_velocity']==20
    assert s['signed_volume_velocity']==20
    assert s['trade_acceleration']==200
    assert s['signed_volume_acceleration']==200
    assert s['price_response_per_aggressive_volume']==0


def test_zero_volume_unknown_aggressor(engine):
    warm(engine)
    s=engine.snapshot(100).features
    assert s['aggression_imbalance'] is None
    assert s['average_buy_trade_size'] is None
    engine.process(trade(110,'UNKNOWN','2'))
    s=engine.snapshot(100).features
    assert s['unknown_aggressive_volume']==2
    assert s['signed_aggressive_volume']==0


def test_replenishment_absorption(engine):
    warm(engine)
    engine.process(update(110,12,[('ask','101','5')]))
    engine.process(trade(120,'BUY','5'))
    engine.process(update(150,13,[('ask','101','10')]))
    s=engine.snapshot(100).features
    assert s['ask_cancel_rate']==50
    assert s['ask_add_rate']==50
    assert s['ask_replenished_volume']==5
    assert s['ask_replenishment_ratio']==1
    assert s['buy_absorption_score']==5
    assert s['sell_absorption_score']==0


@pytest.mark.parametrize('side,prices,name',[('BUY',('101','102'),'buy'),('SELL',('100','99'),'sell')])
def test_sweep_golden(engine,side,prices,name):
    warm(engine)
    engine.process(trade(110,side,'2',prices[0]))
    engine.process(trade(120,side,'3',prices[1]))
    assert engine.snapshot(100).features[name+'_sweep_score']==5


def test_all_horizons_and_registry(engine):
    warm(engine)
    for seq,t in enumerate(range(200,10200,100),start=12):
        engine.process(update(t,seq,[('bid','100','10')]))
    for horizon in engine.config.horizons:
        s=engine.snapshot(horizon)
        assert set(s.features)<=set(catalog())
        assert s.provenance.window_end-s.provenance.window_start==horizon
        assert s.features['spread_absolute']==1
        assert s.features['spread_percentile']==1
        assert s.features['realized_micro_volatility']==0
        assert s.features['total_depth']==200


def test_no_lookahead_receive_time(engine):
    warm(engine)
    old=engine.snapshot(100)
    old_hash=old.content_hash
    engine.process(replace(trade(110,'BUY','9'),receive_time=300))
    with pytest.raises(ValueError):engine.snapshot(100,200)
    assert old.features['buy_aggressive_volume']==0
    assert old.content_hash==old_hash
    assert engine.snapshot(100).features['buy_aggressive_volume']==9


def test_duplicate_trade_and_conflict(engine):
    warm(engine)
    e=trade(110,identity='a')
    engine.process(e)
    engine.process(e)
    assert engine.snapshot(100).features['buy_trade_count']==1
    with pytest.raises(ValueError):engine.process(replace(e,quantity=D(2)))


def test_markout_separate_and_signed():
    m=MarkoutEngine()
    m.add(ReferenceEvent('a',0,'BUY',D(100),D(100),True))
    m.observe(99,D(98),D(98))
    assert not m.results
    for h in (100,250,500,1000,2000,5000,10000):
        m.observe(h,D(99),D(99))
    assert len(m.results)==7
    assert m.results[('a',100)]['adverse_selection']==1
    assert m.results[('a',100)]['signed_mid_markout']==-1
    n=MarkoutEngine((100,))
    n.add(ReferenceEvent('b',0,'SELL',D(100)))
    n.observe(100,D(99))
    assert n.results[('b',100)]['signed_mid_markout']==1


def test_bars_and_clock_boundaries():
    bars=EventBars('TRADE_COUNT',2)
    assert not bars.push(trade(1))
    assert bars.push(trade(2))[0]['count']==2
    bars=EventBars('VOLUME',2)
    assert len(bars.push(trade(1,qty='5')))==2
    assert bars.volume==1
    bars=EventBars('EVENT_COUNT',2)
    assert not bars.push(snap())
    assert bars.push(trade(1))
    bucket=ClockBuckets(100)
    assert not bucket.push(trade(99))
    result=bucket.push(trade(200))
    assert [r['event_count'] for r in result]==[1,0]


def test_cache_simulation_optimization(engine,tmp_path):
    warm(engine)
    s=engine.snapshot(100)
    key=cache_key('a'*64,engine.book.spec,engine.config,(0,100))
    assert key!=cache_key('b'*64,engine.book.spec,engine.config,(0,100))
    assert key!=cache_key('a'*64,engine.book.spec,replace(engine.config,horizons=(100,)),(0,100))
    cache=FeatureCache(tmp_path/'cache')
    cache.put(key,[s])
    assert digest(cache.get(key)[0])==s.content_hash
    state=simulation_market_state(s,{'datasetId':'fixture','datasetVersion':'v1','contentDigest':'a'*64},'1')
    assert state['instrument']=='BTCUSDT'
    assert optimization_feature_ref(s)['contentDigest']==s.content_hash
    with pytest.raises(ValueError):cache.get('../bad')


def test_replay_determinism_partition_continuity(spec,native):
    events=[snap()]+[update(i*100,10+i,[('bid','100',str(10+i))]) for i in range(1,130)]
    hashes=[]
    for chunks in ([events],[events[:17],events[17:61],events[61:]]):
        with CausalOrderBook(spec,native,10) as book:
            engine=MicrostructureFeatureEngine(book,'a'*64)
            snapshots=list(replay(engine,(e for chunk in chunks for e in chunk)))
            hashes.append(digest([s.content_hash for s in snapshots]))
    assert hashes[0]==hashes[1]


def test_bound_capacity(spec,native):
    with CausalOrderBook(spec,native,10) as book:
        engine=MicrostructureFeatureEngine(book,'a'*64,FeatureConfig(max_window_events=2))
        warm(engine)
        with pytest.raises(ValueError,match='CAPACITY'):engine.process(update(200,12,[('bid','100','11')]))


def test_provider_event_atomicity_across_files(spec,tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    t=1782168427319
    rows=[]
    for side,price in [('bid','100.0'),('ask','101.0')]:
        rows.append(dict(event_time=t,transaction_time=t,event_type='snapshot',first_update_id=None,final_update_id=None,
                         prev_final_update_id=None,last_update_id=10,side=side,price=price,quantity='1.000',symbol=spec.symbol,received_time=t*1000))
    paths=[]
    for i,row in enumerate(rows):
        p=tmp_path/f'{i}.parquet'
        pq.write_table(pa.Table.from_pylist([row]),p)
        paths.append(p)
    events=list(book_events(paths,spec,batch_size=1))
    assert len(events)==1
    assert len(events[0].levels)==2
    assert events[0].receive_time==t
    assert receive_ms(t*1000000)==(t,'ns')
    with pytest.raises(ValueError):receive_ms(42)


def test_future_snapshot_watermark_cannot_admit_earlier_event(engine):
    warm(engine)
    engine.snapshot(100,200)
    with pytest.raises(ValueError,match='watermark'):
        engine.process(trade(150))


def test_duplicate_level_rejected():
    with pytest.raises(ValueError,match='duplicate price'):
        replace(snap(),levels=[('bid','100','1'),('bid','100','2'),('ask','101','1')])


def test_markout_monotonic_bounded_and_drain():
    m=MarkoutEngine((100,),max_references=1)
    m.add(ReferenceEvent('a',0,'BUY',D(100)))
    with pytest.raises(ValueError,match='CAPACITY'):
        m.add(ReferenceEvent('b',0,'BUY',D(100)))
    m.observe(150,D(101))
    assert m.results[('a',100)]['observation_lag_ms']==50
    assert m.results[('a',100)]['forward_return']==D('.01')
    with pytest.raises(ValueError,match='out-of-order'):m.observe(100,D(99))
    assert len(m.drain_completed())==1
    assert not m.references and not m.results


def test_feature_parameter_selection_and_oos_boundary(engine,tmp_path):
    from core.market_microstructure.integration import feature_search_space,cached_trial_features
    from core.quant_optimization.models import Window
    warm(engine)
    s=engine.snapshot(100)
    cache=FeatureCache(tmp_path/'features')
    key=cache_key('a'*64,engine.book.spec,engine.config,(0,100))
    cache.put(key,[s])
    space=feature_search_space()
    assert space.validate({'feature_horizon_ms':100,'feature_ofi_depth':5})
    w=Window('search','1970-01-01T00:00:00Z','1970-01-01T00:00:01Z','SEARCH')
    assert len(list(cached_trial_features(cache,key,horizon=100,dataset_hash='a'*64,window=w)))==1
    assert not list(cached_trial_features(cache,key,horizon=250,dataset_hash='a'*64,window=w))
    with pytest.raises(ValueError,match='OOS'):
        list(cached_trial_features(cache,key,horizon=100,dataset_hash='a'*64,window=replace(w,role='OOS')))
