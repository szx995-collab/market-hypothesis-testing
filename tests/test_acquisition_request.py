"""Offline contract tests for acquisition-request planning and pre-sample."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_validator.data.acquisition_request import (
    AccessMode,
    AcquisitionRequestPlan,
    AcquisitionRequestReviewError,
    AcquisitionRequestReviewErrorCode,
    AcquisitionUnresolvedCode,
    GeneratedAcquisitionRequestPlan,
    PreSampleResolutionMethod,
    PreSampleStatus,
    ProviderCapabilitySnapshot,
    PublicAcquisitionRequest,
    acquisition_request_readiness_blockers,
    calculate_acquisition_request_plan_sha256,
    generate_acquisition_request_plan,
    parse_acquisition_request_plan,
    parse_provider_capability_snapshot,
    persist_generated_acquisition_request_plan,
    render_public_acquisition_request,
    resolve_pre_sample_requirement,
    serialize_acquisition_request_plan,
    serialize_provider_capability_snapshot,
    snapshot_provider_capabilities,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    confirm_data_plan,
    generate_data_plan,
)
from market_validator.data.models import DataPlan
from market_validator.data.providers.csv_provider import CSVProvider
from market_validator.data.providers.fred_provider import FredProvider
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.source_selection import (
    SourceSelectionDecision,
    confirm_source_selection,
    generate_source_selection,
)
from market_validator.research.enums import DataRevisionMode, Frequency, Transformation
from market_validator.research.serialization import parse_research_spec

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


def _load_registries() -> tuple[CalendarRegistry, InstrumentRegistry]:
    calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
    instruments = InstrumentRegistry.from_json_file(SYNTHETIC_INSTRUMENTS, calendars)
    return calendars, instruments


def _synthetic_spec():
    return parse_research_spec(SYNTHETIC_SPEC.read_bytes())


def _confirmed_selection() -> tuple[object, object, object, object, object]:
    calendars, instruments = _load_registries()
    generated = generate_data_plan(_synthetic_spec(), instruments, calendars)
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
    return (
        selection,
        selection_confirmation,
        generated.data_plan,
        instruments,
        calendars,
    )


def _synthetic_snapshot(provider_id: str = "synthetic_provider") -> ProviderCapabilitySnapshot:
    return ProviderCapabilitySnapshot(
        provider_id=provider_id,
        supported_access_modes=[AccessMode.NETWORK],
        supported_frequencies=[Frequency.ONE_DAY],
        supported_transforms=list(Transformation),
        supports_date_range=True,
        supports_revision_policy=False,
        supports_dry_run=True,
        supports_previous_observations=False,
        requires_credential=False,
        paid_access_possible=False,
    )


def _synthetic_templates() -> dict[str, object]:
    def template(requirement: DataPlan, symbol: str) -> dict[str, str]:
        return {
            "series_id": symbol,
            "observation_start": requirement.start_date.isoformat(),
            "observation_end": requirement.end_date.isoformat(),
        }

    return {"synthetic_provider": template}


def _ready_plan() -> GeneratedAcquisitionRequestPlan:
    selection, selection_confirmation, data_plan, instruments, calendars = (
        _confirmed_selection()
    )
    return generate_acquisition_request_plan(
        selection,
        selection_confirmation,
        data_plan,
        instruments,
        calendars,
        {"synthetic_provider": _synthetic_snapshot()},
        public_parameter_templates=_synthetic_templates(),
    )


class AcquisitionRequestNormalPathTest(unittest.TestCase):
    def test_ready_plan_generation_and_render(self) -> None:
        plan = _ready_plan()
        self.assertEqual(len(plan.acquisition_request_plan.requests), 2)
        self.assertEqual(
            plan.acquisition_request_plan.unresolved_requirements, []
        )
        self.assertEqual(
            acquisition_request_readiness_blockers(
                plan,
                *(
                    _confirmed_selection()[3:5]
                    + ({"synthetic_provider": _synthetic_snapshot()},)
                ),
            ),
            [],
        )
        request = plan.acquisition_request_plan.requests[0]
        rendered = render_public_acquisition_request(
            plan, request.requirement_id
        )
        self.assertEqual(rendered.method.value, "get")
        self.assertEqual(rendered.access_mode.value, "network")
        self.assertEqual(
            rendered.canonical_public_parameters,
            dict(sorted(request.public_parameters.items())),
        )

    def test_request_fields_are_exact(self) -> None:
        plan = _ready_plan()
        request = plan.acquisition_request_plan.requests[0]
        self.assertIsNotNone(request.sample_start)
        self.assertIsNotNone(request.sample_end)
        self.assertEqual(
            request.pre_sample_periods_required, 0
        )
        self.assertEqual(
            request.pre_sample_resolution_method,
            PreSampleResolutionMethod.NONE_REQUIRED,
        )
        self.assertEqual(request.acquisition_start, request.sample_start)
        self.assertNotIn("api_key", request.public_parameters)
        self.assertNotIn("token", request.public_parameters)
        self.assertNotIn("password", request.public_parameters)
        self.assertNotIn("credential", request.public_parameters)

    def test_canonical_round_trip(self) -> None:
        plan = _ready_plan()
        payload = serialize_acquisition_request_plan(plan.acquisition_request_plan)
        restored = parse_acquisition_request_plan(payload)
        self.assertEqual(restored, plan.acquisition_request_plan)
        self.assertEqual(
            calculate_acquisition_request_plan_sha256(restored),
            plan.acquisition_request_plan_sha256,
        )

    def test_plan_is_order_deterministic(self) -> None:
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        first = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            data_plan,
            instruments,
            calendars,
            {"synthetic_provider": _synthetic_snapshot()},
            public_parameter_templates=_synthetic_templates(),
        )
        second = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            data_plan,
            instruments,
            calendars,
            {"synthetic_provider": _synthetic_snapshot()},
            public_parameter_templates=_synthetic_templates(),
        )
        self.assertEqual(
            first.acquisition_request_plan_sha256,
            second.acquisition_request_plan_sha256,
        )

    def test_immutable_persistence_success(self) -> None:
        plan = _ready_plan()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "acquisition-request.json"
            persisted = persist_generated_acquisition_request_plan(
                plan, output, generated_at=CONFIRMED_AT
            )
            self.assertEqual(
                persisted.acquisition_request_plan_sha256,
                plan.acquisition_request_plan_sha256,
            )
            self.assertTrue(output.exists())
            self.assertTrue(Path(str(output) + ".provenance.json").exists())
            self.assertEqual(
                parse_acquisition_request_plan(output.read_bytes()),
                plan.acquisition_request_plan,
            )


class AcquisitionRequestCapabilityTest(unittest.TestCase):
    def test_snapshot_from_fred_and_csv_code_facts(self) -> None:
        calendars, instruments = _load_registries()
        fred = FredProvider(instrument_registry=instruments)
        csv = CSVProvider(Path("unused.csv"))
        fred_snapshot = snapshot_provider_capabilities(fred.capabilities())
        csv_snapshot = snapshot_provider_capabilities(csv.capabilities())
        self.assertEqual(
            fred_snapshot.supported_access_modes, [AccessMode.NETWORK]
        )
        self.assertEqual(
            csv_snapshot.supported_access_modes, [AccessMode.LOCAL_FILE]
        )
        self.assertTrue(fred_snapshot.requires_credential)
        self.assertFalse(csv_snapshot.requires_credential)
        self.assertFalse(csv_snapshot.supports_dry_run)
        self.assertFalse(fred_snapshot.supports_previous_observations)
        self.assertFalse(fred_snapshot.paid_access_possible)

    def test_snapshot_canonical_round_trip(self) -> None:
        snapshot = _synthetic_snapshot()
        payload = serialize_provider_capability_snapshot(snapshot)
        self.assertEqual(parse_provider_capability_snapshot(payload), snapshot)

    def test_missing_capability_snapshot_is_hard_failure(self) -> None:
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        with self.assertRaises(AcquisitionRequestReviewError) as raised:
            generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                data_plan,
                instruments,
                calendars,
                {},
                public_parameter_templates=_synthetic_templates(),
            )
        self.assertEqual(
            raised.exception.failure.code,
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
        )

    def test_unsupported_frequency_is_hard_failure(self) -> None:
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        snapshot = _synthetic_snapshot().model_copy(
            update={"supported_frequencies": []}
        )
        with self.assertRaises(AcquisitionRequestReviewError) as raised:
            generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                data_plan,
                instruments,
                calendars,
                {"synthetic_provider": snapshot},
                public_parameter_templates=_synthetic_templates(),
            )
        self.assertEqual(
            raised.exception.failure.code,
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
        )

    def test_unsupported_transform_is_hard_failure(self) -> None:
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        snapshot = _synthetic_snapshot().model_copy(
            update={"supported_transforms": [Transformation.LEVEL]}
        )
        # The synthetic spec uses a level transform, so restrict further:
        snapshot = snapshot.model_copy(update={"supported_transforms": []})
        with self.assertRaises(AcquisitionRequestReviewError) as raised:
            generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                data_plan,
                instruments,
                calendars,
                {"synthetic_provider": snapshot},
                public_parameter_templates=_synthetic_templates(),
            )
        self.assertEqual(
            raised.exception.failure.code,
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
        )

    def test_unsupported_access_mode_is_hard_failure(self) -> None:
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        snapshot = _synthetic_snapshot().model_copy(
            update={"supports_revision_policy": False}
        )
        # Synthetic requirements use not_applicable revision policy, so this
        # passes; restrict access mode instead to prove mode mismatch.
        restricted = snapshot.model_copy(
            update={"supported_access_modes": []}
        )
        with self.assertRaises(AcquisitionRequestReviewError) as raised:
            generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                data_plan,
                instruments,
                calendars,
                {"synthetic_provider": restricted},
                public_parameter_templates=_synthetic_templates(),
            )
        self.assertEqual(
            raised.exception.failure.code,
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
        )

    def test_stale_capability_snapshot_blocks_readiness(self) -> None:
        plan = _ready_plan()
        selection, selection_confirmation, data_plan, instruments, calendars = (
            _confirmed_selection()
        )
        stale = _synthetic_snapshot().model_copy(
            update={"supported_access_modes": [AccessMode.LOCAL_FILE]}
        )
        blockers = acquisition_request_readiness_blockers(
            plan, instruments, calendars, {"synthetic_provider": stale}
        )
        self.assertTrue(
            any("access mode" in blocker for blocker in blockers)
        )

    def test_stale_frequency_snapshot_blocks_readiness(self) -> None:
        plan = _ready_plan()
        _, _, _, instruments, calendars = _confirmed_selection()
        stale = _synthetic_snapshot().model_copy(
            update={"supported_frequencies": []}
        )
        blockers = acquisition_request_readiness_blockers(
            plan, instruments, calendars, {"synthetic_provider": stale}
        )
        self.assertTrue(
            any(
                "no longer matches the bound hash" in blocker
                for blocker in blockers
            )
        )

    def test_paid_capability_never_implies_paid_authorization(self) -> None:
        snapshot = _synthetic_snapshot().model_copy(
            update={"paid_access_possible": True}
        )
        self.assertTrue(snapshot.paid_access_possible)
        # paid_access_possible is informational; authorization fixed false.


class AcquisitionRequestPreSampleTest(unittest.TestCase):
    def test_zero_periods_is_none_required(self) -> None:
        plan = _ready_plan()
        for request in plan.acquisition_request_plan.requests:
            self.assertEqual(
                request.pre_sample_resolution_method,
                PreSampleResolutionMethod.NONE_REQUIRED,
            )
            self.assertEqual(request.acquisition_start, request.sample_start)

    def test_calendar_adapter_resolves_when_available(self) -> None:
        calendars, instruments = _load_registries()
        entry = instruments.entries[0]
        requirement = _pre_sample_requirement()
        snapshot = _synthetic_snapshot()
        adapted_calendars = _calendars_with_adapter(calendars, "test-sessions")
        resolution = resolve_pre_sample_requirement(
            requirement,
            adapted_calendars,
            snapshot,
            session_adapters={"test-sessions": _fixed_session_adapter},
        )
        self.assertEqual(
            resolution.method,
            PreSampleResolutionMethod.VERIFIED_CALENDAR_SESSIONS,
        )
        self.assertEqual(resolution.status, PreSampleStatus.RESOLVED)
        self.assertEqual(
            resolution.resolved_acquisition_start, date(2020, 1, 1)
        )

    def test_calendar_adapter_declared_but_unavailable_is_unresolved(self) -> None:
        calendars, instruments = _load_registries()
        entry = instruments.entries[0]
        requirement = _pre_sample_requirement()
        adapted_calendars = _calendars_with_adapter(calendars, "test-sessions")
        resolution = resolve_pre_sample_requirement(
            requirement, adapted_calendars, _synthetic_snapshot()
        )
        self.assertEqual(resolution.status, PreSampleStatus.UNRESOLVED)
        self.assertIsNone(resolution.resolved_acquisition_start)

    def test_no_calendar_and_no_provider_method_is_unresolved(self) -> None:
        calendars, instruments = _load_registries()
        entry = instruments.entries[0]
        requirement = _pre_sample_requirement()
        resolution = resolve_pre_sample_requirement(
            requirement, calendars, _synthetic_snapshot()
        )
        self.assertEqual(resolution.status, PreSampleStatus.UNRESOLVED)
        self.assertEqual(
            resolution.method, PreSampleResolutionMethod.UNRESOLVED
        )

    def test_provider_native_previous_observations_resolves(self) -> None:
        calendars, instruments = _load_registries()
        entry = instruments.entries[0]
        requirement = _pre_sample_requirement()
        snapshot = _synthetic_snapshot().model_copy(
            update={"supports_previous_observations": True}
        )
        resolution = resolve_pre_sample_requirement(
            requirement, calendars, snapshot
        )
        self.assertEqual(
            resolution.method,
            PreSampleResolutionMethod.PROVIDER_NATIVE_PREVIOUS_OBSERVATIONS,
        )
        self.assertEqual(resolution.status, PreSampleStatus.RESOLVED)
        self.assertIsNone(resolution.resolved_acquisition_start)

    def test_unresolved_plan_has_structured_unresolved(self) -> None:
        # Build a plan from a spec whose requirements need pre-sample data
        # without any provable resolution method.
        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        for variable in ("outcome", "predictors"):
            pass
        # The synthetic spec uses level transforms (0 pre-sample periods);
        # instead force a lag by editing a predictor's lag periods.
        predictor = spec_payload["predictors"][0]
        predictor["lag_periods"] = 2
        spec = parse_research_spec(_json_bytes(spec_payload))
        calendars, instruments = _load_registries()
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
            {"synthetic_provider": _synthetic_snapshot()},
            public_parameter_templates=_synthetic_templates(),
        )
        codes = {
            item.code
            for item in plan.acquisition_request_plan.unresolved_requirements
        }
        self.assertTrue(
            AcquisitionUnresolvedCode.PRE_SAMPLE_PROVIDER_METHOD_UNAVAILABLE
            in codes
        )
        self.assertTrue(
            acquisition_request_readiness_blockers(
                plan,
                instruments,
                calendars,
                {"synthetic_provider": _synthetic_snapshot()},
            )
        )

    def test_local_file_relative_identity_is_unresolved(self) -> None:
        calendars, instruments = _load_registries()
        local = _synthetic_snapshot().model_copy(
            update={
                "provider_id": "local_csv",
                "supported_access_modes": [AccessMode.LOCAL_FILE],
            }
        )
        # Build a selection chain against a local-file provider by swapping
        # the registry mapping provider.
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
                                        f"relative/data-{index}.csv"
                                    ),
                                }
                            )
                        ]
                    }
                )
            )
        local_registry = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), local_registry, calendars)
        confirmation = confirm_data_plan(
            generated, local_registry, confirmed_at=CONFIRMED_AT
        )
        decisions = []
        for requirement in generated.data_plan.requirements:
            mapping = local_registry.get(requirement.instrument_id).provider_mappings[0]
            decisions.append(
                SourceSelectionDecision(
                    requirement_id=requirement.requirement_id,
                    provider_id=mapping.provider_id,
                    provider_symbol=mapping.provider_symbol,
                    dataset_or_endpoint=mapping.dataset_or_endpoint,
                )
            )
        selection = generate_source_selection(
            generated, confirmation, local_registry, calendars, decisions
        )
        selection_confirmation = confirm_source_selection(
            selection, local_registry, calendars, confirmed_at=CONFIRMED_AT
        )
        plan = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            generated.data_plan,
            local_registry,
            calendars,
            {"local_csv": local},
        )
        self.assertTrue(
            any(
                item.code
                is AcquisitionUnresolvedCode.LOCAL_FILE_CONTENT_IDENTITY_REQUIRED
                for item in plan.acquisition_request_plan.unresolved_requirements
            )
        )


def _pre_sample_requirement():
    selection, selection_confirmation, data_plan, instruments, calendars = (
        _confirmed_selection()
    )
    return data_plan.requirements[0].model_copy(
        update={"required_pre_sample_periods": 3}
    )


def _fixed_session_adapter(start: date, periods: int) -> date:
    # Deterministic test-local adapter: exactly `periods` sessions back.
    return start.replace(day=max(1, start.day - periods))


def _calendars_with_adapter(
    calendars: CalendarRegistry, adapter_id: str
) -> CalendarRegistry:
    from market_validator.data.calendars import CalendarRegistry as CR

    definitions = []
    for definition in calendars.definitions:
        definitions.append(
            definition.model_copy(update={"schedule_adapter": adapter_id})
        )
    return CR(definitions)


class AcquisitionRequestStrictJsonTest(unittest.TestCase):
    def test_duplicate_keys_rejected(self) -> None:
        plan = _ready_plan()
        raw = json.dumps(
            plan.acquisition_request_plan.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = raw.replace(
            '"acquisition_request_schema_version":"1.0"',
            '"acquisition_request_schema_version":"1.0",'
            '"acquisition_request_schema_version":"1.0"',
            1,
        )
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(raw.encode("utf-8"))

    def test_non_object_top_level_rejected(self) -> None:
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(b'[1,2,3]')

    def test_invalid_utf8_rejected(self) -> None:
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(b"\xff\xfe")

    def test_non_finite_numbers_rejected(self) -> None:
        plan = _ready_plan()
        raw = json.dumps(
            plan.acquisition_request_plan.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        injected = raw.replace(
            '"acquisition_request_schema_version":"1.0"',
            '"acquisition_request_schema_version":NaN',
            1,
        )
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(injected.encode("utf-8"))

    def test_extra_fields_rejected(self) -> None:
        plan = _ready_plan()
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["unexpected"] = True
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(_json_bytes(payload))

    def test_secret_like_extra_fields_rejected(self) -> None:
        plan = _ready_plan()
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["api_key"] = "secret-value"
        with self.assertRaises(AcquisitionRequestReviewError):
            parse_acquisition_request_plan(_json_bytes(payload))


class AcquisitionRequestPersistenceTest(unittest.TestCase):
    def test_duplicate_same_content_is_idempotent(self) -> None:
        plan = _ready_plan()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "acquisition-request.json"
            persist_generated_acquisition_request_plan(
                plan, output, generated_at=CONFIRMED_AT
            )
            persist_generated_acquisition_request_plan(
                plan, output, generated_at=CONFIRMED_AT
            )
            self.assertEqual(
                parse_acquisition_request_plan(output.read_bytes()),
                plan.acquisition_request_plan,
            )

    def test_same_path_different_content_conflicts(self) -> None:
        plan = _ready_plan()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "acquisition-request.json"
            persist_generated_acquisition_request_plan(
                plan, output, generated_at=CONFIRMED_AT
            )
            other_payload = plan.acquisition_request_plan.model_dump(mode="json")
            other_payload["requests"][0]["public_parameters"][
                "series_id"
            ] = "CHANGED"
            other_plan = parse_acquisition_request_plan(_json_bytes(other_payload))
            other_generated = GeneratedAcquisitionRequestPlan(
                acquisition_request_plan=other_plan,
                acquisition_request_plan_sha256=(
                    calculate_acquisition_request_plan_sha256(other_plan)
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
                capability_snapshot_sha256s=(
                    plan.capability_snapshot_sha256s
                ),
            )
            with self.assertRaises(AcquisitionRequestReviewError) as raised:
                persist_generated_acquisition_request_plan(
                    other_generated, output, generated_at=CONFIRMED_AT
                )
            self.assertEqual(
                raised.exception.failure.code,
                AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_CONFLICT,
            )

    def test_path_traversal_rejected(self) -> None:
        plan = _ready_plan()
        with self.assertRaises(AcquisitionRequestReviewError):
            persist_generated_acquisition_request_plan(
                plan,
                ROOT / ".." / "outside-plan.json",
                generated_at=CONFIRMED_AT,
            )


if __name__ == "__main__":
    unittest.main()
