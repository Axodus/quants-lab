"""Immutable research contracts; no execution or promotion authority."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from collections.abc import Mapping
from typing import Any

from core.quant_foundations.canonical import sha256_digest


class OptimizationError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def decimal(value):
    if isinstance(value, (float, bool)):
        raise OptimizationError('authoritative decimals require strings, integers or Decimal')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OptimizationError('invalid decimal') from exc
    if not result.is_finite():
        raise OptimizationError('non-finite decimal')
    return result


def canonical(value):
    if is_dataclass(value):
        return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(v) for v in value]
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f') if value else '0'
    if isinstance(value, float):
        raise OptimizationError('float in canonical evidence')
    return value


def immutable(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: immutable(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(immutable(v) for v in value)
    return value


def digest(value):
    return sha256_digest(canonical(value))


def parameter_hash(params):
    return digest(params)


@dataclass(frozen=True)
class Window:
    name: str
    start: str
    end: str
    role: str

    def __post_init__(self):
        if self.role not in {'SEARCH', 'VALIDATION', 'OOS'} or not self.name:
            raise OptimizationError('invalid window role/name')
        for key in ('start', 'end'):
            dt = datetime.fromisoformat(getattr(self, key).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise OptimizationError('UTC-aware window required')
            object.__setattr__(self, key, dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'))
        if datetime.fromisoformat(self.start.replace('Z', '+00:00')) >= datetime.fromisoformat(self.end.replace('Z', '+00:00')):
            raise OptimizationError('window must be nonempty and half-open')


@dataclass(frozen=True)
class DatasetRef:
    dataset_id: str
    revision: str
    symbol: str
    path: str
    sha256: str
    provenance: str
    oos_eligible: bool = False

    def __post_init__(self):
        if not all((self.dataset_id, self.revision, self.symbol, self.path, self.provenance)):
            raise OptimizationError('incomplete dataset reference')
        if len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256):
            raise OptimizationError('dataset SHA256 required')
        if type(self.oos_eligible) is not bool:
            raise OptimizationError('oos_eligible must be boolean')
        if self.oos_eligible and self.provenance != 'SEALED_UNOBSERVED':
            raise OptimizationError('known/synthetic research data cannot become fresh OOS')


@dataclass(frozen=True)
class Objective:
    name: str = 'NET_EXPECTANCY'
    direction: str = 'MAXIMIZE'

    def __post_init__(self):
        if self.name not in {'NET_EXPECTANCY', 'NET_PNL', 'PROFIT_FACTOR', 'SHARPE', 'MAX_DRAWDOWN', 'RETURN_OVER_DRAWDOWN'}:
            raise OptimizationError('unknown objective')
        if self.direction not in {'MAXIMIZE', 'MINIMIZE'}:
            raise OptimizationError('unknown objective direction')
        if self.name == 'MAX_DRAWDOWN' and self.direction != 'MINIMIZE':
            raise OptimizationError('drawdown must be minimized')


@dataclass(frozen=True)
class Constraint:
    metric: str
    minimum: str | None = None
    maximum: str | None = None
    require_positive: bool = False

    def __post_init__(self):
        if self.metric not in {'net_pnl','gross_pnl','fees','trade_count','net_expectancy','trades_per_day','profit_factor','max_drawdown','max_drawdown_bps','sharpe','return_over_drawdown','slippage'}:
            raise OptimizationError('unsupported constraint metric')
        if type(self.require_positive) is not bool:
            raise OptimizationError('require_positive must be boolean')
        for value in (self.minimum, self.maximum):
            if value is not None:
                decimal(value)
        if self.minimum is not None and self.maximum is not None and decimal(self.minimum) > decimal(self.maximum):
            raise OptimizationError('invalid constraint bounds')

    def evaluate(self, metrics):
        value = metrics.get(self.metric)
        if value is None:
            return False
        value = decimal(value)
        return ((self.minimum is None or value >= decimal(self.minimum))
                and (self.maximum is None or value <= decimal(self.maximum))
                and (not self.require_positive or value > 0))


@dataclass(frozen=True)
class OptimizationSpec:
    strategy_id: str
    base_revision: str
    strategy_source_hash: str
    dataset_refs: tuple[DatasetRef, ...]
    symbols: tuple[str, ...]
    windows: tuple[Window, ...]
    search_space: Any
    objective: Objective
    constraints: tuple[Constraint, ...]
    n_trials: int
    seed: int
    code_ref: str
    sampler: str = 'TPE'
    execution_model_ref: str = 'causal-next-close-taker-v1'
    fee_model_ref: str = 'binance-usdm-maker2-taker5-bps-v1'
    slippage_bps: str = '1'
    initial_capital: str = '1000'
    notional: str = '100'
    validation_candidates: int = 3
    initial_parameter_sets: tuple[Mapping, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'initial_parameter_sets', tuple(immutable(self.search_space.validate(p)) for p in self.initial_parameter_sets))
        if len(self.initial_parameter_sets) > self.n_trials:
            raise OptimizationError('initial parameter sets exceed trial budget')
        for key in ('dataset_refs', 'symbols', 'windows', 'constraints'):
            object.__setattr__(self, key, tuple(getattr(self, key)))
        if not self.strategy_id or not self.base_revision or not self.code_ref or len(self.strategy_source_hash) != 64 or any(c not in '0123456789abcdef' for c in self.strategy_source_hash):
            raise OptimizationError('strategy/code provenance required')
        if type(self.n_trials) is not int or not 0 < self.n_trials <= 100000:
            raise OptimizationError('invalid trial budget')
        if type(self.seed) is not int or self.seed < 0 or self.sampler not in {'TPE', 'RANDOM', 'GRID'}:
            raise OptimizationError('invalid sampler/seed')
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise OptimizationError('unique symbols required')
        if set(d.symbol for d in self.dataset_refs) != set(self.symbols) or len(self.dataset_refs) != len(self.symbols):
            raise OptimizationError('exact per-symbol dataset reference required')
        if self.execution_model_ref != 'causal-next-close-taker-v1' or self.fee_model_ref != 'binance-usdm-maker2-taker5-bps-v1':
            raise OptimizationError('unsupported execution/fee model; maker fills require a separately qualified model')
        if decimal(self.slippage_bps) < 0 or decimal(self.initial_capital) <= 0 or decimal(self.notional) <= 0:
            raise OptimizationError('invalid economic assumptions')
        if type(self.validation_candidates) is not int or self.validation_candidates <= 0:
            raise OptimizationError('invalid validation budget')
        if not any(w.role == 'SEARCH' for w in self.windows):
            raise OptimizationError('SEARCH window required')
        if len({w.name for w in self.windows}) != len(self.windows):
            raise OptimizationError('duplicate window names')
        if len({c.metric for c in self.constraints}) != len(self.constraints):
            raise OptimizationError('duplicate constraint metric')
        ordered = sorted(self.windows, key=lambda w: datetime.fromisoformat(w.start.replace('Z', '+00:00')))
        roles = {'SEARCH': 0, 'VALIDATION': 1, 'OOS': 2}
        for a, b in zip(ordered, ordered[1:]):
            if datetime.fromisoformat(a.end.replace('Z', '+00:00')) > datetime.fromisoformat(b.start.replace('Z', '+00:00')) or roles[a.role] > roles[b.role]:
                raise OptimizationError('overlapping or out-of-order research roles')

    def __hash__(self):
        return int(self.spec_hash[:16], 16)

    @property
    def spec_hash(self):
        return digest(self)


@dataclass(frozen=True)
class OptimizationTrial:
    trial_id: str
    optimization_run_id: str
    strategy_id: str
    base_revision: str
    parameter_set: Mapping
    parameter_hash: str
    dataset_ref: Mapping
    symbol: str
    window_ref: Mapping
    execution_model_ref: str
    fee_model_ref: str
    status: str
    started_at: str
    completed_at: str | None = None
    metrics: Mapping | None = None
    objective_value: str | None = None
    constraint_results: Mapping | None = None
    artifact_refs: tuple[str, ...] = ()
    provenance: Mapping | None = None

    def __post_init__(self):
        if self.status not in {'PENDING', 'RUNNING', 'COMPLETE', 'FAILED', 'PRUNED', 'INVALID', 'INVALID_FOR_SELECTION'}:
            raise OptimizationError('invalid trial status')
        for key in ('parameter_set', 'dataset_ref', 'window_ref', 'metrics', 'constraint_results', 'provenance'):
            object.__setattr__(self, key, immutable(getattr(self, key)))
        object.__setattr__(self, 'artifact_refs', tuple(self.artifact_refs))


@dataclass(frozen=True)
class OptimizationStudy:
    study_id: str
    optimization_run_id: str
    strategy_id: str
    base_revision: str
    optimization_spec_hash: str
    created_at: str
    sampler: str
    seed: int
    n_trials_requested: int
    n_trials_completed: int
    dataset_refs: tuple
    window_refs: tuple
    objective: Mapping
    constraints: tuple
    status: str
    completed_at: str | None = None

    def __post_init__(self):
        for key in ('dataset_refs', 'window_refs', 'objective', 'constraints'):
            object.__setattr__(self, key, immutable(getattr(self, key)))


@dataclass(frozen=True)
class ParameterCandidate:
    strategy_id: str
    base_revision: str
    parameter_set: Mapping
    parameter_hash: str
    optimization_run_id: str
    search_evidence_refs: tuple
    validation_evidence_refs: tuple
    objective_metrics: Mapping
    constraint_results: Mapping
    robustness_metrics: Mapping
    cross_symbol_metrics: Mapping
    temporal_metrics: Mapping
    selection_reason: str
    candidate_status: str

    def __post_init__(self):
        if self.candidate_status not in {'CANDIDATE', 'REJECTED', 'FRAGILE', 'INSUFFICIENT_EVIDENCE'}:
            raise OptimizationError('invalid candidate state')
        for key in ('parameter_set', 'objective_metrics', 'constraint_results', 'robustness_metrics', 'cross_symbol_metrics', 'temporal_metrics'):
            object.__setattr__(self, key, immutable(getattr(self, key)))
        for key in ('search_evidence_refs', 'validation_evidence_refs'):
            object.__setattr__(self, key, tuple(getattr(self, key)))
