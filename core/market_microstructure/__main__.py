"""Offline operator surface; all inputs explicit, no data acquisition or strategy run."""
import argparse
import json
import resource
import time
import os
import sys
import subprocess
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from core.quant_optimization.models import canonical, digest
from core.quant_optimization.provenance import atomic_json,file_hash
from research_notebooks.orderflow_backtest.instrument_spec import InstrumentSpec
from .book import CausalOrderBook
from .features import MicrostructureFeatureEngine
from .models import FeatureConfig, SCHEMA_VERSION
from .markout import MarkoutEngine, ReferenceEvent
from .replay import book_events,trade_events,causal_merge,replay,availability,bootstrap_prefix
from .provenance import FeatureCache,cache_key,implementation_hash


def build(config, command=None):
    spec=InstrumentSpec.load(Path(config['instrument_spec']))
    root=Path(config['output_root']).resolve()
    if 'runs' in root.parts or '064e34bd-55c2-4be9-a8c4-212827eb5c9b' in str(root):
        raise ValueError('live artifact hierarchy forbidden')
    paths=[Path(x).resolve() for x in config['book_paths']]
    tapes=[Path(x).resolve() for x in config['trade_paths']]
    prefixes=[Path(x).resolve() for x in config.get('bootstrap_paths',[])]
    if any(root==p or root in p.parents for p in paths+tapes+prefixes):
        raise ValueError('output must not contain source files')
    duration=int(config.get('duration_ms',30000))
    if duration<10000 or duration>300000:
        raise ValueError('bounded certification duration 10..300 seconds')
    root.mkdir(parents=True,exist_ok=True)
    total_started=time.monotonic()
    source_hashes={str(p):file_hash(p) for p in paths+tapes+prefixes}
    dataset_hash=digest(source_hashes)
    feature_config=FeatureConfig(**config.get('features',{}))
    cache=FeatureCache(root/'cache')
    key=cache_key(dataset_hash,spec,feature_config,{'first_snapshot':True,'duration_ms':duration,'emit_every_ms':config.get('emit_every_ms',100)})
    started=time.monotonic()
    output=[]
    markouts=MarkoutEngine()
    with CausalOrderBook(spec,root/'native',feature_config.depth) as book:
        prefix_started=time.monotonic()
        prefix_rows=bootstrap_prefix(book,prefixes,root) if prefixes else 0
        prefix_seconds=time.monotonic()-prefix_started
        processing_started=time.monotonic()
        engine=MicrostructureFeatureEngine(book,dataset_hash,feature_config)
        if prefixes:
            engine.segment_start=book.state.timestamp
            engine.states.append(book.state.timestamp,book.state)
            engine.chain=digest((engine.chain,book.state,'verified-native-prefix'))
        books=book_events(paths,spec,first_snapshot_only=not bool(prefixes),duration_ms=duration,batch_size=config.get('batch_size',32768))
        first=next(books)
        start=availability(first)
        events=causal_merge(chain((first,),books),trade_events(tapes,spec,start,start+duration))
        reference_added=False
        for snapshot in replay(engine,events,config.get('emit_every_ms',100)):
            output.append(snapshot)
            if not reference_added:
                markouts.add(ReferenceEvent('certification-reference',snapshot.timestamp,'BUY',snapshot.features['mid'],snapshot.features['microprice'],True))
                reference_added=True
            markouts.observe(snapshot.timestamp,snapshot.features['mid'],snapshot.features['microprice'])
        processing_seconds=time.monotonic()-processing_started
        elapsed=time.monotonic()-started
        counter_names = ('events_received','events_processed','events_rejected','sequence_gaps',
                         'crossed_books','bootstrap_failures','stale_states','book_events','trade_events',
                         'mark_price_events','feature_snapshots','duplicate_events')
        counters = {name: engine.counters[name] for name in counter_names}
        counters.update({name: value for name, value in engine.counters.items() if name.startswith('horizon_')})
        final_state=book.state.validity_state
    if not output:
        raise ValueError('no qualified feature snapshots produced')
    persistence_started=time.monotonic()
    refs=cache.put(key,output)
    persistence_seconds=time.monotonic()-persistence_started
    report={'symbol':spec.symbol,'interval':{'start':start,'end':engine.time},'source_hashes':source_hashes,
            'dataset_hash':dataset_hash,'cache_key':key,'counters':counters,'book_status':final_state,
            'bootstrap_rows':prefix_rows,'snapshot_count':len(output),'feature_stream_hash':digest([s.content_hash for s in output]),
            'prefix_seconds':str(prefix_seconds),'feature_processing_seconds':str(processing_seconds),
            'persistence_seconds':str(persistence_seconds),'total_seconds':str(time.monotonic()-total_started),
            'feature_events_per_second':str(counters.get('events_processed',0)/processing_seconds),
            'wall_seconds':str(elapsed),'events_per_second':str(counters.get('events_processed',0)/elapsed),
            'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'markouts':markouts.edge_decay('certification-reference'),'remote_orders':0,'strategy_evaluation':False,
            'performance_scope':'bounded offline replay, not HFT/live suitability or full-dataset qualification'}
    wall_time = time.monotonic()-total_started
    code_root = Path(__file__).resolve().parents[2]
    git_head = subprocess.run(['git','rev-parse','HEAD'],cwd=code_root,capture_output=True,text=True,check=True).stdout.strip()
    source_manifest = {str(p.relative_to(code_root)): file_hash(p)
                       for p in sorted(Path(__file__).parent.glob('*.py'))+sorted(Path(__file__).parent.glob('*.cpp'))}
    for relative in ('research_notebooks/orderflow_backtest/orderbook_kernel.cpp',
                     'research_notebooks/orderflow_backtest/parquet_orderflow_frames.py',
                     'research_notebooks/orderflow_backtest/instrument_spec.py'):
        source_manifest[relative] = file_hash(code_root/relative)
    report.update({
        'dataset_ref': config.get('dataset_ref','CryptoHFTData/Binance-USD-M/normalized-canonical'),
        'interval_utc': {name: datetime.fromtimestamp(value/1000,timezone.utc).isoformat().replace('+00:00','Z')
                         for name,value in report['interval'].items()},
        'source_events': counters['events_received'], 'processed_events': counters['events_processed'],
        'rejected_events': counters['events_rejected'], 'book_events': counters['book_events'],
        'trade_events': counters['trade_events'], 'mark_price_events': counters['mark_price_events'],
        'feature_snapshots': counters['feature_snapshots'],
        'wall_time_seconds': str(wall_time), 'events_per_second': str(counters['events_processed']/wall_time),
        'throughput_denominator': 'end-to-end build: source checksums + compilation/cache load + bootstrap + event processing + feature cache persistence; report writing excluded',
        'source_event_scope': 'logical atomic events in bounded interval, excludes bootstrap_rows; raw L2 rows are not events',
        'peak_rss_mb': {'value':str(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024),
                        'measurement_status':'MEASURED_LINUX_RUSAGE_SELF_PROCESS_HIGH_WATER'},
        'cpu_mode': {'single_process': True,'single_core_or_effective_threads':len(list(Path('/proc/self/task').iterdir())),
                     'affinity': sorted(os.sched_getaffinity(0)),
                     'thread_policy': {key:os.environ.get(key) for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','ARROW_NUM_THREADS')},
                     'note':'native compiler child excluded from RUSAGE_SELF; observed thread count at report time'},
        'horizons_ms': list(feature_config.horizons), 'feature_schema_version':SCHEMA_VERSION,
        'instrument_spec':spec.to_manifest(), 'configuration': config, 'effective_features':canonical(feature_config),
        'reproducibility': {'command':command,'python':sys.executable,'code_revision':git_head,
                            'uncommitted_source_hashes':source_manifest,'implementation_hash':implementation_hash(),
                            'source_manifest_hash':digest(source_manifest)},
        'mark_price_source_status':'NOT_INCLUDED_IN_THIS_L2_TAPE_BENCHMARK',
    })
    atomic_json(root/'example_snapshots.json' ,[s.to_dict() for s in output[:7]])
    atomic_json(root/'benchmark.json',report)
    atomic_json(root/'snapshot_refs.json',refs)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['build-features','benchmark-features','inspect-features','validate-features'])
    p.add_argument('--config')
    p.add_argument('--root')
    args=p.parse_args()
    if args.command in {'build-features','benchmark-features'}:
        result=build(json.loads(Path(args.config).read_text()), [sys.executable,'-m','core.market_microstructure',args.command,'--config',str(Path(args.config).resolve())])
    else:
        root=Path(args.root)
        result=json.loads((root/'benchmark.json').read_text())
        if args.command=='validate-features':
            snapshots=FeatureCache(root/'cache').get(result['cache_key'])
            if digest([digest(s) for s in snapshots])!=result['feature_stream_hash']:
                raise ValueError('feature stream checksum mismatch')
            result={'status':'PASS','snapshots':len(snapshots)}
    print(json.dumps(canonical(result),sort_keys=True))


if __name__=='__main__':
    main()
