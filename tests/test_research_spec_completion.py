"""Offline contract tests for ResearchSpecCompletionAnswers."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_validator.cli import main
from market_validator.hypothesis import (
    HypothesisLifecycleError,
    HypothesisLifecycleErrorCode,
    apply_clarification_answers,
    apply_research_spec_completion_answers,
    calculate_research_hypothesis_proposal_sha256,
    compile_confirmed_research_spec,
    confirm_research_hypothesis_proposal,
    parse_clarification_answers,
    parse_research_hypothesis_proposal,
    parse_research_spec_completion_answers,
    persist_completed_research_hypothesis_proposal,
)
from market_validator.workflow_cli import WorkflowCliExitCode

ROOT = Path(__file__).resolve().parents[1]
OIL_PROPOSAL = ROOT / "examples" / "hypothesis_proposals" / "oil_to_a_share_energy.proposal.json"
OIL_ANSWERS = ROOT / "examples" / "hypothesis_proposals" / "oil_to_a_share_energy.clarifications.json"
OIL_COMPLETION = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.completion.json"
)
CONFIRMED_AT = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _clarified_oil_proposal():
    proposal = parse_research_hypothesis_proposal(OIL_PROPOSAL.read_bytes())
    answers = parse_clarification_answers(OIL_ANSWERS.read_bytes())
    return apply_clarification_answers(proposal, answers).proposal


def _completion_payload(*, source_sha256: str | None = None) -> dict[str, object]:
    payload = json.loads(OIL_COMPLETION.read_text(encoding="utf-8"))
    if source_sha256 is not None:
        payload["source_proposal_sha256"] = source_sha256
    return payload


def _apply_default():
    proposal = _clarified_oil_proposal()
    answers = parse_research_spec_completion_answers(
        _json_bytes(_completion_payload())
    )
    return proposal, answers, apply_research_spec_completion_answers(proposal, answers)


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            exit_code = main(arguments)
        except SystemExit as exc:
            exit_code = int(exc.code)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class ResearchSpecCompletionContractTest(unittest.TestCase):
    def test_valid_completion_creates_new_proposal_version(self) -> None:
        proposal, _, applied = _apply_default()
        self.assertNotEqual(
            applied.completed_proposal_sha256,
            applied.source_proposal_sha256,
        )
        self.assertEqual(
            applied.source_proposal_sha256,
            calculate_research_hypothesis_proposal_sha256(proposal),
        )
        self.assertTrue(applied.proposal.ready_for_spec_review)
        self.assertIsNotNone(applied.proposal.research_spec_inputs)

    def test_source_proposal_is_never_modified(self) -> None:
        proposal, answers, _ = _apply_default()
        before = calculate_research_hypothesis_proposal_sha256(proposal)
        apply_research_spec_completion_answers(proposal, answers)
        self.assertEqual(
            calculate_research_hypothesis_proposal_sha256(proposal), before
        )

    def test_new_proposal_hash_changes_and_old_confirmation_is_invalidated(self) -> None:
        proposal, _, applied = _apply_default()
        old_confirmation = confirm_research_hypothesis_proposal(
            proposal, confirmed_at=CONFIRMED_AT
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(applied.proposal, old_confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.CONFIRMATION_MISMATCH,
        )

    def test_new_proposal_can_be_reconfirmed_and_compiled(self) -> None:
        _, _, applied = _apply_default()
        new_confirmation = confirm_research_hypothesis_proposal(
            applied.proposal, confirmed_at=CONFIRMED_AT
        )
        compiled = compile_confirmed_research_spec(
            applied.proposal, new_confirmation
        )
        self.assertEqual(compiled.research_spec.spec_id, "oil-to-a-share-energy-v1")

    def test_unknown_fields_are_rejected(self) -> None:
        payload = _completion_payload()
        payload["unexpected"] = True
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(_json_bytes(payload))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        payload = _completion_payload()
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        raw = raw.replace(
            '"completion_schema_version":"1.0"',
            '"completion_schema_version":"1.0","completion_schema_version":"1.0"',
            1,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(raw.encode("utf-8"))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_non_finite_numbers_are_rejected(self) -> None:
        payload = _completion_payload()
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).replace('"minimum_observations":500', '"minimum_observations":Infinity')
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(raw.encode("utf-8"))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_markdown_fences_and_extra_text_are_rejected(self) -> None:
        payload = _completion_payload()
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for wrapped in (
            "```json\n" + raw + "\n```",
            "prefix " + raw,
            raw + " suffix",
        ):
            with self.subTest(wrapped=wrapped[:12]):
                with self.assertRaises(HypothesisLifecycleError) as raised:
                    parse_research_spec_completion_answers(wrapped.encode("utf-8"))
                self.assertEqual(
                    raised.exception.failure.code,
                    HypothesisLifecycleErrorCode.INVALID_COMPLETION,
                )

    def test_wrong_source_proposal_hash_is_rejected(self) -> None:
        proposal, _, _ = _apply_default()
        payload = _completion_payload(source_sha256="0" * 64)
        answers = parse_research_spec_completion_answers(_json_bytes(payload))
        with self.assertRaises(HypothesisLifecycleError) as raised:
            apply_research_spec_completion_answers(proposal, answers)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.COMPLETION_MISMATCH,
        )

    def test_unknown_or_duplicate_variable_ids_are_rejected(self) -> None:
        payload = _completion_payload()
        payload["research_spec_inputs"]["variables"].append(
            {
                "variable_id": "mystery_variable",
                "instrument": payload["research_spec_inputs"]["variables"][0][
                    "instrument"
                ],
                "field": "close",
                "availability_lag_periods": 0,
                "price_adjustment": None,
                "rolling_window_periods": None,
            }
        )
        proposal, answers, _ = _apply_default()
        bad_answers = parse_research_spec_completion_answers(_json_bytes(payload))
        with self.assertRaises(HypothesisLifecycleError) as raised:
            apply_research_spec_completion_answers(proposal, bad_answers)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
        )
        duplicate = _completion_payload()
        duplicate["research_spec_inputs"]["variables"].append(
            deepcopy(duplicate["research_spec_inputs"]["variables"][0])
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(_json_bytes(duplicate))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_missing_declared_variable_mapping_is_rejected(self) -> None:
        # The compilation-inputs contract requires at least two variable
        # mappings, so dropping one declared variable is rejected at the model
        # layer before any application step.
        payload = _completion_payload()
        payload["research_spec_inputs"]["variables"].pop()
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(_json_bytes(payload))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_second_completion_on_already_completed_proposal_is_refused(self) -> None:
        proposal, answers, applied = _apply_default()
        payload = _completion_payload(
            source_sha256=applied.completed_proposal_sha256
        )
        second = parse_research_spec_completion_answers(_json_bytes(payload))
        with self.assertRaises(HypothesisLifecycleError) as raised:
            apply_research_spec_completion_answers(applied.proposal, second)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
        )

    def test_completion_never_modifies_other_proposal_fields(self) -> None:
        proposal, _, applied = _apply_default()
        before = proposal.model_dump(mode="json", exclude_unset=True)
        after = applied.proposal.model_dump(mode="json", exclude_unset=True)
        before.pop("research_spec_inputs", None)
        after.pop("research_spec_inputs", None)
        self.assertEqual(after, before)

    def test_asset_type_conflict_is_rejected(self) -> None:
        payload = _completion_payload()
        payload["research_spec_inputs"]["variables"][0]["instrument"][
            "asset_type"
        ] = "commodity_future"
        proposal, answers, _ = _apply_default()
        conflicting = parse_research_spec_completion_answers(_json_bytes(payload))
        with self.assertRaises(HypothesisLifecycleError) as raised:
            apply_research_spec_completion_answers(proposal, conflicting)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
        )

    def test_cross_market_policy_must_be_supplied_as_a_group(self) -> None:
        payload = _completion_payload()
        payload["research_spec_inputs"]["max_staleness_days"] = None
        payload["research_spec_inputs"]["missing_data_policy"] = None
        with self.assertRaises(HypothesisLifecycleError) as raised:
            parse_research_spec_completion_answers(_json_bytes(payload))
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        )

    def test_completion_output_is_create_only_and_immutable(self) -> None:
        _, _, applied = _apply_default()
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "completed.proposal.json"
            first = persist_completed_research_hypothesis_proposal(applied, output)
            self.assertEqual(first.read_bytes(), output.read_bytes())
            second = persist_completed_research_hypothesis_proposal(applied, output)
            self.assertEqual(second, first)
            different_payload = _completion_payload()
            different_payload["research_spec_inputs"]["title"] = "different title"
            different_answers = parse_research_spec_completion_answers(
                _json_bytes(different_payload)
            )
            different = apply_research_spec_completion_answers(
                _clarified_oil_proposal(), different_answers
            )
            with self.assertRaises(HypothesisLifecycleError):
                persist_completed_research_hypothesis_proposal(different, output)


class ResearchSpecCompletionCliTest(unittest.TestCase):
    @staticmethod
    def _write_clarified_proposal(directory: Path) -> Path:
        clarified_path = Path(directory) / "clarified.proposal.json"
        exit_code, stdout, stderr = _invoke(
            [
                "hypothesis",
                "apply-clarifications",
                "--proposal",
                str(OIL_PROPOSAL),
                "--answers",
                str(OIL_ANSWERS),
                "--output",
                str(clarified_path),
            ]
        )
        if exit_code != 0:
            raise AssertionError(
                f"clarification setup failed: {stdout} {stderr}"
            )
        return clarified_path

    def test_completion_schema_is_offline_and_stable(self) -> None:
        exit_code, stdout, stderr = _invoke(
            ["hypothesis", "completion-schema"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        schema = json.loads(stdout)["data"]
        self.assertIn("research_spec_inputs", schema["properties"])

    def test_validate_completion_is_read_only(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            clarified = self._write_clarified_proposal(Path(directory))
            before = OIL_COMPLETION.read_bytes()
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "validate-completion",
                    "--proposal",
                    str(clarified),
                    "--answers",
                    str(OIL_COMPLETION),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            data = json.loads(stdout)["data"]
            self.assertTrue(data["ready_for_spec_review"])
            self.assertEqual(OIL_COMPLETION.read_bytes(), before)

    def test_apply_completion_creates_new_proposal_without_overwriting(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            clarified = self._write_clarified_proposal(Path(directory))
            output = Path(directory) / "completed.proposal.json"
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "apply-completion",
                    "--proposal",
                    str(clarified),
                    "--answers",
                    str(OIL_COMPLETION),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            data = json.loads(stdout)["data"]
            self.assertTrue(data["previous_confirmation_invalidated"])
            completed = parse_research_hypothesis_proposal(output.read_bytes())
            self.assertIsNotNone(completed.research_spec_inputs)
            self.assertTrue(completed.ready_for_spec_review)
            self.assertNotEqual(
                calculate_research_hypothesis_proposal_sha256(completed),
                calculate_research_hypothesis_proposal_sha256(
                    parse_research_hypothesis_proposal(clarified.read_bytes())
                ),
            )

    def test_apply_completion_with_wrong_proposal_has_stable_exit(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            clarified = self._write_clarified_proposal(Path(directory))
            output = Path(directory) / "completed.proposal.json"
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "apply-completion",
                    "--proposal",
                    str(clarified),
                    "--answers",
                    str(OIL_COMPLETION),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            different = Path(directory) / "different.proposal.json"
            payload = json.loads(clarified.read_text(encoding="utf-8"))
            payload["sample"]["end_date"] = "2025-12-31"
            different.write_bytes(_json_bytes(payload))
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "apply-completion",
                    "--proposal",
                    str(different),
                    "--answers",
                    str(OIL_COMPLETION),
                    "--output",
                    str(Path(directory) / "other.proposal.json"),
                ]
            )
            self.assertEqual(
                exit_code,
                int(WorkflowCliExitCode.RESEARCH_SPEC_COMPLETION_MISMATCH),
            )
            self.assertEqual(stdout, "")
            self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
