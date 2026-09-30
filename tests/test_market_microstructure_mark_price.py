from decimal import Decimal
import pytest
from core.market_microstructure import MarkPriceEvent, MicrostructureFeatureEngine, CausalOrderBook
from tests.test_market_microstructure import spec, native, common, snap, update, trade


def test_mark_price_parse():
    ev = MarkPriceEvent.from_record({'venue':'v','market_type':'t','symbol':'s',
        'event_time': 1000000, 'receive_time': 2000000, 'mark_price': '10.5',
        'provenance': 'OBSERVED', 'source_ref': 'ref'}, timestamp_unit='us')
    assert ev.event_time == 1000
    assert ev.receive_time == 2000
    assert ev.mark_price == Decimal('10.5')
    assert ev.index_price is None
    assert ev.event_type == 'MARK_PRICE'


def test_mark_price_invalid():
    with pytest.raises(ValueError): MarkPriceEvent(**common(), mark_price='-1', provenance='OBSERVED')
    with pytest.raises(ValueError): MarkPriceEvent(**common(), mark_price='1', provenance='WRONG')
    with pytest.raises(ValueError): MarkPriceEvent(**common(), mark_price='1', provenance='OBSERVED', index_price='-1')


def test_microstructure_mark_price_context(spec, native):
    with CausalOrderBook(spec, native, 10) as book:
        engine = MicrostructureFeatureEngine(book, 'a'*64)
        assert engine.derivatives_context(100) is None
        engine.process(snap(50))
        engine.process(MarkPriceEvent(**common(100), mark_price='10', provenance='OBSERVED'))
        assert engine.counters['mark_price_events'] == 1
        assert engine.derivatives_context(90) is None
        assert engine.derivatives_context(100).mark_price == Decimal('10')
        assert engine.derivatives_context(200).mark_price == Decimal('10')
        engine.process(trade(110))
        # Book sequence is isolated
        assert engine.derivatives_context(110).mark_price == Decimal('10')


@pytest.mark.parametrize('bidq,askq,expected', [('10','10','100.5'),('30','10','100.75'),('10','30','100.25')])
def test_microprice_semantics(bidq,askq,expected):
    from core.market_microstructure.state import OrderBookState
    from core.market_microstructure.microprice import microprice_fields
    state=OrderBookState(((Decimal(100),Decimal(bidq)),),((Decimal(101),Decimal(askq)),),0,10,'VALID')
    f=microprice_fields(state)
    assert f['microprice']==Decimal(expected)
    assert f['microprice_minus_mid']==Decimal(expected)-Decimal('100.5')
    assert f['microprice_displacement_bps']==(Decimal(expected)-Decimal('100.5'))/Decimal('100.5')*10000
    assert f['microprice_valid'] is True
    assert f['microprice_provenance']=='COMPUTED_FROM_BOOK'


@pytest.mark.parametrize('bid,ask,bidq,askq,state', [
    ('100','101','0','0','VALID'), ('100','101','0','1','VALID'),
    ('100','101','-1','2','VALID'), ('102','101','1','1','VALID'),
    ('101','101','1','1','VALID'), ('100','101','1','1','INVALID_SEQUENCE'),
    ('100','101','NaN','1','VALID')])
def test_microprice_unavailable(bid,ask,bidq,askq,state):
    from core.market_microstructure.state import OrderBookState
    from core.market_microstructure.microprice import microprice_fields
    f=microprice_fields(OrderBookState(((Decimal(bid),Decimal(bidq)),),((Decimal(ask),Decimal(askq)),),0,10,state))
    assert f==dict(microprice=None,microprice_minus_mid=None,microprice_displacement_bps=None,
                  microprice_valid=False,microprice_provenance='UNAVAILABLE')


def test_bbo_derived_sequence(spec,native):
    states=[]
    for _ in range(2):
        with CausalOrderBook(spec,native,10) as book:
            events=[snap(),update(100,11,[('bid','100','12')]),update(200,12,[('ask','101','12')]),
                    update(300,13,[('bid','100','0')]),update(400,14,[('bid','100.5','2')]),
                    update(500,15,[('ask','100.8','2')])]
            expected=[('100','101'),('100','101'),('100','101'),('99','101'),('100.5','101'),('100.5','100.8')]
            run=[]
            for event,(bid,ask) in zip(events,expected):
                book.apply(event)
                st=book.state
                assert st.validity_state=='VALID'
                assert (st.best_bid,st.best_ask)==(Decimal(bid),Decimal(ask))
                assert st.spread==Decimal(ask)-Decimal(bid)
                assert st.mid_price==(Decimal(ask)+Decimal(bid))/2
                run.append(st)
            states.append(run)
    assert states[0]==states[1]


def test_mark_serialization_and_exact_price(spec):
    ev=MarkPriceEvent(**common(100),mark_price='100.12345678',provenance='OBSERVED')
    assert MarkPriceEvent.from_record(ev.to_dict())==ev
    assert ev.to_dict()['mark_price']=='100.12345678'  # no order-tick rounding
    aligned=MarkPriceEvent(**common(100),mark_price='100.1',provenance='OBSERVED')
    assert spec.price_from_ticks(spec.price_to_ticks(aligned.mark_price))==aligned.mark_price
    assert ev.index_price is ev.funding_rate is ev.next_funding_time is None
    ev2=MarkPriceEvent(**common(100),mark_price='100',provenance='OBSERVED',index_price='99.9',funding_rate='-0.0001',next_funding_time=1000)
    assert MarkPriceEvent.from_record(ev2.to_dict())==ev2


@pytest.mark.parametrize('field,value',[('symbol','ZECUSDT'),('venue','other'),('market_type','spot')])
def test_mark_identity_isolation(spec,native,field,value):
    with CausalOrderBook(spec,native,10) as book:
        engine=MicrostructureFeatureEngine(book,'a'*64)
        with pytest.raises(ValueError,match='cross-instrument'):
            engine.process(MarkPriceEvent(**dict(common(100),**{field:value}),mark_price='10',provenance='OBSERVED'))
        assert engine.mark_price_context is None
        assert engine.counters['events_rejected']==1


def test_mark_context_does_not_change_features(spec,native):
    from core.market_microstructure.replay import replay
    hashes=[]
    for with_marks in (False,True):
        with CausalOrderBook(spec,native,10) as book:
            engine=MicrostructureFeatureEngine(book,'a'*64)
            events=[snap()]
            for i in range(1,105):
                if with_marks:
                    events.append(MarkPriceEvent(**common(i*100-1),mark_price='999.12345678',provenance='OBSERVED'))
                events.append(update(i*100,10+i,[('bid','100','10')]))
            hashes.append([s.content_hash for s in replay(engine,events)])
    assert hashes[0] == hashes[1]


@pytest.mark.parametrize('value',[None,0,'0','NaN','Infinity',0.1,True])
def test_mark_invalid_price(value):
    with pytest.raises(ValueError):
        MarkPriceEvent(**common(),mark_price=value,provenance='OBSERVED')


def test_mark_receive_time_and_ordering(spec,native):
    with CausalOrderBook(spec,native,10) as book:
        engine=MicrostructureFeatureEngine(book,'a'*64)
        ev=MarkPriceEvent(**common(100),receive_time=200,mark_price='100',provenance='OBSERVED')
        engine.process(ev)
        assert engine.derivatives_context(199) is None
        assert engine.derivatives_context(200)==ev
        with pytest.raises(ValueError,match='out-of-order'):
            engine.process(MarkPriceEvent(**common(150),mark_price='101',provenance='OBSERVED'))
        assert engine.derivatives_context(200)==ev
