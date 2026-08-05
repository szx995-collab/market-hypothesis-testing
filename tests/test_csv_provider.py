"""Offline tests for local CSV parsing, provenance, and quality."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import QualityStatus
from market_validator.data.planner import plan_data_requirements
from market_validator.data.providers.csv_provider import CSVProvider
from market_validator.data.registry import InstrumentRegistry
from market_validator.research.models import ResearchSpec

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def oil_outcome_requirement():
    calendars = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")
    instruments = InstrumentRegistry.from_json_file(
        ROOT / "config" / "instruments.json", calendars
    )
    spec = ResearchSpec.model_validate_json(
        (ROOT / "examples" / "research_specs" / "oil_to_a_share_energy.json").read_text(
            encoding="utf-8"
        )
    )
    plan = plan_data_requirements(spec, instruments, calendars)
    return next(item for item in plan.requirements if item.variable_id == "a_share_energy_return")


class CSVProviderTest(unittest.TestCase):
    def test_valid_csv_produces_bundle(self) -> None:
        path = FIXTURES / "valid_market_data.csv"
        bundle = CSVProvider(path).fetch(oil_outcome_requirement())
        self.assertEqual(bundle.quality.status, QualityStatus.PASS)
        self.assertEqual(bundle.status, QualityStatus.PASS)
        self.assertEqual(bundle.quality.rows_read, 3)
        self.assertEqual(len(bundle.observations), 3)

    def test_sha256_matches_exact_raw_bytes(self) -> None:
        path = FIXTURES / "valid_market_data.csv"
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        result = CSVProvider(path).inspect()
        self.assertEqual(result.source.content_sha256, expected)

    def test_times_serialize_to_utc_and_session_date_is_preserved(self) -> None:
        result = CSVProvider(FIXTURES / "valid_market_data.csv").inspect()
        rendered = result.observations[0].model_dump(mode="json")
        self.assertEqual(rendered["observation_time"], "2024-01-02T07:00:00Z")
        self.assertEqual(rendered["available_time"], "2024-01-02T07:05:00Z")
        self.assertEqual(rendered["session_date"], "2024-01-02")

    def test_invalid_csv_reports_required_quality_issues(self) -> None:
        result = CSVProvider(FIXTURES / "invalid_market_data.csv").inspect()
        codes = {issue.code for issue in result.quality.issues}
        self.assertEqual(result.quality.status, QualityStatus.FAIL)
        self.assertTrue(
            {
                "duplicate_observation",
                "missing_value",
                "non_finite_value",
                "naive_datetime",
                "availability_before_observation",
                "time_order_error",
                "inconsistent_timezone",
                "inconsistent_currency",
                "inconsistent_unit",
            }.issubset(codes)
        )

    def test_invalid_order_is_preserved(self) -> None:
        result = CSVProvider(FIXTURES / "invalid_market_data.csv").inspect()
        dates = [item.session_date.isoformat() for item in result.observations]
        self.assertEqual(dates[:3], ["2024-01-02", "2024-01-02", "2024-01-01"])

    def test_fetch_reports_unexpected_and_requirement_metadata(self) -> None:
        bundle = CSVProvider(FIXTURES / "invalid_market_data.csv").fetch(
            oil_outcome_requirement()
        )
        codes = {issue.code for issue in bundle.quality.issues}
        self.assertEqual(bundle.quality.status, QualityStatus.FAIL)
        self.assertIn("unexpected_instrument", codes)
        self.assertIn("unexpected_field", codes)
        self.assertIn("currency_mismatch", codes)
        self.assertIn("unit_mismatch", codes)
        self.assertIn("timezone_mismatch", codes)
        self.assertEqual(bundle.status, QualityStatus.FAIL)


if __name__ == "__main__":
    unittest.main()
