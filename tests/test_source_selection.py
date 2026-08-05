"""Offline contract tests for the deterministic source-selection lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    DataPlanReviewError,
    confirm_data_plan,
    generate_data_plan,
)
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.data.source_selection import (
    GeneratedSourceSelection,
    SourceSelection,
    SourceSelectionConfirmation,
    SourceSelectionDecision,
    SourceSelectionReviewError,
    SourceSelectionReviewErrorCode,
    calculate_source_selection_confirmation_sha256,
    calculate_source_selection_sha256,
    confirm_source_selection,
    generate_source_selection,
    parse_source_selection,
    parse_source_selection_confirmation,
    parse_source_selection_provenance,
    persist_generated_source_selection,
    persist_source_selection_confirmation,
    serialize_source_selection,
    serialize_source_selection_confirmation,
    source_selection_readiness_blockers,
    validate_source_selection_confirmation_matches,
)
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


def _generate_data_plan_confirmed():
    calendars, instruments = _load_registries()
    generated = generate_data_plan(_synthetic_spec(), instruments, calendars)
    confirmation = confirm_data_plan(
        generated, instruments, confirmed_at=CONFIRMED_AT
    )
    return generated, confirmation, instruments, calendars


def _decisions_for_all(generated, instruments) -> list[SourceSelectionDecision]:
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
    return decisions


def _generate_ready_selection():
    generated, confirmation, instruments, calendars = (
        _generate_data_plan_confirmed()
    )
    selection = generate_source_selection(
        generated,
        confirmation,
        instruments,
        calendars,
        _decisions_for_all(generated, instruments),
    )
    return selection, instruments, calendars


class SourceSelectionNormalPathTest(unittest.TestCase):
    def test_explicit_decisions_generate_and_confirm(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        selection = generate_source_selection(
            generated,
            confirmation,
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        self.assertEqual(
            len(selection.source_selection.selections),
            len(generated.data_plan.requirements),
        )
        self.assertEqual(
            selection.source_selection.unresolved_requirements, []
        )
        self.assertEqual(
            source_selection_readiness_blockers(
                selection, instruments, calendars
            ),
            [],
        )
        source_confirmation = confirm_source_selection(
            selection, instruments, calendars, confirmed_at=CONFIRMED_AT
        )
        self.assertTrue(source_confirmation.confirmed)
        validate_source_selection_confirmation_matches(
            selection, source_confirmation
        )

    def test_canonical_round_trip(self) -> None:
        selection, _, _ = _generate_ready_selection()
        payload = serialize_source_selection(selection.source_selection)
        restored = parse_source_selection(payload)
        self.assertEqual(restored, selection.source_selection)
        self.assertEqual(
            calculate_source_selection_sha256(restored),
            selection.source_selection_sha256,
        )
        calendars, _ = _load_registries()
        confirmation = confirm_source_selection(
            selection,
            InstrumentRegistry.from_json_file(
                SYNTHETIC_INSTRUMENTS,
                CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS),
            ),
            calendars,
            confirmed_at=CONFIRMED_AT,
        )
        self.assertEqual(
            parse_source_selection_confirmation(
                serialize_source_selection_confirmation(confirmation)
            ),
            confirmation,
        )

    def test_immutable_persistence_success(self) -> None:
        selection, _, _ = _generate_ready_selection()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "source-selection.json"
            persisted = persist_generated_source_selection(
                selection, output, generated_at=CONFIRMED_AT
            )
            self.assertEqual(
                persisted.source_selection_sha256,
                selection.source_selection_sha256,
            )
            self.assertTrue(output.exists())
            self.assertTrue(Path(str(output) + ".provenance.json").exists())
            restored = parse_source_selection(output.read_bytes())
            self.assertEqual(restored, selection.source_selection)
            provenance = parse_source_selection_provenance(
                Path(str(output) + ".provenance.json").read_bytes()
            )
            self.assertEqual(
                provenance.source_selection_sha256,
                selection.source_selection_sha256,
            )


class SourceSelectionNoAutoSelectTest(unittest.TestCase):
    def test_single_verified_mapping_without_decision_stays_unresolved(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        selection = generate_source_selection(
            generated, confirmation, instruments, calendars, []
        )
        self.assertEqual(
            len(selection.source_selection.selections), 0
        )
        unresolved = selection.source_selection.unresolved_requirements
        self.assertEqual(len(unresolved), len(generated.data_plan.requirements))
        for item in unresolved:
            self.assertEqual(item.code.value, "source_not_selected")
        self.assertTrue(
            source_selection_readiness_blockers(
                selection, instruments, calendars
            )
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            confirm_source_selection(
                selection, instruments, calendars, confirmed_at=CONFIRMED_AT
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_NOT_READY,
        )

    def test_second_mapping_never_auto_picked(self) -> None:
        calendars, instruments = _load_registries()
        entries = []
        for entry in instruments.entries:
            mapping = entry.provider_mappings[0]
            entries.append(
                entry.model_copy(
                    update={
                        "provider_mappings": [
                            mapping,
                            mapping.model_copy(
                                update={
                                    "provider_id": "second_synthetic_provider",
                                    "provider_symbol": mapping.provider_symbol
                                    + "_B",
                                }
                            ),
                        ]
                    }
                )
            )
        multi = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), multi, calendars)
        confirmation = confirm_data_plan(generated, multi, confirmed_at=CONFIRMED_AT)
        selection = generate_source_selection(
            generated, confirmation, multi, calendars, []
        )
        self.assertEqual(selection.source_selection.selections, [])
        self.assertTrue(
            all(
                item.code.value == "source_not_selected"
                for item in selection.source_selection.unresolved_requirements
            )
        )
        # Exact choice of the second mapping succeeds without auto-picking.
        requirement = generated.data_plan.requirements[0]
        entry = multi.get(requirement.instrument_id)
        second = entry.provider_mappings[1]
        decision = SourceSelectionDecision(
            requirement_id=requirement.requirement_id,
            provider_id=second.provider_id,
            provider_symbol=second.provider_symbol,
            dataset_or_endpoint=second.dataset_or_endpoint,
        )
        other_requirements = generated.data_plan.requirements[1:]
        others = []
        for other in other_requirements:
            other_entry = multi.get(other.instrument_id)
            mapping = other_entry.provider_mappings[0]
            others.append(
                SourceSelectionDecision(
                    requirement_id=other.requirement_id,
                    provider_id=mapping.provider_id,
                    provider_symbol=mapping.provider_symbol,
                    dataset_or_endpoint=mapping.dataset_or_endpoint,
                )
            )
        chosen = generate_source_selection(
            generated, confirmation, multi, calendars, [decision, *others]
        )
        selected = next(
            item
            for item in chosen.source_selection.selections
            if item.requirement_id == requirement.requirement_id
        )
        self.assertEqual(selected.provider_id, "second_synthetic_provider")
        self.assertTrue(selected.provider_symbol.endswith("_B"))


class SourceSelectionIdentityVerificationTest(unittest.TestCase):
    def _registry_with_identity(self, status: IdentityStatus):
        calendars, instruments = _load_registries()
        entries = [
            entry.model_copy(update={"identity_status": status})
            for entry in instruments.entries
        ]
        return InstrumentRegistry(entries, calendars), calendars

    def test_non_verified_identity_fails_closed_at_data_plan_layer(self) -> None:
        for status in (IdentityStatus.EXAMPLE, IdentityStatus.UNVERIFIED):
            with self.subTest(status=status):
                instruments, calendars = self._registry_with_identity(status)
                generated = generate_data_plan(
                    _synthetic_spec(), instruments, calendars
                )
                with self.assertRaises(DataPlanReviewError):
                    confirm_data_plan(
                        generated, instruments, confirmed_at=CONFIRMED_AT
                    )

    def test_unverified_mapping_choice_is_hard_failure(self) -> None:
        calendars, instruments = _load_registries()
        entries = []
        for entry in instruments.entries:
            verified = entry.provider_mappings[0]
            entries.append(
                entry.model_copy(
                    update={
                        "provider_mappings": [
                            verified,
                            verified.model_copy(
                                update={
                                    "provider_id": "unverified_provider",
                                    "verified": False,
                                    "verified_on": None,
                                    "verification_source_uri": None,
                                }
                            ),
                        ]
                    }
                )
            )
        mixed = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), mixed, calendars)
        confirmation = confirm_data_plan(generated, mixed, confirmed_at=CONFIRMED_AT)
        requirement = generated.data_plan.requirements[0]
        decisions = _decisions_for_all(generated, mixed)
        decisions[0] = SourceSelectionDecision(
            requirement_id=requirement.requirement_id,
            provider_id="unverified_provider",
            provider_symbol="SYNTH_OUTCOME",
            dataset_or_endpoint="/synthetic/series",
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated, confirmation, mixed, calendars, decisions
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
        )

    def test_verified_mapping_requires_uri_and_date_at_model_layer(self) -> None:
        from market_validator.data.registry import ProviderSymbolMapping

        _, instruments = _load_registries()
        mapping = instruments.entries[0].provider_mappings[0]
        base = mapping.model_dump()
        with self.assertRaises(ValidationError):
            ProviderSymbolMapping.model_validate(
                {**base, "verification_source_uri": None}
            )
        with self.assertRaises(ValidationError):
            ProviderSymbolMapping.model_validate(
                {**base, "verified_on": None}
            )

    def test_mapping_market_conflict_is_hard_failure(self) -> None:
        calendars, instruments = _load_registries()
        entries = []
        for entry in instruments.entries:
            entries.append(
                entry.model_copy(
                    update={
                        "provider_mappings": [
                            mapping.model_copy(
                                update={"market": "Conflicting Market"}
                            )
                            for mapping in entry.provider_mappings
                        ]
                    }
                )
            )
        conflicting = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), conflicting, calendars)
        confirmation = confirm_data_plan(
            generated, conflicting, confirmed_at=CONFIRMED_AT
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated,
                confirmation,
                conflicting,
                calendars,
                _decisions_for_all(generated, conflicting),
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
        )

    def test_invalid_decision_input_is_structured_failure(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        decisions = _decisions_for_all(generated, instruments)
        decisions[0] = {
            "requirement_id": decisions[0].requirement_id,
            "provider_id": "synthetic_provider",
            "provider_symbol": "SYNTH_OUTCOME",
            "dataset_or_endpoint": "/synthetic/series",
        }
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated, confirmation, instruments, calendars, decisions
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
        )
        self.assertNotIn("requirement", raised.exception.failure.message)

    def test_unknown_requirement_decision_is_hard_failure(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        unknown = SourceSelectionDecision(
            requirement_id="unknown.requirement",
            provider_id="synthetic_provider",
            provider_symbol="SYNTH_OUTCOME",
            dataset_or_endpoint="/synthetic/series",
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated,
                confirmation,
                instruments,
                calendars,
                [_decisions_for_all(generated, instruments)[0], unknown],
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
        )

    def test_unknown_provider_symbol_is_hard_failure(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        decisions = _decisions_for_all(generated, instruments)
        decisions[0] = SourceSelectionDecision(
            requirement_id=decisions[0].requirement_id,
            provider_id=decisions[0].provider_id,
            provider_symbol="DOES_NOT_EXIST",
            dataset_or_endpoint=decisions[0].dataset_or_endpoint,
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated, confirmation, instruments, calendars, decisions
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
        )

    def test_wrong_endpoint_is_hard_failure(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        decisions = _decisions_for_all(generated, instruments)
        decisions[0] = SourceSelectionDecision(
            requirement_id=decisions[0].requirement_id,
            provider_id=decisions[0].provider_id,
            provider_symbol=decisions[0].provider_symbol,
            dataset_or_endpoint="/wrong/endpoint",
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated, confirmation, instruments, calendars, decisions
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
        )

    def test_requirement_market_conflict_fails_closed_at_data_plan_layer(self) -> None:
        from market_validator.research.serialization import (
            ResearchSpecSerializationError,
        )

        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        spec_payload["outcome"]["instrument"]["market"] = "Conflicting Market"
        # The ResearchSpec contract itself rejects the conflicting market
        # before any DataPlan or SourceSelection step can run.
        with self.assertRaises(ResearchSpecSerializationError):
            parse_research_spec(_json_bytes(spec_payload))

    def test_unregistered_instrument_fails_closed_at_data_plan_layer(self) -> None:
        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        spec_payload["outcome"]["instrument"][
            "instrument_id"
        ] = "not.in.registry.instrument"
        calendars, instruments = _load_registries()
        # The planner rejects an unregistered outcome instrument before any
        # SourceSelection step can run.
        with self.assertRaises(DataPlanReviewError):
            generate_data_plan(
                parse_research_spec(_json_bytes(spec_payload)),
                instruments,
                calendars,
            )


class SourceSelectionStaleBindingsTest(unittest.TestCase):
    def test_changed_data_plan_confirmation_invalidates(self) -> None:
        selection, _, _ = _generate_ready_selection()
        other_confirmation = SourceSelectionConfirmation(
            research_spec_sha256=selection.research_spec_sha256,
            data_plan_sha256=selection.data_plan_sha256,
            data_plan_confirmation_sha256="0" * 64,
            source_selection_sha256=selection.source_selection_sha256,
            instrument_registry_sha256=selection.instrument_registry_sha256,
            calendar_registry_sha256=selection.calendar_registry_sha256,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            validate_source_selection_confirmation_matches(
                selection, other_confirmation
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_CONFIRMATION_MISMATCH,
        )

    def test_changed_source_selection_invalidates(self) -> None:
        selection, instruments, calendars = _generate_ready_selection()
        confirmation = confirm_source_selection(
            selection, instruments, calendars, confirmed_at=CONFIRMED_AT
        )
        selection_payload = json.loads(
            serialize_source_selection(selection.source_selection).decode("utf-8")
        )
        selection_payload["warnings"] = ["extra warning"]
        changed = parse_source_selection(_json_bytes(selection_payload))
        changed_generated = GeneratedSourceSelection(
            source_selection=changed,
            source_selection_sha256=calculate_source_selection_sha256(changed),
            research_spec_sha256=selection.research_spec_sha256,
            data_plan_sha256=selection.data_plan_sha256,
            data_plan_confirmation_sha256=selection.data_plan_confirmation_sha256,
            instrument_registry_sha256=selection.instrument_registry_sha256,
            calendar_registry_sha256=selection.calendar_registry_sha256,
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            validate_source_selection_confirmation_matches(
                changed_generated, confirmation
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_CONFIRMATION_MISMATCH,
        )

    def test_registry_change_blocks_confirmation(self) -> None:
        selection, instruments, calendars = _generate_ready_selection()
        changed_entries = [
            entry.model_copy(
                update={"notes": [*entry.notes, "changed"]}
            )
            for entry in instruments.entries
        ]
        changed_registry = InstrumentRegistry(changed_entries, calendars)
        blockers = source_selection_readiness_blockers(
            selection, changed_registry, calendars
        )
        self.assertTrue(
            any(
                "no longer matches the bound hash" in blocker
                for blocker in blockers
            )
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            confirm_source_selection(
                selection,
                changed_registry,
                calendars,
                confirmed_at=CONFIRMED_AT,
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_NOT_READY,
        )

    def test_confirmation_change_changes_selection_identity(self) -> None:
        generated, _, instruments, calendars = _generate_data_plan_confirmed()
        other_confirmation = confirm_data_plan(
            generated,
            instruments,
            confirmed_at=datetime(
                2026, 8, 5, 1, 0, tzinfo=timezone.utc
            ),
        )
        first = generate_source_selection(
            generated,
            other_confirmation,
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        second = generate_source_selection(
            generated,
            other_confirmation,
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        self.assertEqual(
            first.source_selection_sha256, second.source_selection_sha256
        )
        # A different DataPlanConfirmation (different confirmed_at) must
        # change the confirmation identity and therefore selection_id/hash.
        third = generate_source_selection(
            generated,
            confirm_data_plan(
                generated, instruments, confirmed_at=CONFIRMED_AT
            ),
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        self.assertNotEqual(
            third.source_selection_sha256, first.source_selection_sha256
        )
        self.assertNotEqual(
            third.source_selection.selection_id,
            first.source_selection.selection_id,
        )

    def test_changed_instrument_registry_invalidates_generation(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        changed_entries = [
            entry.model_copy(
                update={"notes": [*entry.notes, "changed"]}
            )
            for entry in instruments.entries
        ]
        changed_registry = InstrumentRegistry(changed_entries, calendars)
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated,
                confirmation,
                changed_registry,
                calendars,
                _decisions_for_all(generated, instruments),
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
        )

    def test_changed_calendar_registry_invalidates_generation(self) -> None:
        from market_validator.data.calendars import CalendarRegistry as CR

        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        changed = CR(
            [
                definition.model_copy(
                    update={"notes": [*definition.notes, "changed"]}
                )
                for definition in calendars.definitions
            ]
        )
        with self.assertRaises(SourceSelectionReviewError) as raised:
            generate_source_selection(
                generated,
                confirmation,
                instruments,
                changed,
                _decisions_for_all(generated, instruments),
            )
        self.assertEqual(
            raised.exception.failure.code,
            SourceSelectionReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
        )

    def test_registry_entry_order_change_preserves_identity(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        entries = list(instruments.entries)
        entries.reverse()
        reordered = InstrumentRegistry(entries, calendars)
        first = generate_source_selection(
            generated,
            confirmation,
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        second = generate_source_selection(
            generated,
            confirmation,
            reordered,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        self.assertEqual(
            first.source_selection_sha256, second.source_selection_sha256
        )


class SourceSelectionDeterminismTest(unittest.TestCase):
    def test_decision_order_does_not_change_hash(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        decisions = _decisions_for_all(generated, instruments)
        first = generate_source_selection(
            generated, confirmation, instruments, calendars, decisions
        )
        second = generate_source_selection(
            generated,
            confirmation,
            instruments,
            calendars,
            list(reversed(decisions)),
        )
        self.assertEqual(
            first.source_selection_sha256, second.source_selection_sha256
        )

    def test_selections_sorted_by_requirement_id(self) -> None:
        selection, _, _ = _generate_ready_selection()
        ids = [
            item.requirement_id for item in selection.source_selection.selections
        ]
        self.assertEqual(ids, sorted(ids))

    def test_canonical_bytes_are_stable(self) -> None:
        selection, _, _ = _generate_ready_selection()
        self.assertEqual(
            serialize_source_selection(selection.source_selection),
            serialize_source_selection(
                parse_source_selection(
                    serialize_source_selection(selection.source_selection)
                )
            ),
        )

    def test_warnings_sorted_and_deduplicated(self) -> None:
        generated, confirmation, instruments, calendars = (
            _generate_data_plan_confirmed()
        )
        selection = generate_source_selection(
            generated,
            confirmation,
            instruments,
            calendars,
            _decisions_for_all(generated, instruments),
        )
        warnings = selection.source_selection.warnings
        self.assertEqual(warnings, sorted(set(warnings)))


class SourceSelectionStrictJsonTest(unittest.TestCase):
    def _selection_payload(self) -> dict[str, object]:
        selection, _, _ = _generate_ready_selection()
        return json.loads(
            serialize_source_selection(selection.source_selection).decode("utf-8")
        )

    def test_duplicate_keys_rejected(self) -> None:
        raw = json.dumps(
            self._selection_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = raw.replace(
            '"source_selection_schema_version":"1.0"',
            '"source_selection_schema_version":"1.0",'
            '"source_selection_schema_version":"1.0"',
            1,
        )
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection(raw.encode("utf-8"))

    def test_non_object_top_level_rejected(self) -> None:
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection(b"[1, 2, 3]")

    def test_invalid_utf8_rejected(self) -> None:
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection(b"\xff\xfe\x00\x01")

    def test_non_finite_numbers_rejected(self) -> None:
        raw = json.dumps(
            self._selection_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                injected = raw.replace(
                    '"source_selection_schema_version":"1.0"',
                    f'"source_selection_schema_version":{token}',
                    1,
                )
                with self.assertRaises(SourceSelectionReviewError):
                    parse_source_selection(injected.encode("utf-8"))

    def test_extra_fields_rejected(self) -> None:
        payload = self._selection_payload()
        payload["unexpected"] = True
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection(_json_bytes(payload))

    def test_false_confirmation_rejected(self) -> None:
        confirmation = SourceSelectionConfirmation(
            research_spec_sha256="0" * 64,
            data_plan_sha256="0" * 64,
            data_plan_confirmation_sha256="0" * 64,
            source_selection_sha256="0" * 64,
            instrument_registry_sha256="0" * 64,
            calendar_registry_sha256="0" * 64,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        payload = json.loads(
            serialize_source_selection_confirmation(confirmation).decode("utf-8")
        )
        payload["confirmed"] = False
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection_confirmation(_json_bytes(payload))

    def test_wrong_confirmation_statement_rejected(self) -> None:
        confirmation = SourceSelectionConfirmation(
            research_spec_sha256="0" * 64,
            data_plan_sha256="0" * 64,
            data_plan_confirmation_sha256="0" * 64,
            source_selection_sha256="0" * 64,
            instrument_registry_sha256="0" * 64,
            calendar_registry_sha256="0" * 64,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        payload = json.loads(
            serialize_source_selection_confirmation(confirmation).decode("utf-8")
        )
        payload["confirmation_statement"] = "I authorize downloads."
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection_confirmation(_json_bytes(payload))

    def test_naive_timestamp_rejected(self) -> None:
        confirmation = SourceSelectionConfirmation(
            research_spec_sha256="0" * 64,
            data_plan_sha256="0" * 64,
            data_plan_confirmation_sha256="0" * 64,
            source_selection_sha256="0" * 64,
            instrument_registry_sha256="0" * 64,
            calendar_registry_sha256="0" * 64,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        payload = json.loads(
            serialize_source_selection_confirmation(confirmation).decode("utf-8")
        )
        payload["confirmed_at"] = "2026-08-05T00:00:00"
        with self.assertRaises(SourceSelectionReviewError):
            parse_source_selection_confirmation(_json_bytes(payload))


class SourceSelectionPersistenceTest(unittest.TestCase):
    def test_duplicate_same_content_is_idempotent(self) -> None:
        selection, _, _ = _generate_ready_selection()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "source-selection.json"
            persist_generated_source_selection(
                selection, output, generated_at=CONFIRMED_AT
            )
            persist_generated_source_selection(
                selection, output, generated_at=CONFIRMED_AT
            )
            self.assertEqual(
                parse_source_selection(output.read_bytes()),
                selection.source_selection,
            )

    def test_same_path_different_content_conflicts(self) -> None:
        selection, _, _ = _generate_ready_selection()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "source-selection.json"
            persist_generated_source_selection(
                selection, output, generated_at=CONFIRMED_AT
            )
            other_payload = json.loads(
                serialize_source_selection(selection.source_selection).decode(
                    "utf-8"
                )
            )
            other_payload["warnings"] = ["different"]
            other_selection = parse_source_selection(_json_bytes(other_payload))
            other_generated = GeneratedSourceSelection(
                source_selection=other_selection,
                source_selection_sha256=calculate_source_selection_sha256(
                    other_selection
                ),
                research_spec_sha256=selection.research_spec_sha256,
                data_plan_sha256=selection.data_plan_sha256,
                data_plan_confirmation_sha256=selection.data_plan_confirmation_sha256,
                instrument_registry_sha256=selection.instrument_registry_sha256,
                calendar_registry_sha256=selection.calendar_registry_sha256,
            )
            with self.assertRaises(SourceSelectionReviewError) as raised:
                persist_generated_source_selection(
                    other_generated, output, generated_at=CONFIRMED_AT
                )
            self.assertEqual(
                raised.exception.failure.code,
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_CONFLICT,
            )

    def test_incomplete_output_pair_conflicts(self) -> None:
        selection, _, _ = _generate_ready_selection()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "source-selection.json"
            persist_generated_source_selection(
                selection, output, generated_at=CONFIRMED_AT
            )
            Path(str(output) + ".provenance.json").unlink()
            with self.assertRaises(SourceSelectionReviewError) as raised:
                persist_generated_source_selection(
                    selection, output, generated_at=CONFIRMED_AT
                )
            self.assertEqual(
                raised.exception.failure.code,
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_CONFLICT,
            )

    def test_path_traversal_rejected(self) -> None:
        selection, _, _ = _generate_ready_selection()
        with self.assertRaises(SourceSelectionReviewError):
            persist_generated_source_selection(
                selection,
                ROOT / ".." / "outside-source-selection.json",
                generated_at=CONFIRMED_AT,
            )

    def test_persist_rejects_mismatched_content_hash(self) -> None:
        selection, _, _ = _generate_ready_selection()
        forged = GeneratedSourceSelection(
            source_selection=selection.source_selection,
            source_selection_sha256="0" * 64,
            research_spec_sha256=selection.research_spec_sha256,
            data_plan_sha256=selection.data_plan_sha256,
            data_plan_confirmation_sha256=selection.data_plan_confirmation_sha256,
            instrument_registry_sha256=selection.instrument_registry_sha256,
            calendar_registry_sha256=selection.calendar_registry_sha256,
        )
        with TemporaryDirectory(dir=ROOT) as directory:
            with self.assertRaises(SourceSelectionReviewError) as raised:
                persist_generated_source_selection(
                    forged,
                    Path(directory) / "source-selection.json",
                    generated_at=CONFIRMED_AT,
                )
            self.assertEqual(
                raised.exception.failure.code,
                SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            )

    def test_confirmation_persist_is_create_only(self) -> None:
        selection, instruments, calendars = _generate_ready_selection()
        confirmation = confirm_source_selection(
            selection, instruments, calendars, confirmed_at=CONFIRMED_AT
        )
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "confirmation.json"
            persist_source_selection_confirmation(confirmation, output)
            persist_source_selection_confirmation(confirmation, output)
            self.assertEqual(
                parse_source_selection_confirmation(output.read_bytes()),
                confirmation,
            )


class SourceSelectionTrustBoundaryTest(unittest.TestCase):
    def test_confirmation_has_no_forbidden_fields(self) -> None:
        selection, instruments, calendars = _generate_ready_selection()
        confirmation = confirm_source_selection(
            selection, instruments, calendars, confirmed_at=CONFIRMED_AT
        )
        serialized = serialize_source_selection_confirmation(confirmation).decode(
            "utf-8"
        )
        for forbidden in (
            "allow_network",
            "network_authorized",
            "credential",
            "api_key",
            "token",
            "secret",
            "password",
            "paid_access",
            "retry",
            "fallback",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_selection_never_contains_secrets(self) -> None:
        selection, _, _ = _generate_ready_selection()
        serialized = serialize_source_selection(selection.source_selection).decode(
            "utf-8"
        )
        for forbidden in ("token", "secret", "password", "api_key", "credential"):
            self.assertNotIn(forbidden, serialized)

    def test_confirmation_has_no_side_effects(self) -> None:
        before = set(ROOT.rglob("*"))
        selection, instruments, calendars = _generate_ready_selection()
        confirm_source_selection(
            selection, instruments, calendars, confirmed_at=CONFIRMED_AT
        )
        after = set(ROOT.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
