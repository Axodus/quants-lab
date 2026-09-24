# Functional Equivalence Matrix — Order Flow Strategies

**Date:** 2026-09-24
**Requirement:** `AXODUS-TRADING-REQ-QUANT-ORDERFLOW-01`
**Source Root:** `/home/mzfshark/.openclaw/workspace/strategies/orderflow/`
**Quants-Lab Module:** `quants-lab/research_notebooks/orderflow_backtest/source_equivalent_adapters.py`

## 1. Strategy 1 — Aggression Momentum Scalper (`orderflow.momentum.aggression`)

| Dimension | Source Prototype (`momentum.py`) | Quants-Lab Source Adapter (`source_equivalent_adapters.py`) | Equivalent? | Notes / Resolution |
|---|---|---|:---:|---|
| Numeric Representation | Python `Decimal` (precision 50) | Python `Decimal` (`OrderFlowFrameV1`) | **YES** | Exact precision preserved; no binary float conversion. |
| Warmup Condition | `len(history) < config.window` | `len(history) < config.window` | **YES** | Warmup emits `NO_SIGNAL / warmup`. |
| Spread Check | `frame.spread_ticks <= max_spread_ticks` | `frame.spread_ticks <= max_spread_ticks` | **YES** | Exact tick spread condition. |
| Standardization | `z = (delta - mean(prior)) / pstdev(prior)` | `z = (delta - mean(prior)) / pstdev(prior)` | **YES** | Centered and scaled by prior deltas only. |
| Imbalance Condition | `abs(obi) > config.imbalance` | `abs(obi) > config.imbalance` | **YES** | Strict inequality as defined in source. |
| Breakout Condition | `price > max(prior.price)` (Long) / `< min(prior.price)` (Short) | `price > max(prior.price)` (Long) / `< min(prior.price)` (Short) | **YES** | Strict breakout over all prior window frames. |
| Execution Profile | Taker Entry / Maker Exit (configured in harness) | Signal classification only | **YES** | Signal does not execute orders. |

---

## 2. Strategy 2 — Absorption Fade (`orderflow.absorption.fade`)

| Dimension | Source Prototype (`absorption.py`) | Quants-Lab Source Adapter (`source_equivalent_adapters.py`) | Equivalent? | Notes / Resolution |
|---|---|---|:---:|---|
| Candidate Identification | `volume > baseline_mean * volume_multiple` on `history[-1]` | Exact volume spike check on `history[-1]` | **YES** | Baseline is `history[-(window+1):-1]`. |
| Stalled Price | `abs(candidate.price - baseline[-1].price) <= move_ticks * tick_size` | Same stalled price formula | **YES** | Verified on candidate frame. |
| Candidate Spread | `candidate.spread_ticks <= max_spread_ticks` | Same spread filter | **YES** | Fails closed if candidate spread wide. |
| Confirmation Rule | `candidate.sell/vol > fraction` and `frame.delta >= confirmation_delta` and `frame.price > candidate.price` (Long) | Same counter-aggression and price rise condition | **YES** | Short mirrors for buy spike. |
| Attribution Claim | Hypothesis only | Hypothesis only | **YES** | Explicitly no institutional / iceberg proof claim. |

---

## 3. Strategy 3 — CVD Divergence Reversal (`orderflow.cvd.divergence.reversal`)

| Dimension | Source Prototype (`divergence.py`) | Quants-Lab Source Adapter (`source_equivalent_adapters.py`) | Equivalent? | Notes / Resolution |
|---|---|---|:---:|---|
| Extrema Window | Prior `window` frames excluding current | Prior `window` frames excluding current | **YES** | Trailing bounds causality enforced. |
| Bullish Divergence | `price <= min(prior.price) - price_ticks * tick_size` and `cvd >= min(prior.cvd) + divergence_delta` and `delta > 0` | Same price low and CVD high condition | **YES** | Requires positive current delta. |
| Bearish Divergence | `price >= max(prior.price) + price_ticks * tick_size` and `cvd <= max(prior.cvd) - divergence_delta` and `delta < 0` | Same price high and CVD low condition | **YES** | Requires negative current delta. |

---

## 4. Summary Status

- **Adapter Equivalence:** `SOURCE_EQUIVALENT` (All 3 strategies match source behavior across positive, negative, boundary, and warmup states).
- **Historical Dataset Status:** `HISTORICAL_DATASET_NOT_AVAILABLE` (Blocks strategy promotion).
- **Harness Authority:** `SHADOW / RESEARCH ONLY` (No trading or financial authority).
