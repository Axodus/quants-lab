"""Explicit research plugin registry; no dynamic imports from operator configuration."""
from dataclasses import dataclass
from typing import Callable
from .ema import STRATEGY_ID, BASE_REVISION, EMAPullbackResearchAdapter, source_hash
from .models import OptimizationError


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    base_revision: str
    implementation_hash: str
    factory: Callable


def default_registry():
    definition=StrategyDefinition(STRATEGY_ID,BASE_REVISION,source_hash(),EMAPullbackResearchAdapter)
    return {(definition.strategy_id,definition.base_revision):definition}


def resolve(registry,spec):
    definition=registry.get((spec.strategy_id,spec.base_revision))
    if definition is None or definition.implementation_hash != spec.strategy_source_hash:
        raise OptimizationError('unsupported or mutated strategy implementation')
    return definition
