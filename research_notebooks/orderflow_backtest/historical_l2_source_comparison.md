# Historical L2 source comparison

## Decision

`NO_ACCEPTABLE_HISTORICAL_L2_SOURCE_FOUND` for the 90-day historical replay gate.

The Tardis.dev Binance Futures CSV product is **partially qualified**: real samples for BTCUSDT and ETHUSDC contain price-level rows and top-25 snapshots, but the observed downloadable CSV schema contains no `U`, `u`, `pu`, or equivalent sequence/update identifier. That prevents a fail-closed causal replay qualification from this product alone.

The Binance USD-M Futures public REST/WebSocket combination is **qualified for prospective capture**, not historical coverage. A live pilot for both symbols observed price-level updates and sequence fields (`U`, `u`, `pu`) and reconstructed books without gaps during the bounded sample.

## Candidate matrix

| Provider/product | BTCUSDT | ETHUSDC | Price levels | Snapshot | Incremental | Sequence | Decision |
|---|---|---|---:|---:|---:|---|---|
| Binance Vision bookDepth | archive class available | archive class available | no | no | no | absent | REJECTED_SCHEMA |
| Tardis Binance Futures CSV | real sample | real sample | yes | yes | yes | absent in observed CSV | PARTIALLY_QUALIFIED |
| Binance REST + WebSocket | live pilot | live pilot | yes | yes | yes | `lastUpdateId`, `U/u/pu` | QUALIFIED_PROSPECTIVE_ONLY |
| Axodus internal archive | not found | not found | unknown | unknown | unknown | unknown | NOT_AVAILABLE |

## Real samples

The qualification fixtures are stored under `qualification_fixtures/` and are small bounded samples, not a 90-day acquisition. Tardis samples were obtained for `2024-09-01` for both target symbols. Binance REST samples were obtained for both symbols.

Observed Tardis CSV fields:

- `exchange`, `symbol`, `timestamp`, `local_timestamp`, `is_snapshot`, `side`, `price`, `amount` for `incremental_book_L2`;
- `exchange`, `symbol`, `timestamp`, `local_timestamp`, and 25 bid/ask price-level pairs for `book_snapshot_25`.

The Tardis sample preserves microsecond timestamps and exact-decimal-compatible text values. It does not expose a sequence field in the downloadable CSV.

## Adapter compatibility

The canonical parser computes spread ticks, top-N OBI, depth skew, and causal tape composition. The bounded compatibility suite passed for the frozen Momentum, Absorption, and CVD Divergence adapter interfaces. This proves input availability and parser compatibility; it does not prove historical strategy performance or sequence integrity for the Tardis CSV source.

## Licensing and cost

Tardis licensing, retention, redistribution, raw/API access, and pricing remain `UNKNOWN` pending contractual/account review. Binance exchange terms also require explicit review before bulk archival. No paid subscription or bulk acquisition was initiated.

## Bulk acquisition

`BULK_ACQUISITION_READY = NO`. The sequence gate and licensing gate are unresolved.
