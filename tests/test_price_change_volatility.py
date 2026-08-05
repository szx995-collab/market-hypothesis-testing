"""Offline tests for the fixed WTI price-change volatility comparison."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import unittest

from pydantic import ValidationError

from market_validator.analysis.price_change_volatility import (
    PriceChangeVolatilityAnalysisError,
    PriceChangeVolatilityParameters,
    PriceChangeVolatilityResult,
    PriceChangeWindow,
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
RUNTIME_PARENT = ROOT / "tests" / ".runtime_price_change_volatility"


class PriceChangeVolatilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)
        self.bundle_path = self.root / "fred-analysis-test.json"
        self.parameters = PriceChangeVolatilityParameters()

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
            or observation_time + timedelta(hours=12),
            session_date=session_date,
            timezone="UTC",
            currency="USD",
            unit="Dollars per Barrel",
            observation_precision=TimePrecision.DATE,
            availability_precision=TimePrecision.TIMESTAMP,
            vintage_date=session_date,
            revision_policy=DataRevisionMode.INITIAL_RELEASE,
            availability_assumption="Synthetic offline test availability.",
        )

    def group(
        self,
        start_date: date,
        changes: list[float],
        *,
        initial_price: float = 100.0,
        interval_days: list[int] | None = None,
    ) -> list[Observation]:
        intervals = interval_days or [1] * len(changes)
        if len(intervals) != len(changes):
            raise AssertionError("test fixture intervals must match changes")
        observations = [self.observation(start_date, initial_price)]
        current_date = start_date
        current_price = initial_price
        for change, days in zip(changes, intervals, strict=True):
            current_date += timedelta(days=days)
            current_price += change
            observations.append(self.observation(current_date, current_price))
        return observations

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

    def make_bundle(
        self,
        observations: list[Observation],
        *,
        missing_rows: tuple[int, ...],
    ) -> DataBundle:
        issues = [
            DataQualityIssue(
                code="provider_missing_value",
                severity=QualitySeverity.WARNING,
                message="Synthetic source row is explicitly missing.",
                row_number=row,
            )
            for row in missing_rows
        ]
        times = [item.observation_time for item in observations]
        return DataBundle(
            requirement=self.requirement(observations),
            observations=observations,
            source=DataSourceMetadata(
                provider_id="fred",
                dataset_id="DCOILWTICO",
                provider_symbol="DCOILWTICO",
                source_uri="https://api.stlouisfed.org/fred/series/observations",
                retrieved_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                public_request_parameters={"series_id": "DCOILWTICO"},
                content_sha256="b" * 64,
                license_note="Synthetic offline analysis fixture.",
                is_fallback=False,
            ),
            quality=DataQualityReport(
                status=QualityStatus.WARN if issues else QualityStatus.PASS,
                rows_read=len(observations) + len(missing_rows),
                observations_parsed=len(observations),
                coverage_start=min(times),
                coverage_end=max(times),
                issues=issues,
            ),
        )

    def write_two_groups(
        self,
        shock_changes: list[float],
        reference_changes: list[float],
        *,
        shock_start: date = date(2020, 3, 1),
        reference_start: date = date(2021, 1, 1),
        shock_intervals: list[int] | None = None,
        reference_intervals: list[int] | None = None,
        delayed_reference_last: bool = False,
    ) -> bytes:
        shock = self.group(
            shock_start, shock_changes, interval_days=shock_intervals
        )
        reference = self.group(
            reference_start,
            reference_changes,
            initial_price=200.0,
            interval_days=reference_intervals,
        )
        if delayed_reference_last:
            reference[-1] = reference[-1].model_copy(
                update={
                    "available_time": datetime(2025, 1, 4, tzinfo=timezone.utc)
                }
            )
        missing_between_groups = 2 + len(shock)
        bundle = self.make_bundle(
            shock + reference,
            missing_rows=(missing_between_groups,),
        )
        content = serialize_data_bundle(bundle)
        self.bundle_path.write_bytes(content)
        return content

    def test_known_summaries_boundaries_gap_accounting_and_source_trace(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        content = self.write_two_groups(values, values)

        result = compare_price_change_volatility(
            self.bundle_path, self.parameters
        )

        summary = result.main.shock
        self.assertEqual(summary.count, 5)
        self.assertEqual(summary.mean, 3.0)
        self.assertEqual(summary.median, 3.0)
        self.assertAlmostEqual(summary.sample_stddev, math.sqrt(2.5))
        self.assertEqual(summary.mad, 1.0)
        self.assertAlmostEqual(summary.quantile_05, 1.2)
        self.assertEqual(summary.quantile_25, 2.0)
        self.assertEqual(summary.quantile_75, 4.0)
        self.assertAlmostEqual(summary.quantile_95, 4.8)
        self.assertEqual(result.main.conclusion, VolatilityConclusion.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.exclusions.source_candidate_pair_count, 11)
        self.assertEqual(result.exclusions.transform_gap_excluded_count, 1)
        self.assertEqual(result.exclusions.shock_included_count, 5)
        self.assertEqual(result.exclusions.reference_included_count, 5)
        self.assertEqual(result.exclusions.outside_windows_excluded_count, 0)
        self.assertEqual(result.source.request_id, "fred-analysis-test")
        self.assertEqual(result.source.bundle_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(result.transformation_formula, "current_price - previous_price")
        self.assertEqual(result.unit, "Dollars per Barrel")
        self.assertEqual(
            PriceChangeVolatilityResult.model_validate_json(result.model_dump_json()),
            result,
        )

    def test_window_boundaries_and_overlap_are_strict(self) -> None:
        self.assertTrue(self.parameters.shock_window.contains(date(2020, 3, 1)))
        self.assertTrue(self.parameters.shock_window.contains(date(2020, 5, 31)))
        self.assertFalse(self.parameters.shock_window.contains(date(2020, 6, 1)))
        overlapping = PriceChangeWindow(
            name="shock",
            start_date=date(2020, 3, 1),
            end_date=date(2021, 1, 1),
        )
        with self.assertRaisesRegex(ValidationError, "must not overlap"):
            PriceChangeVolatilityParameters(shock_window=overlapping)

    def test_as_of_filter_is_exclusive_and_reconciles(self) -> None:
        self.write_two_groups(
            [1, -1, 2, -2, 3, -3],
            [1, -1, 2, -2, 3, -3],
            delayed_reference_last=True,
        )

        result = compare_price_change_volatility(self.bundle_path, self.parameters)

        self.assertEqual(result.exclusions.analysis_as_of_excluded_count, 1)
        self.assertEqual(result.exclusions.reference_included_count, 5)
        self.assertEqual(result.exclusions.outside_windows_excluded_count, 0)
        self.assertEqual(
            result.exclusions.transform_generated_count,
            result.exclusions.analysis_as_of_excluded_count
            + result.exclusions.outside_windows_excluded_count
            + result.exclusions.shock_included_count
            + result.exclusions.reference_included_count,
        )

    def test_weekend_is_in_main_and_removed_only_by_one_day_robustness(self) -> None:
        self.write_two_groups(
            [1, 4, -1, 2, -2, 3],
            [1, -1, 2, -2, 3, -3],
            shock_start=date(2020, 3, 5),
            shock_intervals=[1, 3, 1, 1, 1, 1],
        )

        result = compare_price_change_volatility(self.bundle_path, self.parameters)
        check = next(
            item
            for item in result.robustness_checks
            if item.check_id == "interval_days_equal_1"
        )

        self.assertEqual(result.main.shock.count, 6)
        self.assertEqual(check.shock_excluded_count, 1)
        self.assertEqual(check.estimate.shock.count, 5)
        self.assertTrue(
            any(item.code == "non_one_day_intervals_in_main" for item in result.warnings)
        )

    def test_fixed_seed_is_deterministic_and_all_three_conclusions_are_reachable(self) -> None:
        cases = [
            (
                [10, -10, 20, -20, 30],
                [1, -1, 2, -2, 3],
                VolatilityConclusion.SUPPORTED,
            ),
            (
                [1, -1, 2, -2, 3],
                [10, -10, 20, -20, 30],
                VolatilityConclusion.NOT_SUPPORTED,
            ),
            (
                [1, -1, 2, -2, 3],
                [1, -1, 2, -2, 3],
                VolatilityConclusion.INSUFFICIENT_EVIDENCE,
            ),
        ]
        for shock, reference, expected in cases:
            with self.subTest(expected=expected):
                self.write_two_groups(shock, reference)
                first = compare_price_change_volatility(
                    self.bundle_path, self.parameters
                )
                second = compare_price_change_volatility(
                    self.bundle_path, self.parameters
                )
                self.assertEqual(first.main.conclusion, expected)
                self.assertEqual(
                    first.main.confidence_interval,
                    second.main.confidence_interval,
                )

    def test_negative_date_robustness_can_downgrade_supported_main_result(self) -> None:
        self.write_two_groups(
            [1, 2, 1, -50, 50, 2, 1],
            [1, -1, 1, -1, 1, -1, 1],
            shock_start=date(2020, 4, 16),
        )

        result = compare_price_change_volatility(self.bundle_path, self.parameters)
        check = next(
            item
            for item in result.robustness_checks
            if item.check_id == "exclude_2020_04_20_endpoints"
        )

        self.assertEqual(result.main.conclusion, VolatilityConclusion.SUPPORTED)
        self.assertEqual(check.shock_excluded_count, 2)
        self.assertEqual(check.estimate.shock.count, 5)
        self.assertLess(check.estimate.stddev_ratio, 1.0)
        self.assertEqual(
            result.final_conclusion, VolatilityConclusion.INSUFFICIENT_EVIDENCE
        )

    def test_insufficient_samples_nonfinite_ratio_and_transform_error_are_rejected(self) -> None:
        self.write_two_groups(
            [1, -1, 2, -2],
            [1, -1, 2, -2, 3],
        )
        with self.assertRaisesRegex(
            PriceChangeVolatilityAnalysisError, "block_length"
        ):
            compare_price_change_volatility(self.bundle_path, self.parameters)

        self.write_two_groups(
            [1, -1, 2, -2, 3],
            [1, 1, 1, 1, 1],
        )
        with self.assertRaisesRegex(
            PriceChangeVolatilityAnalysisError, "standard deviations must be positive"
        ):
            compare_price_change_volatility(self.bundle_path, self.parameters)

        shock = self.group(date(2020, 3, 1), [1, -1, 2, -2, 3])
        shock[1] = shock[0].model_copy(update={"value": shock[1].value})
        reference = self.group(date(2021, 1, 1), [1, -1, 2, -2, 3])
        missing_row = 2 + len(shock)
        self.bundle_path.write_bytes(
            serialize_data_bundle(
                self.make_bundle(
                    shock + reference,
                    missing_rows=(missing_row,),
                )
            )
        )
        with self.assertRaisesRegex(
            PriceChangeVolatilityAnalysisError,
            "absolute_price_change transformation failed",
        ):
            compare_price_change_volatility(self.bundle_path, self.parameters)


if __name__ == "__main__":
    unittest.main()
