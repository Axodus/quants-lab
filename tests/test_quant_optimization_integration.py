"""Offline canonical integration: synthetic fixtures never represent market validation."""
import json
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from core.quant_optimization.models import *
from core.quant_optimization.search_space import *
from core.quant_optimization.ema import get_search_space,source_hash,EMAPullbackResearchAdapter
from core.quant_optimization.data import load_states
from core.quant_optimization.runner import CanonicalOptimizationEngine
from core.quant_optimization.governance import ResearchLedger
from core.quant_optimization.provenance import atomic_json,file_hash,read_json
from core.quant_optimization.objective import objective_value,aggregate,distribution
from core.quant_optimization.selection import RobustSelector
from core.quant_optimization.study import parse_spec


def fixture(root,symbol='BTCUSDT'):
    rows=[]
    start=datetime(2024,1,1,tzinfo=timezone.utc)
    for i in range(360):
        # Alternating ramps, broad OHLC ranges; no financial-performance assertion.
        price=D(100)+D((i%40) if (i//40)%2==0 else 40-i%40)/100
        rows.append({'marketTime':(start+timedelta(minutes=i)).isoformat().replace('+00:00','Z'),
                     'symbol':symbol,'marketPrice':str(price),'low':str(price-D('.25')),'high':str(price+D('.25')),
                     'validityContext':'historical'})
    path=root/(symbol+'.json')
    atomic_json(path,rows)
    return DatasetRef('fixture:'+symbol,'1',symbol,str(path),file_hash(path),'SYNTHETIC_CERTIFICATION')


def spec_for(root, trials=4, symbols=('BTCUSDT',),sampler='RANDOM'):
    datasets=tuple(fixture(root,s) for s in symbols)
    windows=(Window('search-a','2024-01-01T00:00:00Z','2024-01-01T02:00:00Z','SEARCH'),
             Window('search-b','2024-01-01T02:00:00Z','2024-01-01T04:00:00Z','SEARCH'),
             Window('validation','2024-01-01T04:00:00Z','2024-01-01T06:00:00Z','VALIDATION'))
    return OptimizationSpec('trend.ema.5x9.pullback.scalper','ema-5x9-v2',source_hash(),datasets,symbols,windows,
                            get_search_space(),Objective(),(),trials,42,'test-version',sampler=sampler)


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.spec=spec_for(self.root)
        self.ledger=ResearchLedger(self.root/'governance')
        self.engine=CanonicalOptimizationEngine(self.root/'evidence',self.ledger)
        self.params={p.name:p.default for p in self.spec.search_space.parameters}

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_types_and_invalid_combinations(self):
        space=SearchSpace((IntegerParameter('a',1,3),DecimalParameter('d','0','1','.5'),BooleanParameter('b'),CategoricalParameter('c',['x','y'],'x')))
        self.assertEqual(space.cardinality,36)
        self.assertEqual(space.validate({'a':1,'d':D('.50'),'b':False,'c':'y'})['d'],'0.5')
        with self.assertRaises(OptimizationError):
            self.spec.search_space.validate(dict(self.params,ema_fast=9,ema_slow=7))
        with self.assertRaises(OptimizationError):
            space.validate({'a':True,'d':'0','b':False,'c':'x'})

    def test_exact_decimal_and_hash(self):
        self.assertEqual(parameter_hash({'a':D('.50')}),parameter_hash({'a':D('0.5')}))
        with self.assertRaises(OptimizationError):
            decimal(0.1)
        with self.assertRaises(OptimizationError):
            decimal('NaN')

    def test_spec_serialization_immutable(self):
        self.assertEqual(parse_spec(canonical(self.spec)).spec_hash,self.spec.spec_hash)
        with self.assertRaises(Exception):
            self.spec.n_trials=2
        self.assertNotEqual(replace(self.spec,seed=1).spec_hash,self.spec.spec_hash)

    def test_overlapping_windows_rejected(self):
        with self.assertRaises(OptimizationError):
            replace(self.spec,windows=(self.spec.windows[0],replace(self.spec.windows[0],name='oos',role='OOS')))

    def test_no_oos_in_optimization(self):
        spec=replace(self.spec,windows=self.spec.windows+(Window('holdout','2030-01-01T00:00:00Z','2030-01-02T00:00:00Z','OOS'),),n_trials=1)
        result,_=self.engine.optimize('no-oos',spec)
        self.assertFalse(result['oos_consumed'])
        self.assertFalse(any(r['role']=='OOS' for r in read_json(self.ledger.root/'consumption_ledger.json')))

    def test_trial_dedup_and_deterministic_replay(self):
        args=(self.spec,self.params,self.spec.dataset_refs[0],self.spec.windows[0],'run')
        first,ref,hit=self.engine.evaluate(*args)
        second,ref2,hit2=self.engine.evaluate(*args)
        third,_,_=self.engine.evaluate(*args,use_cache=False)
        self.assertFalse(hit)
        self.assertTrue(hit2)
        self.assertEqual(ref,ref2)
        self.assertEqual(first['simulation_digest'],third['simulation_digest'])
        self.assertEqual(first['trial']['metrics'],third['trial']['metrics'])

    def test_economics_fees_net_exact(self):
        evidence,_,_=self.engine.evaluate(self.spec,self.params,self.spec.dataset_refs[0],self.spec.windows[0],'run')
        metrics=evidence['trial']['metrics']
        self.assertGreater(D(metrics['fees']),0)
        self.assertLess(abs(D(metrics['gross_pnl'])-D(metrics['fees'])-D(metrics['net_pnl'])),D('1e-22'))
        simulation=self.engine.store.get(evidence['trial']['artifact_refs'][0])
        for fill in simulation['fills']:
            self.assertEqual(D(fill['fee']),D(fill['quantity'])*D(fill['price'])*D('0.0005'))

    def test_resume_budget_and_completed_noop(self):
        paused,_=self.engine.optimize('resume',self.spec,max_new_trials=2)
        self.assertEqual(paused['status'],'PAUSED')
        complete,results=self.engine.optimize('resume',self.spec)
        self.assertEqual(complete['n_trials_attempted'],4)
        self.assertEqual(complete['optimization_run_id'],paused['optimization_run_id'])
        self.assertGreater(len(results),0)
        repeated,again=self.engine.optimize('resume',self.spec)
        self.assertEqual(complete,repeated)
        self.assertEqual(results,again)

    def test_immutable_study_rejects_changed_spec(self):
        self.engine.optimize('frozen',self.spec,max_new_trials=1)
        with self.assertRaises(OptimizationError):
            self.engine.optimize('frozen',replace(self.spec,n_trials=5))

    def test_constraints_prevent_selection(self):
        spec=replace(self.spec,n_trials=1,constraints=(Constraint('trade_count',minimum='100000'),))
        study,results=self.engine.optimize('constrained',spec)
        self.assertEqual(study['n_trials_completed'],0)
        self.assertTrue(all(c['candidate_status']=='REJECTED' for c in results))

    def test_objectives(self):
        keys={'NET_EXPECTANCY':'net_expectancy','NET_PNL':'net_pnl','PROFIT_FACTOR':'profit_factor','SHARPE':'sharpe','MAX_DRAWDOWN':'max_drawdown','RETURN_OVER_DRAWDOWN':'return_over_drawdown'}
        for name,key in keys.items():
            obj=Objective(name,'MINIMIZE' if name=='MAX_DRAWDOWN' else 'MAXIMIZE')
            self.assertEqual(objective_value(obj,{key:'2.25'}),D('2.25'))
            with self.assertRaises(OptimizationError):
                objective_value(obj,{key:None})

    def test_oos_consumption_alias_revision_guard(self):
        dataset=replace(self.spec.dataset_refs[0],provenance='SEALED_UNOBSERVED',oos_eligible=True)
        window=replace(self.spec.windows[0],role='OOS')
        self.ledger.observe('s','v1','p1',dataset,window)
        with self.assertRaises(OptimizationError):
            self.ledger.observe('s','v2','p2',replace(dataset,dataset_id='alias'),window)
        with self.assertRaises(OptimizationError):
            self.ledger.observe('s','v2','p2',dataset,replace(window,role='SEARCH'))

    def test_oos_requires_freeze(self):
        with self.assertRaises(OptimizationError):
            self.engine.evaluate_oos(self.spec,self.params,self.spec.dataset_refs[0],replace(self.spec.windows[0],role='OOS'),'new','run')

    def test_validation_cannot_be_reoptimized(self):
        self.ledger.reserve('original',self.spec)
        with self.assertRaises(OptimizationError):
            self.ledger.reserve('new',replace(self.spec,windows=(replace(self.spec.windows[-1],role='SEARCH'),)))

    def test_live_data_rejected(self):
        dataset=replace(self.spec.dataset_refs[0],path='/data/runs/064e34bd-55c2-4be9-a8c4-212827eb5c9b/orders.json')
        with self.assertRaises(OptimizationError):
            load_states(dataset,self.spec.windows[0])

    def test_dataset_hash_and_gap(self):
        dataset=self.spec.dataset_refs[0]
        rows=read_json(dataset.path)
        rows.pop(10)
        atomic_json(dataset.path,rows)
        with self.assertRaises(OptimizationError):
            load_states(dataset,self.spec.windows[0])
        with self.assertRaises(OptimizationError):
            load_states(replace(dataset,sha256=file_hash(dataset.path)),self.spec.windows[0])

    def test_multi_symbol_cartesian_windows(self):
        spec=spec_for(self.root,trials=1,symbols=('BTCUSDT','ZECUSDT'))
        study,results=self.engine.optimize('multi',spec)
        self.assertEqual(len(results[0]['search_evidence_refs']),4)
        self.assertEqual(len(results[0]['validation_evidence_refs']),2)
        self.assertEqual(results[0]['cross_symbol_metrics']['count'],2)
        self.assertEqual(results[0]['temporal_metrics']['count'],2)

    def test_samplers(self):
        for sampler in ('TPE','GRID'):
            with TemporaryDirectory() as tmp:
                root=Path(tmp)
                spec=spec_for(root,trials=2,sampler=sampler)
                engine=CanonicalOptimizationEngine(root/'out',ResearchLedger(root/'governance'))
                study,_=engine.optimize('sample',spec)
                self.assertEqual(study['n_trials_attempted'],2)

    def test_parameter_candidate_no_deployment(self):
        _,results=self.engine.optimize('candidate',replace(self.spec,n_trials=1))
        self.assertNotIn('deployment_candidate',results[0])
        self.assertEqual(results[0]['base_revision'],'ema-5x9-v2')

    def test_cache_integrity(self):
        _,ref,_=self.engine.evaluate(self.spec,self.params,self.spec.dataset_refs[0],self.spec.windows[0],'r')
        atomic_json(self.engine.store.root/'artifacts'/(ref+'.json'),{'tampered':True})
        with self.assertRaises(OptimizationError):
            self.engine.evaluate(self.spec,self.params,self.spec.dataset_refs[0],self.spec.windows[0],'r')

    def test_path_traversal(self):
        with self.assertRaises(OptimizationError):
            self.engine.optimize('../escape',self.spec)

    def test_best_trials_decimal_order(self):
        study,_=self.engine.optimize('best',self.spec)
        rows=read_json(self.engine._path('best')/'best_trials.json')
        self.assertEqual([D(r['score']) for r in rows],sorted([D(r['score']) for r in rows],reverse=True))

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(OptimizationError):
            self.engine.optimize('bad',replace(self.spec,strategy_id='unknown'))

    def test_fixed_revision_hash(self):
        with self.assertRaises(OptimizationError):
            self.engine.optimize('bad',replace(self.spec,strategy_source_hash='0'*64))

    def test_robust_region_and_isolated_peak(self):
        space=SearchSpace((IntegerParameter('x',1,3),))
        population=[]
        for x in (1,2,3):
            population.append(dict(strategy_id='s',base_revision='v1',parameter_set={'x':x},parameter_hash=digest(x),optimization_run_id='run',
                                   search_evidence_refs=['search'],validation_evidence_refs=['validation'],objective_metrics={'objective_value':'2','net_pnl':'2'},
                                   constraint_results={'min_trades':True},robustness_metrics={},cross_symbol_metrics={'positive_symbol_ratio':'1'},
                                   temporal_metrics={'positive_window_ratio':'1'},selection_reason='',candidate_status='CANDIDATE'))
        result=RobustSelector(space).select(population)
        center=next(c for c in result if c['parameter_set']['x']==2)
        self.assertEqual(center['candidate_status'],'CANDIDATE')
        self.assertEqual(center['robustness_metrics']['neighbor_count'],2)
        for c in (population[0],population[2]):
            c['objective_metrics']['net_pnl']='-1'
        center=next(c for c in RobustSelector(space).select(population) if c['parameter_set']['x']==2)
        self.assertEqual(center['candidate_status'],'FRAGILE')

    def test_freeze_immutability(self):
        c={'candidate_status':'CANDIDATE','base_revision':'v1','strategy_id':'s','parameter_hash':'p'}
        self.ledger.freeze(c,'v2','cto:test')
        self.ledger.require_freeze('v2','p')
        with self.assertRaises(OptimizationError):
            self.ledger.freeze(c,'v2','cto:test')
        with self.assertRaises(OptimizationError):
            self.ledger.freeze(c,'v1','cto:test')

    def test_cross_symbol_normalization(self):
        records=[{'symbol':s,'window_ref':{'name':w},'metrics':{'net_expectancy':v,'net_pnl':v,'max_drawdown':'.1','trades_per_day':'2'}}
                 for s,v in [('A','1000'),('B','-2'),('C','-3')] for w in ('a','b')]
        metrics,_,cross,temporal=aggregate(records,Objective())
        self.assertEqual(metrics['net_expectancy'],'-2')
        self.assertEqual(cross['median_net_expectancy'],'-2')
        self.assertEqual(temporal['count'],2)

    def test_initial_configs_bounded_resume(self):
        initial=(self.params,dict(self.params,pullback_tolerance_bps='4'))
        spec=replace(self.spec,n_trials=2,initial_parameter_sets=initial)
        paused,_=self.engine.optimize('initial',spec,max_new_trials=1)
        self.assertEqual(paused['n_trials_attempted'],1)
        complete,results=self.engine.optimize('initial',spec)
        self.assertEqual(complete['n_trials_attempted'],2)
        self.assertEqual(complete['unique_parameter_sets'],2)

    def test_oos_direct_evaluate_requires_freeze(self):
        with self.assertRaises(OptimizationError):
            self.engine.evaluate(self.spec,self.params,self.spec.dataset_refs[0],replace(self.spec.windows[0],role='OOS'),'run')

    def test_reserved_search_not_fresh_oos(self):
        self.ledger.reserve('s',self.spec)
        dataset=replace(self.spec.dataset_refs[0],provenance='SEALED_UNOBSERVED',oos_eligible=True)
        with self.assertRaises(OptimizationError):
            self.ledger.observe('s','v2','p',dataset,replace(self.spec.windows[0],role='OOS'))

    def test_objective_is_persisted_per_trial(self):
        evidence,_,_=self.engine.evaluate(self.spec,self.params,self.spec.dataset_refs[0],self.spec.windows[0],'run')
        self.assertEqual(evidence['trial']['objective_value'],evidence['trial']['metrics']['net_expectancy'])

    def test_sparse_search_never_claims_robust(self):
        _,results=self.engine.optimize('sparse',replace(self.spec,n_trials=1))
        self.assertTrue(all(c['candidate_status']!='CANDIDATE' for c in results))

    def test_unknown_fee_execution_rejected(self):
        with self.assertRaises(OptimizationError):
            replace(self.spec,execution_model_ref='maker_always_fills')
        with self.assertRaises(OptimizationError):
            replace(self.spec,fee_model_ref='zero-fee')

    def test_cooldown_and_causal_entry(self):
        states=load_states(self.spec.dataset_refs[0],self.spec.windows[0])
        adapter=EMAPullbackResearchAdapter(self.params,'100','1',len(states))
        self.assertTrue(all(adapter.decide(s,D(0)).action=='NO_ACTION' for s in states[:9]))
        # A terminal entry is forbidden; final two bars are reserved for exits.
        adapter.index=len(states)-3
        self.assertEqual(adapter.decide(states[-2],D(0)).action,'NO_ACTION')

    def test_complete_results_tampering_rejected(self):
        self.engine.optimize('tamper',replace(self.spec,n_trials=1))
        atomic_json(self.engine._path('tamper')/'results.json',[])
        with self.assertRaises(OptimizationError):
            self.engine.optimize('tamper',replace(self.spec,n_trials=1))

    def test_frozen_oos_consumed_once(self):
        dataset=replace(self.spec.dataset_refs[0],provenance='SEALED_UNOBSERVED',oos_eligible=True)
        window=replace(self.spec.windows[-1],role='OOS')
        spec=replace(self.spec,dataset_refs=(dataset,),windows=self.spec.windows[:-1]+(window,))
        c={'candidate_status':'CANDIDATE','base_revision':'ema-5x9-v2','strategy_id':self.spec.strategy_id,'parameter_hash':parameter_hash(self.params)}
        self.ledger.freeze(c,'ema-research-frozen','test-approval')
        evidence,_,_=self.engine.evaluate_oos(spec,self.params,dataset,window,'ema-research-frozen','oos-test')
        self.assertEqual(evidence['trial']['base_revision'],'ema-research-frozen')
        with self.assertRaises(OptimizationError):
            self.engine.evaluate_oos(spec,self.params,dataset,window,'ema-research-frozen','oos-test')

    def test_direct_evaluation_cannot_substitute_window_or_dataset(self):
        for dataset, window in (
            (self.spec.dataset_refs[0], replace(self.spec.windows[-1],role='SEARCH')),
            (replace(self.spec.dataset_refs[0],revision='unfrozen'), self.spec.windows[0]),
        ):
            with self.assertRaises(OptimizationError):
                self.engine.evaluate(self.spec,self.params,dataset,window,'unapproved')

    def test_observed_validation_blocks_direct_search_and_new_study(self):
        dataset=self.spec.dataset_refs[0]
        window=self.spec.windows[-1]
        self.engine.evaluate(self.spec,self.params,dataset,window,'validation')
        revised=replace(self.spec,windows=(replace(window,role='SEARCH'),))
        with self.assertRaises(OptimizationError):
            self.engine.evaluate(revised,self.params,dataset,revised.windows[0],'search')
        with self.assertRaises(OptimizationError):
            self.engine.optimize('recycled-validation',revised)

    def test_reserved_validation_blocks_direct_search(self):
        self.ledger.reserve('frozen-study',self.spec)
        revised=replace(self.spec,windows=(replace(self.spec.windows[-1],role='SEARCH'),))
        with self.assertRaises(OptimizationError):
            self.engine.evaluate(revised,self.params,revised.dataset_refs[0],revised.windows[0],'search')

    def test_invalid_trials_excluded_from_best_trials(self):
        spec=replace(self.spec,n_trials=1,constraints=(Constraint('trade_count',minimum='100000'),))
        self.engine.optimize('invalid-best',spec)
        self.assertEqual(read_json(self.engine._path('invalid-best')/'best_trials.json'),[])

    def test_governance_cannot_write_live_run(self):
        with self.assertRaises(OptimizationError):
            ResearchLedger(self.root/'runs'/'live')

    def test_study_nested_metadata_immutable(self):
        study=OptimizationStudy('s','r','strategy','v','hash',now(),'RANDOM',1,1,0,
                                ({'id':'data'},),({'role':'SEARCH'},),{'name':'NET_PNL'},(),'RUNNING')
        with self.assertRaises(TypeError):
            study.objective['name']='SHARPE'
        with self.assertRaises(TypeError):
            study.dataset_refs[0]['id']='other'

    def test_optuna_crash_slot_reconciles(self):
        from core.quant_optimization.study import open_backend
        spec=replace(self.spec,n_trials=2)
        self.engine.optimize('crash',spec,max_new_trials=1)
        backend=open_backend(self.engine.store.root,'crash',spec)
        trial=backend.ask()
        atomic_json(self.engine._path('crash')/'trials'/f'{trial.number:06d}.json',{'number':trial.number,'status':'RUNNING'})
        manifest,_=self.engine.optimize('crash',spec)
        self.assertEqual(manifest['n_trials_attempted'],2)
        self.assertEqual(read_json(self.engine._path('crash')/'trials'/'000001.json')['status'],'FAILED')

    def test_same_study_name_different_store_cannot_reset_validation(self):
        self.engine.optimize('shared-name',replace(self.spec,n_trials=1))
        other=CanonicalOptimizationEngine(self.root/'other',self.ledger)
        with self.assertRaises(OptimizationError):
            other.optimize('shared-name',replace(self.spec,windows=(replace(self.spec.windows[-1],role='SEARCH'),)))

    def test_timezone_fraction_overlap_guard(self):
        a=Window('a','2024-01-01T00:00:00Z','2024-01-01T01:00:00.500Z','SEARCH')
        b=Window('b','2024-01-01T01:00:00Z','2024-01-01T02:00:00Z','VALIDATION')
        with self.assertRaises(OptimizationError):
            replace(self.spec,windows=(a,b))

    def test_spec_hashable(self):
        self.assertEqual(hash(self.spec),hash(parse_spec(canonical(self.spec))))

    def test_live_output_root_prohibited(self):
        with self.assertRaises(OptimizationError):
            CanonicalOptimizationEngine(self.root/'runs'/'live',self.ledger)

    def test_freeze_strategy_identity(self):
        c={'candidate_status':'CANDIDATE','base_revision':'v1','strategy_id':'other','parameter_hash':parameter_hash(self.params)}
        self.ledger.freeze(c,'v2','test')
        with self.assertRaises(OptimizationError):
            self.ledger.require_freeze('v2',parameter_hash(self.params),self.spec.strategy_id)

    def test_operator_cli_round_trip(self):
        import contextlib
        import io
        from core.quant_optimization.__main__ import main
        path=self.root/'spec.json'
        atomic_json(path,replace(self.spec,n_trials=1))
        args=['--store',str(self.root/'cli'), '--governance',str(self.root/'cli-ledger')]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(args+['optimize','--spec',str(path),'--study-id','cli-study'])
        self.assertEqual(json.loads(output.getvalue())['status'],'COMPLETED')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(args+['status','--study-id','cli-study'])
        self.assertEqual(json.loads(output.getvalue())['n_trials_attempted'],1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(args+['results','--study-id','cli-study'])
        self.assertGreater(len(json.loads(output.getvalue())),0)

    def test_promotion_requires_separate_evidence_package(self):
        from core.quant_promotion import StrategyEvidencePackage
        _,candidates=self.engine.optimize('not-promotion',replace(self.spec,n_trials=1))
        with self.assertRaises(TypeError):
            StrategyEvidencePackage(**candidates[0])
