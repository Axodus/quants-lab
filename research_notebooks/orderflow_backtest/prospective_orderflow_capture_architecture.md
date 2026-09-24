# Prospective Order Flow Capture Architecture

## Boundary

The capture service is market-data infrastructure. It has no strategy, portfolio, risk, intent, execution, exchange-order, or capital authority. It uses public market-data endpoints only.

## Pipeline

```text
Binance Futures WebSocket depth/trade streams
                +
        REST initial snapshot
                ↓
        immutable raw JSONL
                ↓
      sequence validator / reconstructor
                ↓
      snapshot reconciliation checkpoints
                ↓
      canonical Decimal normalization
                ↓
     Parquet/zstd research partitions
                ↓
          OrderFlowFrameV1
```

## Bootstrap and continuity

1. Connect to the diff-depth stream and buffer updates.
2. Fetch the REST depth snapshot.
3. Apply only the buffered update range overlapping `lastUpdateId`.
4. Require `pu` to equal the previously accepted `u` for subsequent updates.
5. On mismatch, emit `LIVE_GAP_DETECTED`, quarantine the derived interval, reconnect, and bootstrap from a fresh snapshot.

A restart or disconnect is a provenance boundary. It must never be represented as continuous qualified history.

## Raw retention

Each raw event retains provider, venue, symbol, stream, exchange event time, local receive time, sequence, original payload, and a SHA-256 payload checksum. Raw files are immutable after partition close.

The recommended storage boundary is external to Git:

```text
raw/provider=symbol/date/hour/*.jsonl.gz
normalized/provider=symbol/date/hour/*.parquet
derived/provider=symbol/date/hour/*.parquet
manifests/provider/symbol/date/hour/*.json
quarantine/provider/symbol/date/hour/*
```

## Partition manifest

Each closed partition records first/last event times, first/last accepted sequence, record count, gap records, raw-file checksum, and capture version.

## Tape and metadata

Capture trade ID, price, quantity, aggressor side, exchange time, and local receive time alongside depth. Persist funding and instrument metadata as revisioned periodic records.

## Health

Expose connected state, capture state, last-event age, last accepted sequence, events per second, gap count, writer health, and snapshot status. Disk-full, write, checksum, and partition-close failures are visible failures.

## Backpressure and recovery

Use bounded buffers. If writer throughput falls behind, record a visible backpressure failure and stop qualifying derived state rather than silently dropping events. On restart, close/quarantine the incomplete partition, record a continuity boundary, reconnect, and begin a new partition after snapshot bootstrap.

## Initial scope

Capture BTCUSDT and ETHUSDC only. Validate throughput and storage before adding instruments or long-running supervision. Production daemon deployment is a separate implementation gate.
