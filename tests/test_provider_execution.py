"""Contract tests for authorized provider execution (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import tempfile
import unittest
from unittest import mock

from market_validator.data.acquisition_request import (
    AccessMode,
    PaginationPolicy,
    PublicRequestStep,
    RequestMethod,
    generate_acquisition_request_plan,
)
from market_validator.data.access_authorization import (
    DataAccessAuthorizationError,
    create_data_access_authorization,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    confirm_data_plan,
    generate_data_plan,
)
from market_validator.data.execution import (
    ExecutionError,
    ExecutionErrorCode,
    ExecutionStage,
    FredExecutionAdapter,
    LocalFileExecutionAdapter,
    execute_authorized_acquisition,
)
from market_validator.data.providers.csv_provider import CSVProvider
from market_validator.data.providers.fred_provider import (
    FRED_OBSERVATIONS_PATH,
    FRED_SERIES_PATH,
    FredProvider,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.snapshot import (
    AcquisitionExecutionOutcome,
    ExecutionStatus,
    parse_execution_outcome,
    verify_acquisition_snapshot,
)
from market_validator.data.source_selection import (
    SourceSelectionDecision,
    confirm_source_selection,
    generate_source_selection,
)
from market_validator.research.enums import Frequency, Transformation
from market_validator.research.serialization import parse_research_spec

from tests.test_fred_provider import (
    FakeTransport,
    observations_payload,
    response,
    series_payload,
)

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_CALENDARS = (
    ROOT / "examples" / "synthetic" / "synthetic_market.calendars.json"
)
SYNTHETIC_INSTRUMENTS = (
    ROOT / "examples" / "synthetic" / "synthetic_market.instruments.json"
)
SYNTHETIC_SPEC = ROOT / "examples" / "synthetic" / "synthetic_market.spec.json"
CONFIRMED_AT = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fred_capability_snapshot():
    calendars, instruments = _load_registries()
    fred = FredProvider(instrument_registry=instruments)
    from market_validator.data.acquisition_request import (
        snapshot_provider_capabilities,
    )

    return snapshot_provider_capabilities(fred.capabilities())


def _load_registries() -> tuple[CalendarRegistry, InstrumentRegistry]:
    calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
    instruments = InstrumentRegistry.from_json_file(SYNTHETIC_INSTRUMENTS, calendars)
    return calendars, instruments


def _fred_spec():
    payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))

    def fix(variable):
        variable = dict(variable)
        variable["instrument"] = dict(variable["instrument"])
        variable["instrument"]["asset_type"] = "macro_series"
        variable["instrument"]["unit"] = "Dollars per Barrel"
        variable["field"] = "value"
        variable["revision_policy"] = {"mode": "latest_available"}
        return variable

    payload = dict(payload)
    payload["outcome"] = fix(payload["outcome"])
    payload["predictors"] = [fix(variable) for variable in payload["predictors"]]
    return parse_research_spec(_json_bytes(payload))


def _fred_registry() -> tuple[CalendarRegistry, InstrumentRegistry]:
    calendars, instruments = _load_registries()
    series_ids = ["DCOILWTICO", "DCOILBRENTEU"]
    entries = []
    for index, entry in enumerate(instruments.entries):
        mapping = entry.provider_mappings[0]
        entries.append(
            entry.model_copy(
                update={
                    "asset_type": "macro_series",
                    "unit": "Dollars per Barrel",
                    "provider_mappings": [
                        mapping.model_copy(
                            update={
                                "provider_id": "fred",
                                "provider_symbol": series_ids[index],
                                "dataset_or_endpoint": (
                                    FRED_OBSERVATIONS_PATH
                                ),
                                "verification_source_uri": (
                                    "https://fred.stlouisfed.org/series/"
                                    + series_ids[index]
                                ),
                                "market": entry.market,
                            }
                        )
                    ]
                }
            )
        )
    return calendars, InstrumentRegistry(entries, calendars)


def _build_fred_chain(
    transport: FakeTransport,
    *,
    api_key: str = "a" * 32,
) -> dict[str, object]:
    calendars, instruments = _fred_registry()
    spec = _fred_spec()
    generated = generate_data_plan(spec, instruments, calendars)
    confirmation = confirm_data_plan(generated, instruments, confirmed_at=CONFIRMED_AT)
    decisions = []
    for requirement in generated.data_plan.requirements:
        entry = instruments.get(requirement.instrument_id)
        mapping = next(
            mapping for mapping in entry.provider_mappings if mapping.verified
        )
        decisions.append(
            SourceSelectionDecision(
                requirement_id=requirement.requirement_id,
                provider_id=mapping.provider_id,
                provider_symbol=mapping.provider_symbol,
                dataset_or_endpoint=mapping.dataset_or_endpoint,
            )
        )
    selection = generate_source_selection(
        generated, confirmation, instruments, calendars, decisions
    )
    selection_confirmation = confirm_source_selection(
        selection, instruments, calendars, confirmed_at=CONFIRMED_AT
    )
    plan = generate_acquisition_request_plan(
        selection,
        selection_confirmation,
        generated.data_plan,
        instruments,
        calendars,
        {"fred": _fred_capability_snapshot()},
    )
    request_ids = [
        request.requirement_id for request in plan.acquisition_request_plan.requests
    ]
    authorization = create_data_access_authorization(
        plan,
        instruments,
        calendars,
        {"fred": _fred_capability_snapshot()},
        request_ids,
        authorized_at=CONFIRMED_AT,
    )
    fred = FredProvider(
        instrument_registry=instruments,
        environment={"FRED_API_KEY": api_key},
        transport=transport,
        clock=lambda: CONFIRMED_AT,
    )
    return {
        "plan": plan,
        "authorization": authorization,
        "data_plan": generated.data_plan,
        "instruments": instruments,
        "calendars": calendars,
        "capabilities": {"fred": _fred_capability_snapshot()},
        "adapters": {"fred": FredExecutionAdapter(fred)},
    }


def _bundle_issues(verified, record, artifact):
    from market_validator.data.models import DataBundle

    content = (
        verified.snapshot_path
        / "requests"
        / record.requirement_id
        / artifact.relative_path
    ).read_bytes()
    return DataBundle.model_validate_json(content).quality.issues


def _execute_fred(transport: object, tmp: Path) -> object:
    chain = _build_fred_chain(transport)
    return execute_authorized_acquisition(
        generated_plan=chain["plan"],
        authorization=chain["authorization"],
        data_plan=chain["data_plan"],
        instrument_registry=chain["instruments"],
        calendar_registry=chain["calendars"],
        capability_snapshots=chain["capabilities"],
        attempt_id="attempt-fred-1",
        receipt_path=tmp / "receipt.json",
        snapshot_root=tmp / "snapshots",
        adapters=chain["adapters"],
    )


class _SeriesAwareFakeTransport(FakeTransport):
    """Routes FRED series metadata by series_id for multi-series tests."""

    def get_json(self, path: str, public_parameters):
        parameters = dict(public_parameters)
        self.calls.append((path, parameters))
        if path == FRED_SERIES_PATH:
            series_id = parameters.get("series_id")
            if series_id in ("DCOILWTICO", "DCOILBRENTEU"):
                return response(series_payload(series_id=series_id))
            return response(series_payload(series_id="DCOILWTICO"))
        return response(self.pages[int(parameters["offset"])])


class ExactRequestContractTest(unittest.TestCase):
    def test_fred_plan_has_metadata_and_observations_steps(self) -> None:
        transport = FakeTransport()
        chain = _build_fred_chain(transport)
        request = chain["plan"].acquisition_request_plan.requests[0]
        self.assertEqual([step.sequence for step in request.steps], [1, 2])
        self.assertEqual(
            [step.endpoint for step in request.steps],
            [FRED_SERIES_PATH, FRED_OBSERVATIONS_PATH],
        )
        observations = request.steps[1]
        self.assertEqual(
            observations.public_parameters.get("limit"), "100000"
        )
        self.assertNotIn("revision_policy", observations.public_parameters)
        self.assertNotIn("access_mode", observations.public_parameters)
        self.assertNotIn(
            "requirement_id", observations.public_parameters
        )
        self.assertNotIn(
            "request_plan_id", observations.public_parameters
        )
        self.assertNotIn("authorization_id", observations.public_parameters)
        self.assertEqual(
            observations.pagination_policy,
            PaginationPolicy.FRED_COUNT_OFFSET_V1,
        )

    def test_initial_release_realtime_parameters(self) -> None:
        payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        payload["outcome"] = dict(payload["outcome"])
        payload["outcome"]["instrument"] = dict(payload["outcome"]["instrument"])
        payload["outcome"]["instrument"]["asset_type"] = "macro_series"
        payload["outcome"]["instrument"]["unit"] = "Dollars per Barrel"
        payload["outcome"]["field"] = "value"
        payload["outcome"]["revision_policy"] = {"mode": "initial_release"}
        payload["predictors"] = [
            {
                **dict(predictor),
                "instrument": {
                    **dict(predictor["instrument"]),
                    "asset_type": "macro_series",
                    "unit": "Dollars per Barrel",
                },
                "field": "value",
                "revision_policy": {"mode": "initial_release"},
            }
            for predictor in payload["predictors"]
        ]
        calendars, instruments = _fred_registry()
        spec = parse_research_spec(_json_bytes(payload))
        generated = generate_data_plan(spec, instruments, calendars)
        # Direct step rendering from the provider contract:
        fred = FredProvider(instrument_registry=instruments)
        requirement = generated.data_plan.requirements[0]
        steps = fred.render_public_steps(requirement, "DCOILWTICO")
        observations = steps[1]
        params = observations["public_parameters"]
        self.assertEqual(params.get("output_type"), "4")
        self.assertEqual(params.get("realtime_start"), "1776-07-04")
        self.assertEqual(params.get("realtime_end"), "9999-12-31")

    def test_step_sequence_enters_canonical_hash(self) -> None:
        transport = FakeTransport()
        chain = _build_fred_chain(transport)
        plan = chain["plan"]
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        steps = payload["requests"][0]["steps"]
        steps[0]["sequence"] = 5
        steps[1]["sequence"] = 6
        from market_validator.data.acquisition_request import (
            parse_acquisition_request_plan,
            calculate_acquisition_request_plan_sha256,
        )

        changed = parse_acquisition_request_plan(_json_bytes(payload))
        self.assertNotEqual(
            calculate_acquisition_request_plan_sha256(changed),
            plan.acquisition_request_plan_sha256,
        )

    def test_endpoint_change_invalidates_authorization(self) -> None:
        transport = FakeTransport()
        chain = _build_fred_chain(transport)
        plan = chain["plan"]
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["requests"][0]["steps"][1]["endpoint"] = "/other/endpoint"
        from market_validator.data.acquisition_request import (
            GeneratedAcquisitionRequestPlan,
            calculate_acquisition_request_plan_sha256,
            parse_acquisition_request_plan,
        )

        changed_plan = parse_acquisition_request_plan(_json_bytes(payload))
        changed_generated = GeneratedAcquisitionRequestPlan(
            acquisition_request_plan=changed_plan,
            acquisition_request_plan_sha256=(
                calculate_acquisition_request_plan_sha256(changed_plan)
            ),
            research_spec_sha256=plan.research_spec_sha256,
            data_plan_sha256=plan.data_plan_sha256,
            data_plan_confirmation_sha256=(
                plan.data_plan_confirmation_sha256
            ),
            source_selection_sha256=plan.source_selection_sha256,
            source_selection_confirmation_sha256=(
                plan.source_selection_confirmation_sha256
            ),
            instrument_registry_sha256=plan.instrument_registry_sha256,
            calendar_registry_sha256=plan.calendar_registry_sha256,
            capability_snapshot_sha256s=plan.capability_snapshot_sha256s,
        )
        with self.assertRaises(DataAccessAuthorizationError):
            from market_validator.data.access_authorization import (
                validate_data_access_authorization_matches,
            )

            validate_data_access_authorization_matches(
                changed_generated, chain["authorization"]
            )

    def test_parameter_change_invalidates_authorization(self) -> None:
        transport = FakeTransport()
        chain = _build_fred_chain(transport)
        plan = chain["plan"]
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["requests"][0]["steps"][1]["public_parameters"][
            "limit"
        ] = "10"
        from market_validator.data.acquisition_request import (
            GeneratedAcquisitionRequestPlan,
            calculate_acquisition_request_plan_sha256,
            parse_acquisition_request_plan,
        )

        changed_plan = parse_acquisition_request_plan(_json_bytes(payload))
        changed_generated = GeneratedAcquisitionRequestPlan(
            acquisition_request_plan=changed_plan,
            acquisition_request_plan_sha256=(
                calculate_acquisition_request_plan_sha256(changed_plan)
            ),
            research_spec_sha256=plan.research_spec_sha256,
            data_plan_sha256=plan.data_plan_sha256,
            data_plan_confirmation_sha256=(
                plan.data_plan_confirmation_sha256
            ),
            source_selection_sha256=plan.source_selection_sha256,
            source_selection_confirmation_sha256=(
                plan.source_selection_confirmation_sha256
            ),
            instrument_registry_sha256=plan.instrument_registry_sha256,
            calendar_registry_sha256=plan.calendar_registry_sha256,
            capability_snapshot_sha256s=plan.capability_snapshot_sha256s,
        )
        with self.assertRaises(DataAccessAuthorizationError):
            from market_validator.data.access_authorization import (
                validate_data_access_authorization_matches,
            )

            validate_data_access_authorization_matches(
                changed_generated, chain["authorization"]
            )

    def test_pagination_policy_change_invalidates_authorization(self) -> None:
        transport = FakeTransport()
        chain = _build_fred_chain(transport)
        plan = chain["plan"]
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["requests"][0]["steps"][1][
            "pagination_policy"
        ] = "none"
        from market_validator.data.acquisition_request import (
            parse_acquisition_request_plan,
            calculate_acquisition_request_plan_sha256,
        )

        changed_plan = parse_acquisition_request_plan(_json_bytes(payload))
        self.assertNotEqual(
            calculate_acquisition_request_plan_sha256(changed_plan),
            plan.acquisition_request_plan_sha256,
        )


class FredExecutionAdapterTest(unittest.TestCase):
    def test_metadata_and_observations_execute_with_exact_calls(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = _SeriesAwareFakeTransport()
            verified = _execute_fred(transport, tmp)
            self.assertEqual(
                verified.manifest.snapshot_id, verified.snapshot_id
            )
            paths = [path for path, _ in transport.calls]
            self.assertEqual(
                paths,
                [
                    FRED_SERIES_PATH,
                    FRED_OBSERVATIONS_PATH,
                    FRED_SERIES_PATH,
                    FRED_OBSERVATIONS_PATH,
                ],
            )
            offsets = [
                parameters.get("offset")
                for path, parameters in transport.calls
                if path == FRED_OBSERVATIONS_PATH
            ]
            self.assertEqual(offsets, [0, 0])
            self.assertFalse(verified.outcome.failure)
            self.assertEqual(
                verified.outcome.status, ExecutionStatus.SUCCEEDED
            )

    def test_multi_page_observations(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = _SeriesAwareFakeTransport(
                pages={
                    0: observations_payload(
                        [
                            {"date": "2020-01-01", "value": "1.0",
                             "realtime_start": "2020-01-02"},
                            {"date": "2020-01-02", "value": "2.0",
                             "realtime_start": "2020-01-03"},
                        ],
                        count=4,
                    ),
                    2: observations_payload(
                        [
                            {"date": "2020-01-03", "value": "3.0",
                             "realtime_start": "2020-01-04"},
                            {"date": "2020-01-04", "value": "4.0",
                             "realtime_start": "2020-01-05"},
                        ],
                        count=4,
                        offset=2,
                    ),
                }
            )
            verified = _execute_fred(transport, tmp)
            offsets = [
                parameters.get("offset")
                for path, parameters in transport.calls
                if path == FRED_OBSERVATIONS_PATH
            ]
            self.assertEqual(offsets, [0, 2, 0, 2])

    def test_credential_missing_fails_before_any_request(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = FakeTransport()
            chain = _build_fred_chain(transport, api_key="b" * 32)
            chain["adapters"] = {
                "fred": FredExecutionAdapter(
                    FredProvider(
                        instrument_registry=chain["instruments"],
                        environment={},
                        transport=transport,
                    )
                )
            }
            with self.assertRaises(ExecutionError) as raised:
                execute_authorized_acquisition(
                    generated_plan=chain["plan"],
                    authorization=chain["authorization"],
                    data_plan=chain["data_plan"],
                    instrument_registry=chain["instruments"],
                    calendar_registry=chain["calendars"],
                    capability_snapshots=chain["capabilities"],
                    attempt_id="attempt-missing-cred",
                    receipt_path=tmp / "receipt.json",
                    snapshot_root=tmp / "snapshots",
                    adapters=chain["adapters"],
                )
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.CREDENTIAL_NOT_CONFIGURED,
            )
            self.assertEqual(transport.calls, [])

    def test_secret_never_appears_in_artifacts(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = _SeriesAwareFakeTransport()
            verified = _execute_fred(transport, tmp)
            manifest_bytes = (
                verified.snapshot_path / "manifest.json"
            ).read_bytes()
            trace_path = next(
                (
                    verified.snapshot_path / "requests" / record.requirement_id
                    / "request-trace.json"
                    for record in verified.manifest.request_records
                )
            )
            trace_bytes = trace_path.read_bytes()
            for artifact_bytes in (manifest_bytes, trace_bytes):
                self.assertNotIn(b"test-key", artifact_bytes)
                self.assertNotIn(b"api_key", artifact_bytes.lower())


class CsvExecutionAdapterTest(unittest.TestCase):
    def _csv_chain(self, tmp: Path, csv_path: Path) -> dict[str, object]:
        calendars, instruments = _load_registries()
        names = ["outcome.csv", "predictor.csv"]
        files = []
        for index in range(2):
            target = csv_path if index == 0 else csv_path.parent / names[index]
            if not target.exists():
                target.write_text(
                    "date,value\n2020-01-01,1.0\n2020-01-02,2.0\n",
                    encoding="utf-8",
                )
            files.append(target)
        entries = []
        for index, entry in enumerate(instruments.entries):
            mapping = entry.provider_mappings[0]
            entries.append(
                entry.model_copy(
                    update={
                        "provider_mappings": [
                            mapping.model_copy(
                                update={
                                    "provider_id": "local_csv",
                                    "provider_symbol": str(files[index]),
                                }
                            )
                        ]
                    }
                )
            )
        csv_registry = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(
            parse_research_spec(SYNTHETIC_SPEC.read_bytes()),
            csv_registry,
            calendars,
        )
        confirmation = confirm_data_plan(
            generated, csv_registry, confirmed_at=CONFIRMED_AT
        )
        decisions = []
        for requirement in generated.data_plan.requirements:
            mapping = csv_registry.get(
                requirement.instrument_id
            ).provider_mappings[0]
            decisions.append(
                SourceSelectionDecision(
                    requirement_id=requirement.requirement_id,
                    provider_id=mapping.provider_id,
                    provider_symbol=mapping.provider_symbol,
                    dataset_or_endpoint=mapping.dataset_or_endpoint,
                )
            )
        selection = generate_source_selection(
            generated, confirmation, csv_registry, calendars, decisions
        )
        selection_confirmation = confirm_source_selection(
            selection, csv_registry, calendars, confirmed_at=CONFIRMED_AT
        )
        from market_validator.data.acquisition_request import (
            AccessMode,
            ProviderCapabilitySnapshot,
        )

        snapshot = ProviderCapabilitySnapshot(
            provider_id="local_csv",
            supported_access_modes=[AccessMode.LOCAL_FILE],
            supported_frequencies=[Frequency.ONE_DAY],
            supported_transforms=list(Transformation),
            supports_date_range=True,
            supports_revision_policy=False,
            supports_dry_run=False,
            supports_previous_observations=False,
            requires_credential=False,
            paid_access_possible=False,
        )
        plan = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            generated.data_plan,
            csv_registry,
            calendars,
            {"local_csv": snapshot},
        )
        request_ids = [
            request.requirement_id
            for request in plan.acquisition_request_plan.requests
        ]
        authorization = create_data_access_authorization(
            plan,
            csv_registry,
            calendars,
            {"local_csv": snapshot},
            request_ids,
            authorized_at=CONFIRMED_AT,
        )
        provider = CSVProvider(csv_path)
        return {
            "plan": plan,
            "authorization": authorization,
            "data_plan": generated.data_plan,
            "instruments": csv_registry,
            "calendars": calendars,
            "capabilities": {"local_csv": snapshot},
            "adapters": {"local_csv": LocalFileExecutionAdapter(provider)},
        }

    def test_valid_absolute_file_executes(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            csv_path = tmp / "data.csv"
            csv_path.write_text(
                "date,value\n2020-01-01,1.0\n2020-01-02,2.0\n",
                encoding="utf-8",
            )
            chain = self._csv_chain(tmp, csv_path)
            verified = execute_authorized_acquisition(
                generated_plan=chain["plan"],
                authorization=chain["authorization"],
                data_plan=chain["data_plan"],
                instrument_registry=chain["instruments"],
                calendar_registry=chain["calendars"],
                capability_snapshots=chain["capabilities"],
                attempt_id="attempt-csv-1",
                receipt_path=tmp / "receipt.json",
                snapshot_root=tmp / "snapshots",
                adapters=chain["adapters"],
            )
            self.assertEqual(
                verified.outcome.status, ExecutionStatus.SUCCEEDED
            )
            self.assertEqual(len(verified.manifest.request_records), 2)

    def test_relative_path_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            relative = "relative/data.csv"
            calendars, instruments = _load_registries()
            entries = []
            for index, entry in enumerate(instruments.entries):
                mapping = entry.provider_mappings[0]
                entries.append(
                    entry.model_copy(
                        update={
                            "provider_mappings": [
                                mapping.model_copy(
                                    update={
                                        "provider_id": "local_csv",
                                        "provider_symbol": (
                                            relative if index == 0
                                            else "relative/predictor.csv"
                                        ),
                                    }
                                )
                            ]
                        }
                    )
                )
            from market_validator.data.acquisition_request import (
                ProviderCapabilitySnapshot,
            )

            csv_registry = InstrumentRegistry(entries, calendars)
            generated = generate_data_plan(
                parse_research_spec(SYNTHETIC_SPEC.read_bytes()),
                csv_registry,
                calendars,
            )
            confirmation = confirm_data_plan(
                generated, csv_registry, confirmed_at=CONFIRMED_AT
            )
            decisions = []
            for requirement in generated.data_plan.requirements:
                mapping = csv_registry.get(
                    requirement.instrument_id
                ).provider_mappings[0]
                decisions.append(
                    SourceSelectionDecision(
                        requirement_id=requirement.requirement_id,
                        provider_id=mapping.provider_id,
                        provider_symbol=mapping.provider_symbol,
                        dataset_or_endpoint=mapping.dataset_or_endpoint,
                    )
                )
            selection = generate_source_selection(
                generated, confirmation, csv_registry, calendars, decisions
            )
            selection_confirmation = confirm_source_selection(
                selection, csv_registry, calendars, confirmed_at=CONFIRMED_AT
            )
            snapshot = ProviderCapabilitySnapshot(
                provider_id="local_csv",
                supported_access_modes=[AccessMode.LOCAL_FILE],
                supported_frequencies=[Frequency.ONE_DAY],
                supported_transforms=list(Transformation),
                supports_date_range=True,
                supports_revision_policy=False,
                supports_dry_run=False,
                supports_previous_observations=False,
                requires_credential=False,
                paid_access_possible=False,
            )
            plan = generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                generated.data_plan,
                csv_registry,
                calendars,
                {"local_csv": snapshot},
            )
            # The plan must be unresolved (local file identity not provable).
            self.assertTrue(
                plan.acquisition_request_plan.unresolved_requirements
            )

    def test_directory_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            target = tmp / "not-a-file"
            target.mkdir()
            chain = self._csv_chain(tmp, target)
            with self.assertRaises(ExecutionError) as raised:
                execute_authorized_acquisition(
                    generated_plan=chain["plan"],
                    authorization=chain["authorization"],
                    data_plan=chain["data_plan"],
                    instrument_registry=chain["instruments"],
                    calendar_registry=chain["calendars"],
                    capability_snapshots=chain["capabilities"],
                    attempt_id="attempt-dir",
                    receipt_path=tmp / "receipt.json",
                    snapshot_root=tmp / "snapshots",
                    adapters=chain["adapters"],
                )
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.UNSAFE_LOCAL_FILE,
            )

    def test_invalid_utf8_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            csv_path = tmp / "data.csv"
            csv_path.write_bytes(b"\xff\xfe\x00date,value\n")
            chain = self._csv_chain(tmp, csv_path)
            verified = execute_authorized_acquisition(
                generated_plan=chain["plan"],
                authorization=chain["authorization"],
                data_plan=chain["data_plan"],
                instrument_registry=chain["instruments"],
                calendar_registry=chain["calendars"],
                capability_snapshots=chain["capabilities"],
                attempt_id="attempt-utf8",
                receipt_path=tmp / "receipt.json",
                snapshot_root=tmp / "snapshots",
                adapters=chain["adapters"],
            )
            # Encoding problems are quality findings, not execution failures:
            self.assertEqual(
                verified.outcome.status, ExecutionStatus.SUCCEEDED
            )
            self.assertTrue(
                any(
                    issue.code == "invalid_encoding"
                    for record in verified.manifest.request_records
                    for artifact in record.artifacts
                    if artifact.role == "bundle"
                    for issue in _bundle_issues(verified, record, artifact)
                )
            )


class AuthorizationConsumptionTest(unittest.TestCase):
    def test_receipt_failure_prevents_provider_calls(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = FakeTransport()
            chain = _build_fred_chain(transport)
            # Pre-create a conflicting receipt path (directory) so persistence
            # must fail before any provider call.
            receipt_dir = tmp / "receipt.json"
            receipt_dir.mkdir()
            with self.assertRaises(ExecutionError) as raised:
                execute_authorized_acquisition(
                    generated_plan=chain["plan"],
                    authorization=chain["authorization"],
                    data_plan=chain["data_plan"],
                    instrument_registry=chain["instruments"],
                    calendar_registry=chain["calendars"],
                    capability_snapshots=chain["capabilities"],
                    attempt_id="attempt-receipt-fail",
                    receipt_path=receipt_dir,
                    snapshot_root=tmp / "snapshots",
                    adapters=chain["adapters"],
                )
            self.assertEqual(transport.calls, [])
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.AUTHORIZATION_NOT_CONSUMED,
            )

    def test_second_execution_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = _SeriesAwareFakeTransport()
            chain = _build_fred_chain(transport)
            execute_authorized_acquisition(
                generated_plan=chain["plan"],
                authorization=chain["authorization"],
                data_plan=chain["data_plan"],
                instrument_registry=chain["instruments"],
                calendar_registry=chain["calendars"],
                capability_snapshots=chain["capabilities"],
                attempt_id="attempt-1",
                receipt_path=tmp / "receipt.json",
                snapshot_root=tmp / "snapshots",
                adapters=chain["adapters"],
            )
            calls_after_first = len(transport.calls)
            with self.assertRaises(ExecutionError) as raised:
                execute_authorized_acquisition(
                    generated_plan=chain["plan"],
                    authorization=chain["authorization"],
                    data_plan=chain["data_plan"],
                    instrument_registry=chain["instruments"],
                    calendar_registry=chain["calendars"],
                    capability_snapshots=chain["capabilities"],
                    attempt_id="attempt-2",
                    receipt_path=tmp / "receipt.json",
                    snapshot_root=tmp / "snapshots",
                    adapters=chain["adapters"],
                )
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
            )
            self.assertEqual(len(transport.calls), calls_after_first)

    def test_plan_mutation_rejected_before_execution(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            (tmp / "snapshots").mkdir()
            transport = FakeTransport()
            chain = _build_fred_chain(transport)
            payload = chain["plan"].acquisition_request_plan.model_dump(
                mode="json"
            )
            payload["requests"][0]["steps"][1]["public_parameters"][
                "series_id"
            ] = "MUTATED"
            from market_validator.data.acquisition_request import (
                GeneratedAcquisitionRequestPlan,
                calculate_acquisition_request_plan_sha256,
                parse_acquisition_request_plan,
            )

            mutated = parse_acquisition_request_plan(_json_bytes(payload))
            mutated_generated = GeneratedAcquisitionRequestPlan(
                acquisition_request_plan=mutated,
                acquisition_request_plan_sha256=(
                    calculate_acquisition_request_plan_sha256(mutated)
                ),
                research_spec_sha256=chain["plan"].research_spec_sha256,
                data_plan_sha256=chain["plan"].data_plan_sha256,
                data_plan_confirmation_sha256=(
                    chain["plan"].data_plan_confirmation_sha256
                ),
                source_selection_sha256=chain["plan"].source_selection_sha256,
                source_selection_confirmation_sha256=(
                    chain["plan"].source_selection_confirmation_sha256
                ),
                instrument_registry_sha256=(
                    chain["plan"].instrument_registry_sha256
                ),
                calendar_registry_sha256=(
                    chain["plan"].calendar_registry_sha256
                ),
                capability_snapshot_sha256s=(
                    chain["plan"].capability_snapshot_sha256s
                ),
            )
            with self.assertRaises(ExecutionError) as raised:
                execute_authorized_acquisition(
                    generated_plan=mutated_generated,
                    authorization=chain["authorization"],
                    data_plan=chain["data_plan"],
                    instrument_registry=chain["instruments"],
                    calendar_registry=chain["calendars"],
                    capability_snapshots=chain["capabilities"],
                    attempt_id="attempt-mutated",
                    receipt_path=tmp / "receipt.json",
                    snapshot_root=tmp / "snapshots",
                    adapters=chain["adapters"],
                )
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.AUTHORIZATION_MISMATCH,
            )
            self.assertEqual(transport.calls, [])


class ReviewRegressionTest(unittest.TestCase):
    def test_fred_transport_capture_path_never_retries(self) -> None:
        from pydantic import SecretStr

        from market_validator.data.providers.fred_errors import (
            FredServiceUnavailableError,
        )
        from market_validator.data.providers.fred_transport import (
            FredHttpsTransport,
        )
        from tests.test_fred_transport import FakeResponse, ScriptedOpener

        error_body = json.dumps(
            {"error_code": 500, "error_message": "Internal provider error."}
        ).encode()
        opener = ScriptedOpener([FakeResponse(500, error_body)])
        transport = FredHttpsTransport(
            SecretStr("a" * 32),
            opener=opener,
            retries=0,
        )
        with self.assertRaises(FredServiceUnavailableError):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 1)

    def test_fred_http_error_path_never_retries_with_retries_zero(self) -> None:
        from pydantic import SecretStr

        from market_validator.data.providers.fred_errors import (
            FredServiceUnavailableError,
        )
        from market_validator.data.providers.fred_transport import (
            FredHttpsTransport,
        )
        from tests.test_fred_transport import ScriptedOpener, http_error

        opener = ScriptedOpener([http_error(503), http_error(503)])
        transport = FredHttpsTransport(
            SecretStr("a" * 32),
            opener=opener,
            retries=0,
        )
        with self.assertRaises(FredServiceUnavailableError):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 1)

    def test_local_file_replaced_by_symlink_during_read_rejected(self) -> None:
        import stat as stat_module

        from market_validator.data.acquisition_request import (
            PreSampleResolutionMethod,
        )
        from market_validator.research.enums import DataRevisionMode
        from market_validator.data.execution import (
            ExecutionErrorCode,
            LocalFileExecutionAdapter,
            fail_execution,
        )
        from market_validator.data.providers.csv_provider import CSVProvider

        adapter = LocalFileExecutionAdapter(CSVProvider(Path("unused.csv")))
        from market_validator.data.acquisition_request import (
            PublicAcquisitionRequest,
        )

        class _FakeRegularStat:
            st_mode = stat_module.S_IFREG | 0o644
            st_size = 10
            st_mtime_ns = 1
            st_ino = 1

        class _FakeSymlinkStat:
            st_mode = stat_module.S_IFLNK | 0o777
            st_size = 5
            st_mtime_ns = 1
            st_ino = 2

        calls = {"count": 0}

        class _SwitchingPath:
            def __init__(self, inner: Path) -> None:
                self._inner = inner

            def lstat(self):
                calls["count"] += 1
                if calls["count"] == 1:
                    return _FakeRegularStat()
                return _FakeSymlinkStat()

            def is_symlink(self) -> bool:
                return False

            @property
            def parent(self):
                return self._inner.parent

            def read_bytes(self) -> bytes:
                return b"date,value\n2020-01-01,1.0\n"

            def __str__(self) -> str:
                return str(self._inner)

        tmp_dir = Path(tempfile.mkdtemp(dir=ROOT))
        try:
            target = _SwitchingPath(tmp_dir / "data.csv")
            request = PublicAcquisitionRequest(
                requirement_id="req-1",
                variable_id="var-1",
                instrument_id="ins-1",
                provider_id="local_csv",
                provider_symbol=str(tmp_dir / "data.csv"),
                dataset_or_endpoint=str(tmp_dir / "data.csv"),
                access_mode=AccessMode.LOCAL_FILE,
                request_method=RequestMethod.READ,
                public_parameters={"source_uri": str(tmp_dir / "data.csv")},
                sample_start=datetime(2020, 1, 1, tzinfo=timezone.utc).date(),
                sample_end=datetime(2020, 1, 2, tzinfo=timezone.utc).date(),
                acquisition_start=datetime(2020, 1, 1, tzinfo=timezone.utc).date(),
                acquisition_end=datetime(2020, 1, 2, tzinfo=timezone.utc).date(),
                pre_sample_periods_required=0,
                pre_sample_resolution_method=PreSampleResolutionMethod.NONE_REQUIRED,
                revision_policy=DataRevisionMode.LATEST_AVAILABLE,
                steps=[
                    PublicRequestStep(
                        step_id="local-file-read",
                        sequence=1,
                        method=RequestMethod.READ,
                        endpoint=str(tmp_dir / "data.csv"),
                        public_parameters={
                            "source_uri": str(tmp_dir / "data.csv")
                        },
                        pagination_policy=PaginationPolicy.NONE,
                    )
                ],
            )
            from market_validator.data.calendars import CalendarRegistry
            from market_validator.data.data_plan_review import (
                generate_data_plan,
            )
            from market_validator.data.registry import InstrumentRegistry
            from market_validator.research.serialization import (
                parse_research_spec,
            )

            calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
            instruments = InstrumentRegistry.from_json_file(
                SYNTHETIC_INSTRUMENTS, calendars
            )
            generated = generate_data_plan(
                parse_research_spec(SYNTHETIC_SPEC.read_bytes()),
                instruments,
                calendars,
            )
            requirement = generated.data_plan.requirements[0]
            with mock.patch(
                "market_validator.data.execution._resolve_file_identity",
                return_value=target,
            ):
                with self.assertRaises(ExecutionError) as raised:
                    adapter.execute_capture(request, requirement)
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.LOCAL_FILE_CHANGED_DURING_READ,
            )
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
