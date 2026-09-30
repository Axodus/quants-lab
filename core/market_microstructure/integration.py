"""Causal snapshot bridge to Quant Simulation; no strategy decisions."""
from datetime import datetime, timezone
from core.quant_data.market_state import validate_market_state_schema


def simulation_market_state(snapshot,dataset_ref,observation_id):
    if not dataset_ref.get('datasetId') or not dataset_ref.get('datasetVersion'):
        raise ValueError('dataset identity required')
    if dataset_ref.get('contentDigest') != snapshot.provenance.dataset_hash:
        raise ValueError('dataset digest mismatch')
    if snapshot.provenance.window_end != snapshot.timestamp:
        raise ValueError('future feature rejected')
    stamp=datetime.fromtimestamp(snapshot.timestamp/1000,tz=timezone.utc).isoformat().replace('+00:00','Z')
    features=[{'featureId':name,'featureVersion':snapshot.feature_schema_version,'value':str(value) if value is not None else None,
               'computedAt':stamp} for name,value in snapshot.features.items()]
    state={'marketStateId':f"microstructure:{dataset_ref['datasetId']}:{observation_id}",
           'marketPrice':str(snapshot.features['mid']),'symbol':snapshot.symbol,
           'instrument':snapshot.symbol,'venue':snapshot.venue,'timeframe':f'{snapshot.horizon}ms',
           'marketTime':stamp,'observedAt':stamp,'constructedAt':stamp,'validityContext':'historical',
           'freshness':'historical','schemaVersion':'1.0.0','features':features,
           'sourceRefs':list(snapshot.provenance.source_refs)+[snapshot.source_state_hash],
           'validationRefs':['microstructure-causality-v1'],'completeness':'complete'}
    validate_market_state_schema(state)
    return state


def optimization_feature_ref(snapshot):
    return {'featureSchemaVersion':snapshot.feature_schema_version,'contentDigest':snapshot.content_hash,
            'datasetHash':snapshot.provenance.dataset_hash,'horizonMs':snapshot.horizon,
            'featureParameterHash':snapshot.provenance.config_hash}


def feature_search_space():
    """Feature construction parameters only; decision thresholds stay strategy-owned."""
    from core.quant_optimization.search_space import SearchSpace,Parameter
    from .models import HORIZONS
    return SearchSpace((Parameter('feature_horizon_ms','integer','CONTROL',HORIZONS,100),
                        Parameter('feature_ofi_depth','integer','CONTROL',(1,5,10),1)))


def cached_trial_features(cache,key,*,horizon,dataset_hash,window):
    """Research input selection from precomputed evidence, never a new event replay.

    SEARCH/VALIDATION/OOS consumption must still pass the optimization governance
    ledger. This feature accessor refuses OOS; OOS consumers require their separate
    frozen revision gate. No strategy or executable trial is created here.
    """
    from datetime import datetime,timezone
    if window.role not in {'SEARCH','VALIDATION'}:
        raise ValueError('feature search accessor cannot consume OOS')
    start=int(datetime.fromisoformat(window.start.replace('Z','+00:00')).timestamp()*1000)
    end=int(datetime.fromisoformat(window.end.replace('Z','+00:00')).timestamp()*1000)
    for snapshot in cache.get(key):
        if snapshot['provenance']['dataset_hash']!=dataset_hash:
            raise ValueError('cached dataset mismatch')
        if snapshot['horizon']==horizon and start<=snapshot['timestamp']<end:
            yield snapshot
