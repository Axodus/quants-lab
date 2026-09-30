"""Run snapshots through actual canonical simulation and governed trial engine."""
from decimal import Decimal as D
from core.market_microstructure.models import MicrostructureFeatureSnapshot, FeatureProvenance
from core.market_microstructure.integration import simulation_market_state, optimization_feature_ref
from core.quant_simulation import SimulationEngine, NoActionStrategy
from core.quant_foundations.models import ExperimentDefinition


def snapshot(t=1100):
    prov=FeatureProvenance('a'*64,'b'*64,'c'*64,'d'*64,'e'*64,('fixture',),t-100,t,1)
    return MicrostructureFeatureSnapshot('BTCUSDT','binance','USD-M Futures',t,12,100,'f'*64,prov,{'mid':D(100),'spread_bps':D('1.5')})


def test_actual_simulation():
    ref={'datasetId':'fixture','datasetVersion':'v1','contentDigest':'a'*64,'qualityStatus':'verified'}
    states=[simulation_market_state(snapshot(t),ref,str(t)) for t in (1100,1200,1300)]
    exp=ExperimentDefinition('micro-fixture','1','fixture-only','b'*64,(ref,),(),{},
                             {'execution':'no-action'},{'methodology_ref':'offline-fixture'},
                             {'seed':0},{'code_ref':'fixture'})
    result=SimulationEngine().run(exp,exp.to_reference(),ref,states,{'revisionId':'fixture-only'},NoActionStrategy())
    assert result.metrics['fillCount']=='0'
    assert result.metrics['netPnl']=='0'
    assert optimization_feature_ref(snapshot())['contentDigest']==snapshot().content_hash


def test_lineage_rejection():
    import pytest
    with pytest.raises(ValueError,match='digest'):
        simulation_market_state(snapshot(),{'datasetId':'wrong','datasetVersion':'v1','contentDigest':'0'*64},'1')
