"""Supervise resumable 14-day CryptoHFTData acquisition until completion."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from bulk_historical_downloader import BulkHistoricalDownloader

MANIFEST = Path("/opt/Axodus/Trading/quants-lab/research_notebooks/orderflow_backtest/historical_acquisition_manifest.json")
DATA_ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data")


def main() -> int:
    api_key = os.environ.get("HFT_DATA_API_KEY")
    if not api_key:
        print("HFT_DATA_API_KEY missing", file=sys.stderr)
        return 1

    downloader = BulkHistoricalDownloader(
        api_key=api_key,
        data_root=DATA_ROOT,
        manifest_path=MANIFEST,
    )

    while True:
        manifest = json.loads(MANIFEST.read_text())
        summary = manifest["summary"]
        remaining = summary["expectedPartitions"] - summary["qualified"]
        if remaining <= 0:
            print("ACQUISITION_COMPLETE")
            return 0

        result = downloader.run_batch(max_partitions_per_batch=12)
        print(f"BATCH {result} remaining_before_next={remaining}", flush=True)
        if result["processed"] == 0:
            print("ACQUISITION_STALLED", file=sys.stderr)
            return 2
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
