"""Deterministic backtest and simulation foundations."""

from .engine import SimulationEngine, SimulationInvalidatedError, SimulationValidationError
from .execution import DeterministicExecutionModel, ExecutionModel
from .models import (
    ClosedTradeResult,
    ExecutionAssumptionProfile,
    NoActionStrategy,
    ReferenceThresholdStrategy,
    SimulatedDecision,
    SimulationResult,
)

__all__ = [
    "ClosedTradeResult",
    "DeterministicExecutionModel",
    "ExecutionAssumptionProfile",
    "ExecutionModel",
    "NoActionStrategy",
    "ReferenceThresholdStrategy",
    "SimulatedDecision",
    "SimulationEngine",
    "SimulationInvalidatedError",
    "SimulationResult",
    "SimulationValidationError",
]
