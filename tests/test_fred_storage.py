"""Offline persistence and secret-containment tests for FRED snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest

from pydantic import ValidationError

from market_validator.data.bundle_io import (
    DataBundleWireFormatError,
    deserialize_data_bundle,
    load_data_bundle,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import DataBundle, DataRequirement
from market_validator.data.providers.fred_provider import FredProvider
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.storage import (
    FredSnapshotManifest,
    FredStorage,
    FredStorageError,
    resolve_data_directory,
)
from tests.test_fred_provider import FakeTransport, RETRIEVED_AT, SENTINEL_KEY

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_fred_storage"


class FredStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        calendars = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")
        cls.registry = InstrumentRegistry.from_json_file(
            ROOT / "config" / "instruments.json", calendars
        )

    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    def requirement(self) -> DataRequirement:
        return DataRequirement.model_validate_json(
            (
                ROOT
                / "examples"
                / "data_requirements"
                / "fred_wti_spot_initial.json"
            ).read_text(encoding="utf-8")
        )

    def fetch_to_storage(self):
        storage = FredStorage(self.root)
        provider = FredProvider(
            self.registry,
            allow_network=True,
            environment={"FRED_API_KEY": SENTINEL_KEY},
            transport=FakeTransport(),
            storage=storage,
            clock=lambda: RETRIEVED_AT,
        )
        bundle = provider.fetch(self.requirement())
        return storage, provider.last_snapshot, bundle

    def test_default_and_relative_data_directory_resolution(self) -> None:
        self.assertEqual(
            resolve_data_directory({}, current_directory=self.root),
            (self.root / ".market_validator" / "data").resolve(),
        )
        self.assertEqual(
            resolve_data_directory(
                {"MARKET_VALIDATOR_DATA_DIR": "artifacts"},
                current_directory=self.root,
            ),
            (self.root / "artifacts").resolve(),
        )

    def test_complete_snapshot_has_exact_raw_bytes_and_verified_hashes(self) -> None:
        _, snapshot, bundle = self.fetch_to_storage()
        self.assertIsNotNone(snapshot)
        paths = [
            snapshot.raw_series_path,
            *snapshot.raw_observation_paths,
            snapshot.bundle_path,
            snapshot.manifest_path,
        ]
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(len(paths), 4)

        manifest = FredSnapshotManifest.model_validate_json(
            snapshot.manifest_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest.public_request_parameters["realtime_start"], "1776-07-04"
        )
        self.assertEqual(
            manifest.public_request_parameters["realtime_end"], "9999-12-31"
        )
        self.assertEqual(manifest.public_request_parameters["output_type"], 4)
        self.assertEqual(
            manifest.public_request_parameters["observation_start"], "2020-01-01"
        )
        self.assertEqual(
            manifest.public_request_parameters["observation_end"], "2024-12-31"
        )
        self.assertEqual(
            manifest.raw_series.sha256,
            hashlib.sha256(snapshot.raw_series_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest.raw_observations[0].sha256,
            hashlib.sha256(snapshot.raw_observation_paths[0].read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest.bundle.sha256,
            hashlib.sha256(snapshot.bundle_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(snapshot.bundle_sha256, manifest.bundle.sha256)
        self.assertEqual(
            snapshot.manifest_sha256,
            hashlib.sha256(snapshot.manifest_path.read_bytes()).hexdigest(),
        )
        bundle_bytes = snapshot.bundle_path.read_bytes()
        stored_payload = json.loads(bundle_bytes)
        stored_bundle = DataBundle.model_validate_json(bundle_bytes)
        self.assertNotIn("status", stored_payload)
        self.assertEqual(
            stored_payload["quality"]["status"], bundle.quality.status.value
        )
        self.assertEqual(stored_bundle.status, bundle.status)
        self.assertEqual(stored_bundle, bundle)
        self.assertEqual(load_data_bundle(snapshot.bundle_path), bundle)

    def test_matching_legacy_status_is_loaded_without_rewriting(self) -> None:
        _, snapshot, bundle = self.fetch_to_storage()
        payload = json.loads(snapshot.bundle_path.read_bytes())
        payload["status"] = payload["quality"]["status"]
        legacy_path = self.root / "legacy-bundle.json"
        legacy_bytes = json.dumps(payload).encode("utf-8")
        legacy_path.write_bytes(legacy_bytes)

        loaded = load_data_bundle(legacy_path)

        self.assertEqual(loaded, bundle)
        self.assertEqual(loaded.status, bundle.quality.status)
        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)

    def test_mismatched_legacy_status_is_rejected(self) -> None:
        _, snapshot, _ = self.fetch_to_storage()
        payload = json.loads(snapshot.bundle_path.read_bytes())
        payload["status"] = "fail"

        with self.assertRaisesRegex(
            DataBundleWireFormatError, "must exactly match quality.status"
        ):
            deserialize_data_bundle(json.dumps(payload))

    def test_legacy_status_with_another_unknown_field_is_rejected(self) -> None:
        _, snapshot, _ = self.fetch_to_storage()
        payload = json.loads(snapshot.bundle_path.read_bytes())
        payload["status"] = payload["quality"]["status"]
        payload["unexpected"] = True

        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            deserialize_data_bundle(json.dumps(payload))

    def test_ordinary_unknown_bundle_field_remains_strictly_rejected(self) -> None:
        _, snapshot, _ = self.fetch_to_storage()
        payload = json.loads(snapshot.bundle_path.read_bytes())
        payload["unexpected"] = True

        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            deserialize_data_bundle(json.dumps(payload))

    def test_sentinel_key_is_absent_from_all_names_and_files(self) -> None:
        _, snapshot, bundle = self.fetch_to_storage()
        self.assertNotIn(SENTINEL_KEY, bundle.model_dump_json())
        for path in self.root.rglob("*"):
            self.assertNotIn(SENTINEL_KEY, path.name)
            if path.is_file():
                self.assertNotIn(SENTINEL_KEY.encode(), path.read_bytes())
        self.assertNotIn(SENTINEL_KEY, repr(snapshot.public_summary()))

    def test_identical_snapshot_is_never_overwritten(self) -> None:
        storage, snapshot, bundle = self.fetch_to_storage()
        original_manifest = snapshot.manifest_path.read_bytes()
        with self.assertRaisesRegex(FredStorageError, "overwrite"):
            storage.persist(
                series_id="DCOILWTICO",
                observation_start=self.requirement().start_date,
                observation_end=self.requirement().end_date,
                revision_policy=self.requirement().revision_policy.mode,
                retrieved_at=RETRIEVED_AT,
                public_request_parameters={"series_id": "DCOILWTICO"},
                raw_series=snapshot.raw_series_path.read_bytes(),
                raw_observations=[snapshot.raw_observation_paths[0].read_bytes()],
                bundle=bundle,
            )
        self.assertEqual(snapshot.manifest_path.read_bytes(), original_manifest)

    def test_storage_rejects_sensitive_parameters_and_unsafe_series_ids(self) -> None:
        _, snapshot, bundle = self.fetch_to_storage()
        common = {
            "observation_start": self.requirement().start_date,
            "observation_end": self.requirement().end_date,
            "revision_policy": self.requirement().revision_policy.mode,
            "retrieved_at": RETRIEVED_AT,
            "raw_series": snapshot.raw_series_path.read_bytes(),
            "raw_observations": [snapshot.raw_observation_paths[0].read_bytes()],
            "bundle": bundle,
        }
        with self.assertRaises(FredStorageError):
            FredStorage(self.root / "sensitive").persist(
                series_id="DCOILWTICO",
                public_request_parameters={"api_key": SENTINEL_KEY},
                **common,
            )
        with self.assertRaises(FredStorageError):
            FredStorage(self.root / "unsafe").persist(
                series_id="../escape",
                public_request_parameters={},
                **common,
            )
        self.assertFalse((self.root / "sensitive").exists())
        self.assertFalse((self.root / "unsafe").exists())


if __name__ == "__main__":
    unittest.main()
