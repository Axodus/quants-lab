"""Durable research trials. Optuna samples; canonical Quant simulation owns economics."""
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4
import optuna
from core.quant_foundations.models import ExperimentDefinition
from core.quant_simulation import SimulationEngine, DeterministicExecutionModel, ExecutionAssumptionProfile
from core.quant_robustness.models import TrialResultRecord
from core.quant_robustness.robustness import ParameterRobustnessAnalyzer
from core.quant_validation.statistics import StatisticalAnalyzer
from .models import OptimizationError, OptimizationStudy, OptimizationTrial, canonical, decimal, digest, now, parameter_hash
from .objective import aggregate, economics, objective_value
from .provenance import EvidenceStore, atomic_json, exclusive, read_json, file_hash
from .data import load_states, approved_path
from .selection import RobustSelector
from .study import open_backend
from .strategy import default_registry, resolve


def implementation_hash():
    base = Path(__file__).parents[1]
    return digest({str(p.relative_to(base)):file_hash(p) for folder in ('quant_optimization','quant_simulation','quant_foundations','quant_validation','quant_robustness')
                   for p in sorted((base/folder).glob('*.py'))})


class CanonicalOptimizationEngine:
    def __init__(self, root_dir, governance, strategies=None):
        self.store = EvidenceStore(root_dir)
        self.governance = governance
        self.sim = SimulationEngine()
        self.strategies = strategies if strategies is not None else default_registry()
        self.version = implementation_hash()

    def _path(self, study_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',study_id):
            raise OptimizationError('invalid study identity')
        return self.store.root/'studies'/study_id

    def fingerprint(self,spec,params,dataset,window):
        return digest({'strategy':spec.strategy_id,'revision':spec.base_revision,'strategy_source':spec.strategy_source_hash,
                       'parameters':params,'dataset':canonical(replace(dataset,path='CONTENT_ADDRESSED')),
                       'window':window,'execution':spec.execution_model_ref,'fees':spec.fee_model_ref,
                       'slippage':spec.slippage_bps,'capital':spec.initial_capital,'notional':spec.notional,
                       'code_ref':spec.code_ref,'implementation':self.version,'objective':spec.objective,'constraints':spec.constraints})

    def evaluate(self,spec,params,dataset,window,run_id,*,use_cache=True):
        if dataset not in spec.dataset_refs or window not in spec.windows:
            raise OptimizationError('evaluation dataset/window outside frozen specification')
        params = spec.search_space.validate(params)
        approved_path(dataset)
        resolve(self.strategies,spec)
        if window.role == 'OOS':
            self.governance.require_freeze(spec.base_revision,parameter_hash(params),spec.strategy_id)
            use_cache = False
        fingerprint = self.fingerprint(spec,params,dataset,window)
        cache = self.store.root/'cache'/(fingerprint+'.json')
        # Verify input on every cache hit as well as on initial computation.
        if file_hash(dataset.path) != dataset.sha256:
            raise OptimizationError('dataset changed')
        self.governance.observe(spec.strategy_id,spec.base_revision,parameter_hash(params),dataset,window)
        if use_cache and cache.exists():
            index=read_json(cache)
            evidence=self.store.get(index['evidence'])
            if evidence['fingerprint'] != fingerprint:
                raise OptimizationError('cache identity mismatch')
            for ref in evidence['trial']['artifact_refs']:
                self.store.get(ref)
            return evidence,index['evidence'],True
        start=now()
        states=load_states(dataset,window)
        reference={'datasetId':dataset.dataset_id,'datasetVersion':dataset.revision,'contentDigest':dataset.sha256,'qualityStatus':'verified'}
        exp=ExperimentDefinition('optimization:'+fingerprint,'1',spec.base_revision,spec.strategy_source_hash,(reference,),(),params,
                                 {'execution_model':spec.execution_model_ref,'fee_model':spec.fee_model_ref},
                                 {'methodology_ref':'optimization-search-validation-v1','window':canonical(window)},
                                 {'seed':spec.seed},{'code_ref':spec.code_ref,'implementation_hash':self.version})
        profile=ExecutionAssumptionProfile(profile_id=spec.execution_model_ref,fee_bps=D(5),slippage_bps=decimal(spec.slippage_bps),latency_steps=1)
        adapter=resolve(self.strategies,spec).factory(params,spec.notional,spec.slippage_bps,len(states))
        result=self.sim.run(exp,exp.to_reference(),reference,states,{'revisionId':spec.base_revision},adapter,
                            DeterministicExecutionModel(profile),initial_capital=spec.initial_capital,run_id='trial:'+fingerprint)
        duration=(datetime.fromisoformat(window.end.replace('Z','+00:00'))-datetime.fromisoformat(window.start.replace('Z','+00:00')))
        metrics,trades=economics(result,D(duration.days)+D(duration.seconds)/86400+D(duration.microseconds)/D(86400000000))
        sim_ref=self.store.put(result.to_canonical_dict())
        experiment_ref=self.store.put(exp.to_canonical_dict())
        trades_ref=self.store.put({'trades':trades,'liquidity_role':'TAKER','commission_provenance':'MODELLED',
                                  'fee_model':{'maker_bps':'2','taker_bps':'5','applied_role':'TAKER'},
                                  'limitations':['CANDLE_NEXT_CLOSE_EXECUTION','NO_QUEUE_MODEL','FUNDING_NOT_MODELLED','RESEARCH_ONLY']})
        constraints={c.metric:c.evaluate(metrics) for c in spec.constraints}
        individual_objective = None
        try:
            individual_objective = canonical(objective_value(spec.objective,metrics))
        except OptimizationError:
            pass
        trial=OptimizationTrial(fingerprint,run_id,spec.strategy_id,spec.base_revision,params,parameter_hash(params),canonical(dataset),dataset.symbol,
                                canonical(window),spec.execution_model_ref,spec.fee_model_ref,'COMPLETE' if all(constraints.values()) else 'INVALID_FOR_SELECTION',
                                start,now(),metrics,individual_objective,constraints,(sim_ref,experiment_ref,trades_ref),{'implementation_hash':self.version,'code_ref':spec.code_ref})
        evidence={'fingerprint':fingerprint,'trial':canonical(trial),'simulation_digest':result.result_digest}
        ref=self.store.put(evidence)
        atomic_json(cache,{'evidence':ref})
        return evidence,ref,False

    def _evaluate_set(self,spec,params,role,run_id):
        evaluations=[]
        refs=[]
        constraints={c.metric:True for c in spec.constraints}
        hits=0
        for dataset in spec.dataset_refs:
            for window in spec.windows:
                if window.role != role:
                    continue
                evidence,ref,hit=self.evaluate(spec,params,dataset,window,run_id)
                trial=evidence['trial']
                evaluations.append({'symbol':dataset.symbol,'window_ref':canonical(window),'metrics':trial['metrics']})
                refs.append(ref)
                hits+=hit
                for key,value in trial['constraint_results'].items():
                    constraints[key] &= value
        if not evaluations:
            raise OptimizationError('missing role evaluations')
        metrics,score,cross,temporal=aggregate(evaluations,spec.objective)
        metrics['objective_value']=score
        return {'metrics':metrics,'score':score,'cross':cross,'temporal':temporal,'constraints':constraints,'refs':refs,'cache_hits':hits}

    def optimize(self,study_id,spec,*,max_new_trials=None):
        path=self._path(study_id)
        resolve(self.strategies,spec)
        with exclusive(path/'owner.lock'):
            manifest_path=path/'study.json'
            if manifest_path.exists():
                manifest=read_json(manifest_path)
                if manifest['optimization_spec_hash'] != spec.spec_hash or manifest['implementation_hash'] != self.version:
                    raise OptimizationError('immutable study spec/code changed')
                if manifest['status']=='COMPLETED':
                    results=read_json(path/'results.json')
                    if self.store.get(manifest['results_ref']) != results:
                        raise OptimizationError('completed results checksum mismatch')
                    for candidate in results:
                        for ref in candidate['search_evidence_refs']+candidate['validation_evidence_refs']:
                            trial=self.store.get(ref)['trial']
                            for artifact in trial['artifact_refs']:
                                self.store.get(artifact)
                    return manifest,results
            else:
                run_id=str(uuid4())
                self.governance.reserve(digest({"store":str(self.store.root),"study":study_id,"spec":spec.spec_hash}),spec)
                study=OptimizationStudy(study_id,run_id,spec.strategy_id,spec.base_revision,spec.spec_hash,now(),spec.sampler,spec.seed,
                                        spec.n_trials,0,tuple(canonical(d) for d in spec.dataset_refs),tuple(canonical(w) for w in spec.windows),
                                        canonical(spec.objective),tuple(canonical(c) for c in spec.constraints),'RUNNING')
                manifest=dict(canonical(study),implementation_hash=self.version,sampler_version=optuna.__version__,
                              sampler_resume_policy='RESEEDED_SEARCH_ORDER_NOT_GUARANTEED; INDIVIDUAL_TRIAL_REPLAY_EXACT')
                atomic_json(path/'spec.json',spec)
                atomic_json(manifest_path,manifest)
            backend=open_backend(self.store.root,study_id,spec)
            if not backend.trials:
                for params in spec.initial_parameter_sets:
                    backend.enqueue_trial(canonical(params))
            # Interrupted sampling slots become auditable failures, never silently completed.
            for t in backend.trials:
                if t.state==optuna.trial.TrialState.RUNNING:
                    backend.tell(t.number,state=optuna.trial.TrialState.FAIL)
                    interrupted_path=path/'trials'/f'{t.number:06d}.json'
                    interrupted=read_json(interrupted_path) if interrupted_path.exists() else {'number':t.number}
                    interrupted.update(status='FAILED',error='PROCESS_INTERRUPTED',completed_at=now())
                    atomic_json(interrupted_path,interrupted)
            attempted=sum(t.state!=optuna.trial.TrialState.WAITING for t in backend.trials)
            remaining=max(0,spec.n_trials-attempted)
            if max_new_trials is not None and (type(max_new_trials) is not int or max_new_trials <= 0):
                raise OptimizationError('positive bounded trial budget required')
            budget=remaining if max_new_trials is None else min(remaining,max_new_trials)
            def objective(trial):
                record={'number':trial.number,'status':'RUNNING','started_at':now()}
                target=path/'trials'/f'{trial.number:06d}.json'
                atomic_json(target,record)
                try:
                    params=spec.search_space.validate({p.name:trial.suggest_categorical(p.name,list(p.values)) for p in spec.search_space.parameters})
                    record.update(parameter_set=params,parameter_hash=parameter_hash(params))
                    value=self._evaluate_set(spec,params,'SEARCH',manifest['optimization_run_id'])
                    record.update(value,status='COMPLETE' if all(value['constraints'].values()) else 'INVALID_FOR_SELECTION',completed_at=now())
                    atomic_json(target,record)
                    trial.set_user_attr('canonical_record',str(target.relative_to(self.store.root)))
                    if record['status']!='COMPLETE':
                        raise optuna.TrialPruned('hard constraints violated')
                    return float(decimal(value['score'])) # backend suggestion only; never economic authority
                except optuna.TrialPruned:
                    raise
                except OptimizationError as exc:
                    record.update(status='INVALID',error=str(exc),completed_at=now())
                    atomic_json(target,record)
                    raise optuna.TrialPruned('canonical trial invalid') from exc
                except Exception:
                    record.update(status='FAILED',error='SIMULATION_FAILURE',completed_at=now())
                    atomic_json(target,record)
                    raise
            if budget:
                try:
                    backend.optimize(objective,n_trials=budget)
                except BaseException:
                    manifest.update(status='INTERRUPTED',interrupted_at=now())
                    atomic_json(manifest_path,manifest)
                    raise
            manifest['n_trials_completed']=sum(t.state==optuna.trial.TrialState.COMPLETE for t in backend.trials)
            manifest['n_trials_attempted']=sum(t.state!=optuna.trial.TrialState.WAITING for t in backend.trials)
            for t in backend.trials:
                target=path/'trials'/f'{t.number:06d}.json'
                if t.state==optuna.trial.TrialState.FAIL and target.exists():
                    record=read_json(target)
                    if record['status']=='RUNNING':
                        record.update(status='FAILED',error='PROCESS_INTERRUPTED',completed_at=now())
                        atomic_json(target,record)
            records=[read_json(p) for p in sorted((path/'trials').glob('*.json'))]
            if manifest['n_trials_attempted']<spec.n_trials and not (spec.sampler=='GRID' and backend.sampler.is_exhausted(backend)):
                manifest['status']='PAUSED'
                atomic_json(manifest_path,manifest)
                return manifest,[]
            unique={r['parameter_hash']:r for r in records if r['status'] in {'COMPLETE','INVALID_FOR_SELECTION'}}
            ranked=sorted(unique.values(),key=lambda r:decimal(r['score']),reverse=spec.objective.direction=='MAXIMIZE')
            # Shortlist is frozen BEFORE any validation results are observed.
            shortlist_path=path/'validation_shortlist.json'
            if shortlist_path.exists():
                shortlist=read_json(shortlist_path)
            else:
                shortlist=[r['parameter_hash'] for r in ranked if r['status']=='COMPLETE'][:spec.validation_candidates]
                atomic_json(shortlist_path,shortlist)
            candidates=[]
            for r in ranked:
                validation=None
                if r['parameter_hash'] in shortlist and any(w.role=='VALIDATION' for w in spec.windows):
                    validation=self._evaluate_set(spec,r['parameter_set'],'VALIDATION',manifest['optimization_run_id'])
                constraints=dict(r['constraints'])
                if validation:
                    constraints.update({'validation:'+k:v for k,v in validation['constraints'].items()})
                    constraints['validation_net_positive']=decimal(validation['metrics']['net_pnl'])>0
                    constraints['validation_cross_symbol']=decimal(validation['cross']['positive_symbol_ratio'])>=D('0.75')
                    constraints['validation_temporal']=decimal(validation['temporal']['positive_window_ratio'])>=D('0.75')
                candidates.append({'strategy_id':spec.strategy_id,'base_revision':spec.base_revision,'parameter_set':r['parameter_set'],
                                   'parameter_hash':r['parameter_hash'],'optimization_run_id':manifest['optimization_run_id'],
                                   'search_evidence_refs':r['refs'],'validation_evidence_refs':validation['refs'] if validation else [],
                                   'objective_metrics':r['metrics'],'constraint_results':constraints,'robustness_metrics':{},
                                   'cross_symbol_metrics':r['cross'],'temporal_metrics':r['temporal'],'selection_reason':'','candidate_status':'INSUFFICIENT_EVIDENCE'})
            results=RobustSelector(spec.search_space).select(candidates,spec.objective.direction)
            robust_trials=[TrialResultRecord(r['parameter_hash'],study_id,'1',spec.base_revision,{}, {},r['parameter_set'],'COMPLETED',
                           {'objective':canonical(decimal(r['score'])*(1 if spec.objective.direction=='MAXIMIZE' else -1))}) for r in ranked]
            robustness=ParameterRobustnessAnalyzer().analyze(study_id,'1',spec.base_revision,{},spec.search_space.robustness_space(),robust_trials,
                        sum(r['status']=='INVALID' for r in records),primary_metric='objective')
            robust_ref=self.store.put(robustness.to_canonical_dict())
            diagnostics=StatisticalAnalyzer.analyze_series('search_objective',[decimal(r['score']) for r in ranked]).to_canonical_dict()
            atomic_json(path/'results.json',results)
            atomic_json(path/'best_trials.json',[r for r in ranked if r['status']=='COMPLETE'])
            manifest.update(status='COMPLETED',completed_at=now(),unique_parameter_sets=len(unique),
                            robustness_ref=robust_ref,statistical_diagnostics_ref=self.store.put(diagnostics),
                            results_ref=self.store.put(results),deployment_authorized=False,oos_consumed=False)
            atomic_json(manifest_path,manifest)
            return manifest,results

    def evaluate_oos(self,spec,params,dataset,window,revision,run_id):
        if window.role!='OOS':
            raise OptimizationError('OOS window required')
        if dataset not in spec.dataset_refs or window not in spec.windows:
            raise OptimizationError('OOS dataset/window outside frozen specification')
        self.governance.require_freeze(revision,parameter_hash(params),spec.strategy_id)
        # evaluate reserves the holdout before opening market inputs; cache forbidden.
        frozen_spec=replace(spec,base_revision=revision)
        definition=resolve(self.strategies,spec)
        from .strategy import StrategyDefinition
        self.strategies[(spec.strategy_id,revision)]=StrategyDefinition(spec.strategy_id,revision,definition.implementation_hash,definition.factory)
        return self.evaluate(frozen_spec,params,dataset,window,run_id,use_cache=False)
