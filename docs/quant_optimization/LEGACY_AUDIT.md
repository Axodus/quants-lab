# Legacy audit and migration

Inspected before completing the canonical implementation. No legacy source, tasks, scheduler config, dependency declaration,
or existing simulation/validation/robustness/promotion module was changed.

| Component | Classification | Disposition |
|---|---|---|
| `core/backtesting/optimizer.py::StrategyOptimizer` | ADAPT / EXTEND | Reuse Optuna study storage, seeded search concepts, trial budgets, custom configs and inspection; separate canonical metadata and governance. Preserve legacy implementation. |
| `BaseStrategyConfigGenerator` / `BacktestingConfig` | ADAPT | Strategy-declared provider-neutral SearchSpace and StrategyDefinition; ControllerConfigBase remains available to legacy tasks. |
| `core/backtesting/engine.py` | REUSE for legacy / ADAPT canonical path | Preserve Hummingbot BacktestingEngineBase path; canonical trials use existing Quant SimulationEngine instead of importing live connector stack. |
| `app/tasks/backtesting/macd_bb_backtesting_task.py` | REUSE legacy / DEPRECATE as canonical authority | Preserve task behavior. Recurring mutable lookback studies do not satisfy immutable SEARCH/VALIDATION/OOS governance; migrate operator studies to new CLI. |
| `app/tasks/backtesting/trend_example_backtesting_task.py` | REUSE legacy / ADAPT | Preserve controller generators; use strategy-defined canonical parameter spaces for new research. |
| `config/template_1_candles_optimization.yml` | REUSE legacy / DEPRECATE for governed studies | Preserve candles/download schedule. Do not run it for this certification. New frozen JSON spec is explicit. |
| `pyproject.toml` Optuna dependencies | REUSE | Optuna already declared; canonical dedicated runtime has Optuna 4.5.0. No dependency installs performed. |
| Optuna TPESampler, RandomSampler, GridSampler, SQLite | REUSE | Sampling implementation only; float objective values never economic evidence authority. |
| Gross-only winner / same-bar EMA fill / implicit deployment | REJECT | No gross-only positive acceptance, no retrospective fill authority, no optimization-to-deployment shortcut. |
| canonical metadata, OOS ledger, causal EMA research adapter | EXTEND | New research-only module and typed artifacts. |

Compatibility evidence: legacy Python files parse successfully and remain byte-for-byte unchanged from HEAD.
Actual legacy import in the canonical market-data runtime is **ENVIRONMENT_UNAVAILABLE** (`plotly` missing; Hummingbot also
not installed). This is not an import PASS. Do not install the legacy transport stack into system Python to hide the gap.
Use the legacy supported Hummingbot environment for existing controller studies; use the dedicated canonical interpreter
for the new optimizer. This is a documented migration, not proof that the legacy runtime executed here.

The untracked parent `ema_strategy_dev/` files were inspected as local reference and known historical input only; they were
not changed or promoted into a production revision. Their existing economic results were not a selection criterion.
