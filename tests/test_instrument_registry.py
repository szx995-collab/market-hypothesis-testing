"""Tests for normalized identity and provider-symbol mapping constraints."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from market_validator.data.calendars import CalendarRegistry, CalendarRegistryError
from market_validator.data.registry import (
    InstrumentRegistry,
    InstrumentRegistryDocument,
    InstrumentRegistryEntry,
    InstrumentRegistryError,
    ProviderSymbolMapping,
)

ROOT = Path(__file__).resolve().parents[1]


class InstrumentRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calendars = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")
        self.registry = InstrumentRegistry.from_json_file(
            ROOT / "config" / "instruments.json", self.calendars
        )

    def test_only_officially_verified_fred_symbols_are_registered(self) -> None:
        self.assertEqual(len(self.registry.entries), 9)
        legacy_ids = {
            "cn.a_share.energy_etf_proxy",
            "cn.a_share.energy.sector_index",
            "global.crude_oil.continuous_front",
            "jp.nikkei_225.index",
            "us.broad_equity.market_index",
        }
        self.assertTrue(
            all(
                not entry.provider_mappings
                for entry in self.registry.entries
                if entry.instrument_id in legacy_ids
            )
        )
        mappings = {
            entry.instrument_id: entry.provider_mappings[0].provider_symbol
            for entry in self.registry.entries
            if entry.provider_mappings
        }
        self.assertEqual(
            mappings,
            {
                "global.crude_oil.wti_spot": "DCOILWTICO",
                "global.crude_oil.brent_spot": "DCOILBRENTEU",
                "us.dollar.nominal_broad_index": "DTWEXBGS",
                "fx.usd_cny.reference_rate": "DEXCHUS",
            },
        )

    def test_duplicate_instrument_id_is_rejected(self) -> None:
        entry = self.registry.entries[0]
        with self.assertRaisesRegex(InstrumentRegistryError, "must be unique"):
            InstrumentRegistry([entry, entry], self.calendars)

    def test_alias_conflict_is_rejected(self) -> None:
        first, second = self.registry.entries[:2]
        conflicting = second.model_copy(update={"aliases": [first.aliases[0]]})
        with self.assertRaisesRegex(InstrumentRegistryError, "ambiguous alias"):
            InstrumentRegistry([first, conflicting], self.calendars)

    def test_provider_mapping_conflict_is_rejected(self) -> None:
        mapping = ProviderSymbolMapping(
            provider_id="example_provider",
            provider_symbol="SYNTHETIC",
            dataset_or_endpoint="daily",
            market="example",
            verified=False,
            verified_on=None,
            notes=["Test-only mapping."],
        )
        first, second = self.registry.entries[:2]
        first = first.model_copy(update={"provider_mappings": [mapping]})
        second = second.model_copy(update={"provider_mappings": [mapping]})
        with self.assertRaisesRegex(
            InstrumentRegistryError, "multiple instruments"
        ):
            InstrumentRegistry([first, second], self.calendars)

    def test_unknown_calendar_is_rejected(self) -> None:
        entry = self.registry.entries[0].model_copy(
            update={"calendar_id": "unknown.calendar"}
        )
        with self.assertRaisesRegex(CalendarRegistryError, "unknown calendar_id"):
            InstrumentRegistry([entry], self.calendars)

    def test_invalid_timezone_is_rejected(self) -> None:
        payload = self.registry.entries[0].model_dump(mode="json")
        payload["timezone"] = "Mars/Olympus"
        with self.assertRaisesRegex(ValidationError, "valid IANA timezone"):
            InstrumentRegistryEntry.model_validate_json(json.dumps(payload))

    def test_sensitive_authentication_field_is_rejected(self) -> None:
        payload = {
            "schema_version": "1.0",
            "instruments": [self.registry.entries[0].model_dump(mode="json")],
        }
        payload["instruments"][0]["api_key"] = "fake-do-not-store"
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            InstrumentRegistryDocument.model_validate_json(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
