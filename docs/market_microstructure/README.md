# Canonical microstructure event and feature contracts

Status: locally validated, awaiting CTO review. Research-only: no strategy,
execution, credentials, live service changes, or promotion authority.

## BBO has one authority

`BookSnapshotEvent + BookDeltaEvent -> CausalOrderBook.state` owns best bid,
best ask, spread (`ask - bid`) and mid (`(ask + bid) / 2`). The existing native
Binance event-atomic kernel remains unchanged. BBO is derived canonical state,
not a separate source stream. The unused source-level `BBOUpdateEvent` class
has been removed; no consumers existed in this checkout. No derived BBO event
is necessary: immutable book state already carries timestamp and sequence.
FeatureProvenance links snapshots to dataset, source interval, sequence and
implementation hash. This FIX introduces no second book or BBO authority.

## MarkPriceEvent

Mandatory fields: venue, market_type, symbol, mark_price, event_time,
source_ref, provenance. Optional fields: receive_time, transaction_time,
sequence, effective_resolution_ms, index_price, funding_rate, next_funding_time.
Provenance is OBSERVED, INFERRED or UNKNOWN; no default silently claims observed
context. Mark/index prices must be positive finite Decimal values. Funding rates
may be negative. Missing optional fields are None, never zero or synthesized.
Float and boolean numeric inputs are rejected.

Mark/index prices are contextual reference prices, not executable order levels.
They retain exact Decimal source precision, including precision finer than the
InstrumentSpec order tick. They are not rounded to an order price. An aligned
value can round-trip through the existing InstrumentSpec fixed-point methods;
an unaligned context value remains Decimal. Book and tape tick rules do not change.

`MarkPriceEvent.from_record(record, timestamp_unit='ms'|'us'|'ns')` normalizes
explicit integer epoch units to UTC milliseconds. It does not infer units. Sub-ms
parts are truncated; source_ref identifies the original source. `to_dict()`
serializes exact decimal strings and an explicit MARK_PRICE event type.

`MicrostructureFeatureEngine.process(event)` verifies instrument identity and
chronological availability before retaining an immutable mark context. Context
availability is `max(event_time, receive_time when present)`.
`derivatives_context(as_of)` returns only the latest retained context if available
by as_of; it returns None otherwise. This is a latest-context accessor, not a
historical mark-price archive. Out-of-order updates fail closed. Persist the event
stream externally if historical context queries are needed.

Mark context never mutates book levels, sequence, OFI, queue imbalance,
microprice, sweeps, rolling feature state, or feature source hash. Mark events do
not trigger feature emission or advance the L2/tape feature clock. Dedicated
mark counters and the context's source_ref retain independent provenance.
There is no exchange transport or source download in this module.

## Exact microprice meaning

For valid top-of-book prices b < a and quantities qb > 0, qa > 0:

```
microprice                  = (b * qa + a * qb) / (qb + qa)
mid                         = (b + a) / 2
microprice_minus_mid         = microprice - mid
microprice_displacement_bps  = (microprice - mid) / mid * 10000
```

This is the top-of-book opposite-quantity weighted price, sometimes called
weighted mid. It is NOT a fitted/calibrated Stoikov transition model, and NOT a
plain-mid fallback. Balanced quantities produce mid by the equation itself;
bid-heavy quantities move the result toward ask, and ask-heavy toward bid.
Inputs and output use Decimal; division follows the canonical Decimal context.

`microprice_fields(state)` reports `microprice_valid=true` and
`microprice_provenance=COMPUTED_FROM_BOOK` for valid inputs. Invalid book state,
empty side, crossed/locked BBO, nonfinite or nonpositive price/quantity returns
null for all three numeric outputs, `microprice_valid=false`, and
`microprice_provenance=UNAVAILABLE`. No silent mid fallback exists.
The feature engine still rejects invalid book snapshots entirely; the null
contract is also available to direct consumers of the microprice helper.

Feature schema is **microstructure-v1.1**. The version changes for explicit
validity/provenance fields and removal of invalid-input fallback. Valid-book
numerical formulas are unchanged. Existing v1 evidence is not overwritten.
Markout formulas are unchanged and remain separate forward evaluation labels;
no markout feeds a causal feature snapshot.

## Other accepted formulas and limits

The feature catalog is `core.market_microstructure.registry.catalog()`.
Horizon membership is `(t-h, t]`; price anchors use latest valid state at/before
`t-h`. Available horizons: 100, 250, 500, 1000, 2000, 5000, 10000 milliseconds.
Effective timestamp resolution is recorded; it is not a promise of feed cadence.

- QI = bid_depth/(bid_depth+ask_depth); signed QI = (bid_depth-ask_depth)/total.
- Rank-wise OFI uses current quantity for an improving level, negative previous
  quantity for a worsening level, and size difference at unchanged price; sums
  bid contributions minus ask contributions for ranks 1, 5 or 10. Normalized
  OFI divides by current total depth. This FIX does not change it.
