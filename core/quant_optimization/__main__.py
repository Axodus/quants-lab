"""Read-only operator inspection and bounded offline optimization CLI."""
import argparse
import json
from pathlib import Path
from .models import OptimizationError
from .governance import ResearchLedger
from .provenance import read_json
from .runner import CanonicalOptimizationEngine
from .study import parse_spec


def main(argv=None):
    parser=argparse.ArgumentParser(prog='python -m core.quant_optimization')
    parser.add_argument('--governance',type=Path,default=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/research/quant_governance'),help='shared canonical OOS ledger; do not reset between studies')
    parser.add_argument('--store',type=Path,required=True,help='durable research evidence root; never live run root')
    subs=parser.add_subparsers(dest='command',required=True)
    run=subs.add_parser('optimize')
    run.add_argument('--spec',type=Path,required=True)
    run.add_argument('--study-id',required=True)
    run.add_argument('--max-new-trials',type=int)
    for command in ('status','results'):
        sub=subs.add_parser(command)
        sub.add_argument('--study-id',required=True)
    args=parser.parse_args(argv)
    store=args.store.resolve()
    if 'runs' in store.parts or str(store)=='/opt/Axodus/Trading/quants-lab':
        parser.error('research store cannot be a live-run root or repository root')
    if args.command=='optimize':
        spec=parse_spec(read_json(args.spec))
        engine=CanonicalOptimizationEngine(store,ResearchLedger(args.governance))
        manifest,results=engine.optimize(args.study_id,spec,max_new_trials=args.max_new_trials)
        output={'study_id':args.study_id,'status':manifest['status'],'run_id':manifest['optimization_run_id'],
                'trials_attempted':manifest.get('n_trials_attempted',0),'trials_completed':manifest['n_trials_completed'],
                'candidate_count':len(results),'deployment_authorized':False}
    else:
        if not args.study_id.replace('_','').replace('-','').isalnum():
            parser.error('invalid study id')
        study=store/'studies'/args.study_id
        output=read_json(study/('study.json' if args.command=='status' else 'results.json'))
    print(json.dumps(output,sort_keys=True))


if __name__=='__main__':
    main()
