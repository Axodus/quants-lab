"""Strategy-declared, finite exact search spaces; no Optuna domain objects."""
from dataclasses import dataclass
from decimal import Decimal

from core.quant_robustness.models import ParameterDefinition, ParameterSpace
from .models import OptimizationError, canonical, decimal


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str
    domain: str
    values: tuple
    default: object

    def __post_init__(self):
        object.__setattr__(self, 'values', tuple(self.values))
        if not self.name or self.kind not in {'integer', 'decimal', 'categorical', 'boolean'}:
            raise OptimizationError('invalid parameter')
        if self.domain not in {'SIGNAL', 'RISK', 'EXECUTION', 'CONTROL'}:
            raise OptimizationError('invalid parameter domain')
        if not self.values or len(self.values) > 10000:
            raise OptimizationError('bounded parameter values required')
        values = self.values
        if self.kind == 'integer' and any(type(v) is not int for v in values):
            raise OptimizationError('integer values required')
        if self.kind == 'boolean' and any(type(v) is not bool for v in values):
            raise OptimizationError('boolean values required')
        if self.kind == 'categorical' and any(not isinstance(v, str) for v in values):
            raise OptimizationError('string categories required')
        if self.kind == 'decimal':
            object.__setattr__(self, 'values', tuple(canonical(decimal(v)) for v in values))
            object.__setattr__(self, 'default', canonical(decimal(self.default)))
        if len(set(self.values)) != len(self.values) or self.default not in self.values:
            raise OptimizationError('duplicate values or invalid default')


def IntegerParameter(name, minimum, maximum, step=1, default=None, domain='SIGNAL'):
    if any(type(v) is not int for v in (minimum, maximum, step)) or step <= 0 or minimum > maximum:
        raise OptimizationError('invalid integer range')
    if (maximum - minimum) // step > 9999:
        raise OptimizationError('range exceeds bound')
    return Parameter(name, 'integer', domain, tuple(range(minimum, maximum + 1, step)), minimum if default is None else default)


def DecimalParameter(name, minimum, maximum, step, default=None, domain='SIGNAL'):
    lo, hi, delta = map(decimal, (minimum, maximum, step))
    if delta <= 0 or lo > hi or (hi - lo) / delta > 9999:
        raise OptimizationError('invalid decimal range')
    values = tuple(canonical(lo + i * delta) for i in range(int((hi-lo)/delta) + 1))
    return Parameter(name, 'decimal', domain, values, canonical(lo if default is None else decimal(default)))


def CategoricalParameter(name, choices, default, domain='SIGNAL'):
    return Parameter(name, 'categorical', domain, tuple(choices), default)


def BooleanParameter(name, default=False, domain='CONTROL'):
    if type(default) is not bool:
        raise OptimizationError('invalid boolean default')
    return Parameter(name, 'boolean', domain, (False, True), default)


@dataclass(frozen=True)
class SearchSpace:
    parameters: tuple[Parameter, ...]
    less_than: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'parameters', tuple(self.parameters))
        object.__setattr__(self, 'less_than', tuple(tuple(v) for v in self.less_than))
        names = [p.name for p in self.parameters]
        if not names or len(set(names)) != len(names):
            raise OptimizationError('empty/duplicate parameters')
        if any(a not in names or b not in names for a, b in self.less_than):
            raise OptimizationError('unknown constraint parameter')

    def validate(self, params):
        if set(params) != {p.name for p in self.parameters}:
            raise OptimizationError('parameter names mismatch')
        normalized = {}
        for p in self.parameters:
            value = params[p.name]
            if p.kind == 'decimal':
                value = canonical(decimal(value))
            if p.kind == 'integer' and type(value) is not int or p.kind == 'boolean' and type(value) is not bool:
                raise OptimizationError('parameter type mismatch')
            if value not in p.values:
                raise OptimizationError('parameter outside declared search space')
            normalized[p.name] = value
        if any(decimal(normalized[a]) >= decimal(normalized[b]) for a, b in self.less_than):
            raise OptimizationError('invalid parameter combination')
        return normalized

    def robustness_space(self):
        return ParameterSpace(tuple(ParameterDefinition(p.name, p.kind, p.values, default_value=p.default) for p in self.parameters))

    @property
    def cardinality(self):
        result = 1
        for p in self.parameters:
            result *= len(p.values)
        return result
