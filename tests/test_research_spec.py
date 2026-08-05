"""Domain validation tests for ResearchSpec version 1.0."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from market_validator.research.models import ResearchSpec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPOSITORY_ROOT / "examples" / "research_specs"
OIL_EXAMPLE = EXAMPLES / "oil_to_a_share_energy.json"
NIKKEI_EXAMPLE = EXAMPLES / "nikkei_to_us_market.json"


def load_payload(path: Path = OIL_EXAMPLE) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_payload(payload: dict[str, object]) -> ResearchSpec:
    return ResearchSpec.model_validate_json(json.dumps(payload))


class ResearchSpecValidTest(unittest.TestCase):
    def test_both_examples_parse(self) -> None:
        for path in (OIL_EXAMPLE, NIKKEI_EXAMPLE):
            with self.subTest(path=path.name):
                spec = ResearchSpec.model_validate_json(path.read_text(encoding="utf-8"))
                self.assertEqual(spec.schema_version, "1.0")

    def test_json_round_trip_is_stable(self) -> None:
        original = ResearchSpec.model_validate_json(OIL_EXAMPLE.read_text(encoding="utf-8"))
        restored = ResearchSpec.model_validate_json(original.model_dump_json())
        self.assertEqual(restored, original)

    def test_json_schema_is_generated_and_forbids_top_level_extras(self) -> None:
        schema = ResearchSpec.model_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("$defs", schema)

    def test_legacy_specs_default_revision_policy_to_not_applicable(self) -> None:
        spec = ResearchSpec.model_validate_json(OIL_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(spec.predictors[0].revision_policy.mode, "not_applicable")
        self.assertIsNone(spec.predictors[0].revision_policy.as_of_date)


class ResearchSpecInvalidTest(unittest.TestCase):
    def assert_invalid(self, payload: dict[str, object], message: str | None = None) -> None:
        with self.assertRaises(ValidationError) as context:
            validate_payload(payload)
        if message is not None:
            self.assertIn(message, str(context.exception))

    def test_unknown_field_is_rejected(self) -> None:
        payload = load_payload()
        payload["unexpected"] = True
        self.assert_invalid(payload, "extra_forbidden")

    def test_nested_unknown_field_is_rejected(self) -> None:
        payload = load_payload()
        payload["sample"]["unexpected"] = True
        self.assert_invalid(payload, "extra_forbidden")

    def test_start_date_after_end_date_is_rejected(self) -> None:
        payload = load_payload()
        payload["sample"]["start_date"] = "2025-01-01"
        payload["sample"]["end_date"] = "2024-01-01"
        self.assert_invalid(payload, "start_date must not be later")

    def test_no_predictor_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"] = []
        self.assert_invalid(payload, "too_short")

    def test_second_outcome_role_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["role"] = "outcome"
        self.assert_invalid(payload, "role=predictor")

    def test_duplicate_variable_id_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["variable_id"] = payload["outcome"]["variable_id"]
        self.assert_invalid(payload, "variable_id values must be unique")

    def test_negative_lag_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["lag_periods"] = -1
        self.assert_invalid(payload, "greater than or equal to 0")

    def test_negative_availability_lag_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["availability_lag_periods"] = -1
        self.assert_invalid(payload, "greater than or equal to 0")

    def test_invalid_timezone_is_rejected(self) -> None:
        payload = load_payload()
        payload["outcome"]["instrument"]["timezone"] = "Mars/Olympus"
        self.assert_invalid(payload, "not a valid IANA timezone")

    def test_cross_market_without_information_cutoff_is_rejected(self) -> None:
        payload = load_payload()
        del payload["alignment"]["information_cutoff"]
        self.assert_invalid(payload, "Field required")

    def test_cross_market_without_alignment_is_rejected(self) -> None:
        payload = load_payload()
        payload["alignment"] = None
        self.assert_invalid(payload, "cross-market or cross-timezone")

    def test_equity_or_etf_without_adjustment_is_rejected(self) -> None:
        payload = load_payload()
        payload["outcome"]["price_adjustment"] = None
        self.assert_invalid(payload, "require price_adjustment")

    def test_continuous_future_without_roll_method_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["contract_roll_method"] = None
        self.assert_invalid(payload, "require contract_roll_method")

    def test_spot_commodity_with_roll_method_is_rejected(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["instrument"]["asset_type"] = "commodity_spot"
        payload["predictors"][0]["instrument"]["continuous_contract"] = False
        self.assert_invalid(payload, "only be set for commodity_future")

    def test_non_daily_frequency_is_rejected(self) -> None:
        payload = load_payload()
        payload["sample"]["frequency"] = "1h"
        self.assert_invalid(payload, "frequency")

    def test_unbounded_forward_fill_policy_is_not_supported(self) -> None:
        payload = load_payload()
        payload["alignment"]["missing_data_policy"] = "forward_fill"
        self.assert_invalid(payload, "missing_data_policy")

    def test_control_list_requires_control_role(self) -> None:
        payload = load_payload()
        control = deepcopy(payload["predictors"][0])
        control["variable_id"] = "market_control"
        payload["controls"] = [control]
        payload["model"]["formula_variable_ids"].append("market_control")
        self.assert_invalid(payload, "role=control")

    def test_formula_variable_reference_must_exist(self) -> None:
        payload = load_payload()
        payload["model"]["formula_variable_ids"][1] = "unknown_variable"
        self.assert_invalid(payload, "unknown variable IDs")

    def test_significance_level_must_be_between_zero_and_one(self) -> None:
        for value in (0.0, 1.0):
            with self.subTest(value=value):
                payload = load_payload()
                payload["model"]["significance_level"] = value
                self.assert_invalid(payload, "significance_level")

    def test_revision_as_of_date_is_only_valid_for_matching_mode(self) -> None:
        payload = load_payload()
        payload["predictors"][0]["revision_policy"] = {
            "mode": "as_of_date",
            "as_of_date": None,
        }
        self.assert_invalid(payload, "requires as_of_date")

        payload = load_payload()
        payload["predictors"][0]["revision_policy"] = {
            "mode": "latest_available",
            "as_of_date": "2024-01-01",
        }
        self.assert_invalid(payload, "only valid when mode=as_of_date")


if __name__ == "__main__":
    unittest.main()
