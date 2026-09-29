# Canonical optimization research

Owner: Quants-Lab. Request: AXODUS-TRADING-REQ-QUANT-OPTIMIZATION-TRIAL-ENGINE-01.

Trial != strategy revision. Best trial != validated strategy. ParameterCandidate != deployment candidate.
This module has no exchange transport, credential loader, order execution, live runtime management, or deployment authority.

## Architecture and authority

`OptimizationSpec` freezes the strategy implementation hash, base revision, exact finite search space, datasets and hashes,
SEARCH/VALIDATION/OOS windows, economic model, objective, constraints, sampler, seed and budget.
Every study has an immutable UUID. Optuna 4.5.0 supplies TPE/RANDOM/GRID suggestions and SQLite study storage.
Canonical JSON and content-addressed simulation/experiment/trade artifacts remain available without Optuna queries.

`SimulationEngine` and `DeterministicExecutionModel` execute historical trial decisions. `ParameterRobustnessAnalyzer`
provides canonical neighborhood evidence; `StatisticalAnalyzer` supplies supplemental diagnostic summaries.
Selection records exact Decimal neighborhood sensitivity, dispersion, positive ratios, equal-weight symbol medians,
worst-symbol expectancy and temporal distributions. Missing neighbors remain insufficient evidence; negative neighbors
are retained. A highest scalar score alone cannot establish a robust region.

`ParameterCandidate` is deliberately incompatible with `quant_promotion.StrategyEvidencePackage`.
Promotion still needs an explicitly approved new revision plus fresh OOS, validation and the existing promotion gate.
No CLI command freezes revisions or grants deployment eligibility. The trusted research API `ResearchLedger.freeze`
requires a new revision and an explicit approval reference. Access to this API belongs to the authorized freeze workflow.

## EMA integration and economic limitations

The initial registered strategy is `trend.ema.5x9.pullback.scalper`, **base** `ema-5x9-v2`.
The adapter is a parameterized research variant, not a replacement for the frozen production strategy.
It preserves the two EMA pullback trigger family, SMA initialization, TP/SL/reversal conditions, and adds declared cooldown.
Ranges reside in `ema.get_search_space()`, never in Optuna internals. No new indicators were introduced.

The old standalone EMA simulator enters retrospectively at the same candle's calculated EMA and permits same-bar exits.
The canonical adapter does **not** claim economic equivalence to that model: it evaluates closed candles and executes at
the next observed close with explicit slippage. On the entry fill candle it cannot use earlier intrabar highs/lows as if
observed after the fill. Two terminal observations are reserved for causal liquidation. Each slice starts without position
or EMA state; warm-up is inside the slice. `causal-next-close-taker-v1` is the versioned execution assumption.

All fills in this initial execution backend are TAKER. Frozen fee baseline is maker 2 bps / taker 5 bps per execution;
applied fees are 5 bps times actual executed notional. Maker/execution-mode search is **fail-closed unsupported** until a
conservative maker model is qualified. No maker-submission-equals-fill shortcut exists. Funding is not modeled and is
explicitly disclosed; these candle trials do not prove deployment economics or queue realism. Slippage is explicit.
Sharpe is an unannualized per-observation mean/std ratio; undefined ratios (zero loss, zero variance, zero drawdown) remain
undefined and cannot win that objective. Authoritative economics and rankings use Decimal, not Optuna float scores.
Supplemental StatisticalAnalyzer confidence intervals are diagnostic and not selection authority.

## Governance

Only SEARCH evaluations enter the sampler. Once the fixed trial budget ends, the top constraint-passing shortlist is
persisted **before** VALIDATION is accessed; validation cannot request more trials. OOS is never run by `optimize`.
The separate trusted `evaluate_oos` API requires an exact approved revision/parameter freeze and reserves consumption
before simulation. Even failed OOS evaluation remains consumed. Dataset aliases and revised parameters cannot clear
symbol/time consumption. Unknown or previously known inputs cannot be advertised as untouched OOS.
Both direct evaluation and study execution require the exact dataset and window declared in the immutable specification.
An OOS certification/evaluation must declare its OOS window prospectively; callers cannot relabel a SEARCH window at dispatch.
Observed or reserved VALIDATION windows cannot be recycled through direct SEARCH evaluation or a new study.

Keep one shared durable governance root across studies. The operator default is
`/run/media/mzfshark/Storage/Axodus/Trading/market-data/research/quant_governance`.
`--governance` supports isolated test infrastructure; do not use an empty ledger to bypass research history.
Existing external OOS history must be registered/imported by governance before permitting fresh OOS through this domain;
this implementation does not rewrite or silently import older validation artifacts.
Known inputs remain `KNOWN_HISTORICAL`; a dataset eligibility declaration cannot replace a real sealing process.

The protected PAPER run and all paths under `runs/` are rejected as inputs/optimization output roots.
No live partial outcomes were used. Operator configuration is not an adversarial security sandbox: filesystem and
trusted Python API access must remain governed outside this research library.

## Trinity operator commands

Run from Quants-Lab with the dedicated interpreter; no dependency installation is needed:

```bash
PY=/run/media/mzfshark/Storage/Axodus/Trading/market-data/venv/bin/python
STORE=/run/media/mzfshark/Storage/Axodus/Trading/market-data/research/optimization
$PY -m core.quant_optimization --store "$STORE" optimize --spec config/ema_optimization.example.json --study-id ema-research-001
$PY -m core.quant_optimization --store "$STORE" status --study-id ema-research-001
$PY -m core.quant_optimization --store "$STORE" results --study-id ema-research-001
```

Configure dataset paths, hashes, known provenance, source hash, code reference and the **prospectively chosen** windows
before running the example. The checked-in example intentionally contains placeholders and fails closed until configured.
No source-code edit is needed for normal runs. Decimal values are JSON strings. `--max-new-trials N` pauses after a bounded
number of new attempts; repeating the identical command resumes the same run and total budget. A changed spec or code hash
requires a different study. Completed runs verify result artifacts and return without resampling.

Evidence layout: `studies/<id>/study.json`, `spec.json`, `trials/`, `validation_shortlist.json`, `best_trials.json`, `results.json`,
`artifacts/<sha256>.json`, `cache/`, `optuna.sqlite3`. Writes use file fsync, atomic rename and directory fsync.
A study process lock prevents concurrent owners. Interrupted Optuna slots are recorded as failures, consuming budget;
completed compatible simulations remain reusable. Sampler RNG is reseeded on resume, so interrupted vs uninterrupted
**search order is not guaranteed equal**. Each individual trial is independently reproducible and hash-verifiable.
`best_trials.json` contains only constraint-passing completed search trials. Constraint failures remain persisted and
participate in neighborhood analysis, but cannot win best-trial selection.

## Extension surface

Declare a `SearchSpace` per strategy with integer/decimal/categorical/boolean dimensions and SIGNAL/RISK/EXECUTION/CONTROL
domains. Register a trusted `StrategyDefinition` factory implementing the existing simulation strategy protocol.
Operator config cannot dynamically import arbitrary code. Initial explicit configurations are supported as a frozen
`initial_parameter_sets` list before seeded exploration. Other execution and fee scenarios currently fail closed.

## Tests

```bash
PYTHONPATH=.:research_notebooks "$PY" -m pytest -q tests research_notebooks/orderflow_backtest
```

Offline fixtures cover operator commands, exact economics, resume/crash handling, cache corruption, robust regions,
negative neighborhoods, symbol/time aggregation, OOS alias guards, freeze identity, current-run exclusion and promotion separation.
Historical certification is a separate command documented in the certification report; profitability is not an acceptance condition.