- Weighted depth defaults to reciprocal rank weights; pressure is weighted bid
  depth minus weighted ask depth, optionally normalized by their sum.
- Aggression uses explicitly classified tape quantity; velocities are per second.
- Visible removal rates named `cancel_rate` are quantity-reduction proxies.
  L2 does not establish whether every reduction was cancellation or execution.
- Replenishment measures visible restoration following reduction, not proven
  hidden liquidity. Absorption combines aggressive quantity, restoration ratio
  and low mid-price response; it is a feature, not a decision.
- Sweep scores infer non-overlapping monotone trade-price clusters; they do not
  prove a single market order consumed those levels.
- Exact queue position and hidden liquidity are unavailable from this evidence.

## Reproducible bounded benchmark

Use the dedicated market-data environment; never install its dependencies into
system/Condor Python. Run from the Quants-Lab root. The entrypoint is a module
(`-m`), not a direct execution of `__main__.py`.

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 ARROW_NUM_THREADS=1 \
PYTHONPATH=.:research_notebooks \
/run/media/mzfshark/Storage/Axodus/Trading/market-data/venv/bin/python \
-m core.market_microstructure benchmark-features \
--config docs/market_microstructure/benchmark/run1.config.json

OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 ARROW_NUM_THREADS=1 \
PYTHONPATH=.:research_notebooks \
/run/media/mzfshark/Storage/Axodus/Trading/market-data/venv/bin/python \
-m core.market_microstructure benchmark-features \
--config docs/market_microstructure/benchmark/run2.config.json
```

Inputs are local immutable CryptoHFTData canonical Parquet. Configs contain exact
paths, InstrumentSpec and output roots, outside live `runs/`. Source hashes,
InstrumentSpec manifest, Git HEAD plus uncommitted source hashes, effective
feature configuration, command and interpreter are recorded in each report.
No network/download, orders, strategy evaluation or optimization is performed.

| Measurement | Run 1 | Run 2 |
|---|---:|---:|
| UTC interval | 2026-06-23 00:00:00–00:00:20 | identical |
| Received / processed logical events | 2,488 / 2,488 | identical |
| Book / trade / mark events | 755 / 1,733 / 0 | identical |
| Rejected / gaps / crossed / bootstrap failures / stale states | 0 each | 0 each |
| Feature snapshots | 459 | 459 |
| End-to-end wall seconds | 34.166710383 | 34.426764809 |
| Events/s, end-to-end | 72.819419 | 72.269352 |
| Feature processing seconds | 15.736010501 | 13.776443883 |
| Events/s, feature processing stage | 158.108690 | 180.598130 |
| Peak process RSS (MiB) | 318.6875 | 326.66796875 |
| Pre-window bootstrap rows | 5,342,647 | 5,342,647 |
| Bootstrap seconds | 12.298915943 | 14.110668067 |

End-to-end time includes input hashing, native compilation/loading, bootstrap,
normalization, feature computation and cache persistence, excluding final report
writing. Logical events are distinct from raw L2 level rows; bootstrap rows are
reported separately and never used to inflate the event throughput numerator.
Feature-stage timing includes decoding, causal merge and snapshots; it is not a
pure C++ kernel benchmark. The process had 4 observed threads at report time and
CPU affinity 0–7 despite thread limit environment variables; it was not pinned
or certified single-core. RSS uses Linux RUSAGE_SELF high-water, excludes compiler
child memory and is reported in MiB despite the legacy `peak_rss_mb` key.
Concurrent host workloads may affect timings.

Dataset hash:
`3157fed1453dac41dbcafcb8ba6b36574563e330e08565159401a1bd4f556fba`.
Identical feature stream hash in both independent replays:
`f896b351f343ab0cc25a70d7b75e7f45bdfa35133b101a2bdd5ae4c4710d171d`.
Markout outputs, counters, interval and cache identity also match. Cache checksum
validation reopened all 459 snapshots successfully for both runs.

These results supersede the unsupported 12,000–25,000 events/s statement in the
previous chat handoff. They do not establish live/HFT suitability, cluster
capacity or multi-day throughput. Native reconstruction is reused, but Python
normalization and feature calculations remain part of the measured path. The
benchmark contains no historical mark stream: MarkPriceEvent support is tested
with deterministic fixtures. It certifies BTC only, not four-symbol qualification.

Full regression: **291 PASS / 0 FAIL**. Focused microstructure: **65 PASS**
(36 foundation + 2 integration + 27 remediation). Quant data 8; simulation 5;
optimization 50; validation 6; robustness 8; foundations 9; promotion 9;
historical orderflow 131. See `benchmark/validation.json` for per-class counts.

The parent worktree includes a preexisting one-line optimization resume fixture
change; this FIX does not change the optimization engine or policy. No change
was made to accepted book/kernel, OFI, queue, sweep, replenishment, absorption
or markout formulas.
