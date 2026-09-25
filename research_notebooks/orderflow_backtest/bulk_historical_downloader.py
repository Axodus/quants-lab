"""Partitioned, idempotent, resumable historical market-data downloader for CryptoHFTData.

Dual-layer provenance:
1. RAW layer: preserves exact byte stream as delivered by CryptoHFTData (.parquet.zst or .parquet)
   under DATA_ROOT/raw/{date}/{hour}/{symbol}_{dataType}.parquet.zst with SHA-256
2. NORMALIZED layer: stores validated, decompressed Parquet without data mutation
   under DATA_ROOT/normalized/{date}/{hour}/{symbol}_{dataType}.parquet with SHA-256
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Sequence

import cryptohftdata as chd
import pyarrow.parquet as pq
import zstandard as zstd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bulk_downloader")

ZSTD_MAGIC = bytes.fromhex("28b52ffd")


class BulkHistoricalDownloader:
    def __init__(
        self,
        api_key: str,
        data_root: Path,
        manifest_path: Path,
        symbols: Sequence[str] = ("BTCUSDT", "ETHUSDC"),
        data_types: Sequence[str] = ("orderbook", "trades"),
        window_start: str = "2026-06-23",
        window_end: str = "2026-07-06",
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.data_root = Path(data_root)
        self.raw_root = self.data_root / "raw"
        self.normalized_root = self.data_root / "normalized" / "canonical"
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.normalized_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(manifest_path)
        self.symbols = tuple(symbols)
        self.data_types = tuple(data_types)
        self.start_date = datetime.date.fromisoformat(window_start)
        self.end_date = datetime.date.fromisoformat(window_end)
        self.max_retries = max_retries
        self.timeout = timeout
        self.client = chd.CryptoHFTDataClient(api_key=self.api_key, timeout=self.timeout)
        self._load_or_init_manifest()

    def _load_or_init_manifest(self):
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())
            # The prior 90-day acquisition state is retained as provenance, but
            # the active primary scope is now the CTO-approved 14-day window.
            self.manifest["datasetId"] = "orderflow-binance-futures-14d-20260623-20260706-v1"
            self.manifest["datasetRevision"] = "v1"
            self.manifest["windowStart"] = f"{self.start_date}T00:00:00Z"
            self.manifest["windowEnd"] = f"{self.end_date}T23:59:59.999Z"
            self.manifest["contiguousDays"] = (self.end_date - self.start_date).days + 1
            self.manifest["previousTargetDays"] = 90
            self.manifest["scopeChangeReason"] = "progressive_evidence_model"
            self.manifest["primaryTargetDays"] = 14
            self.manifest["isDays"] = 10
            self.manifest["oosDays"] = 4
            self.manifest.setdefault("scopeHistory", []).append({
                "previousTargetDays": 90,
                "revisedTargetDays": 14,
                "reason": "progressive_evidence_model",
                "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            self.manifest["summary"]["expectedPartitions"] = 14 * 24 * len(self.symbols) * len(self.data_types)
        else:
            self.manifest = {
                "datasetId": "orderflow-binance-futures-14d-20260623-20260706-v1",
                "datasetRevision": "v1",
                "provider": "CryptoHFTData",
                "venue": "Binance USD-M Futures",
                "symbols": list(self.symbols),
                "windowStart": f"{self.start_date}T00:00:00Z",
                "windowEnd": f"{self.end_date}T23:59:59.999Z",
                "contiguousDays": (self.end_date - self.start_date).days + 1,
                "previousTargetDays": 90,
                "scopeChangeReason": "progressive_evidence_model",
                "primaryTargetDays": 14,
                "isDays": 10,
                "oosDays": 4,
                "dataRoot": str(self.data_root),
                "partitions": {},
                "summary": {
                    "expectedPartitions": ((self.end_date - self.start_date).days + 1) * 24 * len(self.symbols) * len(self.data_types),
                    "downloaded": 0,
                    "qualified": 0,
                    "failed": 0,
                    "retries": 0,
                    "totalBytes": 0,
                },
                "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

    def save_manifest(self):
        self.manifest["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        parts = self.manifest["partitions"]
        def in_primary_scope(p: dict) -> bool:
            try:
                return self.start_date <= datetime.date.fromisoformat(p["date"]) <= self.end_date
            except (KeyError, TypeError, ValueError):
                return False

        primary_parts = [p for p in parts.values() if in_primary_scope(p)]
        extra_parts = [p for p in parts.values() if not in_primary_scope(p)]
        downloaded = sum(1 for p in primary_parts if p.get("status") == "QUALIFIED")
        failed = sum(1 for p in primary_parts if p.get("status") in {"FAILED", "QUARANTINED"})
        total_bytes = sum(p.get("rawDeliveredBytes", 0) for p in primary_parts if p.get("status") == "QUALIFIED")
        self.manifest["summary"]["downloaded"] = downloaded
        self.manifest["summary"]["qualified"] = downloaded
        self.manifest["summary"]["failed"] = failed
        self.manifest["summary"]["totalBytes"] = total_bytes
        self.manifest["summary"]["expectedPartitions"] = ((self.end_date - self.start_date).days + 1) * 24 * len(self.symbols) * len(self.data_types)
        self.manifest["summary"]["extraArchivedPartitions"] = sum(1 for p in extra_parts if p.get("status") == "QUALIFIED")
        self.manifest["summary"]["extraArchivedBytes"] = sum(p.get("rawDeliveredBytes", 0) for p in extra_parts if p.get("status") == "QUALIFIED")
        for key in ("summary",):
            self.manifest[key]["primaryScope"] = {
                "start": f"{self.start_date}T00:00:00Z",
                "end": f"{self.end_date}T23:59:59.999Z",
                "days": 14,
                "isDays": 10,
                "oosDays": 4,
            }
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2))

    def download_partition(self, symbol: str, data_type: str, date_str: str, hour: int) -> bool:
        partition_key = f"{symbol}/{data_type}/{date_str}/{hour:02d}"
        if self.manifest["partitions"].get(partition_key, {}).get("status") == "QUALIFIED":
            return True

        raw_hour_dir = self.raw_root / date_str / f"{hour:02d}"
        norm_hour_dir = self.normalized_root / date_str / f"{hour:02d}"
        raw_hour_dir.mkdir(parents=True, exist_ok=True)
        norm_hour_dir.mkdir(parents=True, exist_ok=True)

        raw_local_path = raw_hour_dir / f"{symbol}_{data_type}.parquet.zst"
        norm_local_path = norm_hour_dir / f"{symbol}_{data_type}.parquet"

        # If both exist and match manifest, skip
        if raw_local_path.exists() and norm_local_path.exists():
            try:
                pf = pq.ParquetFile(norm_local_path)
                raw_sha = hashlib.sha256(raw_local_path.read_bytes()).hexdigest()
                norm_sha = hashlib.sha256(norm_local_path.read_bytes()).hexdigest()
                self.manifest["partitions"][partition_key] = {
                    "symbol": symbol,
                    "dataType": data_type,
                    "date": date_str,
                    "hour": hour,
                    "rawPath": str(raw_local_path),
                    "rawDeliveredBytes": raw_local_path.stat().st_size,
                    "rawDeliveredSha256": raw_sha,
                    "normalizedPath": str(norm_local_path),
                    "normalizedBytes": norm_local_path.stat().st_size,
                    "normalizedSha256": norm_sha,
                    "rows": pf.metadata.num_rows,
                    "status": "QUALIFIED",
                    "acquiredAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                return True
            except Exception:
                raw_local_path.unlink(missing_ok=True)
                norm_local_path.unlink(missing_ok=True)

        remote_path = self.client._generate_file_path(
            chd.exchanges.BINANCE_FUTURES, symbol, data_type, date_str, hour
        )

        for attempt in range(1, self.max_retries + 1):
            try:
                res = self.client._download_session.get(
                    self.client.download_endpoint,
                    params={"file": remote_path, "api_key": self.api_key},
                    headers={"X-API-Key": self.api_key},
                    timeout=self.timeout,
                )
                if res.status_code != 200:
                    raise ValueError(f"HTTP {res.status_code} for {remote_path}")

                raw_bytes = res.content
                if len(raw_bytes) == 0:
                    raise ValueError(f"Empty content for {remote_path}")

                raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()

                # Write raw immutable byte stream
                raw_local_path.write_bytes(raw_bytes)

                # Decompress Zstandard or parse plain Parquet
                if raw_bytes.startswith(ZSTD_MAGIC) or remote_path.endswith(".zst"):
                    dctx = zstd.ZstdDecompressor()
                    parquet_bytes = dctx.decompress(raw_bytes)
                else:
                    parquet_bytes = raw_bytes

                # Verify schema on decompressed bytes without modifying columns
                pf = pq.ParquetFile(io.BytesIO(parquet_bytes))
                rows = pf.metadata.num_rows

                # Write normalized uncompressed Parquet
                norm_local_path.write_bytes(parquet_bytes)
                norm_sha256 = hashlib.sha256(parquet_bytes).hexdigest()

                self.manifest["partitions"][partition_key] = {
                    "symbol": symbol,
                    "dataType": data_type,
                    "date": date_str,
                    "hour": hour,
                    "rawPath": str(raw_local_path),
                    "rawDeliveredBytes": len(raw_bytes),
                    "rawDeliveredSha256": raw_sha256,
                    "normalizedPath": str(norm_local_path),
                    "normalizedBytes": len(parquet_bytes),
                    "normalizedSha256": norm_sha256,
                    "rows": rows,
                    "status": "QUALIFIED",
                    "acquiredAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                logger.info(f"[{symbol} {data_type} {date_str} {hour:02d}Z] Raw: {len(raw_bytes)} B (SHA: {raw_sha256[:8]}), Normalized: {len(parquet_bytes)} B ({rows} rows)")
                return True
            except Exception as e:
                logger.warning(f"[{symbol} {data_type} {date_str} {hour:02d}Z] Attempt {attempt} failed: {e}")
                self.manifest["summary"]["retries"] += 1
                time.sleep(1.0 * attempt)

        self.manifest["partitions"][partition_key] = {
            "symbol": symbol,
            "dataType": data_type,
            "date": date_str,
            "hour": hour,
            "status": "FAILED",
            "lastError": str(e),
            "attemptedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        return False

    def run_batch(self, max_partitions_per_batch: int = 40) -> dict[str, int]:
        processed = 0
        success = 0
        current_d = self.start_date
        while current_d <= self.end_date:
            d_str = str(current_d)
            for hour in range(24):
                for sym in self.symbols:
                    for dt in self.data_types:
                        pk = f"{sym}/{dt}/{d_str}/{hour:02d}"
                        if self.manifest["partitions"].get(pk, {}).get("status") != "QUALIFIED":
                            ok = self.download_partition(sym, dt, d_str, hour)
                            processed += 1
                            if ok:
                                success += 1
                            if processed % 10 == 0:
                                self.save_manifest()
                            if processed >= max_partitions_per_batch:
                                self.save_manifest()
                                return {"processed": processed, "success": success}
            current_d += datetime.timedelta(days=1)
        self.save_manifest()
        return {"processed": processed, "success": success}


if __name__ == "__main__":
    api_key = os.environ.get("HFT_DATA_API_KEY")
    if not api_key:
        raise RuntimeError("HFT_DATA_API_KEY is not set in environment.")
    downloader = BulkHistoricalDownloader(
        api_key=api_key,
        data_root=Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data"),
        manifest_path=Path("/opt/Axodus/Trading/quants-lab/research_notebooks/orderflow_backtest/historical_acquisition_manifest.json"),
    )
    res = downloader.run_batch(max_partitions_per_batch=12)
    print(f"Verified provenance batch result: {res}")
