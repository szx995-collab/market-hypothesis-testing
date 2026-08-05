"""Offline contract tests for data-access authorization and consumption."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from market_validator.data.access_authorization import (
    DataAccessAuthorization,
    DataAccessAuthorizationError,
    DataAccessAuthorizationErrorCode,
    DataAccessAuthorizationReceipt,
    calculate_data_access_authorization_sha256,
    consume_data_access_authorization,
    create_data_access_authorization,
    parse_data_access_authorization,
    parse_data_access_authorization_receipt,
    persist_data_access_authorization,
    persist_data_access_authorization_receipt,
    serialize_data_access_authorization,
    validate_data_access_authorization_matches,
)
from market_validator.data.acquisition_request import (
    AccessMode,
    ProviderCapabilitySnapshot,
    generate_acquisition_request_plan,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    confirm_data_plan,
    generate_data_plan,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.source_selection import (
    SourceSelectionDecision,
    confirm_source_selection,
    generate_source_selection,
)
from market_validator.research.enums import Frequency, Transformation
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
AUTHORIZATION_STATEMENT = (
    "I explicitly authorize one execution attempt of these exact "
    "acquisition requests under the stated access limits."
)


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
    from market_validator.data.acquisition_request import (
        PaginationPolicy,
        PublicRequestStep,
        RequestMethod,
    )

    def template(requirement, symbol: str):
        return [
            PublicRequestStep(
                step_id="synthetic-observations",
                sequence=1,
                method=RequestMethod.GET,
                endpoint="/synthetic/series",
                public_parameters={
                    "series_id": symbol,
                    "observation_start": requirement.start_date.isoformat(),
                    "observation_end": requirement.end_date.isoformat(),
                },
                pagination_policy=PaginationPolicy.NONE,
            )
        ]

    return {"synthetic_provider": template}


def _ready_plan():
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
    plan = generate_acquisition_request_plan(
        selection,
        selection_confirmation,
        generated.data_plan,
        instruments,
        calendars,
        {"synthetic_provider": _synthetic_snapshot()},
        public_parameter_templates=_synthetic_templates(),
    )
    return plan, instruments, calendars


def _ready_authorization() -> tuple[DataAccessAuthorization, object, object, object]:
    plan, instruments, calendars = _ready_plan()
    request_ids = [
        request.requirement_id for request in plan.acquisition_request_plan.requests
    ]
    authorization = create_data_access_authorization(
        plan,
        instruments,
        calendars,
        {"synthetic_provider": _synthetic_snapshot()},
        request_ids,
        authorized_at=CONFIRMED_AT,
    )
    return authorization, plan, instruments, calendars


class DataAccessAuthorizationNormalPathTest(unittest.TestCase):
    def test_create_validate_consume(self) -> None:
        authorization, plan, instruments, calendars = _ready_authorization()
        self.assertTrue(authorization.network_access_authorized)
        self.assertFalse(authorization.local_file_read_authorized)
        self.assertFalse(authorization.paid_access_authorized)
        self.assertFalse(authorization.automatic_retry_authorized)
        self.assertFalse(authorization.fallback_authorized)
        self.assertTrue(authorization.single_use)
        self.assertEqual(authorization.authorization_statement, AUTHORIZATION_STATEMENT)
        validate_data_access_authorization_matches(plan, authorization)
        receipt = consume_data_access_authorization(
            authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
        )
        self.assertEqual(receipt.status.value, "consumed")
        self.assertEqual(receipt.attempt_id, "attempt-1")
        self.assertEqual(
            receipt.authorization_sha256,
            calculate_data_access_authorization_sha256(authorization),
        )

    def test_second_consumption_is_rejected(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        receipt = consume_data_access_authorization(
            authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
        )
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            consume_data_access_authorization(
                authorization,
                attempt_id="attempt-2",
                consumed_at=CONFIRMED_AT,
                existing_receipt=receipt,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
        )

    def test_canonical_round_trip(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        payload = serialize_data_access_authorization(authorization)
        self.assertEqual(parse_data_access_authorization(payload), authorization)
        self.assertEqual(
            calculate_data_access_authorization_sha256(
                parse_data_access_authorization(payload)
            ),
            calculate_data_access_authorization_sha256(authorization),
        )

    def test_no_side_effects(self) -> None:
        with mock.patch(
            "market_validator.data.providers.fred_provider.FredProvider.fetch",
            side_effect=AssertionError("fetch must not be called"),
        ), mock.patch(
            "market_validator.credentials.resolver.CredentialResolver.resolve",
            side_effect=AssertionError("resolve must not be called"),
        ):
            authorization, plan, instruments, calendars = _ready_authorization()
            validate_data_access_authorization_matches(plan, authorization)
            consume_data_access_authorization(
                authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
            )


class DataAccessAuthorizationAccessModeTest(unittest.TestCase):
    def _local_file_plan(self):
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
                                        f"C:/data/synthetic-{index}.csv"
                                    ),
                                }
                            )
                        ]
                    }
                )
            )
        local_registry = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(
            _synthetic_spec(), local_registry, calendars
        )
        confirmation = confirm_data_plan(
            generated, local_registry, confirmed_at=CONFIRMED_AT
        )
        decisions = []
        for requirement in generated.data_plan.requirements:
            mapping = local_registry.get(
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
            generated, confirmation, local_registry, calendars, decisions
        )
        selection_confirmation = confirm_source_selection(
            selection, local_registry, calendars, confirmed_at=CONFIRMED_AT
        )
        snapshot = _synthetic_snapshot(provider_id="local_csv").model_copy(
            update={"supported_access_modes": [AccessMode.LOCAL_FILE]}
        )
        plan = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            generated.data_plan,
            local_registry,
            calendars,
            {"local_csv": snapshot},
        )
        return plan, local_registry, calendars, snapshot

    def test_local_file_authorization_flags(self) -> None:
        plan, local_registry, calendars, snapshot = self._local_file_plan()
        self.assertEqual(
            plan.acquisition_request_plan.unresolved_requirements, []
        )
        authorization = create_data_access_authorization(
            plan,
            local_registry,
            calendars,
            {"local_csv": snapshot},
            [r.requirement_id for r in plan.acquisition_request_plan.requests],
            authorized_at=CONFIRMED_AT,
        )
        self.assertTrue(authorization.local_file_read_authorized)
        self.assertFalse(authorization.network_access_authorized)

    def test_network_not_authorized_when_unneeded(self) -> None:
        plan, local_registry, calendars, snapshot = self._local_file_plan()
        authorization = create_data_access_authorization(
            plan,
            local_registry,
            calendars,
            {"local_csv": snapshot},
            [r.requirement_id for r in plan.acquisition_request_plan.requests],
            authorized_at=CONFIRMED_AT,
        )
        self.assertFalse(authorization.network_access_authorized)

    def test_missing_request_id_rejected(self) -> None:
        authorization_plan, instruments, calendars = _ready_plan()
        request_ids = [
            r.requirement_id for r in authorization_plan.acquisition_request_plan.requests
        ]
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            create_data_access_authorization(
                authorization_plan,
                instruments,
                calendars,
                {"synthetic_provider": _synthetic_snapshot()},
                request_ids[:-1],
                authorized_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
        )

    def test_extra_request_id_rejected(self) -> None:
        authorization_plan, instruments, calendars = _ready_plan()
        request_ids = [
            r.requirement_id for r in authorization_plan.acquisition_request_plan.requests
        ]
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            create_data_access_authorization(
                authorization_plan,
                instruments,
                calendars,
                {"synthetic_provider": _synthetic_snapshot()},
                [*request_ids, "extra.request"],
                authorized_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
        )

    def test_duplicate_request_id_rejected(self) -> None:
        authorization_plan, instruments, calendars = _ready_plan()
        request_ids = [
            r.requirement_id for r in authorization_plan.acquisition_request_plan.requests
        ]
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            create_data_access_authorization(
                authorization_plan,
                instruments,
                calendars,
                {"synthetic_provider": _synthetic_snapshot()},
                [request_ids[0], request_ids[0]],
                authorized_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
        )

    def test_authorization_forbids_paid_retry_fallback(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        self.assertFalse(authorization.paid_access_authorized)
        self.assertFalse(authorization.automatic_retry_authorized)
        self.assertFalse(authorization.fallback_authorized)
        serialized = serialize_data_access_authorization(authorization).decode(
            "utf-8"
        )
        for forbidden in (
            "api_key",
            "token",
            "secret",
            "password",
            "credential_value",
            "authorization_header",
        ):
            self.assertNotIn(forbidden, serialized)


class DataAccessAuthorizationStaleBindingTest(unittest.TestCase):
    def test_changed_plan_invalidates_authorization(self) -> None:
        authorization, plan, instruments, calendars = _ready_authorization()
        # Recreate the plan with an extra warning -> different hash.
        payload = plan.acquisition_request_plan.model_dump(mode="json")
        payload["requests"][0]["public_parameters"][
            "series_id"
        ] = "CHANGED"
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
            data_plan_confirmation_sha256=plan.data_plan_confirmation_sha256,
            source_selection_sha256=plan.source_selection_sha256,
            source_selection_confirmation_sha256=(
                plan.source_selection_confirmation_sha256
            ),
            instrument_registry_sha256=plan.instrument_registry_sha256,
            calendar_registry_sha256=plan.calendar_registry_sha256,
            capability_snapshot_sha256s=plan.capability_snapshot_sha256s,
        )
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            validate_data_access_authorization_matches(
                changed_generated, authorization
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.DATA_ACCESS_AUTHORIZATION_MISMATCH,
        )

    def test_changed_registry_invalidates_authorization(self) -> None:
        authorization, plan, instruments, calendars = _ready_authorization()
        changed_entries = [
            entry.model_copy(update={"notes": [*entry.notes, "changed"]})
            for entry in instruments.entries
        ]
        changed_registry = InstrumentRegistry(changed_entries, calendars)
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            create_data_access_authorization(
                plan,
                changed_registry,
                calendars,
                {"synthetic_provider": _synthetic_snapshot()},
                authorization.authorized_request_ids,
                authorized_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.ACQUISITION_REQUEST_NOT_READY,
        )

    def test_stale_capability_blocks_authorization(self) -> None:
        authorization, plan, instruments, calendars = _ready_authorization()
        stale = _synthetic_snapshot().model_copy(
            update={"supported_access_modes": [AccessMode.LOCAL_FILE]}
        )
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            create_data_access_authorization(
                plan,
                instruments,
                calendars,
                {"synthetic_provider": stale},
                authorization.authorized_request_ids,
                authorized_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.ACQUISITION_REQUEST_NOT_READY,
        )

    def test_changed_receipt_does_not_match(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        receipt = consume_data_access_authorization(
            authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
        )
        other = receipt.model_copy(
            update={"authorization_sha256": "0" * 64}
        )
        with self.assertRaises(DataAccessAuthorizationError) as raised:
            consume_data_access_authorization(
                authorization,
                attempt_id="attempt-2",
                consumed_at=CONFIRMED_AT,
                existing_receipt=other,
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataAccessAuthorizationErrorCode.DATA_ACCESS_AUTHORIZATION_MISMATCH,
        )


class DataAccessAuthorizationStrictJsonTest(unittest.TestCase):
    def _authorization_payload(self) -> dict[str, object]:
        authorization, _, _, _ = _ready_authorization()
        return json.loads(
            serialize_data_access_authorization(authorization).decode("utf-8")
        )

    def test_duplicate_keys_rejected(self) -> None:
        raw = json.dumps(
            self._authorization_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = raw.replace(
            '"authorization_schema_version":"1.0"',
            '"authorization_schema_version":"1.0",'
            '"authorization_schema_version":"1.0"',
            1,
        )
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(raw.encode("utf-8"))

    def test_non_object_top_level_rejected(self) -> None:
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(b"[1,2,3]")

    def test_invalid_utf8_rejected(self) -> None:
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(b"\xff\xfe")

    def test_extra_fields_rejected(self) -> None:
        payload = self._authorization_payload()
        payload["secret_value"] = "x"
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(_json_bytes(payload))

    def test_naive_timestamp_rejected(self) -> None:
        payload = self._authorization_payload()
        payload["authorized_at"] = "2026-08-05T00:00:00"
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(_json_bytes(payload))

    def test_false_confirmation_flags_rejected(self) -> None:
        for flag in ("paid_access_authorized", "automatic_retry_authorized",
                     "fallback_authorized"):
            with self.subTest(flag=flag):
                payload = self._authorization_payload()
                payload[flag] = True
                with self.assertRaises(DataAccessAuthorizationError):
                    parse_data_access_authorization(_json_bytes(payload))

    def test_single_use_false_rejected(self) -> None:
        payload = self._authorization_payload()
        payload["single_use"] = False
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(_json_bytes(payload))

    def test_wrong_statement_rejected(self) -> None:
        payload = self._authorization_payload()
        payload["authorization_statement"] = "I authorize unlimited downloads."
        with self.assertRaises(DataAccessAuthorizationError):
            parse_data_access_authorization(_json_bytes(payload))


class DataAccessAuthorizationPersistenceTest(unittest.TestCase):
    def test_authorization_persist_success_and_idempotent(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "authorization.json"
            persisted = persist_data_access_authorization(
                authorization, output, authorized_at=CONFIRMED_AT
            )
            self.assertEqual(
                persisted.authorization_sha256,
                calculate_data_access_authorization_sha256(authorization),
            )
            self.assertTrue(Path(str(output) + ".provenance.json").exists())
            persist_data_access_authorization(
                authorization, output, authorized_at=CONFIRMED_AT
            )
            self.assertEqual(
                parse_data_access_authorization(output.read_bytes()),
                authorization,
            )

    def test_receipt_persist_create_only(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        receipt = consume_data_access_authorization(
            authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
        )
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "receipt.json"
            persisted = persist_data_access_authorization_receipt(receipt, output)
            self.assertEqual(
                parse_data_access_authorization_receipt(output.read_bytes()),
                receipt,
            )
            persist_data_access_authorization_receipt(receipt, output)
            self.assertEqual(
                parse_data_access_authorization_receipt(output.read_bytes()),
                receipt,
            )

    def test_receipt_conflict_on_different_content(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        receipt = consume_data_access_authorization(
            authorization, attempt_id="attempt-1", consumed_at=CONFIRMED_AT
        )
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "receipt.json"
            persist_data_access_authorization_receipt(receipt, output)
            other = receipt.model_copy(update={"attempt_id": "attempt-9"})
            with self.assertRaises(DataAccessAuthorizationError) as raised:
                persist_data_access_authorization_receipt(other, output)
            self.assertEqual(
                raised.exception.failure.code,
                DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_CONFLICT,
            )

    def test_authorization_path_traversal_rejected(self) -> None:
        authorization, _, _, _ = _ready_authorization()
        with self.assertRaises(DataAccessAuthorizationError):
            persist_data_access_authorization(
                authorization,
                ROOT / ".." / "outside-authorization.json",
                authorized_at=CONFIRMED_AT,
            )

    def test_authorization_has_no_side_effects(self) -> None:
        before = set(ROOT.rglob("*"))
        _ready_authorization()
        after = set(ROOT.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
