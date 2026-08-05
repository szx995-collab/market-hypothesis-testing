"""Fully offline FRED provider behavior and mapping tests."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest

from pydantic import SecretStr

from market_validator.data.calendars import CalendarRegistry
from market_validator.credentials import (
    CredentialInputCancelledError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialResolver,
    PromptResult,
    PromptStatus,
)
from market_validator.data.models import DataRequirement, QualityStatus, TimePrecision
from market_validator.data.providers.fred_provider import (
    FredCapabilityError,
    FredDataValidationError,
    FredMappingError,
    FredNetworkNotAuthorizedError,
    FredPersistenceError,
    FredProvider,
    is_valid_fred_api_key,
)
from market_validator.data.providers.base import DataProvider
from market_validator.data.providers.fred_transport import (
    FredResponseFormatError,
    FredTransportResponse,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.storage import FredSnapshotRecord, FredStorageError
from market_validator.research.enums import DataRevisionMode
from market_validator.research.models import DataRevisionSpec

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_KEY = "k7" * 16
RETRIEVED_AT = datetime(2025, 1, 2, 12, 30, tzinfo=timezone.utc)


def response(payload: dict[str, object]) -> FredTransportResponse:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return FredTransportResponse(raw_body=raw, payload=payload, status_code=200)


def series_payload(
    series_id: str = "DCOILWTICO",
    *,
    frequency: str = "Daily",
    units: str = "Dollars per Barrel",
) -> dict[str, object]:
    return {
        "seriess": [
            {
                "id": series_id,
                "title": "Official test fixture title",
                "frequency": frequency,
                "units": units,
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "last_updated": "2025-01-01 00:00:00-06",
                "notes": "Offline fixture only.",
            }
        ]
    }


def observations_payload(
    observations: list[dict[str, str]],
    *,
    count: int | None = None,
    offset: int = 0,
    limit: int = 100_000,
) -> dict[str, object]:
    return {
        "count": len(observations) if count is None else count,
        "offset": offset,
        "limit": limit,
        "observations": observations,
    }


class FakeTransport:
    def __init__(
        self,
        metadata: dict[str, object] | None = None,
        pages: dict[int, dict[str, object]] | None = None,
    ) -> None:
        self.metadata = metadata or series_payload()
        self.pages = pages or {
            0: observations_payload(
                [
                    {
                        "date": "2020-01-01",
                        "value": "61.25",
                        "realtime_start": "2020-01-02",
                    },
                    {
                        "date": "2020-01-02",
                        "value": "62.50",
                        "realtime_start": "2020-01-03",
                    },
                ]
            )
        }
        self.calls: list[tuple[str, dict[str, str | int]]] = []

    def get_json(self, path: str, public_parameters):
        parameters = dict(public_parameters)
        self.calls.append((path, parameters))
        if path == "/fred/series":
            return response(self.metadata)
        return response(self.pages[int(parameters["offset"])])


class RecordingStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def persist(self, **arguments):
        if self.fail:
            raise FredStorageError("offline injected write failure")
        self.calls.append(arguments)
        return FredSnapshotRecord(
            request_id="fred-test-snapshot",
            manifest_path=Path("manifest.json"),
            bundle_path=Path("bundle.json"),
            raw_series_path=Path("series.json"),
            raw_observation_paths=(Path("observations.json"),),
            manifest_sha256="a" * 64,
            bundle_sha256="b" * 64,
        )


class StaticPrompt:
    source_name = "fake"

    def __init__(self, result: PromptResult) -> None:
        self.result = result
        self.calls = 0

    def request(self, _spec) -> PromptResult:
        self.calls += 1
        return self.result


class FredProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        calendars = CalendarRegistry.from_json_file(ROOT / "config" / "calendars.json")
        cls.calendars = calendars
        cls.registry = InstrumentRegistry.from_json_file(
            ROOT / "config" / "instruments.json", calendars
        )

    def requirement(self, filename: str = "fred_wti_spot_initial.json") -> DataRequirement:
        return DataRequirement.model_validate_json(
            (ROOT / "examples" / "data_requirements" / filename).read_text(
                encoding="utf-8"
            )
        )

    def provider(
        self,
        *,
        transport: FakeTransport | None = None,
        storage: RecordingStorage | None = None,
        allow_network: bool = True,
        environment: dict[str, str] | None = None,
    ) -> FredProvider:
        return FredProvider(
            self.registry,
            allow_network=allow_network,
            environment={"FRED_API_KEY": SENTINEL_KEY}
            if environment is None
            else environment,
            transport=transport or FakeTransport(),
            storage=storage or RecordingStorage(),
            clock=lambda: RETRIEVED_AT,
        )

    def test_key_format_and_local_status(self) -> None:
        self.assertTrue(is_valid_fred_api_key(SENTINEL_KEY))
        for key in (None, "", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "-"):
            with self.subTest(key=key):
                self.assertFalse(is_valid_fred_api_key(key))
        missing = self.provider(environment={}).status()
        self.assertFalse(missing.configured)
        self.assertFalse(missing.ready)
        self.assertFalse(missing.network_tested)
        configured = self.provider().status()
        self.assertTrue(configured.configured)
        self.assertFalse(configured.ready)
        self.assertFalse(configured.network_tested)

    def test_provider_protocol_and_capabilities_are_explicit(self) -> None:
        provider = self.provider(allow_network=False)
        self.assertIsInstance(provider, DataProvider)
        capabilities = provider.capabilities()
        self.assertEqual(capabilities.provider_id, "fred")
        self.assertEqual(
            {item.value for item in capabilities.supported_asset_types},
            {"commodity_spot", "fx", "macro_series"},
        )
        self.assertEqual(capabilities.supported_fields, ["value"])
        self.assertFalse(capabilities.supports_continuous_futures)
        self.assertTrue(capabilities.requires_authentication)
        self.assertTrue(capabilities.requires_network)
        self.assertEqual(
            {item.value for item in capabilities.supported_revision_policies},
            {"latest_available", "initial_release"},
        )

    def test_dry_run_never_calls_transport_or_requires_key(self) -> None:
        transport = FakeTransport()
        provider = self.provider(
            transport=transport, allow_network=False, environment={}
        )
        payload = provider.dry_run(self.requirement())
        self.assertEqual(transport.calls, [])
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_requested"])
        self.assertEqual(payload["series_id"], "DCOILWTICO")
        self.assertEqual(payload["public_parameters"]["output_type"], 4)
        self.assertEqual(
            payload["public_parameters"]["realtime_start"], "1776-07-04"
        )
        self.assertEqual(
            payload["public_parameters"]["realtime_end"], "9999-12-31"
        )
        self.assertEqual(
            payload["public_parameters"]["observation_start"], "2020-01-01"
        )
        self.assertEqual(
            payload["public_parameters"]["observation_end"], "2024-12-31"
        )

    def test_fetch_requires_explicit_network_consent_before_key_or_transport(self) -> None:
        transport = FakeTransport()
        provider = self.provider(transport=transport, allow_network=False)
        with self.assertRaises(FredNetworkNotAuthorizedError):
            provider.fetch(self.requirement())
        self.assertEqual(transport.calls, [])

    def test_initial_release_does_not_use_request_day_as_realtime_period(self) -> None:
        provider = self.provider(allow_network=False, environment={})
        parameters = provider.dry_run(self.requirement())["public_parameters"]
        self.assertEqual(
            (parameters["realtime_start"], parameters["realtime_end"]),
            ("1776-07-04", "9999-12-31"),
        )
        self.assertNotEqual(
            (parameters["realtime_start"], parameters["realtime_end"]),
            ("2026-08-04", "2026-08-04"),
        )

    def test_live_without_key_does_not_call_transport(self) -> None:
        transport = FakeTransport()
        provider = self.provider(transport=transport, environment={})
        with self.assertRaises(CredentialMissingError) as caught:
            provider.fetch(self.requirement())
        self.assertEqual(transport.calls, [])
        self.assertNotIn(SENTINEL_KEY, str(caught.exception))

    def test_interactive_fake_prompt_supplies_protected_secret_to_transport(self) -> None:
        gui = StaticPrompt(
            PromptResult(
                status=PromptStatus.SUBMITTED,
                secret=SecretStr(SENTINEL_KEY),
            )
        )
        terminal = StaticPrompt(PromptResult(status=PromptStatus.UNAVAILABLE))
        resolver = CredentialResolver(
            environment={}, gui_prompt=gui, terminal_prompt=terminal
        )
        transport = FakeTransport()
        received_secrets: list[SecretStr] = []

        def transport_factory(secret: SecretStr) -> FakeTransport:
            received_secrets.append(secret)
            return transport

        provider = FredProvider(
            self.registry,
            allow_network=True,
            interactive=True,
            credential_resolver=resolver,
            transport_factory=transport_factory,
            storage=RecordingStorage(),
            clock=lambda: RETRIEVED_AT,
        )
        bundle = provider.fetch(self.requirement())
        self.assertEqual(len(bundle.observations), 2)
        self.assertEqual(gui.calls, 1)
        self.assertEqual(terminal.calls, 0)
        self.assertEqual(len(received_secrets), 1)
        self.assertIsInstance(received_secrets[0], SecretStr)
        self.assertNotIn(SENTINEL_KEY, repr(received_secrets))

    def test_interactive_cancel_or_invalid_never_creates_transport(self) -> None:
        cases = (
            (PromptStatus.CANCELLED, CredentialInputCancelledError),
            (PromptStatus.INVALID, CredentialInvalidError),
        )
        for status, error_type in cases:
            with self.subTest(status=status):
                gui = StaticPrompt(PromptResult(status=status))
                resolver = CredentialResolver(
                    environment={},
                    gui_prompt=gui,
                    terminal_prompt=StaticPrompt(
                        PromptResult(status=PromptStatus.UNAVAILABLE)
                    ),
                )
                factory_calls: list[SecretStr] = []

                def transport_factory(secret: SecretStr) -> FakeTransport:
                    factory_calls.append(secret)
                    return FakeTransport()

                provider = FredProvider(
                    self.registry,
                    allow_network=True,
                    interactive=True,
                    credential_resolver=resolver,
                    transport_factory=transport_factory,
                    storage=RecordingStorage(),
                )
                with self.assertRaises(error_type):
                    provider.fetch(self.requirement())
                self.assertEqual(gui.calls, 1)
                self.assertEqual(factory_calls, [])

    def test_dry_run_never_calls_interactive_prompt(self) -> None:
        gui = StaticPrompt(PromptResult(status=PromptStatus.CANCELLED))
        resolver = CredentialResolver(
            environment={},
            gui_prompt=gui,
            terminal_prompt=StaticPrompt(
                PromptResult(status=PromptStatus.UNAVAILABLE)
            ),
        )
        provider = FredProvider(
            self.registry,
            allow_network=False,
            interactive=True,
            credential_resolver=resolver,
        )
        payload = provider.dry_run(self.requirement())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(gui.calls, 0)

    def test_verified_mappings_resolve_to_exact_official_series(self) -> None:
        expected = {
            "global.crude_oil.wti_spot": "DCOILWTICO",
            "global.crude_oil.brent_spot": "DCOILBRENTEU",
            "us.dollar.nominal_broad_index": "DTWEXBGS",
            "fx.usd_cny.reference_rate": "DEXCHUS",
        }
        provider = self.provider(allow_network=False)
        for instrument_id, series_id in expected.items():
            with self.subTest(instrument_id=instrument_id):
                entry = self.registry.get(instrument_id)
                self.assertIsNotNone(entry)
                mapping = entry.provider_mappings[0]
                self.assertTrue(mapping.verified)
                self.assertEqual(mapping.provider_symbol, series_id)
                self.assertTrue(mapping.verification_source_uri.startswith("https://"))
                requirement = self.requirement().model_copy(
                    update={
                        "instrument_id": entry.instrument_id,
                        "asset_type": entry.asset_type,
                        "market": entry.market,
                        "calendar_id": entry.calendar_id,
                        "timezone": entry.timezone,
                        "currency": entry.currency,
                        "unit": entry.unit,
                    }
                )
                self.assertEqual(provider.dry_run(requirement)["series_id"], series_id)
        self.assertIn("not the ICE", self.registry.get("us.dollar.nominal_broad_index").notes[0])
        self.assertIn(
            "one U.S. dollar",
            self.registry.get("fx.usd_cny.reference_rate").provider_mappings[0].notes[0],
        )

    def test_continuous_future_is_not_mapped_to_fred_spot(self) -> None:
        entry = self.registry.get("global.crude_oil.continuous_front")
        self.assertEqual(entry.provider_mappings, [])

    def test_unverified_or_missing_mapping_is_rejected(self) -> None:
        requirement = self.requirement().model_copy(
            update={"instrument_id": "global.crude_oil.continuous_front"}
        )
        with self.assertRaises(FredMappingError):
            self.provider(allow_network=False).dry_run(requirement)

        entries = list(self.registry.entries)
        index = next(
            i
            for i, entry in enumerate(entries)
            if entry.instrument_id == "global.crude_oil.wti_spot"
        )
        mapping = entries[index].provider_mappings[0].model_copy(
            update={
                "verified": False,
                "verified_on": None,
                "verification_source_uri": None,
            }
        )
        entries[index] = entries[index].model_copy(
            update={"provider_mappings": [mapping]}
        )
        unverified_registry = InstrumentRegistry(entries, self.calendars)
        provider = FredProvider(unverified_registry, allow_network=False)
        with self.assertRaises(FredMappingError):
            provider.dry_run(self.requirement())

    def test_series_metadata_conflict_and_non_daily_series_fail(self) -> None:
        cases = (
            (series_payload(series_id="OTHER"), FredDataValidationError),
            (series_payload(frequency="Monthly"), FredDataValidationError),
            (series_payload(units="Index"), FredDataValidationError),
            ({"seriess": [{}]}, FredResponseFormatError),
        )
        for metadata, error_type in cases:
            with self.subTest(metadata=metadata):
                provider = self.provider(transport=FakeTransport(metadata=metadata))
                with self.assertRaises(error_type):
                    provider.fetch(self.requirement())

    def test_initial_release_normalizes_date_precision_and_output_type_four(self) -> None:
        transport = FakeTransport()
        storage = RecordingStorage()
        provider = self.provider(transport=transport, storage=storage)
        bundle = provider.fetch(self.requirement())
        observation = bundle.observations[0]
        self.assertEqual(observation.observation_time.isoformat(), "2020-01-01T00:00:00+00:00")
        self.assertEqual(observation.available_time.isoformat(), "2020-01-02T23:59:59.999999+00:00")
        self.assertEqual(observation.observation_precision, TimePrecision.DATE)
        self.assertEqual(observation.availability_precision, TimePrecision.DATE)
        self.assertEqual(observation.vintage_date.isoformat(), "2020-01-02")
        self.assertEqual(observation.revision_policy, DataRevisionMode.INITIAL_RELEASE)
        self.assertIn("end-of-day", observation.availability_assumption)
        observation_call = transport.calls[1]
        self.assertEqual(observation_call[1]["output_type"], 4)
        self.assertEqual(observation_call[1]["realtime_start"], "1776-07-04")
        self.assertEqual(observation_call[1]["realtime_end"], "9999-12-31")
        self.assertEqual(observation_call[1]["observation_start"], "2020-01-01")
        self.assertEqual(observation_call[1]["observation_end"], "2024-12-31")
        self.assertNotEqual(
            (
                observation_call[1]["realtime_start"],
                observation_call[1]["realtime_end"],
            ),
            ("2026-08-04", "2026-08-04"),
        )
        self.assertEqual(bundle.quality.status, QualityStatus.WARN)
        self.assertTrue(provider.status().ready)
        self.assertTrue(provider.status().network_tested)
        self.assertEqual(len(storage.calls), 1)
        self.assertEqual(
            bundle.source.public_request_parameters["realtime_start"],
            "1776-07-04",
        )
        self.assertEqual(
            bundle.source.public_request_parameters["realtime_end"],
            "9999-12-31",
        )
        self.assertEqual(
            storage.calls[0]["public_request_parameters"]["realtime_start"],
            "1776-07-04",
        )
        self.assertEqual(
            storage.calls[0]["public_request_parameters"]["realtime_end"],
            "9999-12-31",
        )

    def test_latest_available_uses_fetch_time_and_warns_about_hindsight(self) -> None:
        requirement = self.requirement().model_copy(
            update={
                "revision_policy": DataRevisionSpec(
                    mode=DataRevisionMode.LATEST_AVAILABLE
                )
            }
        )
        transport = FakeTransport()
        bundle = self.provider(transport=transport).fetch(requirement)
        observation = bundle.observations[0]
        self.assertEqual(observation.available_time, RETRIEVED_AT)
        self.assertEqual(observation.availability_precision, TimePrecision.TIMESTAMP)
        self.assertEqual(transport.calls[1][1]["output_type"], 1)
        self.assertNotIn("realtime_start", transport.calls[1][1])
        self.assertNotIn("realtime_end", transport.calls[1][1])
        self.assertIn(
            "latest_revision_hindsight_risk",
            {issue.code for issue in bundle.quality.issues},
        )

    def test_as_of_date_and_implicit_revision_modes_are_rejected(self) -> None:
        for revision in (
            DataRevisionSpec(mode=DataRevisionMode.AS_OF_DATE, as_of_date=RETRIEVED_AT.date()),
            DataRevisionSpec(),
        ):
            with self.subTest(mode=revision.mode):
                requirement = self.requirement().model_copy(
                    update={"revision_policy": revision}
                )
                with self.assertRaises(FredCapabilityError):
                    self.provider(allow_network=False).dry_run(requirement)

    def test_provider_missing_value_is_warning_and_not_zero_observation(self) -> None:
        records = [
            {"date": "2020-01-01", "value": ".", "realtime_start": "2020-01-02"},
            {"date": "2020-01-02", "value": "2.5", "realtime_start": "2020-01-03"},
        ]
        bundle = self.provider(
            transport=FakeTransport(pages={0: observations_payload(records)})
        ).fetch(self.requirement())
        self.assertEqual([item.value for item in bundle.observations], [2.5])
        self.assertIn(
            "provider_missing_value", {issue.code for issue in bundle.quality.issues}
        )

    def test_observation_required_fields_are_not_guessed(self) -> None:
        records = (
            {"value": "1", "realtime_start": "2020-01-02"},
            {"date": "2020-01-01", "realtime_start": "2020-01-02"},
            {"date": "2020-01-01", "value": "1"},
        )
        for record in records:
            with self.subTest(record=record):
                transport = FakeTransport(
                    pages={0: observations_payload([record])}
                )
                with self.assertRaises(FredResponseFormatError):
                    self.provider(transport=transport).fetch(self.requirement())

    def test_wrong_order_and_duplicates_are_reported_without_silent_sorting(self) -> None:
        records = [
            {"date": "2020-01-02", "value": "2", "realtime_start": "2020-01-03"},
            {"date": "2020-01-01", "value": "1", "realtime_start": "2020-01-02"},
            {"date": "2020-01-01", "value": "1.1", "realtime_start": "2020-01-02"},
        ]
        bundle = self.provider(
            transport=FakeTransport(pages={0: observations_payload(records)})
        ).fetch(self.requirement())
        self.assertEqual(
            [item.session_date.isoformat() for item in bundle.observations],
            ["2020-01-02", "2020-01-01", "2020-01-01"],
        )
        codes = {issue.code for issue in bundle.quality.issues}
        self.assertIn("time_order_error", codes)
        self.assertIn("duplicate_observation", codes)
        self.assertEqual(bundle.quality.status, QualityStatus.FAIL)

    def test_pagination_checks_offsets_count_and_preserves_each_raw_page(self) -> None:
        first = observations_payload(
            [
                {"date": "2020-01-01", "value": "1", "realtime_start": "2020-01-02"},
                {"date": "2020-01-02", "value": "2", "realtime_start": "2020-01-03"},
            ],
            count=3,
            offset=0,
            limit=2,
        )
        second = observations_payload(
            [{"date": "2020-01-03", "value": "3", "realtime_start": "2020-01-04"}],
            count=3,
            offset=2,
            limit=2,
        )
        transport = FakeTransport(pages={0: first, 2: second})
        storage = RecordingStorage()
        bundle = self.provider(transport=transport, storage=storage).fetch(self.requirement())
        self.assertEqual(len(bundle.observations), 3)
        self.assertEqual([call[1]["offset"] for call in transport.calls[1:]], [0, 2])
        first_parameters = transport.calls[1][1]
        for _, parameters in transport.calls[2:]:
            self.assertEqual(
                {key: value for key, value in parameters.items() if key != "offset"},
                {
                    key: value
                    for key, value in first_parameters.items()
                    if key != "offset"
                },
            )
            self.assertEqual(parameters["output_type"], 4)
            self.assertEqual(parameters["realtime_start"], "1776-07-04")
            self.assertEqual(parameters["realtime_end"], "9999-12-31")
        raw_pages = storage.calls[0]["raw_observations"]
        self.assertEqual(len(raw_pages), 2)
        digest = hashlib.sha256()
        for page in raw_pages:
            digest.update(len(page).to_bytes(8, "big"))
            digest.update(page)
        self.assertEqual(bundle.source.content_sha256, digest.hexdigest())

    def test_truncated_or_malformed_pagination_is_rejected(self) -> None:
        cases = (
            {0: observations_payload([], count=1)},
            {0: {"count": 1, "offset": 0, "observations": []}},
            {0: observations_payload([], count=0, offset=1)},
        )
        for pages in cases:
            with self.subTest(pages=pages):
                with self.assertRaises(FredResponseFormatError):
                    self.provider(transport=FakeTransport(pages=pages)).fetch(
                        self.requirement()
                    )

    def test_persistence_failure_cannot_be_reported_as_success(self) -> None:
        provider = self.provider(storage=RecordingStorage(fail=True))
        with self.assertRaises(FredPersistenceError) as caught:
            provider.fetch(self.requirement())
        self.assertIsNone(provider.last_snapshot)
        self.assertFalse(provider.status().ready)
        self.assertNotIn(SENTINEL_KEY, str(caught.exception))

    def test_key_never_enters_bundle_storage_arguments_or_snapshot_paths(self) -> None:
        storage = RecordingStorage()
        provider = self.provider(storage=storage)
        bundle = provider.fetch(self.requirement())
        combined = json.dumps(bundle.model_dump(mode="json"), sort_keys=True)
        combined += repr(storage.calls)
        combined += repr(provider.last_snapshot.public_summary())
        self.assertNotIn(SENTINEL_KEY, combined)


if __name__ == "__main__":
    unittest.main()
