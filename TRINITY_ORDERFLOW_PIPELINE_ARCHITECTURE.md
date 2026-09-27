# Trinity Order Flow Research Pipeline

## Authority boundary

Trinity is a typed research requestor. Axodus validates each operation through
`ResearchCapabilityBoundary`; `BoundedResearchExecutor` resolves the dedicated
market-data runtime, checks paths and persists a receipt. There is no
`run_shell` action. An authorization decision is not an exchange or production
execution authority.

## Pipeline state

`SCANNED → PREEXISTING_DATASET_RESOLVED / ACQUIRED → QUALIFIED → FRAMES_BUILT →
REPLAY_COMPLETE → CANONICAL_EVIDENCE_PERSISTED`. Unsupported actions, missing
metadata, missing exact-window data, partial qualification and OOS requests
fail closed. Receipts are stored under the market-data root and carry zero
mutation counters.

## Runtime and storage

The runtime is `/run/media/mzfshark/Storage/Axodus/Trading/market-data/venv`;
data and large artifacts remain under
`/run/media/mzfshark/Storage/Axodus/Trading/market-data`. The executor reports
capacity and checks PyArrow, CryptoHFTData and Zstandard. It never provisions a
fallback environment in a home directory.

## Causal asset selection

`FIXED_UNIVERSE` is valid when frozen independently of later outcomes.
`POINT_IN_TIME` requires an immutable historical snapshot identifier and a
selection timestamp no later than the evaluation start; reconstruction of a
historical scanner snapshot is not currently wired. `FORWARD_SELECTED` cohorts
are prospectively labelled and rejected for historical acquisition/replay.
BTC VAL-02A remains a fixed-universe canonical regression reference. The
2026-09-27 live-selected cohort is not historical evidence.

## Instrument and replay contract

Each builder requires a registered, validated `InstrumentSpec` sourced from a
frozen venue metadata artifact. The fixed-point kernel derives tick and step
scales from that contract; sequence continuity, snapshot bridging, event
atomicity and `OrderFlowFrameV1` semantics remain unchanged. BTC must preserve
14,400 frames and hash
`fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf`.

The accepted BTC evidence is reusable. ETHUSDC remains `PARTIAL` at 67.9255%
coverage and cannot be replay-authorized. The generic non-BTC path must be
qualified independently before it can emit strategy evidence.

## Current integration status

The bounded interface and BTC evidence-resolution dry/research flow are
implemented and tested. Live scanner API execution, new historical download
handler integration, generic non-BTC frame generation, and a full strategy
replay invocation through the Trinity executor remain unattached. Existing
acquisition code is a separate CLI and is not implicitly granted to Trinity.
Thus this report records an implementation-validated bounded boundary, not a
complete production Trinity/OpenClaw integration.
