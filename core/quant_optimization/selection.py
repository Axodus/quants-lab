"""Selection of research candidates from observed parameter neighborhoods."""
from decimal import Decimal as D
from .models import canonical, decimal, ParameterCandidate
from .objective import distribution


class RobustSelector:
    def __init__(self, space):
        self.space = space

    def distance(self, left, right):
        return sum((int(left[p.name]!=right[p.name]) if p.kind in {'categorical','boolean'}
                    else abs(p.values.index(left[p.name])-p.values.index(right[p.name]))) for p in self.space.parameters)

    def select(self, population, direction='MAXIMIZE'):
        population = list({c['parameter_hash']:c for c in population}.values())
        selected = []
        for c in population:
            neighbors = [n for n in population if self.distance(c['parameter_set'],n['parameter_set']) == 1]
            values = [decimal(n['objective_metrics']['objective_value']) for n in neighbors]
            positive = [n for n in neighbors if decimal(n['objective_metrics']['net_pnl']) > 0 and all(n['constraint_results'].values())]
            metrics = {'neighbor_count':len(neighbors),
                       'neighbor_positive_ratio':canonical(D(len(positive))/len(neighbors)) if neighbors else None,
                       'objective_dispersion':distribution(values).get('dispersion'),
                       'parameter_sensitivity':canonical(max((abs(decimal(c['objective_metrics']['objective_value'])-v) for v in values),default=D(0)))}
            if not all(c['constraint_results'].values()) or decimal(c['objective_metrics']['net_pnl']) <= 0:
                status,reason='REJECTED','ECONOMIC_OR_CONSTRAINT_FAILURE'
            elif not c['validation_evidence_refs'] or len(neighbors) < 2:
                status,reason='INSUFFICIENT_EVIDENCE','VALIDATION_OR_NEIGHBORHOOD_INSUFFICIENT'
            elif (values and abs(decimal(c['objective_metrics']['objective_value'])-sum(values,D(0))/len(values)) >
                  max(abs(decimal(c['objective_metrics']['objective_value'])),D('1e-28'))/2):
                status,reason='FRAGILE','OBJECTIVE_NEIGHBORHOOD_SENSITIVITY'
            elif len(positive)*4 < len(neighbors)*3:
                status,reason='FRAGILE','ISOLATED_POSITIVE_POINT'
            elif (decimal(c['cross_symbol_metrics']['positive_symbol_ratio']) < D('0.75') or
                  decimal(c['temporal_metrics']['positive_window_ratio']) < D('0.75')):
                status,reason='FRAGILE','SYMBOL_OR_TEMPORAL_INSTABILITY'
            else:
                status,reason='CANDIDATE','ROBUST_REGION_REQUIRES_NEW_REVISION_AND_FRESH_OOS'
            selected.append(canonical(ParameterCandidate(**dict(c,robustness_metrics=metrics,candidate_status=status,selection_reason=reason))))
        selected.sort(key=lambda c:decimal(c['objective_metrics']['objective_value']),reverse=direction=='MAXIMIZE')
        selected.sort(key=lambda c:{'CANDIDATE':0,'FRAGILE':1,'INSUFFICIENT_EVIDENCE':2,'REJECTED':3}[c['candidate_status']])
        return selected
