"""Contract tests for deterministic data readiness (Phase 4, A-M)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_validator.data.acquisition_request import (
    generate_acquisition_request_plan,
)
from market_validator.data.access_authorization import (
    create_data_access_authorization,
)
from market_validator.data.calendars import CalendarDefinition, CalendarRegistry
from market_validator.data.data_plan_review import (
    confirm_data_plan,
    generate_data_plan,
)
from market_validator.data.execution import (
    FredExecutionAdapter,
    execute_authorized_acquisition,
)
from market_validator.data.providers.fred_provider import (
    FRED_OBSERVATIONS_PATH,
    FRED_SERIES_PATH,
    FredProvider,
)
from market_validator.data.readiness import (
    DataReadinessAssessment,
    ReadinessError,
    ReadinessErrorCode,
    ReadinessStatus,
    assess_data_readiness,
    calculate_data_ready_manifest_sha256,
    create_data_ready_manifest,
    data_readiness_blockers,
    parse_data_ready_manifest,
    parse_data_readiness_assessment,
    persist_data_ready_manifest,
    persist_data_readiness_assessment,
    serialize_data_ready_manifest,
    serialize_data_readiness_assessment,
    validate_data_ready_manifest_matches,
    verify_persisted_data_ready_manifest,
)
from market_validator.data.registry import InstrumentRegistry
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
from tests.test_provider_execution import _fred_spec

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_CALENDARS = (
    ROOT / "examples" / "synthetic" / "synthetic_market.calendars.json"
)
SYNTHETIC_INSTRUMENTS = (
    ROOT / "examples" / "synthetic" / "synthetic_market.instruments.json"
)
CONFIRMED_AT = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _weekdays_between(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current = current.fromordinal(current.toordinal() + 1)
    return result


class _FixedDailyAdapter:
    """TEST-ONLY synthetic verified schedule adapter (weekdays only)."""

    adapter_id = "test.fixed.daily"

    def sessions_between(self, start_date: date, end_date: date) -> list[date]:
        return _weekdays_between(start_date, end_date)

    def previous_sessions(self, before_date: date, count: int) -> list[date]:
        result: list[date] = []
        current = before_date.fromordinal(before_date.toordinal() - 1)
        while len(result) < count:
            if current.weekday() < 5:
                result.append(current)
            current = current.fromordinal(current.toordinal() - 1)
        return list(reversed(result))


class _HolidayAdapter(_FixedDailyAdapter):
    """Synthetic adapter where 2020-01-02 is not a session."""

    adapter_id = "test.holiday.daily"

    def sessions_between(self, start_date: date, end_date: date) -> list[date]:
        return [
            session
            for session in super().sessions_between(start_date, end_date)
            if session != date(2020, 1, 2)
        ]


class _SeriesAwareTransport(FakeTransport):
    def get_json(self, path: str, public_parameters):
        parameters = dict(public_parameters)
        self.calls.append((path, parameters))
        if path == FRED_SERIES_PATH:
            series_id = parameters.get("series_id")
            return response(series_payload(series_id=series_id))
        return response(self.pages[int(parameters["offset"])])


def _calendar_registry_with_adapter() -> CalendarRegistry:
    calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
    definition = calendars.get("synthetic.test.equity")
    updated = definition.model_copy(
        update={"schedule_adapter": _FixedDailyAdapter.adapter_id}
    )
    return CalendarRegistry([CalendarDefinition.model_validate(updated)])


def _fred_registry() -> InstrumentRegistry:
    calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
    instruments = InstrumentRegistry.from_json_file(
        SYNTHETIC_INSTRUMENTS, calendars
    )
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
                    ],
                }
            )
        )
    return InstrumentRegistry(entries, calendars)


def _short_window_spec(pre_sample_periods: int = 0):
    payload = json.loads(
        Path(
            ROOT / "examples" / "synthetic" / "synthetic_market.spec.json"
        ).read_text(encoding="utf-8")
    )

    def fix(variable, periods):
        variable = dict(variable)
        variable["instrument"] = dict(variable["instrument"])
        variable["instrument"]["asset_type"] = "macro_series"
        variable["instrument"]["unit"] = "Dollars per Barrel"
        variable["field"] = "value"
        variable["revision_policy"] = {"mode": "latest_available"}
        variable["lag_periods"] = periods
        variable["availability_lag_periods"] = periods
        return variable

    payload["outcome"] = fix(payload["outcome"], pre_sample_periods)
    payload["predictors"] = [
        fix(variable, pre_sample_periods)
        for variable in payload["predictors"]
    ]
    payload["sample"]["start_date"] = "2020-01-01"
    payload["sample"]["end_date"] = "2020-01-10"
    return parse_research_spec(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    )


def _build_chain(
    tmp: Path,
    transport: _SeriesAwareTransport,
    *,
    pre_sample_periods: int = 0,
) -> dict[str, object]:
    calendars = _calendar_registry_with_adapter()
    instruments = _fred_registry()
    spec = _short_window_spec(pre_sample_periods)
    generated = generate_data_plan(spec, instruments, calendars)
    confirmation = confirm_data_plan(
        generated, instruments, confirmed_at=CONFIRMED_AT
    )
    decisions = []
    for requirement in generated.data_plan.requirements:
        mapping = instruments.get(
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
        generated, confirmation, instruments, calendars, decisions
    )
    selection_confirmation = confirm_source_selection(
        selection, instruments, calendars, confirmed_at=CONFIRMED_AT
    )
    from market_validator.data.acquisition_request import (
        snapshot_provider_capabilities,
    )

    fred_probe = FredProvider(instrument_registry=instruments)
    capabilities = {
        "fred": snapshot_provider_capabilities(fred_probe.capabilities())
    }
    plan = generate_acquisition_request_plan(
        selection,
        selection_confirmation,
        generated.data_plan,
        instruments,
        calendars,
        capabilities,
        session_adapters={
            _FixedDailyAdapter.adapter_id: lambda start, periods: (
                _FixedDailyAdapter().previous_sessions(start, periods)[0]
            )
        },
    )
    request_ids = [
        request.requirement_id
        for request in plan.acquisition_request_plan.requests
    ]
    authorization = create_data_access_authorization(
        plan,
        instruments,
        calendars,
        capabilities,
        request_ids,
        authorized_at=CONFIRMED_AT,
    )
    fred = FredProvider(
        instrument_registry=instruments,
        environment={"FRED_API_KEY": "a" * 32},
        transport=transport,
        clock=lambda: CONFIRMED_AT,
    )
    return {
        "plan": plan,
        "authorization": authorization,
        "data_plan": generated.data_plan,
        "instruments": instruments,
        "calendars": calendars,
        "capabilities": capabilities,
        "adapters": {"fred": FredExecutionAdapter(fred)},
    }


def _ready_snapshot(
    tmp: Path,
    *,
    pre_sample_periods: int = 0,
    observation_dates: list[str] | None = None,
    count: int | None = None,
) -> tuple[object, dict[str, object], _SeriesAwareTransport]:
    (tmp / "snapshots").mkdir(exist_ok=True)
    transport = _SeriesAwareTransport()
    dates = observation_dates or [
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
        "2020-01-06",
        "2020-01-07",
        "2020-01-08",
        "2020-01-09",
        "2020-01-10",
    ]
    transport.pages[0] = observations_payload(
        [
            {
                "date": value,
                "value": "1.0",
                "realtime_start": "2020-01-02",
            }
            for value in dates
        ],
        count=count or len(dates),
    )
    chain = _build_chain(tmp, transport, pre_sample_periods=pre_sample_periods)
    verified = execute_authorized_acquisition(
        generated_plan=chain["plan"],
        authorization=chain["authorization"],
        data_plan=chain["data_plan"],
        instrument_registry=chain["instruments"],
        calendar_registry=chain["calendars"],
        capability_snapshots=chain["capabilities"],
        attempt_id="attempt-ready",
        receipt_path=tmp / "receipt.json",
        snapshot_root=tmp / "snapshots",
        adapters=chain["adapters"],
    )
    return verified, chain, transport


def _assess(
    tmp: Path,
    verified,
    chain,
) -> DataReadinessAssessment:
    return assess_data_readiness(
        generated_plan=chain["plan"],
        data_plan=chain["data_plan"],
        instrument_registry=chain["instruments"],
        calendar_registry=chain["calendars"],
        snapshot_path=verified.snapshot_path,
        session_adapters={_FixedDailyAdapter.adapter_id: _FixedDailyAdapter()},
    )


class AcquisitionWindowRegressionTest(unittest.TestCase):
    def test_schema_is_1_2(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            self.assertEqual(
                chain["plan"].acquisition_request_plan
                .acquisition_request_schema_version,
                "1.2",
            )

    def test_old_1_1_artifact_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            payload = chain["plan"].acquisition_request_plan.model_dump(
                mode="json"
            )
            payload["acquisition_request_schema_version"] = "1.1"
            from market_validator.data.acquisition_request import (
                parse_acquisition_request_plan,
            )

            with self.assertRaises(Exception):
                parse_acquisition_request_plan(
                    json.dumps(
                        payload, sort_keys=True
                    ).encode("utf-8")
                )

    def test_fred_observation_start_uses_acquisition_start(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, transport = _ready_snapshot(tmp)
            request = chain["plan"].acquisition_request_plan.requests[0]
            observations_step = request.steps[1]
            self.assertEqual(
                observations_step.public_parameters["observation_start"],
                request.acquisition_start.isoformat(),
            )

    def test_bundle_keeps_original_requirement(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            from market_validator.data.models import DataBundle

            record = verified.manifest.request_records[0]
            artifact = next(
                a for a in record.artifacts if a.role == "bundle"
            )
            content = (
                verified.snapshot_path
                / "requests"
                / record.requirement_id
                / artifact.relative_path
            ).read_bytes()
            bundle = DataBundle.model_validate_json(content)
            requirement = chain["data_plan"].requirements[0]
            self.assertEqual(bundle.requirement, requirement)

    def test_plan_window_change_invalidates_authorization(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            payload = chain["plan"].acquisition_request_plan.model_dump(
                mode="json"
            )
            payload["requests"][0]["steps"][1]["public_parameters"][
                "observation_start"
            ] = "2019-01-01"
            from market_validator.data.acquisition_request import (
                GeneratedAcquisitionRequestPlan,
                calculate_acquisition_request_plan_sha256,
                parse_acquisition_request_plan,
            )
            from market_validator.data.access_authorization import (
                DataAccessAuthorizationError,
                validate_data_access_authorization_matches,
            )

            changed = parse_acquisition_request_plan(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            )
            changed_generated = GeneratedAcquisitionRequestPlan(
                acquisition_request_plan=changed,
                acquisition_request_plan_sha256=(
                    calculate_acquisition_request_plan_sha256(changed)
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
            with self.assertRaises(DataAccessAuthorizationError):
                validate_data_access_authorization_matches(
                    changed_generated, chain["authorization"]
                )


class HappyPathTest(unittest.TestCase):
    def test_ready_assessment_and_manifest(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            self.assertEqual(assessment.status, ReadinessStatus.READY)
            self.assertEqual(assessment.blockers, [])
            self.assertEqual(len(assessment.requirements), 2)
            generated = create_data_ready_manifest(
                assessment=assessment,
                generated_plan=chain["plan"],
            )
            validate_data_ready_manifest_matches(
                generated,
                assessment,
                chain["plan"],
                chain["data_plan"],
                chain["instruments"],
                chain["calendars"],
            )
            self.assertEqual(
                generated.data_ready_manifest.data_ready_schema_version,
                "1.0",
            )
            persisted, provenance = persist_data_ready_manifest(
                generated,
                tmp / "data_ready.json",
                snapshot_path=verified.snapshot_path,
            )
            verify_persisted_data_ready_manifest(
                persisted, generated
            )
            self.assertTrue(provenance.exists())

    def test_pre_sample_periods_complete(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            dates = [
                "2019-12-30",
                "2019-12-31",
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-06",
                "2020-01-07",
                "2020-01-08",
                "2020-01-09",
                "2020-01-10",
            ]
            verified, chain, _ = _ready_snapshot(
                tmp,
                pre_sample_periods=2,
                observation_dates=dates,
            )
            assessment = _assess(tmp, verified, chain)
            self.assertEqual(assessment.status, ReadinessStatus.READY)
            item = assessment.requirements[0]
            self.assertEqual(item.pre_sample_observed_sessions, 2)

    def test_quality_warn_preserved(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            generated = create_data_ready_manifest(
                assessment=assessment,
                generated_plan=chain["plan"],
            )
            self.assertTrue(
                any(
                    "latest_revision_hindsight_risk" in item.warnings
                    for item in assessment.requirements
                )
            )
            self.assertTrue(
                any(
                    "latest_revision_hindsight_risk" in record.warnings
                    for record in generated.data_ready_manifest.bundles
                )
            )


class AggregateCompletenessTest(unittest.TestCase):
    def test_missing_bundle_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            # Tamper: remove one bundle artifact entirely.
            record = verified.manifest.request_records[0]
            bundle = next(
                a for a in record.artifacts if a.role == "bundle"
            )
            (
                verified.snapshot_path
                / "requests"
                / record.requirement_id
                / bundle.relative_path
            ).unlink()
            with self.assertRaises(ReadinessError) as raised:
                _assess(tmp, verified, chain)
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.SNAPSHOT_VERIFICATION_FAILED,
            )

    def test_blocked_requirement_blocks_aggregate_and_manifest(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            from market_validator.data.readiness import (
                ReadinessBlocker,
                ReadinessStage,
            )

            assessment = assessment.model_copy(
                update={
                    "status": ReadinessStatus.BLOCKED,
                    "blockers": [
                        ReadinessBlocker(
                            code="quality_failed",
                            stage=ReadinessStage.QUALITY_VALIDATION,
                            message="injected quality failure",
                        )
                    ],
                }
            )
            self.assertEqual(assessment.status, ReadinessStatus.BLOCKED)
            with self.assertRaises(ReadinessError) as raised:
                create_data_ready_manifest(
                    assessment=assessment,
                    generated_plan=chain["plan"],
                )
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.DATA_READINESS_NOT_READY,
            )

    def test_blocked_assessment_cannot_create_manifest(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            from market_validator.data.readiness import (
                ReadinessBlocker,
                ReadinessStage,
            )

            blocked = assessment.model_copy(
                update={
                    "status": ReadinessStatus.BLOCKED,
                    "blockers": [
                        ReadinessBlocker(
                            code="quality_failed",
                            stage=ReadinessStage.QUALITY_VALIDATION,
                            message="injected",
                        )
                    ],
                }
            )
            with self.assertRaises(ReadinessError) as raised:
                create_data_ready_manifest(
                    assessment=blocked,
                    generated_plan=chain["plan"],
                )
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.DATA_READINESS_NOT_READY,
            )


class QualityBlockTest(unittest.TestCase):
    def test_duplicate_observation_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            dates = [
                "2020-01-01",
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-06",
                "2020-01-07",
                "2020-01-08",
            ]
            verified, chain, _ = _ready_snapshot(
                tmp, observation_dates=dates
            )
            assessment = _assess(tmp, verified, chain)
            self.assertEqual(assessment.status, ReadinessStatus.BLOCKED)
            self.assertIn(
                ReadinessErrorCode.QUALITY_FAILED.value,
                data_readiness_blockers(assessment),
            )


class CoverageTest(unittest.TestCase):
    def test_missing_middle_session_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            dates = [
                "2020-01-01",
                "2020-01-02",
                "2020-01-06",
                "2020-01-07",
                "2020-01-08",
            ]  # missing 2020-01-03
            verified, chain, _ = _ready_snapshot(
                tmp, observation_dates=dates
            )
            assessment = _assess(tmp, verified, chain)
            self.assertEqual(assessment.status, ReadinessStatus.BLOCKED)
            self.assertIn(
                ReadinessErrorCode.SAMPLE_COVERAGE_INCOMPLETE.value,
                data_readiness_blockers(assessment),
            )

    def test_extra_sample_session_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = assess_data_readiness(
                generated_plan=chain["plan"],
                data_plan=chain["data_plan"],
                instrument_registry=chain["instruments"],
                calendar_registry=chain["calendars"],
                snapshot_path=verified.snapshot_path,
                session_adapters={
                    _FixedDailyAdapter.adapter_id: _HolidayAdapter()
                },
            )
            self.assertIn(
                ReadinessErrorCode.SAMPLE_COVERAGE_EXTRA_OBSERVATIONS.value,
                data_readiness_blockers(assessment),
            )

    def test_no_adapter_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = assess_data_readiness(
                generated_plan=chain["plan"],
                data_plan=chain["data_plan"],
                instrument_registry=chain["instruments"],
                calendar_registry=chain["calendars"],
                snapshot_path=verified.snapshot_path,
                session_adapters={},
            )
            self.assertEqual(assessment.status, ReadinessStatus.BLOCKED)
            self.assertIn(
                ReadinessErrorCode.SAMPLE_COVERAGE_UNVERIFIABLE.value,
                data_readiness_blockers(assessment),
            )


class PreSampleTest(unittest.TestCase):
    def test_missing_pre_sample_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            dates = [
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-06",
                "2020-01-07",
                "2020-01-08",
                "2020-01-09",
                "2020-01-10",
            ]  # no pre-sample dates, but 2 required
            verified, chain, _ = _ready_snapshot(
                tmp,
                pre_sample_periods=2,
                observation_dates=dates,
            )
            assessment = _assess(tmp, verified, chain)
            self.assertIn(
                ReadinessErrorCode.PRE_SAMPLE_MISSING_OBSERVATIONS.value,
                data_readiness_blockers(assessment),
            )

    def test_pre_sample_duplicate_blocks(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            dates = [
                "2019-12-30",
                "2019-12-31",
                "2020-01-01",
                "2020-01-02",
                "2020-01-03",
                "2020-01-06",
                "2020-01-07",
                "2020-01-08",
                "2020-01-09",
                "2020-01-10",
            ]
            verified, chain, _ = _ready_snapshot(
                tmp,
                pre_sample_periods=2,
                observation_dates=dates,
            )
            # Inject a duplicate pre-sample observation into the bundle file.
            from market_validator.data.models import DataBundle

            record = verified.manifest.request_records[0]
            artifact = next(
                a for a in record.artifacts if a.role == "bundle"
            )
            bundle_path = (
                verified.snapshot_path
                / "requests"
                / record.requirement_id
                / artifact.relative_path
            )
            bundle = DataBundle.model_validate_json(bundle_path.read_bytes())
            first = bundle.observations[0]
            duplicated = bundle.model_copy(
                update={"observations": [first, *bundle.observations]}
            )
            bundle_path.write_bytes(
                json.dumps(
                    duplicated.model_dump(
                        mode="json", exclude_computed_fields=True
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            with self.assertRaises(ReadinessError):
                _assess(tmp, verified, chain)


class AvailabilityRevisionTest(unittest.TestCase):
    def test_latest_available_matches(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            for item in assessment.requirements:
                self.assertEqual(item.revision_status, "verified")
                self.assertEqual(item.availability_status, "verified")


class ContentIdentityTest(unittest.TestCase):
    def test_fred_content_identity_matches(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            self.assertEqual(assessment.status, ReadinessStatus.READY)

    def test_observation_page_change_fails(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            record = verified.manifest.request_records[0]
            page = next(
                a
                for a in record.artifacts
                if a.relative_path.startswith("raw/observations-page-")
            )
            target = (
                verified.snapshot_path
                / "requests"
                / record.requirement_id
                / page.relative_path
            )
            target.write_bytes(b"tampered")
            with self.assertRaises(ReadinessError) as raised:
                _assess(tmp, verified, chain)
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.SNAPSHOT_VERIFICATION_FAILED,
            )


class DeterminismTest(unittest.TestCase):
    def test_canonical_bytes_and_ids_stable(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            payload = serialize_data_readiness_assessment(assessment)
            restored = parse_data_readiness_assessment(payload)
            self.assertEqual(restored, assessment)
            generated = create_data_ready_manifest(
                assessment=assessment,
                generated_plan=chain["plan"],
            )
            manifest_payload = serialize_data_ready_manifest(
                generated.data_ready_manifest
            )
            self.assertEqual(
                parse_data_ready_manifest(manifest_payload),
                generated.data_ready_manifest,
            )
            self.assertEqual(
                calculate_data_ready_manifest_sha256(
                    generated.data_ready_manifest
                ),
                generated.data_ready_manifest_sha256,
            )


class PersistenceTest(unittest.TestCase):
    def test_assessment_persist_idempotent_and_conflict(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            first = persist_data_readiness_assessment(
                assessment, tmp / "assessment.json"
            )
            second = persist_data_readiness_assessment(
                assessment, tmp / "assessment.json"
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            changed = assessment.model_copy(
                update={"snapshot_id": "tampered"}
            )
            with self.assertRaises(ReadinessError) as raised:
                persist_data_readiness_assessment(
                    changed, tmp / "assessment.json"
                )
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.DATA_READY_OUTPUT_CONFLICT,
            )


class NoSideEffectsTest(unittest.TestCase):
    def test_readiness_never_calls_providers_or_analysis(self) -> None:
        from unittest import mock

        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            guards = [
                "market_validator.data.providers.fred_provider.FredProvider.fetch",
                "market_validator.data.providers.fred_provider.FredProvider.dry_run",
                "market_validator.data.providers.csv_provider.CSVProvider.fetch",
                "market_validator.data.providers.csv_provider.CSVProvider.inspect",
                "market_validator.data.providers.fred_transport.FredHttpsTransport.get_json",
                "market_validator.credentials.resolver.CredentialResolver.resolve",
                "market_validator.analysis.price_change_volatility.compare_price_change_volatility",
            ]
            patchers = [
                mock.patch(target, side_effect=AssertionError("must not be called"))
                for target in guards
            ]
            for patcher in patchers:
                patcher.start()
            try:
                assessment = _assess(tmp, verified, chain)
                self.assertEqual(assessment.status, ReadinessStatus.READY)
            finally:
                for patcher in patchers:
                    patcher.stop()


class ReviewRegressionTest(unittest.TestCase):
    def test_outcome_authorization_hash_tamper_rejected(self) -> None:
        from market_validator.data.snapshot import SnapshotError

        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            outcome_path = verified.snapshot_path / "outcome.json"
            from market_validator.data.snapshot import (
                parse_execution_outcome,
            )

            outcome = parse_execution_outcome(outcome_path.read_bytes())
            tampered = outcome.model_copy(
                update={"authorization_sha256": "0" * 64}
            )
            from market_validator.data.snapshot import (
                serialize_execution_outcome,
            )

            outcome_path.write_bytes(serialize_execution_outcome(tampered))
            with self.assertRaises(ReadinessError) as raised:
                _assess(tmp, verified, chain)
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.SNAPSHOT_VERIFICATION_FAILED,
            )

    def test_inconsistent_ready_assessment_rejected(self) -> None:
        from market_validator.data.readiness import (
            ReadinessBlocker,
            ReadinessStage,
        )

        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            item = assessment.requirements[0]
            inconsistent_item = item.model_copy(
                update={
                    "status": "ready",
                    "blockers": [
                        ReadinessBlocker(
                            code="quality_failed",
                            stage=ReadinessStage.QUALITY_VALIDATION,
                            message="injected",
                        )
                    ],
                }
            )
            inconsistent = assessment.model_copy(
                update={
                    "requirements": [
                        inconsistent_item
                        if r.requirement_id == inconsistent_item.requirement_id
                        else r
                        for r in assessment.requirements
                    ]
                }
            )
            with self.assertRaises(ReadinessError) as raised:
                create_data_ready_manifest(
                    assessment=inconsistent,
                    generated_plan=chain["plan"],
                )
            self.assertEqual(
                raised.exception.code,
                ReadinessErrorCode.DATA_READINESS_NOT_READY,
            )

    def test_quality_issue_codes_preserved_in_manifest(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain, _ = _ready_snapshot(tmp)
            assessment = _assess(tmp, verified, chain)
            generated = create_data_ready_manifest(
                assessment=assessment,
                generated_plan=chain["plan"],
            )
            record = generated.data_ready_manifest.bundles[0]
            self.assertIn(
                "latest_revision_hindsight_risk",
                record.quality_issue_codes,
            )


if __name__ == "__main__":
    unittest.main()
