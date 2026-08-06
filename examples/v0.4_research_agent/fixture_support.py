"""TEST-ONLY fixture support for the offline v0.4 research agent example.

This module is deliberately small: it re-implements the minimal fixtures
that the Phase 4 longitudinal test uses, WITHOUT importing tests.* and
without registering any fixture backend as a real provider.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path


class FixedClock:
    """Deterministic clock so artifacts and reports are reproducible."""

    def __call__(self) -> datetime:
        return datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


QUESTION = "原油价格变化与 A 股能源板块是否存在关联？"


class FixedDailyAdapter:
    """Verified calendar-session adapter for the synthetic calendar."""

    adapter_id = "test.fixed.daily"

    def _weekdays(self, start: date, end: date) -> list[date]:
        result = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                result.append(current)
            current = date.fromordinal(current.toordinal() + 1)
        return result

    def sessions_between(self, start_date: date, end_date: date) -> list[date]:
        return self._weekdays(start_date, end_date)

    def previous_sessions(self, before_date: date, count: int) -> list[date]:
        result = []
        current = date.fromordinal(before_date.toordinal() - 1)
        while len(result) < count:
            if current.weekday() < 5:
                result.append(current)
            current = date.fromordinal(current.toordinal() - 1)
        return list(reversed(result))


def schedule_snapshots() -> dict[str, object]:
    from market_validator.data.readiness import (
        ExplicitSessionScheduleSnapshot,
    )

    adapter = FixedDailyAdapter()
    snapshot = ExplicitSessionScheduleSnapshot(
        schedule_id="test-fixed-daily-v1",
        calendar_id="synthetic.test.equity",
        schedule_adapter_id=adapter.adapter_id,
        coverage_start=date(2019, 12, 27),
        coverage_end=date(2020, 1, 31),
        sessions=adapter.sessions_between(date(2019, 12, 27), date(2020, 1, 31)),
        verification_source_uri=(
            "https://example.invalid/schedule/fixed-daily"
        ),
        verified_as_of=date(2026, 1, 1),
    )
    return {snapshot.schedule_adapter_id: snapshot}


def session_adapters() -> dict[str, object]:
    adapter = FixedDailyAdapter()
    return {
        adapter.adapter_id: lambda start, periods: (
            adapter.previous_sessions(start, periods)[0]
        )
    }


def _ready_association_payload() -> dict[str, object]:
    """Minimal ready proposal payload (mirrors tests, not imported)."""
    return {
        "proposal_schema_version": "1.0",
        "original_question": (
            "原油价格变化与 A 股能源板块是否存在关联？"
        ),
        "normalized_research_question": (
            "synthetic oil-energy association"
        ),
        "claim_type": "association",
        "outcome": {
            "variable_id": "energy_sector_return",
            "concept_name": "Synthetic energy sector return",
            "role": "outcome",
            "market_context": "synthetic energy sector",
            "asset_type": "macro_series",
            "transformation": "level",
            "time_relation": {
                "relation": "outcome_period",
                "lag_periods": None,
                "available_before_outcome": None,
                "description": "daily outcome observation period",
            },
            "proxy_for": None,
            "contract_roll_method": None,
        },
        "predictors": [
            {
                "variable_id": "oil_price_change",
                "concept_name": "Synthetic oil price change",
                "role": "predictor",
                "market_context": "synthetic commodity market",
                "asset_type": "macro_series",
                "transformation": "level",
                "time_relation": {
                    "relation": "contemporaneous",
                    "lag_periods": 0,
                    "available_before_outcome": True,
                    "description": "same observation period as the outcome",
                },
                "proxy_for": None,
                "contract_roll_method": None,
            }
        ],
        "controls": [],
        "sample": {
            "start_date": "2020-01-01",
            "end_date": "2020-01-10",
            "frequency": "1d",
        },
        "alignment": {
            "market_relation": "same_market",
            "target_market": None,
            "target_timezone": None,
            "target_calendar": None,
            "target_session": None,
            "information_cutoff": None,
        },
        "statistical_hypothesis": {
            "statistical_method": "pearson_correlation",
            "target_parameter": {
                "kind": "correlation",
                "predictor_variable_id": "oil_price_change",
                "reference_value": 0,
            },
            "direction": "two_sided",
            "null_hypothesis": "H0: rho = 0",
            "alternative_hypothesis": "H1: rho != 0",
            "significance_level": 0.05,
            "minimum_effect_size": 0.1,
        },
        "assumptions": ["The question requests association, not causation."],
        "ambiguities": [],
        "unsupported_requests": [],
        "ready_for_spec_review": True,
        "research_spec_inputs": {
            "spec_id": "synthetic-oil-energy-association-v1",
            "title": "Synthetic oil and energy association",
            "variables": [
                {
                    "variable_id": "energy_sector_return",
                    "instrument": {
                        "instrument_id": "energy.sector.index",
                        "display_name": "Energy Sector Index",
                        "asset_type": "macro_series",
                        "market": "synthetic",
                        "exchange_or_venue": "synthetic",
                        "timezone": "UTC",
                        "currency": "USD",
                        "unit": "Index",
                        "continuous_contract": False,
                    },
                    "field": "close",
                    "availability_lag_periods": 0,
                    "price_adjustment": None,
                    "rolling_window_periods": None,
                    "revision_policy": {
                        "mode": "not_applicable",
                        "as_of_date": None,
                    },
                },
                {
                    "variable_id": "oil_price_change",
                    "instrument": {
                        "instrument_id": "oil.price",
                        "display_name": "Oil Price",
                        "asset_type": "macro_series",
                        "market": "synthetic",
                        "exchange_or_venue": "synthetic",
                        "timezone": "UTC",
                        "currency": "USD",
                        "unit": "Dollars per Barrel",
                        "continuous_contract": False,
                    },
                    "field": "close",
                    "availability_lag_periods": 0,
                    "price_adjustment": None,
                    "rolling_window_periods": None,
                    "revision_policy": {
                        "mode": "not_applicable",
                        "as_of_date": None,
                    },
                },
            ],
            "minimum_observations": 4,
            "join_policy": None,
            "max_staleness_days": None,
            "missing_data_policy": None,
            "multiple_testing_correction": "none",
            "robustness_checks": [],
            "limitations": [
                "This retrospective association does not establish causation."
            ],
        },
    }


class FixtureProposalBackend:
    """Deterministic offline StructuredGenerationBackend for the example."""

    def status(self):
        class Status:
            name = "fixture"
            ready = True

        return Status()

    def _payload(self) -> dict:
        return _ready_association_payload()

    def generate(self, request):
        from market_validator.backends.base import (
            StructuredGenerationResult,
        )

        return StructuredGenerationResult(
            data=self._payload(),
            backend="fixture",
            model="fixture-v1",
            metadata={"test_only": True},
        )


def build_synthetic_workspace(root: Path) -> None:
    """Create config/calendars.json, config/instruments.json and copy csvs."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = root / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)

    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    for name in ("energy.csv", "oil.csv"):
        (csv_dir / name).write_bytes((fixture_dir / name).read_bytes())

    calendars = {
        "schema_version": "1.0",
        "calendars": [
            {
                "calendar_id": "synthetic.test.equity",
                "display_name": "Synthetic Test Equity",
                "timezone": "UTC",
                "calendar_type": "equity_exchange",
                "schedule_adapter": FixedDailyAdapter.adapter_id,
                "supports_special_sessions": False,
                "notes": ["synthetic"],
            }
        ],
    }
    (config_dir / "calendars.json").write_text(
        json.dumps(calendars), encoding="utf-8"
    )

    energy_file = (csv_dir / "energy.csv").resolve()
    oil_file = (csv_dir / "oil.csv").resolve()
    instruments = {
        "schema_version": "1.0",
        "instruments": [
            {
                "instrument_id": "energy.sector.index",
                "display_name": "Energy Sector Index",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "calendar_id": "synthetic.test.equity",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Index",
                "aliases": [],
                "identity_status": "verified",
                "provider_mappings": [
                    {
                        "provider_id": "local_csv",
                        "provider_symbol": str(energy_file),
                        "dataset_or_endpoint": str(energy_file),
                        "market": "synthetic",
                        "verified": True,
                        "verified_on": "2026-01-01",
                        "verification_source_uri": (
                            "https://example.invalid/verified/energy"
                        ),
                        "notes": ["fixture"],
                    }
                ],
                "notes": ["fixture"],
            },
            {
                "instrument_id": "oil.price",
                "display_name": "Oil Price",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "calendar_id": "synthetic.test.equity",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Dollars per Barrel",
                "aliases": [],
                "identity_status": "verified",
                "provider_mappings": [
                    {
                        "provider_id": "local_csv",
                        "provider_symbol": str(oil_file),
                        "dataset_or_endpoint": str(oil_file),
                        "market": "synthetic",
                        "verified": True,
                        "verified_on": "2026-01-01",
                        "verification_source_uri": (
                            "https://example.invalid/verified/oil"
                        ),
                        "notes": ["fixture"],
                    }
                ],
                "notes": ["fixture"],
            },
        ],
    }
    (config_dir / "instruments.json").write_text(
        json.dumps(instruments), encoding="utf-8"
    )


def fixture_interpreter():
    from market_validator.interpretation import FixtureLLMClient

    return FixtureLLMClient()
