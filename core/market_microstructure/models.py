"""Feature configuration and immutable, content-addressed evidence."""
from dataclasses import dataclass
from collections.abc import Mapping
from core.quant_optimization.models import canonical, digest, immutable, decimal

SCHEMA_VERSION = 'microstructure-v1.1'
HORIZONS = (100, 250, 500, 1000, 2000, 5000, 10000)


@dataclass(frozen=True)
class FeatureConfig:
    horizons: tuple = HORIZONS
    depth: int = 10
    stale_after_ms: int = 10000
    context_ms: int = 60000
    max_window_events: int = 250000
    sweep_min_levels: int = 2
    sweep_max_duration_ms: int = 250
    weights: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'horizons', tuple(self.horizons))
        if not self.horizons or any(type(x) is not int or x <= 0 for x in self.horizons) or len(set(self.horizons)) != len(self.horizons):
            raise ValueError('unique positive horizons required')
        if self.depth not in {10, 20, 50} or self.stale_after_ms <= 0 or self.context_ms < max(self.horizons):
            raise ValueError('invalid depth/staleness/context')
        if self.max_window_events < 2 or self.sweep_min_levels < 2 or self.sweep_max_duration_ms <= 0:
            raise ValueError('invalid bounded-state/sweep policy')
        weights = tuple(decimal(x) for x in self.weights) if self.weights else tuple(decimal(1) / (i + 1) for i in range(self.depth))
        if len(weights) != self.depth or any(w <= 0 for w in weights):
            raise ValueError('one positive weight per depth level required')
        object.__setattr__(self, 'weights', weights)


@dataclass(frozen=True)
class FeatureProvenance:
    dataset_hash: str
    instrument_spec_hash: str
    code_hash: str
    config_hash: str
    source_range_hash: str
    source_refs: tuple
    window_start: int
    window_end: int
    effective_resolution_ms: int
    limitations: tuple = ('L2_QUEUE_UNKNOWN', 'HIDDEN_LIQUIDITY_UNKNOWN', 'REMOVALS_NOT_PROVEN_CANCELS',
                         'WEIGHTED_MID_PROXY_NOT_CALIBRATED_STOIKOV_MICROPRICE')


@dataclass(frozen=True)
class MicrostructureFeatureSnapshot:
    symbol: str
    venue: str
    market_type: str
    timestamp: int
    book_sequence: int
    horizon: int
    source_state_hash: str
    provenance: FeatureProvenance
    features: Mapping
    feature_schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, 'features', immutable(self.features))
        if self.provenance.window_end != self.timestamp or self.horizon <= 0:
            raise ValueError('inconsistent feature window')

    @property
    def content_hash(self):
        return digest(self)

    def to_dict(self):
        return canonical(self)
