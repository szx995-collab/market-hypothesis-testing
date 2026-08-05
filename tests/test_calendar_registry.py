"""Tests for identity-only calendar metadata."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from market_validator.data.calendars import (
    CalendarDefinition,
    CalendarOperationNotImplementedError,
    CalendarRegistry,
    CalendarRegistryError,
)

ROOT = Path(__file__).resolve().parents[1]


class CalendarRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")

    def test_required_calendar_scope_is_registered(self) -> None:
        expected = {
            "cn.a_share",
            "us.equity",
            "jp.equity",
            "kr.equity",
            "global.fx_24_5",
            "global.energy_futures",
            "global.macro_release",
        }
        self.assertEqual({item.calendar_id for item in self.registry.definitions}, expected)

    def test_duplicate_calendar_id_is_rejected(self) -> None:
        definition = self.registry.definitions[0]
        with self.assertRaisesRegex(CalendarRegistryError, "must be unique"):
            CalendarRegistry([definition, definition])

    def test_invalid_timezone_is_rejected(self) -> None:
        payload = self.registry.definitions[0].model_dump(mode="json")
        payload["timezone"] = "Mars/Olympus"
        with self.assertRaisesRegex(ValidationError, "valid IANA timezone"):
            CalendarDefinition.model_validate_json(json.dumps(payload))

    def test_unknown_calendar_is_rejected(self) -> None:
        with self.assertRaisesRegex(CalendarRegistryError, "unknown calendar_id"):
            self.registry.require("unknown.calendar")

    def test_schedule_calculation_fails_clearly(self) -> None:
        with self.assertRaisesRegex(
            CalendarOperationNotImplementedError, "not implemented"
        ):
            self.registry.sessions_between("2024-01-01", "2024-01-31")


if __name__ == "__main__":
    unittest.main()
