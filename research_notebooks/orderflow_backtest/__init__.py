"""
Order Flow Backtesting harness for Axodus / Quants Lab.

This package implements a 90-day high-frequency backtest engine that evaluates
three institutional Order Flow strategies against synthetic or historical
tick data with exact economic fee and slippage accounting.

Public surface:
    from orderflow_backtest import run_backtest, BacktestRunner
"""

from .data_fixture import generate_synthetic_ticks
from .engine import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    FeeModel,
    MetricCalculator,
    PerformanceReport,
    run_backtest,
)
from .strategies import (
    AggressionMomentumScalper,
    InstitutionalAbsorptionFade,
    CVDDivergenceReversal,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BacktestRunner",
    "FeeModel",
    "MetricCalculator",
    "PerformanceReport",
    "run_backtest",
    "generate_synthetic_ticks",
    "AggressionMomentumScalper",
    "InstitutionalAbsorptionFade",
    "CVDDivergenceReversal",
]

__version__ = "1.0.0"
