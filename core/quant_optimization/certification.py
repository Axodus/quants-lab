"""Bounded engineering certification, never strategy profitability acceptance."""
import argparse
from dataclasses import replace
from pathlib import Path
from .ema import get_search_space,source_hash,STRATEGY_ID,BASE_REVISION
from .models import DatasetRef,Window,OptimizationSpec,Objective,Constraint,canonical
from .provenance import file_hash,atomic_json,read_json
from .runner import CanonicalOptimizationEngine
from .governance import ResearchLedger


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--historical-root',type=Path,required=True)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--study-id',default='ema-v2-certification-02')
    parser.add_argument('--code-ref',required=True)
    args=parser.parse_args()
    symbols=('BTCUSDT','ZECUSDT','SUIUSDT','WLDUSDT')
    refs=[]
    for symbol in symbols:
        path=args.historical_root/f'{symbol}_1m_20260623_20260706.json'
        refs.append(DatasetRef('known-ema-candles:'+symbol,'1',symbol,str(path.resolve()),file_hash(path),'KNOWN_HISTORICAL'))
    space=get_search_space()
    base={p.name:p.default for p in space.parameters}
    # Frozen, non-performance-selected neighborhood plus seeded exploration.
    initial=[base,dict(base,ema_fast=4),dict(base,ema_fast=6),dict(base,take_profit_bps='20'),dict(base,stop_loss_bps='15')]
    spec=OptimizationSpec(STRATEGY_ID,BASE_REVISION,source_hash(),tuple(refs),symbols,
                          (Window('search-a','2026-06-23T19:00:00Z','2026-06-23T21:00:00Z','SEARCH'),
                           Window('search-b','2026-06-23T21:00:00Z','2026-06-23T23:00:00Z','SEARCH'),
                           Window('validation','2026-06-23T23:00:00Z','2026-06-24T01:00:00Z','VALIDATION')),
                          space,Objective(),(Constraint('trade_count',minimum='1'),),8,2718,args.code_ref,
                          sampler='TPE',validation_candidates=3,initial_parameter_sets=tuple(initial))
    store=args.store.resolve()
    if 'runs' in store.parts:
        raise ValueError('certification cannot write a live run root')
    atomic_json(store/'certification_spec.json',spec)
    engine=CanonicalOptimizationEngine(store,ResearchLedger(store/'governance'))
    partial,_=engine.optimize(args.study_id,spec,max_new_trials=3)
    complete,candidates=engine.optimize(args.study_id,spec)
    repeated,repeated_results=engine.optimize(args.study_id,spec)
    replay=[]
    for dataset in spec.dataset_refs:
        a,_,_=engine.evaluate(spec,base,dataset,spec.windows[0],complete['optimization_run_id'])
        b,_,_=engine.evaluate(spec,base,dataset,spec.windows[0],complete['optimization_run_id'],use_cache=False)
        replay.append({'symbol':dataset.symbol,'simulation_hash':a['simulation_digest'],
                       'deterministic':a['simulation_digest']==b['simulation_digest'],
                       'metrics_equal':a['trial']['metrics']==b['trial']['metrics'],'fees':a['trial']['metrics']['fees']})
    report={'classification':'ENGINEERING_CERTIFICATION_KNOWN_HISTORICAL_NOT_OOS','study':complete,
            'resume_same_run':partial['optimization_run_id']==complete['optimization_run_id'],
            'first_attempt_count':partial['n_trials_attempted'],'completed_noop':complete==repeated and candidates==repeated_results,
            'replay':replay,'candidate_statuses':[c['candidate_status'] for c in candidates],
            'neighborhood_counts':[c['robustness_metrics']['neighbor_count'] for c in candidates],
            'profitability_required':False,'oos_consumed':False,'real_orders':0,
            'current_paper_run_used':False}
    atomic_json(store/'certification_report.json',report)
    print(__import__('json').dumps(report,sort_keys=True))


if __name__=='__main__':
    main()
