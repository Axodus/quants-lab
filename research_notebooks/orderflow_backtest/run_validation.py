#!/usr/bin/env python3
"""Reproducible validation runner. It never promotes a strategy."""
import argparse,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
from orderflow_backtest.data_fixture import MarketTick,generate_synthetic_ticks
from orderflow_backtest.engine import SCENARIOS,run_backtest,write_artifacts

def load_historical(path):
 """Offline JSON loader; requires a list of 1m rows with all MarketTick fields, UTC timestamps and raw_l2/trades provenance metadata. No network fetch occurs."""
 obj=json.loads(Path(path).read_text()); rows=obj.get('rows',obj) if isinstance(obj,dict) else obj
 required=set(MarketTick.__dataclass_fields__)
 if not isinstance(rows,list) or not rows or not required.issubset(rows[0]): raise ValueError('historical schema invalid: rows require every MarketTick field; provide raw L2+trades provenance externally')
 return [MarketTick(**{k:r[k] for k in required}) for r in rows]
def main():
 p=argparse.ArgumentParser(); p.add_argument('--pair',choices=['BTCUSDT','ETHUSDC'],required=True); p.add_argument('--strategy',choices=['momentum','absorption','divergence'],default='momentum'); p.add_argument('--scenario',choices=list(SCENARIOS)+['all'],default='all'); p.add_argument('--data-mode',choices=['synthetic','historical'],default='synthetic'); p.add_argument('--historical-file'); p.add_argument('--days',type=int,default=90); p.add_argument('--seed',type=int,default=42); p.add_argument('--output-root',default=str(HERE/'runs')); p.add_argument('--request-promotion',action='store_true',help='refused; validation runner does not promote')
 a=p.parse_args()
 if a.request_promotion: p.error('promotion is refused by this runner; use governance after independent historical review')
 if a.data_mode=='historical':
  if not a.historical_file:p.error('--historical-file is required in historical mode')
  ticks=load_historical(a.historical_file)
 else: ticks=generate_synthetic_ticks(a.pair,a.days,seed=a.seed)
 scenarios=list(SCENARIOS) if a.scenario=='all' else [a.scenario]
 for s in scenarios:
  r=run_backtest(a.pair,a.days,a.strategy,ticks,scenario=s,data_mode=a.data_mode)
  meta={'data_mode':a.data_mode,'validation_label':'ENGINE_VALIDATION_SYNTHETIC' if a.data_mode=='synthetic' else 'HISTORICAL_VALIDATION','disposition':'DATASET_INSUFFICIENT / RESEARCH_CONTINUE' if a.data_mode=='synthetic' else 'HISTORICAL_REVIEW_REQUIRED','promotion_allowed':False,'scenario':s,'seed':a.seed if a.data_mode=='synthetic' else None,'timestamp_utc':'reproducible-input-derived'}
  provisional=Path(a.output_root)/f'{a.pair}_{a.strategy}_{s}_{a.data_mode}'
  rid=write_artifacts(r,provisional,meta); final=Path(a.output_root)/rid
  if final.exists():
   import shutil; shutil.rmtree(final)
  provisional.rename(final); print(json.dumps({'run_id':rid,'path':str(final),'disposition':meta['disposition']}))
if __name__=='__main__': main()
