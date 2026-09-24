# Execution model and anti-lookahead rules

## Decision and entry
A strategy sees only records through decision bar `idx`. A signal generated from that bar enters **no earlier than the next bar's open** (or the next timestamped snapshot in a future event-level loader). It cannot transact at the decision close. Brackets are calculated from that next-bar entry reference price.

## Exit ordering
After entry, each subsequent OHLC bar is checked. If both a stop and TP are touched in the same bar, the engine applies **stop first**, a deliberately adverse ordering. If a tick/path feed supplies ordering, an event-level implementation may replace this rule only by preserving ordered evidence. Time exit uses the close of the bar at or beyond the frozen horizon. This model does not infer intra-bar paths.

## Orders, fill status, and costs
Taker fills are modeled at the executable reference price plus adverse slippage and schedule fee. `BASE` uses 1 bp taker slip; `STRESS_1` uses 5 bp. `STRESS_2` increases fee multipliers 1.5x and forces exits taker-heavy; `STRESS_3` combines both.

Maker limit entries/exits require a book-executable price and a queue/fill model using L2 updates and trades. The synthetic fixture has neither. Any synthetic maker fill therefore has ledger status `ASSUMED` / `ASSUMED_SYNTHETIC_NOT_BOOK_EXECUTABLE`; it is not an executable-fill claim. This is especially material to Absorption Fade.

Funding is **NOT_MODELED**. Ledger funding is exactly zero solely as an explicit modeling assumption, not evidence that funding was zero. No latency, rejects, partial fills, liquidation, or venue-specific margin are modeled.

## Costs and economics
Gross PnL uses raw reference entry/exit prices; fees and adverse slippage are separately deducted to net PnL. Reports include cost decomposition. Synthetic economic output is engine behavior under assumptions, never historical strategy validation or a promotion basis.
