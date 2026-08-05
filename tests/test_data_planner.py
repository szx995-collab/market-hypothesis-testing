"""Tests for deterministic ResearchSpec-to-DataPlan planning."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.planner import DataPlanningError, plan_data_requirements
from market_validator.data.registry import InstrumentRegistry
from market_validator.research.models import ResearchSpec

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "research_specs"


class DataPlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calendars = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")
        self.instruments = InstrumentRegistry.from_json_file(
            ROOT / "config" / "instruments.json", self.calendars
        )

    def load_spec(self, name: str = "oil_to_a_share_energy.json") -> ResearchSpec:
        return ResearchSpec.model_validate_json(
            (EXAMPLES / name).read_text(encoding="utf-8")
        )

    def test_both_examples_produce_one_requirement_per_variable(self) -> None:
        for name in ("oil_to_a_share_energy.json", "nikkei_to_us_market.json"):
            with self.subTest(name=name):
                spec = self.load_spec(name)
                plan = plan_data_requirements(spec, self.instruments, self.calendars)
                expected = 1 + len(spec.predictors) + len(spec.controls)
                self.assertEqual(len(plan.requirements), expected)

    def test_same_inputs_produce_same_plan_id(self) -> None:
        spec = self.load_spec()
        first = plan_data_requirements(spec, self.instruments, self.calendars)
        second = plan_data_requirements(spec, self.instruments, self.calendars)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first, second)

    def test_planning_does_not_mutate_research_spec(self) -> None:
        spec = self.load_spec()
        before = spec.model_dump_json()
        plan_data_requirements(spec, self.instruments, self.calendars)
        self.assertEqual(spec.model_dump_json(), before)

    def test_log_return_plus_lag_two_requires_three_periods(self) -> None:
        payload = json.loads((EXAMPLES / "oil_to_a_share_energy.json").read_text(encoding="utf-8"))
        payload["predictors"][0]["lag_periods"] = 2
        payload["predictors"][0]["availability_lag_periods"] = 7
        spec = ResearchSpec.model_validate_json(json.dumps(payload))
        plan = plan_data_requirements(spec, self.instruments, self.calendars)
        requirement = next(
            item for item in plan.requirements if item.variable_id == "oil_return"
        )
        self.assertEqual(requirement.required_pre_sample_periods, 3)
        self.assertEqual(requirement.availability_lag_periods, 7)

    def test_rolling_window_and_lag_are_added(self) -> None:
        payload = json.loads((EXAMPLES / "oil_to_a_share_energy.json").read_text(encoding="utf-8"))
        payload["predictors"][0]["transformation"] = "rolling_mean"
        payload["predictors"][0]["rolling_window_periods"] = 5
        payload["predictors"][0]["lag_periods"] = 2
        spec = ResearchSpec.model_validate_json(json.dumps(payload))
        plan = plan_data_requirements(spec, self.instruments, self.calendars)
        requirement = next(
            item for item in plan.requirements if item.variable_id == "oil_return"
        )
        self.assertEqual(requirement.required_pre_sample_periods, 6)
        self.assertEqual(requirement.rolling_window_periods, 5)

    def test_unverified_and_unmapped_instruments_produce_warnings(self) -> None:
        plan = plan_data_requirements(
            self.load_spec("nikkei_to_us_market.json"),
            self.instruments,
            self.calendars,
        )
        self.assertEqual(len(plan.unresolved_instruments), 2)
        self.assertTrue(plan.warnings)
        self.assertTrue(all(item.status == "unresolved" for item in plan.requirements))

    def test_registry_metadata_conflict_is_rejected(self) -> None:
        entries = list(self.instruments.entries)
        index = next(
            index
            for index, entry in enumerate(entries)
            if entry.instrument_id == "global.crude_oil.continuous_front"
        )
        entries[index] = entries[index].model_copy(update={"currency": "EUR"})
        conflicting_registry = InstrumentRegistry(entries, self.calendars)
        with self.assertRaisesRegex(DataPlanningError, "registry metadata conflicts"):
            plan_data_requirements(
                self.load_spec(), conflicting_registry, self.calendars
            )

    def test_plan_never_invents_provider_symbols(self) -> None:
        plan = plan_data_requirements(
            self.load_spec(), self.instruments, self.calendars
        )
        rendered = plan.model_dump_json()
        self.assertNotIn("provider_symbol", rendered)
        self.assertNotIn("dataset_or_endpoint", rendered)

    def test_revision_policy_is_transferred_without_provider_defaulting(self) -> None:
        payload = json.loads(
            (EXAMPLES / "oil_to_a_share_energy.json").read_text(encoding="utf-8")
        )
        payload["predictors"][0]["revision_policy"] = {
            "mode": "initial_release",
            "as_of_date": None,
        }
        spec = ResearchSpec.model_validate_json(json.dumps(payload))
        plan = plan_data_requirements(spec, self.instruments, self.calendars)
        requirement = next(
            item for item in plan.requirements if item.variable_id == "oil_return"
        )
        self.assertEqual(requirement.revision_policy.mode, "initial_release")
        self.assertIsNone(requirement.revision_policy.as_of_date)


if __name__ == "__main__":
    unittest.main()
