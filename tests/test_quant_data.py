import unittest
from datetime import timedelta

from core.quant_data import (
    CandleNormalizer,
    DatasetBuilder,
    DatasetRegistry,
    DatasetUnavailableError,
    InMemoryDatasetStorage,
    InMemoryHistoricalSource,
    LookAheadViolation,
    reconstruct_historical_market_state,
    validate_market_state_schema,
    MarketStateValidationError,
)
from core.quant_foundations.models import DatasetManifest


def manifest(version="1.0.0"):
    return DatasetManifest(
        dataset_id="fixture:btc-usdt-1m",
        dataset_version=version,
        source_ref="fixture:synthetic",
        venue_scope=("synthetic",),
        instrument_scope=("BTC-USDT",),
        temporal_from="2024-01-01T00:00:00Z",
        temporal_to="2024-01-01T00:04:00Z",
        resolution="1m",
        schema_version="market-observation-candle-v1",
        normalization_version=CandleNormalizer.normalizer_version,
        quality_status="unknown",
    )


def candle(minute, close="100", observation_id=None):
    return {
        "instrument": "BTC-USDT",
        "venue": "synthetic",
        "observation_id": observation_id or f"obs-{minute}",
        "event_time": f"2024-01-01T00:0{minute}:00Z",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": "1.0",
        "interval": "1m",
    }


class QuantDataTests(unittest.TestCase):
    def setUp(self):
        self.builder = DatasetBuilder(CandleNormalizer())

    def test_order_is_deterministic_and_storage_is_not_identity(self):
        first = self.builder.build(InMemoryHistoricalSource([candle(2), candle(1)]), manifest(), expected_interval=timedelta(minutes=1))
        second = self.builder.build(InMemoryHistoricalSource([candle(1), candle(2)]), manifest(), expected_interval=timedelta(minutes=1))
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual([r.event_time for r in first.records], ["2024-01-01T00:01:00.000Z", "2024-01-01T00:02:00.000Z"])
        self.assertEqual(first.manifest.to_shared_reference()["contentDigest"], first.content_digest)

    def test_duplicates_are_reported_without_silent_semantic_collision(self):
        artifact = self.builder.build(InMemoryHistoricalSource([candle(1), candle(1, observation_id="obs-duplicate")]), manifest())
        self.assertEqual(artifact.quality.duplicate_count, 1)
        self.assertEqual(artifact.quality.status, "VALID_WITH_LIMITATIONS")
        self.assertEqual(len(artifact.records), 1)

    def test_missing_cadence_interval_is_preserved_in_quality_report(self):
        artifact = self.builder.build(InMemoryHistoricalSource([candle(1), candle(3)]), manifest(), expected_interval=timedelta(minutes=1))
        self.assertEqual(artifact.quality.status, "INCOMPLETE")
        self.assertEqual(artifact.quality.gaps[0]["from"], "2024-01-01T00:02:00Z")

    def test_invalid_source_record_is_explicit(self):
        artifact = self.builder.build(InMemoryHistoricalSource([{"instrument": "BTC-USDT"}]), manifest())
        self.assertEqual(artifact.quality.status, "INVALID")
        self.assertEqual(artifact.quality.normalization_failure_count, 1)

    def test_registry_resolves_exact_version_without_latest_fallback(self):
        storage = InMemoryDatasetStorage()
        registry = DatasetRegistry(storage)
        artifact = self.builder.build(InMemoryHistoricalSource([candle(1)]), manifest())
        registry.register(artifact)
        self.assertTrue(registry.verify("fixture:btc-usdt-1m", "1.0.0"))
        with self.assertRaises(DatasetUnavailableError):
            registry.resolve("fixture:btc-usdt-1m", "2.0.0")

    def test_historical_market_state_has_shared_semantics_and_causality(self):
        artifact = self.builder.build(InMemoryHistoricalSource([candle(1)]), manifest())
        state = reconstruct_historical_market_state(
            artifact.records[0],
            artifact.manifest.to_shared_reference(),
            [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:01:00.000Z"}],
        )
        self.assertEqual(state["validityContext"], "historical")
        self.assertEqual(state["freshness"], "historical")
        self.assertIn("dataset:fixture:btc-usdt-1m@1.0.0", state["sourceRefs"])
        with self.assertRaises(LookAheadViolation):
            reconstruct_historical_market_state(
                artifact.records[0], artifact.manifest.to_shared_reference(),
                [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:02:00.000Z"}],
            )

    def test_dataset_version_correction_is_distinct_and_immutable(self):
        storage = InMemoryDatasetStorage()
        registry = DatasetRegistry(storage)
        first = self.builder.build(InMemoryHistoricalSource([candle(1, "100")]), manifest("1.0.0"))
        corrected = self.builder.build(InMemoryHistoricalSource([candle(1, "101")]), manifest("2.0.0"))
        registry.register(first)
        registry.register(corrected)
        self.assertEqual(registry.resolve("fixture:btc-usdt-1m", "1.0.0").records[0].value["close"], "100")
        self.assertEqual(registry.resolve("fixture:btc-usdt-1m", "2.0.0").records[0].value["close"], "101")
        with self.assertRaises(ValueError):
            registry.register(self.builder.build(InMemoryHistoricalSource([candle(1, "102")]), manifest("1.0.0")))



    def test_validate_market_state_schema_enforces_cross_epic_conformance(self):
        valid_state = {
            "marketStateId": "historical:fixture:1:obs-1",
            "instrument": "BTC-USDT",
            "venue": "synthetic",
            "timeframe": "1m",
            "marketTime": "2024-01-01T00:01:00.000Z",
            "observedAt": "2024-01-01T00:01:00.000Z",
            "constructedAt": "2024-01-01T00:01:00.000Z",
            "validityContext": "historical",
            "freshness": "historical",
            "schemaVersion": "1.0.0",
            "features": [{"featureId": "close", "featureVersion": "1.0.0", "value": "100", "computedAt": "2024-01-01T00:01:00.000Z"}],
            "sourceRefs": ["fixture:synthetic"],
            "completeness": "complete",
            "validationRefs": ["historical-causality-v1"],
        }
        # Valid state passes
        validate_market_state_schema(valid_state)

        # Missing required field fails
        invalid_missing = dict(valid_state)
        del invalid_missing["schemaVersion"]
        with self.assertRaises(MarketStateValidationError):
            validate_market_state_schema(invalid_missing)

        # Invalid feature fails
        invalid_feat = dict(valid_state, features=[{"featureId": "close", "featureVersion": "1.0.0", "computedAt": "2024-01-01T00:01:00.000Z"}])
        with self.assertRaises(MarketStateValidationError):
            validate_market_state_schema(invalid_feat)

        # Live context with non-current freshness fails
        invalid_live = dict(valid_state, validityContext="live", freshness="stale")
        with self.assertRaises(MarketStateValidationError):
            validate_market_state_schema(invalid_live)


if __name__ == "__main__":
    unittest.main()
