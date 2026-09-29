# AXODUS-TRADING-REQ-QUANT-OPTIMIZATION-TRIAL-ENGINE-01 — FINAL HANDOFF

STATUS: LOCAL_VALIDATED / AWAITING_CTO_REVIEW

## Legacy audit

See LEGACY_AUDIT.md. Optimizer, engine, backtesting tasks, YAML template and pyproject are byte-identical to HEAD.
Optuna storage/search concepts reused; ControllerConfigBase flow preserved for legacy users; canonical metadata,
causal simulation, research governance and selection added. No legacy removal.
Legacy import: ENVIRONMENT_UNAVAILABLE (plotly absent in canonical venv); documented migration, not runtime PASS.

## Architecture / contracts

Added core/quant_optimization: models, search_space, objective, study, runner, selection, provenance,
governance, data, ema, strategy, certification, package exports and CLI.
OptimizationSpec, SearchSpace, OptimizationStudy, OptimizationTrial and ParameterCandidate implemented.
TPE/RANDOM/GRID; Optuna 4.5.0 SQLite plus canonical content-addressed JSON; resume and cache integrity tested.
Integer/Decimal/Categorical/Boolean, four parameter domains, ema_fast < ema_slow and metric hard constraints supported.
Objectives: NET_EXPECTANCY, NET_PNL, PROFIT_FACTOR, SHARPE, MAX_DRAWDOWN, RETURN_OVER_DRAWDOWN.
Best-trial retrieval excludes hard-constraint failures; neighborhood analysis retains unfavorable observations.

## Research governance

SEARCH alone feeds sampler. Validation shortlist frozen before access. Direct evaluation enforces spec dataset/window
identity. Reserved or observed validation cannot silently become search. OOS is separate, requires approved immutable
revision/parameter freeze and consumes holdout before evaluation, including failures. Aliases/revised parameters do not
reset consumption. No OOS evaluated in certification. No freeze or deployment performed.
Use the shared governance ledger; historical external OOS records require governance import before using new OOS.
ParameterCandidate does not satisfy the existing promotion evidence package; no deployment eligibility emitted.

## Economics / robustness

Decimal economics and ranking; maker baseline 2 bps, taker 5 bps per executed notional.
Initial adapter is taker-only, causal-next-close-taker-v1 with explicit 1 bps slippage.
Maker simulation/search remains unsupported until independently qualified. Funding is not modeled.
Parameterized EMA research variant preserves trigger family but does not claim equivalence to legacy same-bar fills.
Neighborhood count/positive ratio/dispersion/sensitivity, equal-weight symbol medians/worst expectancy,
frequency/drawdown distributions and temporal consistency persisted. Sparse regions are insufficient evidence.
Robust-region acceptance/rejection tested using controlled fixtures; real certification found no accepted candidate.
Interrupted search sampling order is not guaranteed identical; individual trial replay is deterministic.

## EMA certification

Strategy: trend.ema.5x9.pullback.scalper; base revision: ema-5x9-v2.
Symbols: BTCUSDT, ZECUSDT, SUIUSDT, WLDUSDT.
Study: ema-v2-certification-03; run: 862c63d0-b06f-4890-80fb-18bbef5425c9.
Trials requested/completed/unique: 8/8/8. Pause after 3 then resume same run: PASS.
Completed study re-entry: no-op PASS. Four independent trial replays: hashes and metrics equal.
Two SEARCH slices: 2026-06-23 19:00–21:00 UTC and 21:00–23:00 UTC.
VALIDATION: 2026-06-23 23:00 UTC–2026-06-24 01:00 UTC.
Known historical engineering inputs, not fresh OOS. Candidate records: 8 REJECTED.
Neighborhood counts: [1, 4, 1, 1, 1, 0, 0, 0].
Profitability relevance: NOT_REQUIRED_FOR_ACCEPTANCE.
Evidence root: /run/media/mzfshark/Storage/Axodus/Trading/market-data/research/quant_optimization_trial_engine_03
Implementation hash: c7f7e464dc5cc365654959268b6ccf36470f6166a19fd3b53714d0b5de06bb0b
Results artifact: 7476170ab2b142d10442e2679af9aa751f363e10f59372b11b9d43430091c62d

## Tests

Canonical Python: /run/media/mzfshark/Storage/Axodus/Trading/market-data/venv/bin/python.
Command: PYTHONPATH=.:research_notebooks <canonical-python> -m pytest -q tests research_notebooks/orderflow_backtest
Full Quants-Lab: 226 PASS / 0 FAIL / 0 SKIP (17.94s).
Included scopes: optimization 50; simulation 5; validation 6; robustness 8; promotion 9;
foundations 9; quant data 8; orderflow 131. These counts are subsets, not additional tests.
Optimization focused command: <canonical-python> -m pytest -q tests/test_quant_optimization_integration.py
Focused: 50 PASS / 0 FAIL / 0 SKIP (14.22s).
Legacy AST validation PASS; unchanged-file verification PASS. Secret-pattern scan: zero findings in 18 implementation/config/test/doc files.
No package installed. No exchange/runtime/service calls made by this task. Diff check PASS (tracked files);
new files separately inspected and parsed/tested.

## Current PAPER run

064e34bd-55c2-4be9-a8c4-212827eb5c9b: no data read, no source/state/service mutation, no optimization input.
No claim of current run health; services were not inspected or restarted.
REAL_ORDERS: 0. REAL_CAPITAL: 0. MAINNET_REMOTE_EXECUTION: 0.

## Files / operator surface

New: core/quant_optimization/*.py (14), tests/test_quant_optimization_integration.py,
config/ema_optimization.example.json, docs/quant_optimization/LEGACY_AUDIT.md, README.md and FINAL_HANDOFF.md.
Parent coordination receipt and VALIDATION.md updated. Existing ema_strategy_dev files preserved.
CLI: python -m core.quant_optimization --store <root> optimize --spec <json> --study-id <id>;
status/results --study-id <id>. Configure example placeholders before operation. No code edits for registered EMA studies.
TRINITY_OPERATOR_READY: YES for bounded offline research; no freeze/deployment authority.
PROPOSED_COMMIT: feat(quants): add governed canonical optimization trial engine
Scope: canonical module, example, tests and docs only. Exclude raw historical data, runtime SQLite/JSON, caches and credentials.
COMMIT: NOT_PERFORMED. PUSH: NOT_PERFORMED. CTO_REVIEW_REQUIRED: YES.
