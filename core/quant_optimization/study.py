"""Strict serialization and an Optuna-only sampling adapter."""
import optuna
from .models import OptimizationSpec, DatasetRef, Window, Objective, Constraint
from .search_space import SearchSpace, Parameter


def parse_spec(raw):
    raw = dict(raw)
    raw['dataset_refs'] = tuple(DatasetRef(**v) for v in raw['dataset_refs'])
    raw['windows'] = tuple(Window(**v) for v in raw['windows'])
    raw['objective'] = Objective(**raw['objective'])
    raw['constraints'] = tuple(Constraint(**v) for v in raw['constraints'])
    raw['search_space'] = SearchSpace(tuple(Parameter(**p) for p in raw['search_space']['parameters']),tuple(raw['search_space'].get('less_than',())))
    return OptimizationSpec(**raw)


def open_backend(root, study_id, spec):
    choices = {p.name:list(p.values) for p in spec.search_space.parameters}
    if spec.sampler == 'TPE':
        sampler = optuna.samplers.TPESampler(seed=spec.seed)
    elif spec.sampler == 'RANDOM':
        sampler = optuna.samplers.RandomSampler(seed=spec.seed)
    else:
        sampler = optuna.samplers.GridSampler(choices,seed=spec.seed)
    return optuna.create_study(study_name=study_id,storage='sqlite:///'+str(root/'optuna.sqlite3'),
                               sampler=sampler,direction=spec.objective.direction.lower(),load_if_exists=True)
