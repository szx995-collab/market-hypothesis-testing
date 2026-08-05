"""Offline tests for missing-aware, auditable price return transformations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import unittest

from pydantic import ValidationError

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
from market_validator.data.returns import (
    AbsolutePriceChangeError,
    AbsolutePriceChangeSeries,
    ReturnSeries,
    ReturnTransformationError,
    transform_absolute_price_change,
    transform_price_bundle,
)
from market_validator.research.enums import DataRevisionMode, Transformation

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_returns"


class ReturnTransformationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)
        self.bundle_path = self.root / "fred-test-request.json"

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    @staticmethod
    def observation(
        session_date: date,
        value: float,
        *,
        available_time: datetime | None = None,
    ) -> Observation:
        observation_time = datetime.combine(
            session_date, datetime.min.time(), tzinfo=timezone.utc
        )
        return Observation(
            instrument_id="global.crude_oil.wti_spot",
            field="value",
            value=value,
            observation_time=observation_time,
            available_time=available_time
            or observation_time + timedelta(days=1),
            session_date=session_date,
            timezone="UTC",
            currency="USD",
            unit="Dollars per Barrel",
            observation_precision=TimePrecision.DATE,
            availability_precision=TimePrecision.DATE,
            vintage_date=session_date + timedelta(days=1),
            revision_policy=DataRevisionMode.INITIAL_RELEASE,
            availability_assumption="Synthetic conservative test availability.",
        )

    @staticmethod
    def requirement(observations: list[Observation]) -> DataRequirement:
        payload = json.loads(
            (
                ROOT
                / "examples"
                / "data_requirements"
                / "fred_wti_spot_initial.json"
            ).read_text(encoding="utf-8")
        )
        payload["start_date"] = min(item.session_date for item in observations).isoformat()
        payload["end_date"] = max(item.session_date for item in observations).isoformat()
        return DataRequirement.model_validate_json(json.dumps(payload))

    def bundle(
        self,
        observations: list[Observation],
        *,
        missing_rows: tuple[int, ...] = (),
    ) -> DataBundle:
        issues = [
            DataQualityIssue(
                code="provider_missing_value",
                severity=QualitySeverity.WARNING,
                message="Provider represented this source row as missing.",
                row_number=row,
            )
            for row in missing_rows
        ]
        times = [item.observation_time for item in observations]
        quality = DataQualityReport(
            status=QualityStatus.WARN if issues else QualityStatus.PASS,
            rows_read=len(observations) + len(missing_rows),
            observations_parsed=len(observations),
            coverage_start=min(times),
            coverage_end=max(times),
            issues=issues,
        )
        source = DataSourceMetadata(
            provider_id="fred",
            dataset_id="DCOILWTICO",
            provider_symbol="DCOILWTICO",
            source_uri="https://api.stlouisfed.org/fred/series/observations",
            retrieved_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            public_request_parameters={"series_id": "DCOILWTICO"},
            content_sha256="a" * 64,
            license_note="Synthetic offline fixture.",
            is_fallback=False,
        )
        return DataBundle(
            requirement=self.requirement(observations),
            observations=observations,
            source=source,
            quality=quality,
        )

    def write_bundle(self, bundle: DataBundle, *, legacy: bool = False) -> bytes:
        content = serialize_data_bundle(bundle)
        if legacy:
            payload = json.loads(content)
            payload["status"] = payload["quality"]["status"]
            content = json.dumps(payload).encode("utf-8")
        self.bundle_path.write_bytes(content)
        return content

    def test_simple_returns_and_provenance_are_serializable(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 3), 110.0),
            self.observation(date(2024, 1, 4), 99.0),
        ]
        content = self.write_bundle(self.bundle(observations))

        result = transform_price_bundle(
            self.bundle_path, Transformation.SIMPLE_RETURN
        )

        self.assertEqual(result.input_observation_count, 3)
        self.assertEqual(result.standard_return_count, 2)
        self.assertAlmostEqual(result.returns[0].value, 0.1)
        self.assertAlmostEqual(result.returns[1].value, -0.1)
        self.assertEqual(result.returns[0].start_session_date, date(2024, 1, 2))
        self.assertEqual(result.source.request_id, "fred-test-request")
        self.assertEqual(
            result.source.bundle_sha256, hashlib.sha256(content).hexdigest()
        )
        self.assertEqual(
            result.parameters.formula, "current_price / previous_price - 1"
        )
        self.assertEqual(ReturnSeries.model_validate_json(result.model_dump_json()), result)

    def test_log_returns_use_the_price_ratio(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 3), 110.0),
        ]
        self.write_bundle(self.bundle(observations))

        result = transform_price_bundle(self.bundle_path, "log_return")

        self.assertAlmostEqual(result.returns[0].value, math.log(1.1))
        self.assertEqual(
            result.parameters.formula, "log(current_price / previous_price)"
        )

    def test_return_availability_is_the_later_endpoint(self) -> None:
        later_first = datetime(2024, 1, 8, 23, 59, tzinfo=timezone.utc)
        earlier_second = datetime(2024, 1, 5, 23, 59, tzinfo=timezone.utc)
        observations = [
            self.observation(
                date(2024, 1, 2), 100.0, available_time=later_first
            ),
            self.observation(
                date(2024, 1, 3), 101.0, available_time=earlier_second
            ),
        ]
        self.write_bundle(self.bundle(observations))

        item = transform_price_bundle(self.bundle_path, "simple_return").returns[0]

        self.assertEqual(item.start_available_time, later_first)
        self.assertEqual(item.end_available_time, earlier_second)
        self.assertEqual(item.available_time, later_first)

    def test_legacy_bundle_uses_the_unified_loader(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 3), 101.0),
        ]
        original = self.write_bundle(self.bundle(observations), legacy=True)

        result = transform_price_bundle(self.bundle_path, "simple_return")

        self.assertEqual(result.standard_return_count, 1)
        self.assertEqual(self.bundle_path.read_bytes(), original)

    def test_single_price_produces_no_return(self) -> None:
        observations = [self.observation(date(2024, 1, 2), 100.0)]
        self.write_bundle(self.bundle(observations))

        result = transform_price_bundle(self.bundle_path, "simple_return")

        self.assertEqual(result.standard_return_count, 0)
        self.assertEqual(result.returns, [])
        self.assertEqual(result.gap_warnings, [])

    def test_weekend_interval_is_not_a_provider_missing_gap(self) -> None:
        observations = [
            self.observation(date(2024, 1, 5), 100.0),
            self.observation(date(2024, 1, 8), 102.0),
        ]
        self.write_bundle(self.bundle(observations))

        result = transform_price_bundle(self.bundle_path, "simple_return")

        self.assertEqual(result.standard_return_count, 1)
        self.assertEqual(result.excluded_gap_count, 0)
        self.assertEqual(result.returns[0].interval_days, 3)

    def test_explicit_missing_row_excludes_only_the_cross_gap_pair(self) -> None:
        observations = [
            self.observation(date(2024, 1, 8), 100.0),
            self.observation(date(2024, 1, 10), 102.0),
            self.observation(date(2024, 1, 11), 103.0),
        ]
        self.write_bundle(self.bundle(observations, missing_rows=(3,)))

        result = transform_price_bundle(self.bundle_path, "simple_return")

        self.assertEqual(result.excluded_gap_count, 1)
        self.assertEqual(result.standard_return_count, 1)
        self.assertEqual(result.gap_warnings[0].provider_missing_row_numbers, [3])
        self.assertEqual(result.returns[0].start_session_date, date(2024, 1, 10))
        self.assertEqual(result.returns[0].end_session_date, date(2024, 1, 11))
        self.assertEqual(result.parameters.missing_value_fill, "none")

    def test_2024_long_gap_is_warned_and_excluded(self) -> None:
        observations = [
            self.observation(date(2024, 5, 30), 79.0),
            self.observation(date(2024, 6, 26), 81.0),
            self.observation(date(2024, 6, 27), 82.0),
        ]
        self.write_bundle(self.bundle(observations, missing_rows=tuple(range(3, 21))))

        result = transform_price_bundle(self.bundle_path, "log_return")

        warning = result.gap_warnings[0]
        self.assertEqual(result.excluded_gap_count, 1)
        self.assertEqual(result.standard_return_count, 1)
        self.assertEqual(warning.start_session_date, date(2024, 5, 30))
        self.assertEqual(warning.end_session_date, date(2024, 6, 26))
        self.assertEqual(warning.interval_days, 27)
        self.assertEqual(warning.provider_missing_count, 18)
        self.assertTrue(warning.excluded_from_standard_returns)

    def test_zero_and_negative_prices_are_rejected(self) -> None:
        for bad_price in (0.0, -1.0):
            with self.subTest(price=bad_price):
                observations = [
                    self.observation(date(2024, 1, 2), 100.0),
                    self.observation(date(2024, 1, 3), bad_price),
                ]
                self.write_bundle(self.bundle(observations))
                with self.assertRaisesRegex(
                    ReturnTransformationError, "strictly positive"
                ):
                    transform_price_bundle(self.bundle_path, "simple_return")

    def test_non_finite_price_is_rejected_by_bundle_loading(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 3), 101.0),
        ]
        self.write_bundle(self.bundle(observations))
        payload = json.loads(self.bundle_path.read_bytes())
        payload["observations"][1]["value"] = float("nan")
        self.bundle_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ValidationError):
            transform_price_bundle(self.bundle_path, "simple_return")

    def test_duplicate_and_out_of_order_prices_are_rejected(self) -> None:
        duplicate = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 2), 101.0),
        ]
        self.write_bundle(self.bundle(duplicate))
        with self.assertRaisesRegex(ReturnTransformationError, "duplicate"):
            transform_price_bundle(self.bundle_path, "simple_return")

        reversed_observations = [
            self.observation(date(2024, 1, 3), 101.0),
            self.observation(date(2024, 1, 2), 100.0),
        ]
        self.write_bundle(self.bundle(reversed_observations))
        with self.assertRaisesRegex(ReturnTransformationError, "out of order"):
            transform_price_bundle(self.bundle_path, "simple_return")

    def test_source_bundle_manifest_and_raw_files_are_unchanged(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 100.0),
            self.observation(date(2024, 1, 3), 101.0),
        ]
        self.write_bundle(self.bundle(observations))
        related_paths = [
            self.bundle_path,
            self.root / "manifest.json",
            self.root / "series.json",
            self.root / "observations.json",
        ]
        related_paths[1].write_bytes(b'{"manifest":true}')
        related_paths[2].write_bytes(b'{"series":true}')
        related_paths[3].write_bytes(b'{"observations":true}')
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in related_paths
        }

        transform_price_bundle(self.bundle_path, "simple_return")

        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in related_paths
        }
        self.assertEqual(after, before)

    def test_absolute_price_change_is_signed_auditable_and_serializable(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 70.0),
            self.observation(date(2024, 1, 3), 72.5),
            self.observation(date(2024, 1, 4), 68.0),
        ]
        content = self.write_bundle(self.bundle(observations))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.input_observation_count, 3)
        self.assertEqual(result.candidate_pair_count, 2)
        self.assertEqual(result.price_change_count, 2)
        self.assertEqual(result.price_changes[0].start_price, 70.0)
        self.assertEqual(result.price_changes[0].end_price, 72.5)
        self.assertEqual(result.price_changes[0].value, 2.5)
        self.assertEqual(result.price_changes[1].value, -4.5)
        self.assertEqual(result.formula, "current_price - previous_price")
        self.assertEqual(result.unit, "Dollars per Barrel")
        self.assertEqual(result.source.request_id, "fred-test-request")
        self.assertEqual(
            result.source.bundle_sha256, hashlib.sha256(content).hexdigest()
        )
        self.assertEqual(result.price_changes[0].source_request_id, result.source.request_id)
        self.assertEqual(
            result.price_changes[0].source_bundle_sha256,
            result.source.bundle_sha256,
        )
        self.assertEqual(result.price_changes[0].start_source_row_number, 2)
        self.assertEqual(result.price_changes[0].end_source_row_number, 3)
        self.assertEqual(
            AbsolutePriceChangeSeries.model_validate_json(result.model_dump_json()),
            result,
        )

    def test_negative_price_endpoints_are_preserved(self) -> None:
        observations = [
            self.observation(date(2020, 4, 17), 18.31),
            self.observation(date(2020, 4, 20), -36.98),
            self.observation(date(2020, 4, 21), 8.91),
        ]
        self.write_bundle(self.bundle(observations))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.price_change_count, 2)
        self.assertEqual(result.price_changes[0].end_price, -36.98)
        self.assertAlmostEqual(result.price_changes[0].value, -55.29)
        self.assertEqual(result.price_changes[1].start_price, -36.98)
        self.assertAlmostEqual(result.price_changes[1].value, 45.89)

    def test_zero_price_endpoints_are_preserved(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 5.0),
            self.observation(date(2024, 1, 3), 0.0),
            self.observation(date(2024, 1, 4), 7.0),
        ]
        self.write_bundle(self.bundle(observations))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual([item.value for item in result.price_changes], [-5.0, 7.0])

    def test_first_price_produces_no_absolute_change(self) -> None:
        observations = [self.observation(date(2024, 1, 2), -36.98)]
        self.write_bundle(self.bundle(observations))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.candidate_pair_count, 0)
        self.assertEqual(result.price_change_count, 0)
        self.assertEqual(result.price_changes, [])

    def test_absolute_change_uses_later_availability_not_fetch_time(self) -> None:
        later_first = datetime(2024, 1, 8, 23, 59, tzinfo=timezone.utc)
        earlier_second = datetime(2024, 1, 5, 23, 59, tzinfo=timezone.utc)
        observations = [
            self.observation(
                date(2024, 1, 2), -1.0, available_time=later_first
            ),
            self.observation(
                date(2024, 1, 3), 0.0, available_time=earlier_second
            ),
        ]
        self.write_bundle(self.bundle(observations))

        item = transform_absolute_price_change(self.bundle_path).price_changes[0]

        self.assertEqual(item.available_time, later_first)
        self.assertNotEqual(item.available_time, datetime(2026, 8, 4, tzinfo=timezone.utc))

    def test_weekend_absolute_change_is_generated_with_three_day_interval(self) -> None:
        observations = [
            self.observation(date(2024, 1, 5), 70.0),
            self.observation(date(2024, 1, 8), 69.0),
        ]
        self.write_bundle(self.bundle(observations))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.price_change_count, 1)
        self.assertEqual(result.excluded_gap_count, 0)
        self.assertEqual(result.price_changes[0].interval_days, 3)
        self.assertEqual(result.price_changes[0].value, -1.0)

    def test_missing_row_excludes_cross_gap_absolute_change_without_fill(self) -> None:
        observations = [
            self.observation(date(2024, 1, 8), 70.0),
            self.observation(date(2024, 1, 10), 72.0),
            self.observation(date(2024, 1, 11), 73.0),
        ]
        self.write_bundle(self.bundle(observations, missing_rows=(3,)))

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.candidate_pair_count, 2)
        self.assertEqual(result.price_change_count, 1)
        self.assertEqual(result.excluded_gap_count, 1)
        self.assertEqual(result.other_exclusion_count, 0)
        self.assertEqual(result.gap_warnings[0].provider_missing_row_numbers, [3])
        self.assertEqual(result.gap_warnings[0].provider_missing_count, 1)
        self.assertEqual(result.price_changes[0].start_session_date, date(2024, 1, 10))
        self.assertEqual(result.parameters.missing_value_fill, "none")
        self.assertNotIn(
            (date(2024, 1, 8), date(2024, 1, 10)),
            [
                (item.start_session_date, item.end_session_date)
                for item in result.price_changes
            ],
        )

    def test_2024_long_gap_absolute_change_is_warned_and_excluded(self) -> None:
        observations = [
            self.observation(date(2024, 5, 30), 79.0),
            self.observation(date(2024, 6, 26), 81.0),
            self.observation(date(2024, 6, 27), 82.0),
        ]
        self.write_bundle(self.bundle(observations, missing_rows=tuple(range(3, 21))))

        result = transform_absolute_price_change(self.bundle_path)

        warning = result.gap_warnings[0]
        self.assertEqual(result.candidate_pair_count, 2)
        self.assertEqual(result.price_change_count, 1)
        self.assertEqual(result.excluded_gap_count, 1)
        self.assertEqual(warning.start_session_date, date(2024, 5, 30))
        self.assertEqual(warning.end_session_date, date(2024, 6, 26))
        self.assertEqual(warning.provider_missing_count, 18)
        self.assertEqual(warning.provider_missing_row_numbers, list(range(3, 21)))
        self.assertTrue(warning.excluded_from_price_changes)

    def test_absolute_change_accepts_legacy_bundle_without_rewriting(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), -1.0),
            self.observation(date(2024, 1, 3), 0.0),
        ]
        original = self.write_bundle(self.bundle(observations), legacy=True)

        result = transform_absolute_price_change(self.bundle_path)

        self.assertEqual(result.price_changes[0].value, 1.0)
        self.assertEqual(self.bundle_path.read_bytes(), original)

    def test_absolute_change_rejects_nonfinite_duplicate_order_and_ambiguity(self) -> None:
        observations = [
            self.observation(date(2024, 1, 2), 1.0),
            self.observation(date(2024, 1, 3), 2.0),
        ]
        self.write_bundle(self.bundle(observations))
        payload = json.loads(self.bundle_path.read_bytes())
        payload["observations"][1]["value"] = float("nan")
        self.bundle_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValidationError):
            transform_absolute_price_change(self.bundle_path)

        duplicate = [
            self.observation(date(2024, 1, 2), -1.0),
            self.observation(date(2024, 1, 2), 0.0),
        ]
        self.write_bundle(self.bundle(duplicate))
        with self.assertRaisesRegex(AbsolutePriceChangeError, "duplicate"):
            transform_absolute_price_change(self.bundle_path)

        reversed_observations = [
            self.observation(date(2024, 1, 3), 0.0),
            self.observation(date(2024, 1, 2), -1.0),
        ]
        self.write_bundle(self.bundle(reversed_observations))
        with self.assertRaisesRegex(AbsolutePriceChangeError, "out of order"):
            transform_absolute_price_change(self.bundle_path)

        ambiguous = self.bundle(observations)
        ambiguous_quality = ambiguous.quality.model_copy(
            update={"rows_read": ambiguous.quality.rows_read + 1}
        )
        self.write_bundle(ambiguous.model_copy(update={"quality": ambiguous_quality}))
        with self.assertRaisesRegex(AbsolutePriceChangeError, "mapped uniquely"):
            transform_absolute_price_change(self.bundle_path)


if __name__ == "__main__":
    unittest.main()
