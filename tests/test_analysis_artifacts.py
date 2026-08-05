"""Offline integrity tests for price-change volatility artifact packages."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock

from pydantic import ValidationError

from market_validator.analysis.artifacts import (
    ArtifactConflictError,
    ArtifactFileMetadata,
    ArtifactIntegrityError,
    ArtifactPathError,
    PriceChangeVolatilityArtifactManifest,
    load_price_change_volatility_artifact,
    persist_price_change_volatility_artifact,
    render_price_change_volatility_report,
    serialize_price_change_volatility_result,
)
from market_validator.analysis.price_change_volatility import (
    PriceChangeVolatilityParameters,
    PriceChangeVolatilityResult,
    VolatilityConclusion,
    compare_price_change_volatility,
)
from market_validator.data.bundle_io import serialize_data_bundle
from market_validator.data.models import (
    DataBundle,
    DataQualityIssue,
    DataQualityReport,
    DataRequirement,
    DataSourceMetadata,
    Observation,
    QualitySeverity,
    QualityStatus,
    TimePrecision,
)
from market_validator.research.enums import DataRevisionMode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_analysis_artifacts"


class PriceChangeVolatilityArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.bundle_path = self.root / "fred-artifact-test.json"
        self.artifact_root = self.root / "artifacts"
        self.result = self._compare_fixture()

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    @staticmethod
    def _observation(session_date: date, value: float) -> Observation:
        observation_time = datetime.combine(
            session_date, datetime.min.time(), tzinfo=timezone.utc
        )
        return Observation(
            instrument_id="global.crude_oil.wti_spot",
            field="value",
            value=value,
            observation_time=observation_time,
            available_time=observation_time + timedelta(hours=12),
            session_date=session_date,
            timezone="UTC",
            currency="USD",
            unit="Dollars per Barrel",
            observation_precision=TimePrecision.DATE,
            availability_precision=TimePrecision.TIMESTAMP,
            vintage_date=session_date,
            revision_policy=DataRevisionMode.INITIAL_RELEASE,
            availability_assumption="Synthetic offline artifact fixture.",
        )

    def _group(
        self,
        start_date: date,
        changes: list[float],
        *,
        initial_price: float,
    ) -> list[Observation]:
        observations = [self._observation(start_date, initial_price)]
        current_date = start_date
        current_price = initial_price
        for change in changes:
            current_date += timedelta(days=1)
            current_price += change
            observations.append(self._observation(current_date, current_price))
        return observations

    @staticmethod
    def _requirement(observations: list[Observation]) -> DataRequirement:
        payload = json.loads(
            (
                ROOT
                / "examples"
                / "data_requirements"
                / "fred_wti_spot_initial.json"
            ).read_text(encoding="utf-8")
        )
        payload["start_date"] = min(
            item.session_date for item in observations
        ).isoformat()
        payload["end_date"] = max(
            item.session_date for item in observations
        ).isoformat()
        return DataRequirement.model_validate_json(json.dumps(payload))

    def _compare_fixture(self) -> PriceChangeVolatilityResult:
        shock = self._group(
            date(2020, 3, 1),
            [1.0, -2.0, 3.0, -4.0, 5.0],
            initial_price=100.0,
        )
        reference = self._group(
            date(2021, 1, 1),
            [0.5, -0.75, 1.0, -1.25, 1.5],
            initial_price=200.0,
        )
        observations = shock + reference
        missing_row = len(shock) + 2
        bundle = DataBundle(
            requirement=self._requirement(observations),
            observations=observations,
            source=DataSourceMetadata(
                provider_id="fred",
                dataset_id="DCOILWTICO",
                provider_symbol="DCOILWTICO",
                source_uri="https://api.stlouisfed.org/fred/series/observations",
                retrieved_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                public_request_parameters={"series_id": "DCOILWTICO"},
                content_sha256="b" * 64,
                license_note="Synthetic offline artifact fixture.",
                is_fallback=False,
            ),
            quality=DataQualityReport(
                status=QualityStatus.WARN,
                rows_read=len(observations) + 1,
                observations_parsed=len(observations),
                coverage_start=min(item.observation_time for item in observations),
                coverage_end=max(item.observation_time for item in observations),
                issues=[
                    DataQualityIssue(
                        code="provider_missing_value",
                        severity=QualitySeverity.WARNING,
                        message="Synthetic gap between the two test groups.",
                        row_number=missing_row,
                    )
                ],
            ),
        )
        self.bundle_path.write_bytes(serialize_data_bundle(bundle))
        return compare_price_change_volatility(
            self.bundle_path,
            PriceChangeVolatilityParameters(),
        )

    @staticmethod
    def _sha(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _persist(self):
        return persist_price_change_volatility_artifact(
            self.result,
            self.artifact_root,
        )

    def _read_manifest(
        self, artifact_path: Path
    ) -> PriceChangeVolatilityArtifactManifest:
        return PriceChangeVolatilityArtifactManifest.model_validate_json(
            (artifact_path / "manifest.json").read_bytes()
        )

    @staticmethod
    def _write_manifest(
        artifact_path: Path,
        manifest: PriceChangeVolatilityArtifactManifest,
    ) -> None:
        (artifact_path / "manifest.json").write_bytes(
            manifest.model_dump_json(
                indent=2,
                exclude_computed_fields=True,
            ).encode("utf-8")
        )

    def test_compare_serialize_persist_load_round_trip(self) -> None:
        persisted = self._persist()
        raw_result = persisted.result_path.read_bytes()

        direct = PriceChangeVolatilityResult.model_validate_json(raw_result)
        loaded = load_price_change_volatility_artifact(persisted.artifact_path)

        self.assertEqual(direct, self.result)
        self.assertEqual(loaded.result, self.result)
        self.assertEqual(loaded.manifest.parameters, self.result.parameters)
        self.assertEqual(loaded.manifest.source_request_id, self.result.source.request_id)
        self.assertEqual(
            loaded.manifest.source_bundle_sha256,
            self.result.source.bundle_sha256,
        )

    def test_serialization_and_report_are_byte_deterministic(self) -> None:
        self.assertEqual(
            serialize_price_change_volatility_result(self.result),
            serialize_price_change_volatility_result(self.result),
        )
        self.assertEqual(
            render_price_change_volatility_report(self.result),
            render_price_change_volatility_report(self.result),
        )

    def test_manifest_file_sizes_and_hashes_match_actual_bytes(self) -> None:
        persisted = self._persist()
        manifest = self._read_manifest(persisted.artifact_path)
        result_bytes = persisted.result_path.read_bytes()
        report_bytes = persisted.report_path.read_bytes()

        self.assertEqual(manifest.result_file.byte_size, len(result_bytes))
        self.assertEqual(manifest.result_file.sha256, self._sha(result_bytes))
        self.assertEqual(manifest.report_file.byte_size, len(report_bytes))
        self.assertEqual(manifest.report_file.sha256, self._sha(report_bytes))
        self.assertEqual(
            persisted.manifest_sha256,
            self._sha(persisted.manifest_path.read_bytes()),
        )

    def test_result_tampering_is_rejected(self) -> None:
        persisted = self._persist()
        persisted.result_path.write_bytes(persisted.result_path.read_bytes() + b" ")
        with self.assertRaises(ArtifactIntegrityError):
            load_price_change_volatility_artifact(persisted.artifact_path)

    def test_report_tampering_is_rejected(self) -> None:
        persisted = self._persist()
        persisted.report_path.write_bytes(persisted.report_path.read_bytes() + b"\n")
        with self.assertRaises(ArtifactIntegrityError):
            load_price_change_volatility_artifact(persisted.artifact_path)

    def test_manifest_unknown_field_is_rejected(self) -> None:
        persisted = self._persist()
        manifest_payload = json.loads(persisted.manifest_path.read_text("utf-8"))
        manifest_payload["unexpected"] = True
        persisted.manifest_path.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with self.assertRaises(ArtifactIntegrityError):
            load_price_change_volatility_artifact(persisted.artifact_path)

    def test_result_unknown_field_remains_strictly_rejected(self) -> None:
        persisted = self._persist()
        result_payload = json.loads(persisted.result_path.read_text("utf-8"))
        result_payload["unexpected"] = True
        changed_result = json.dumps(
            result_payload, ensure_ascii=False, indent=2
        ).encode("utf-8")
        persisted.result_path.write_bytes(changed_result)
        manifest = self._read_manifest(persisted.artifact_path)
        changed_metadata = ArtifactFileMetadata(
            filename="result.json",
            byte_size=len(changed_result),
            sha256=self._sha(changed_result),
        )
        self._write_manifest(
            persisted.artifact_path,
            manifest.model_copy(update={"result_file": changed_metadata}),
        )
        with self.assertRaises(ArtifactIntegrityError):
            load_price_change_volatility_artifact(persisted.artifact_path)
        with self.assertRaises(ValidationError):
            PriceChangeVolatilityResult.model_validate(result_payload)

    def test_manifest_result_identity_parameters_and_conclusion_mismatch_rejected(self) -> None:
        changes = (
            {"source_request_id": "different-request"},
            {"source_bundle_sha256": "c" * 64},
            {"main_conclusion": VolatilityConclusion.NOT_SUPPORTED},
            {"final_conclusion": VolatilityConclusion.NOT_SUPPORTED},
        )
        for index, update in enumerate(changes):
            with self.subTest(update=update):
                root = self.root / f"case-{index}"
                persisted = persist_price_change_volatility_artifact(self.result, root)
                manifest = self._read_manifest(persisted.artifact_path)
                self._write_manifest(
                    persisted.artifact_path,
                    manifest.model_copy(update=update),
                )
                with self.assertRaises(ArtifactIntegrityError):
                    load_price_change_volatility_artifact(persisted.artifact_path)

        parameter_root = self.root / "parameter-case"
        persisted = persist_price_change_volatility_artifact(self.result, parameter_root)
        payload = json.loads(persisted.manifest_path.read_text("utf-8"))
        payload["parameters"]["random_seed"] = 1
        persisted.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self.assertRaises(ArtifactIntegrityError):
            load_price_change_volatility_artifact(persisted.artifact_path)

    def test_coordinated_report_and_manifest_hash_change_still_rejected(self) -> None:
        persisted = self._persist()
        changed_report = persisted.report_path.read_bytes().replace(
            "最终结论：".encode(),
            "最终结论（被篡改）：".encode(),
            1,
        )
        persisted.report_path.write_bytes(changed_report)
        manifest = self._read_manifest(persisted.artifact_path)
        changed_metadata = ArtifactFileMetadata(
            filename="report.md",
            byte_size=len(changed_report),
            sha256=self._sha(changed_report),
        )
        self._write_manifest(
            persisted.artifact_path,
            manifest.model_copy(update={"report_file": changed_metadata}),
        )
        with self.assertRaisesRegex(ArtifactIntegrityError, "deterministic rendering"):
            load_price_change_volatility_artifact(persisted.artifact_path)

    def test_external_manifest_hash_accepts_match_and_rejects_mismatch(self) -> None:
        persisted = self._persist()
        loaded = load_price_change_volatility_artifact(
            persisted.artifact_path,
            expected_manifest_sha256=persisted.manifest_sha256,
        )
        self.assertEqual(loaded.manifest_sha256, persisted.manifest_sha256)
        with self.assertRaisesRegex(ArtifactIntegrityError, "external trust anchor"):
            load_price_change_volatility_artifact(
                persisted.artifact_path,
                expected_manifest_sha256="0" * 64,
            )

    def test_repeated_persistence_is_idempotent_and_keeps_bytes_and_mtimes(self) -> None:
        first = self._persist()
        filenames = ("result.json", "report.md", "manifest.json")
        before = {
            name: (
                (first.artifact_path / name).read_bytes(),
                (first.artifact_path / name).stat().st_mtime_ns,
            )
            for name in filenames
        }
        second = self._persist()
        after = {
            name: (
                (second.artifact_path / name).read_bytes(),
                (second.artifact_path / name).stat().st_mtime_ns,
            )
            for name in filenames
        }
        self.assertEqual(second, first)
        self.assertEqual(after, before)

    def test_existing_different_content_is_conflict_and_not_overwritten(self) -> None:
        persisted = self._persist()
        tampered = persisted.report_path.read_bytes() + b"tampered"
        persisted.report_path.write_bytes(tampered)

        with self.assertRaises(ArtifactConflictError):
            self._persist()

        self.assertEqual(persisted.report_path.read_bytes(), tampered)

    def test_path_traversal_and_symbolic_link_are_rejected(self) -> None:
        persisted = self._persist()
        traversal_path = (
            persisted.artifact_path / ".." / persisted.artifact_path.name
        )
        with self.assertRaisesRegex(ArtifactPathError, "traversal"):
            load_price_change_volatility_artifact(traversal_path)

        with mock.patch(
            "market_validator.analysis.artifacts._path_is_symlink",
            return_value=True,
        ):
            with self.assertRaisesRegex(ArtifactPathError, "symbolic"):
                load_price_change_volatility_artifact(persisted.artifact_path)

    def test_write_failure_leaves_no_visible_partial_artifact(self) -> None:
        with mock.patch(
            "market_validator.analysis.artifacts._publish_directory",
            side_effect=OSError("synthetic publication failure"),
        ):
            with self.assertRaisesRegex(OSError, "synthetic publication failure"):
                self._persist()

        self.assertTrue(self.artifact_root.exists())
        self.assertEqual(list(self.artifact_root.iterdir()), [])

    def test_exact_directory_structure_rejects_extra_entries(self) -> None:
        persisted = self._persist()
        (persisted.artifact_path / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(ArtifactPathError):
            load_price_change_volatility_artifact(persisted.artifact_path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
