"""Explicit research plugin registry; no dynamic imports from operator configuration."""
from dataclasses import dataclass
from typing import Callable
from .ema import STRATEGY_ID, BASE_REVISION, EMAPullbackResearchAdapter, source_hash
from core.quant_strategies.microstructure.microprice_displacement import (
    BASE_REVISION as MICROPRICE_BASE_REVISION,
    STRATEGY_ID as MICROPRICE_STRATEGY_ID,
    MicropriceDisplacementStrategy,
    source_hash as microprice_source_hash,
)
from core.quant_strategies.microtrend import (
    BASE_REVISION as MICROTREND_BASE_REVISION,
    STRATEGY_ID as MICROTREND_STRATEGY_ID,
    MicrotrendScalperStrategy,
    source_hash as microtrend_source_hash,
)
from .models import OptimizationError


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    base_revision: str
    implementation_hash: str
    factory: Callable


def default_registry():
    ema_definition = StrategyDefinition(STRATEGY_ID, BASE_REVISION, source_hash(), EMAPullbackResearchAdapter)
    microprice_definition = StrategyDefinition(
        MICROPRICE_STRATEGY_ID,
        MICROPRICE_BASE_REVISION,
        microprice_source_hash(),
        MicropriceDisplacementStrategy.from_parameters,
    )
    microtrend_definition = StrategyDefinition(
        MICROTREND_STRATEGY_ID,
        MICROTREND_BASE_REVISION,
        microtrend_source_hash(),
        MicrotrendScalperStrategy.from_parameters,
    )
    return {
        (ema_definition.strategy_id, ema_definition.base_revision): ema_definition,
        (microprice_definition.strategy_id, microprice_definition.base_revision): microprice_definition,
        (microtrend_definition.strategy_id, microtrend_definition.base_revision): microtrend_definition,
    }


def resolve(registry,spec):
    definition=registry.get((spec.strategy_id,spec.base_revision))
    if definition is None or definition.implementation_hash != spec.strategy_source_hash:
        raise OptimizationError('unsupported or mutated strategy implementation')
    return definition
