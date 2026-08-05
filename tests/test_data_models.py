"""Tests for normalized observations and secret-free source metadata."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import unittest

from pydantic import ValidationError

from market_validator.data.models import (
    DataQualityIssue,
    DataQualityReport,
    DataSourceMetadata,
    Observation,
    QualitySeverity,
    QualityStatus,
)


def observation_payload() -> dict[str, object]:
    return {
        "instrument_id": "example.instrument",
        "field": "close",
        "value": 1.25,
        "observation_time": "2024-01-02T15:00:00+08:00",
        "available_time": "2024-01-02T15:05:00+08:00",
        "session_date": "2024-01-02",
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
        "unit": "index points",
    }


class ObservationTest(unittest.TestCase):
    def test_json_serialization_normalizes_times_to_utc(self) -> None:
        observation = Observation.model_validate_json(json.dumps(observation_payload()))
        rendered = observation.model_dump(mode="json")
        self.assertEqual(rendered["observation_time"], "2024-01-02T07:00:00Z")
        self.assertEqual(rendered["available_time"], "2024-01-02T07:05:00Z")
        self.assertEqual(rendered["session_date"], "2024-01-02")

    def test_naive_datetime_is_rejected(self) -> None:
        payload = observation_payload()
        payload["observation_time"] = "2024-01-02T15:00:00"
        with self.assertRaisesRegex(ValidationError, "timezone offset"):
            Observation.model_validate_json(json.dumps(payload))

    def test_available_time_before_observation_is_rejected(self) -> None:
        payload = observation_payload()
        payload["available_time"] = "2024-01-02T14:59:00+08:00"
        with self.assertRaisesRegex(ValidationError, "must not be earlier"):
            Observation.model_validate_json(json.dumps(payload))

    def test_non_finite_value_is_rejected(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Observation(
                    instrument_id="example.instrument",
                    field="close",
                    value=value,
                    observation_time=datetime.now(timezone.utc),
                    available_time=datetime.now(timezone.utc),
                    session_date=date(2024, 1, 2),
                    timezone="UTC",
                    currency="USD",
                    unit="index points",
                )

    def test_unknown_field_is_rejected(self) -> None:
        payload = observation_payload()
        payload["unknown"] = True
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            Observation.model_validate_json(json.dumps(payload))


class DataSourceMetadataTest(unittest.TestCase):
    def metadata(self, **updates: object) -> DataSourceMetadata:
        values = {
            "provider_id": "local_csv",
            "dataset_id": "fixture.csv",
            "provider_symbol": None,
            "source_uri": "file:///tmp/fixture.csv",
            "retrieved_at": datetime.now(timezone.utc),
            "public_request_parameters": {"source_kind": "local_file"},
            "content_sha256": "a" * 64,
            "license_note": "Test fixture only.",
            "is_fallback": False,
        }
        values.update(updates)
        return DataSourceMetadata(**values)

    def test_sensitive_parameter_names_are_rejected(self) -> None:
        for name in ("api_key", "access-token", "client_secret", "password", "Authorization"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.metadata(public_request_parameters={name: "not-a-real-secret"})

    def test_authentication_query_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "authentication parameters"):
            self.metadata(source_uri="https://example.invalid/data?token=fake")

    def test_retrieved_at_must_be_utc(self) -> None:
        non_utc = timezone(timedelta(hours=8))
        with self.assertRaisesRegex(ValidationError, "expressed in UTC"):
            self.metadata(retrieved_at=datetime.now(non_utc))


class DataQualityReportTest(unittest.TestCase):
    def test_error_issue_requires_fail_status(self) -> None:
        issue = DataQualityIssue(
            code="test_error",
            severity=QualitySeverity.ERROR,
            message="Synthetic test error.",
        )
        with self.assertRaisesRegex(ValidationError, "status must be 'fail'"):
            DataQualityReport(
                status=QualityStatus.PASS,
                rows_read=1,
                observations_parsed=1,
                coverage_start=datetime.now(timezone.utc),
                coverage_end=datetime.now(timezone.utc),
                issues=[issue],
            )


if __name__ == "__main__":
    unittest.main()
