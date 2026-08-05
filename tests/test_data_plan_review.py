"""Offline contract tests for the deterministic DataPlan lifecycle."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_validator.cli import main
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    DataPlanReviewError,
    DataPlanReviewErrorCode,
    confirm_data_plan,
    data_plan_readiness_blockers,
    generate_data_plan,
    parse_data_plan_confirmation,
    persist_data_plan_confirmation,
    persist_generated_data_plan,
    validate_data_plan_confirmation_matches,
)
from market_validator.data.models import DataRequirementStatus
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.data.serialization import (
    calculate_data_plan_sha256,
    parse_data_plan,
    serialize_data_plan,
)
from market_validator.research.serialization import parse_research_spec
from market_validator.workflow_cli import WorkflowCliExitCode

ROOT = Path(__file__).resolve().parents[1]
CONFIG_CALENDARS = ROOT / "config" / "calendars.json"
CONFIG_INSTRUMENTS = ROOT / "config" / "instruments.json"
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


def _load_synthetic_registries() -> tuple[CalendarRegistry, InstrumentRegistry]:
    calendars = CalendarRegistry.from_json_file(SYNTHETIC_CALENDARS)
    instruments = InstrumentRegistry.from_json_file(SYNTHETIC_INSTRUMENTS, calendars)
    return calendars, instruments


def _synthetic_spec():
    return parse_research_spec(SYNTHETIC_SPEC.read_bytes())


def _generate_synthetic():
    calendars, instruments = _load_synthetic_registries()
    generated = generate_data_plan(_synthetic_spec(), instruments, calendars)
    return generated, instruments


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            exit_code = main(arguments)
        except SystemExit as exc:
            exit_code = int(exc.code)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class DataPlanGenerationTest(unittest.TestCase):
    def test_same_inputs_produce_identical_canonical_identity(self) -> None:
        first, _ = _generate_synthetic()
        second, _ = _generate_synthetic()
        self.assertEqual(first.data_plan_sha256, second.data_plan_sha256)
        self.assertEqual(
            serialize_data_plan(first.data_plan),
            serialize_data_plan(second.data_plan),
        )

    def test_registry_entry_order_does_not_change_identity(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        entries = list(instruments.entries)
        entries.reverse()
        reordered = InstrumentRegistry(entries, calendars)
        first, _ = _generate_synthetic()
        second = generate_data_plan(_synthetic_spec(), reordered, calendars)
        self.assertEqual(first.data_plan_sha256, second.data_plan_sha256)
        self.assertEqual(
            first.instrument_registry_sha256,
            second.instrument_registry_sha256,
        )

    def test_registry_content_change_changes_identity(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        first, _ = _generate_synthetic()
        changed_entries = [
            entry.model_copy(
                update={"notes": [*entry.notes, "changed registry note"]}
            )
            for entry in instruments.entries
        ]
        changed = InstrumentRegistry(changed_entries, calendars)
        second = generate_data_plan(_synthetic_spec(), changed, calendars)
        self.assertNotEqual(
            first.instrument_registry_sha256,
            second.instrument_registry_sha256,
        )
        self.assertNotEqual(first.data_plan_sha256, second.data_plan_sha256)

    def test_registry_metadata_conflict_fails_explicitly(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        # Currency is not part of the cross-market detection, so the spec stays
        # valid while the registry metadata check must fail explicitly.
        spec_payload["outcome"]["instrument"]["currency"] = "EUR"
        spec = parse_research_spec(_json_bytes(spec_payload))
        with self.assertRaises(DataPlanReviewError) as raised:
            generate_data_plan(spec, instruments, calendars)
        self.assertEqual(
            raised.exception.failure.code,
            DataPlanReviewErrorCode.REGISTRY_MISMATCH,
        )

    def test_cross_market_spec_without_alignment_is_rejected(self) -> None:
        from market_validator.research.serialization import (
            ResearchSpecSerializationError,
        )

        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        spec_payload["predictors"][0]["instrument"]["market"] = "Other Market"
        spec_payload["predictors"][0]["instrument"]["timezone"] = "Europe/London"
        # The existing ResearchSpec validator rejects cross-market research
        # that lacks an alignment before any DataPlan step can run.
        with self.assertRaises(ResearchSpecSerializationError):
            parse_research_spec(_json_bytes(spec_payload))

    def test_unregistered_instruments_stay_unresolved(self) -> None:
        calendars = CalendarRegistry.from_json_file(CONFIG_CALENDARS)
        instruments = InstrumentRegistry.from_json_file(CONFIG_INSTRUMENTS, calendars)
        oil_spec = parse_research_spec(
            (
                ROOT
                / "examples"
                / "research_specs"
                / "oil_to_a_share_energy.json"
            ).read_bytes()
        )
        generated = generate_data_plan(oil_spec, instruments, calendars)
        self.assertTrue(generated.data_plan.unresolved_instruments)
        for requirement in generated.data_plan.requirements:
            if requirement.instrument_id in generated.data_plan.unresolved_instruments:
                self.assertEqual(
                    requirement.status, DataRequirementStatus.UNRESOLVED
                )

    def test_required_pre_sample_periods_are_computed_correctly(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        spec_payload["predictors"][0]["transformation"] = "log_return"
        spec_payload["predictors"][0]["lag_periods"] = 2
        spec = parse_research_spec(_json_bytes(spec_payload))
        generated = generate_data_plan(spec, instruments, calendars)
        requirement = next(
            item
            for item in generated.data_plan.requirements
            if item.variable_id == "synthetic_predictor"
        )
        self.assertEqual(requirement.required_pre_sample_periods, 3)


class DataPlanConfirmationTest(unittest.TestCase):
    def test_confirmation_binds_all_canonical_hashes(self) -> None:
        generated, instruments = _generate_synthetic()
        confirmation = confirm_data_plan(
            generated, instruments, confirmed_at=CONFIRMED_AT
        )
        self.assertTrue(confirmation.confirmed)
        self.assertEqual(
            confirmation.research_spec_sha256, generated.research_spec_sha256
        )
        self.assertEqual(
            confirmation.data_plan_sha256, generated.data_plan_sha256
        )
        self.assertEqual(
            confirmation.instrument_registry_sha256,
            generated.instrument_registry_sha256,
        )
        self.assertEqual(
            confirmation.calendar_registry_sha256,
            generated.calendar_registry_sha256,
        )

    def test_changed_spec_invalidates_old_confirmation(self) -> None:
        generated, instruments = _generate_synthetic()
        confirmation = confirm_data_plan(
            generated, instruments, confirmed_at=CONFIRMED_AT
        )
        spec_payload = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
        spec_payload["title"] = "Changed synthetic title"
        calendars, _ = _load_synthetic_registries()
        changed = generate_data_plan(
            parse_research_spec(_json_bytes(spec_payload)),
            instruments,
            calendars,
        )
        with self.assertRaises(DataPlanReviewError) as raised:
            validate_data_plan_confirmation_matches(changed, confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            DataPlanReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
        )

    def test_changed_registry_invalidates_old_confirmation(self) -> None:
        generated, instruments = _generate_synthetic()
        confirmation = confirm_data_plan(
            generated, instruments, confirmed_at=CONFIRMED_AT
        )
        calendars, _ = _load_synthetic_registries()
        changed_entries = [
            entry.model_copy(
                update={"notes": [*entry.notes, "changed registry note"]}
            )
            for entry in instruments.entries
        ]
        changed_registry = InstrumentRegistry(changed_entries, calendars)
        changed = generate_data_plan(
            _synthetic_spec(), changed_registry, calendars
        )
        with self.assertRaises(DataPlanReviewError) as raised:
            validate_data_plan_confirmation_matches(changed, confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            DataPlanReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
        )

    def test_unverified_identity_cannot_be_confirmed(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        entries = [
            entry.model_copy(update={"identity_status": IdentityStatus.UNVERIFIED})
            for entry in instruments.entries
        ]
        unverified = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), unverified, calendars)
        blockers = data_plan_readiness_blockers(generated, unverified)
        self.assertTrue(blockers)
        with self.assertRaises(DataPlanReviewError) as raised:
            confirm_data_plan(generated, unverified, confirmed_at=CONFIRMED_AT)
        self.assertEqual(
            raised.exception.failure.code,
            DataPlanReviewErrorCode.DATA_PLAN_NOT_READY,
        )

    def test_unverified_mapping_cannot_be_confirmed(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        entries = [
            entry.model_copy(
                update={
                    "provider_mappings": [
                        mapping.model_copy(
                            update={
                                "verified": False,
                                "verified_on": None,
                                "verification_source_uri": None,
                            }
                        )
                        for mapping in entry.provider_mappings
                    ]
                }
            )
            for entry in instruments.entries
        ]
        unverified_mappings = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(
            _synthetic_spec(), unverified_mappings, calendars
        )
        with self.assertRaises(DataPlanReviewError) as raised:
            confirm_data_plan(
                generated, unverified_mappings, confirmed_at=CONFIRMED_AT
            )
        self.assertEqual(
            raised.exception.failure.code,
            DataPlanReviewErrorCode.DATA_PLAN_NOT_READY,
        )

    def test_multiple_verified_mappings_never_select_a_provider(self) -> None:
        calendars, instruments = _load_synthetic_registries()
        entries = []
        for entry in instruments.entries:
            mapping = entry.provider_mappings[0]
            second = mapping.model_copy(
                update={
                    "provider_id": "second_synthetic_provider",
                    "provider_symbol": mapping.provider_symbol + "_2",
                }
            )
            entries.append(
                entry.model_copy(
                    update={
                        "provider_mappings": [
                            *entry.provider_mappings,
                            second,
                        ]
                    }
                )
            )
        multi = InstrumentRegistry(entries, calendars)
        generated = generate_data_plan(_synthetic_spec(), multi, calendars)
        # The DataPlan itself carries no provider symbol or provider id.
        serialized = serialize_data_plan(generated.data_plan).decode("utf-8")
        self.assertNotIn("provider_symbol", serialized)
        self.assertNotIn("provider_id", serialized)
        self.assertNotIn("SYNTH_OUTCOME", serialized)
        confirmation = confirm_data_plan(
            generated, multi, confirmed_at=CONFIRMED_AT
        )
        self.assertTrue(confirmation.confirmed)

    def test_confirmation_has_no_side_effects(self) -> None:
        before = set(ROOT.rglob("*"))
        generated, instruments = _generate_synthetic()
        confirm_data_plan(generated, instruments, confirmed_at=CONFIRMED_AT)
        after = set(ROOT.rglob("*"))
        self.assertEqual(before, after)


class DataPlanPersistenceTest(unittest.TestCase):
    def test_persist_writes_plan_and_provenance_pair(self) -> None:
        generated, _ = _generate_synthetic()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "plan.json"
            persisted = persist_generated_data_plan(generated, output)
            self.assertEqual(persisted.data_plan_sha256, generated.data_plan_sha256)
            self.assertTrue(output.exists())
            self.assertTrue(Path(str(output) + ".provenance.json").exists())
            restored = parse_data_plan(output.read_bytes())
            self.assertEqual(restored, generated.data_plan)
            self.assertEqual(
                calculate_data_plan_sha256(restored), generated.data_plan_sha256
            )

    def test_persist_is_create_only_and_idempotent(self) -> None:
        generated, _ = _generate_synthetic()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "plan.json"
            persist_generated_data_plan(
                generated, output, generated_at=CONFIRMED_AT
            )
            # Identical generated plan and identical audit time -> idempotent.
            persist_generated_data_plan(
                generated, output, generated_at=CONFIRMED_AT
            )
            different_spec_payload = json.loads(
                SYNTHETIC_SPEC.read_text(encoding="utf-8")
            )
            different_spec_payload["title"] = "Different title"
            calendars, instruments = _load_synthetic_registries()
            other = generate_data_plan(
                parse_research_spec(_json_bytes(different_spec_payload)),
                instruments,
                calendars,
            )
            with self.assertRaises(DataPlanReviewError) as raised:
                persist_generated_data_plan(
                    other, output, generated_at=CONFIRMED_AT
                )
            self.assertEqual(
                raised.exception.failure.code,
                DataPlanReviewErrorCode.OUTPUT_CONFLICT,
            )

    def test_persist_rejects_incomplete_output_pair(self) -> None:
        generated, _ = _generate_synthetic()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "plan.json"
            persist_generated_data_plan(generated, output)
            Path(str(output) + ".provenance.json").unlink()
            with self.assertRaises(DataPlanReviewError) as raised:
                persist_generated_data_plan(generated, output)
            self.assertEqual(
                raised.exception.failure.code,
                DataPlanReviewErrorCode.OUTPUT_CONFLICT,
            )

    def test_confirmation_persist_is_create_only(self) -> None:
        generated, instruments = _generate_synthetic()
        confirmation = confirm_data_plan(
            generated, instruments, confirmed_at=CONFIRMED_AT
        )
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "confirmation.json"
            first = persist_data_plan_confirmation(confirmation, output)
            self.assertEqual(first.read_bytes(), output.read_bytes())
            persist_data_plan_confirmation(confirmation, output)  # idempotent
            restored = parse_data_plan_confirmation(output.read_bytes())
            self.assertEqual(restored, confirmation)


class DataPlanCliTest(unittest.TestCase):
    def test_generate_and_validate_synthetic_plan(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "plan.json"
            exit_code, stdout, stderr = _invoke(
                [
                    "data-plan",
                    "generate",
                    "--research-spec",
                    str(SYNTHETIC_SPEC),
                    "--instrument-registry",
                    str(SYNTHETIC_INSTRUMENTS),
                    "--calendar-registry",
                    str(SYNTHETIC_CALENDARS),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            data = json.loads(stdout)["data"]
            self.assertTrue(data["ready_for_confirmation"])
            self.assertEqual(data["unresolved_instruments"], [])
            validate_code, validate_out, validate_err = _invoke(
                ["data-plan", "validate", "--plan", str(output)]
            )
            self.assertEqual(validate_code, 0)
            self.assertEqual(validate_err, "")
            self.assertEqual(
                json.loads(validate_out)["data"]["data_plan_sha256"],
                data["data_plan_sha256"],
            )

    def test_confirm_and_validate_confirmation_round_trip(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            plan_path = Path(directory) / "plan.json"
            confirmation_path = Path(directory) / "confirmation.json"
            exit_code, _, _ = _invoke(
                [
                    "data-plan",
                    "generate",
                    "--research-spec",
                    str(SYNTHETIC_SPEC),
                    "--instrument-registry",
                    str(SYNTHETIC_INSTRUMENTS),
                    "--calendar-registry",
                    str(SYNTHETIC_CALENDARS),
                    "--output",
                    str(plan_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            exit_code, stdout, stderr = _invoke(
                [
                    "data-plan",
                    "confirm",
                    "--plan",
                    str(plan_path),
                    "--research-spec",
                    str(SYNTHETIC_SPEC),
                    "--instrument-registry",
                    str(SYNTHETIC_INSTRUMENTS),
                    "--calendar-registry",
                    str(SYNTHETIC_CALENDARS),
                    "--output",
                    str(confirmation_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            data = json.loads(stdout)["data"]
            self.assertTrue(data["confirmed"])
            exit_code, stdout, stderr = _invoke(
                [
                    "data-plan",
                    "validate-confirmation",
                    "--confirmation",
                    str(confirmation_path),
                    "--plan",
                    str(plan_path),
                    "--research-spec",
                    str(SYNTHETIC_SPEC),
                    "--instrument-registry",
                    str(SYNTHETIC_INSTRUMENTS),
                    "--calendar-registry",
                    str(SYNTHETIC_CALENDARS),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["data"]["matches"])

    def test_confirm_rejects_unresolved_oil_plan(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            plan_path = Path(directory) / "oil_plan.json"
            oil_spec = ROOT / "examples" / "research_specs" / "oil_to_a_share_energy.json"
            exit_code, _, _ = _invoke(
                [
                    "data-plan",
                    "generate",
                    "--research-spec",
                    str(oil_spec),
                    "--instrument-registry",
                    str(CONFIG_INSTRUMENTS),
                    "--calendar-registry",
                    str(CONFIG_CALENDARS),
                    "--output",
                    str(plan_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            exit_code, stdout, stderr = _invoke(
                [
                    "data-plan",
                    "confirm",
                    "--plan",
                    str(plan_path),
                    "--research-spec",
                    str(oil_spec),
                    "--instrument-registry",
                    str(CONFIG_INSTRUMENTS),
                    "--calendar-registry",
                    str(CONFIG_CALENDARS),
                    "--output",
                    str(Path(directory) / "confirmation.json"),
                ]
            )
            self.assertEqual(
                exit_code, int(WorkflowCliExitCode.DATA_PLAN_NOT_READY)
            )
            self.assertEqual(stdout, "")
            self.assertNotIn("Traceback", stderr)

    def test_confirmation_schema_is_offline(self) -> None:
        exit_code, stdout, stderr = _invoke(["data-plan", "confirmation-schema"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        schema = json.loads(stdout)["data"]
        self.assertIn("data_plan_sha256", schema["properties"])
        self.assertIn("research_spec_sha256", schema["properties"])


if __name__ == "__main__":
    unittest.main()
