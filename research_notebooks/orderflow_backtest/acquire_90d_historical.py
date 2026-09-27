"""Resumable, provenance-first 90-day CryptoHFTData acquisition.

This command intentionally owns a dataset root and manifest separate from the
accepted 14-day dataset.  It never rewrites a raw source artifact: verified
existing artifacts may be hard-linked into the new dataset revision, while
missing partitions are retrieved once and then checksum recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import cryptohftdata as chd
import pyarrow.parquet as pq
import zstandard as zstd


DATASET_ID = "orderflow-binance-futures-90d-20260623-20260920-v1"
WINDOW_START = dt.date(2026, 6, 23)
WINDOW_END = dt.date(2026, 9, 20)
SYMBOLS = ("BTCUSDT", "ETHUSDC")
DATA_TYPES = ("orderbook", "trades")
ZSTD_MAGIC = bytes.fromhex("28b52ffd")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_hft_api_key(env_file: Path) -> str:
    for line in env_file.read_text().splitlines():
        item = line.strip()
        if item.startswith("HFT_DATA_API_KEY="):
            value = item.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise RuntimeError("HFT_DATA_API_KEY is absent from the configured environment file")


class Acquisition90d:
    def __init__(self, dataset_root: Path, env_file: Path, retries: int = 3, timeout: int = 30):
        self.dataset_root = dataset_root
        self.raw_root = dataset_root / "raw"
        self.normalized_root = dataset_root / "normalized"
        self.quarantine_root = dataset_root / "quarantine"
        self.manifest_path = dataset_root / "manifests" / "historical_acquisition_manifest.json"
        self.retries = retries
        self.timeout = timeout
        for path in (self.raw_root, self.normalized_root, self.quarantine_root, self.manifest_path.parent):
            path.mkdir(parents=True, exist_ok=True)
        self.api_key = load_hft_api_key(env_file)
        self.client = chd.CryptoHFTDataClient(api_key=self.api_key, timeout=timeout)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            result = json.loads(self.manifest_path.read_text())
            if result.get("datasetId") != DATASET_ID:
                raise RuntimeError("manifest datasetId does not match the frozen 90-day dataset identity")
            return result
        return {
            "datasetId": DATASET_ID,
            "datasetRevision": "v1-candidate",
            "provider": "CryptoHFTData",
            "product": "Binance USD-M Futures Order Book & Trades (hourly Parquet/Zstandard)",
            "venue": "Binance USD-M Futures",
            "symbols": list(SYMBOLS),
            "windowStart": f"{WINDOW_START}T00:00:00Z",
            "windowEnd": f"{WINDOW_END}T23:59:59.999Z",
            "contiguousDays": 90,
            "target": {"primaryTargetDays": 90, "isDays": 60, "oosDays": 30},
            "previousDataset": "orderflow-binance-futures-14d-20260623-20260706-v1",
            "partitions": {},
            "summary": {"expectedPartitions": 8640, "qualified": 0, "failed": 0, "retries": 0, "rawBytes": 0},
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def save(self) -> None:
        parts = self.manifest["partitions"].values()
        qualified = [item for item in parts if item.get("status") == "QUALIFIED"]
        self.manifest["summary"].update({
            "expectedPartitions": 90 * 24 * len(SYMBOLS) * len(DATA_TYPES),
            "qualified": len(qualified),
            "failed": sum(1 for item in parts if item.get("status") in {"FAILED", "QUARANTINED"}),
            "rawBytes": sum(item.get("rawBytes", 0) for item in qualified),
        })
        self.manifest["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self.manifest_path)

    @staticmethod
    def key(symbol: str, data_type: str, day: dt.date, hour: int) -> str:
        return f"{symbol}/{data_type}/{day.isoformat()}/{hour:02d}"

    def paths(self, symbol: str, data_type: str, day: dt.date, hour: int) -> tuple[Path, Path]:
        raw = self.raw_root / day.isoformat() / f"{hour:02d}" / f"{symbol}_{data_type}.parquet.zst"
        normalized = self.normalized_root / day.isoformat() / f"{hour:02d}" / f"{symbol}_{data_type}.parquet"
        raw.parent.mkdir(parents=True, exist_ok=True)
        normalized.parent.mkdir(parents=True, exist_ok=True)
        return raw, normalized

    def _record_existing(self, key: str, symbol: str, data_type: str, day: dt.date, hour: int, raw: Path, normalized: Path, source: str) -> bool:
        try:
            parquet = pq.ParquetFile(normalized)
            self.manifest["partitions"][key] = {
                "symbol": symbol, "dataType": data_type, "date": day.isoformat(), "hour": hour,
                "rawPath": str(raw), "normalizedPath": str(normalized), "rawBytes": raw.stat().st_size,
                "normalizedBytes": normalized.stat().st_size, "rawSha256": sha256_file(raw),
                "normalizedSha256": sha256_file(normalized), "rows": parquet.metadata.num_rows,
                "status": "QUALIFIED", "source": source,
                "accountedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            return True
        except Exception:
            return False

    def reuse_14d_partition(self, symbol: str, data_type: str, day: dt.date, hour: int) -> bool:
        if not (WINDOW_START <= day <= dt.date(2026, 7, 6)):
            return False
        legacy = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data")
        legacy_raw = legacy / "raw" / day.isoformat() / f"{hour:02d}" / f"{symbol}_{data_type}.parquet.zst"
        legacy_normalized = legacy / "normalized" / "canonical" / day.isoformat() / f"{hour:02d}" / f"{symbol}_{data_type}.parquet"
        raw, normalized = self.paths(symbol, data_type, day, hour)
        if not (legacy_raw.exists() and legacy_normalized.exists()):
            return False
        for source, target in ((legacy_raw, raw), (legacy_normalized, normalized)):
            if not target.exists():
                os.link(source, target)
        return self._record_existing(self.key(symbol, data_type, day, hour), symbol, data_type, day, hour, raw, normalized, "REUSED_14D_VERIFIED_ARTIFACT")

    def acquire(self, symbol: str, data_type: str, day: dt.date, hour: int) -> bool:
        key = self.key(symbol, data_type, day, hour)
        existing = self.manifest["partitions"].get(key, {})
        if existing.get("status") == "QUALIFIED":
            return True
        raw, normalized = self.paths(symbol, data_type, day, hour)
        if raw.exists() and normalized.exists() and self._record_existing(key, symbol, data_type, day, hour, raw, normalized, "EXISTING_VERIFIED_ARTIFACT"):
            return True
        if self.reuse_14d_partition(symbol, data_type, day, hour):
            return True

        remote_path = self.client._generate_file_path(chd.exchanges.BINANCE_FUTURES, symbol, data_type, day.isoformat(), hour)
        error = "unknown"
        for attempt in range(1, self.retries + 1):
            try:
                response = self.client._download_session.get(
                    self.client.download_endpoint,
                    params={"file": remote_path, "api_key": self.api_key},
                    headers={"X-API-Key": self.api_key}, timeout=self.timeout,
                )
                if response.status_code != 200 or not response.content:
                    raise RuntimeError(f"HTTP {response.status_code}, bytes={len(response.content)}")
                payload = response.content
                parquet_bytes = zstd.ZstdDecompressor().decompress(payload) if payload.startswith(ZSTD_MAGIC) else payload
                parquet = pq.ParquetFile(io.BytesIO(parquet_bytes))
                raw.write_bytes(payload)
                normalized.write_bytes(parquet_bytes)
                self.manifest["partitions"][key] = {
                    "symbol": symbol, "dataType": data_type, "date": day.isoformat(), "hour": hour,
                    "providerObject": remote_path, "rawPath": str(raw), "normalizedPath": str(normalized),
                    "rawBytes": len(payload), "normalizedBytes": len(parquet_bytes),
                    "rawSha256": hashlib.sha256(payload).hexdigest(),
                    "normalizedSha256": hashlib.sha256(parquet_bytes).hexdigest(),
                    "rows": parquet.metadata.num_rows, "status": "QUALIFIED", "source": "CRYPTOHFTDATA_DOWNLOAD",
                    "acquiredAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                return True
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc)
                self.manifest["summary"]["retries"] += 1
                time.sleep(attempt)
        self.manifest["partitions"][key] = {
            "symbol": symbol, "dataType": data_type, "date": day.isoformat(), "hour": hour,
            "status": "FAILED", "attempts": self.retries, "lastError": error,
            "attemptedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        return False

    def planned_partitions(self) -> Iterable[tuple[str, str, dt.date, int]]:
        day = WINDOW_START
        while day <= WINDOW_END:
            for hour in range(24):
                for symbol in SYMBOLS:
                    for data_type in DATA_TYPES:
                        yield symbol, data_type, day, hour
            day += dt.timedelta(days=1)

    def run(self, limit: int, dry_run: bool) -> dict[str, int]:
        processed = successful = 0
        for symbol, data_type, day, hour in self.planned_partitions():
            key = self.key(symbol, data_type, day, hour)
            if self.manifest["partitions"].get(key, {}).get("status") == "QUALIFIED":
                continue
            if dry_run:
                processed += 1
            else:
                successful += int(self.acquire(symbol, data_type, day, hour))
                processed += 1
                if processed % 8 == 0:
                    self.save()
            if processed >= limit:
                break
        if not dry_run:
            self.save()
        return {"processed": processed, "successful": successful, "remaining": self.manifest["summary"]["expectedPartitions"] - self.manifest["summary"]["qualified"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, default=Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data/datasets") / DATASET_ID)
    parser.add_argument("--env-file", type=Path, default=Path("/opt/Axodus/Trading/.env.local"))
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    acquisition = Acquisition90d(args.dataset_root, args.env_file)
    print(json.dumps(acquisition.run(args.limit, args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
