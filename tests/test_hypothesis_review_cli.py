"""Offline CLI tests for hypothesis clarification, confirmation, and compilation."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from market_validator.cli import main
from market_validator.hypothesis import (
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_confirmation,
    parse_research_hypothesis_proposal,
    serialize_research_hypothesis_proposal,
)
from market_validator.research import parse_research_spec
from market_validator.workflow_cli import WorkflowCliExitCode
from tests.test_hypothesis_review import _ready_association


ROOT = Path(__file__).resolve().parents[1]
OIL_PROPOSAL = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.proposal.json"
)
OIL_ANSWERS = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.clarifications.json"
)


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            exit_code = main(arguments)
        except SystemExit as exc:
            exit_code = int(exc.code)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _write_ready_proposal(path: Path) -> None:
    path.write_bytes(serialize_research_hypothesis_proposal(_ready_association()))


class HypothesisReviewCliTest(unittest.TestCase):
    def test_clarification_schema_matches_for_both_entry_points(self) -> None:
        console_script = shutil.which("market-validator")
        if console_script is None:
            executable_name = (
                "market-validator.exe" if os.name == "nt" else "market-validator"
            )
            console_script = str(Path(sys.executable).with_name(executable_name))
        commands = (
            [
                sys.executable,
                "-m",
                "market_validator",
                "hypothesis",
                "clarification-schema",
            ],
            [console_script, "hypothesis", "clarification-schema"],
        )
        results = [
            subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            for command in commands
        ]
        self.assertTrue(all(item.returncode == 0 for item in results))
        self.assertEqual(json.loads(results[0].stdout), json.loads(results[1].stdout))
        self.assertTrue(all(item.stderr == "" for item in results))

    def test_validate_clarifications_is_read_only_and_reports_ready(self) -> None:
        before_proposal = OIL_PROPOSAL.read_bytes()
        before_answers = OIL_ANSWERS.read_bytes()
        exit_code, stdout, stderr = _invoke(
            [
                "hypothesis",
                "validate-clarifications",
                "--proposal",
                str(OIL_PROPOSAL),
                "--answers",
                str(OIL_ANSWERS),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(json.loads(stdout)["data"]["ready_for_spec_review"])
        self.assertEqual(OIL_PROPOSAL.read_bytes(), before_proposal)
        self.assertEqual(OIL_ANSWERS.read_bytes(), before_answers)

    def test_apply_creates_new_ready_proposal_without_overwriting_source(self) -> None:
        source_bytes = OIL_PROPOSAL.read_bytes()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "clarified.proposal.json"
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "apply-clarifications",
                    "--proposal",
                    str(OIL_PROPOSAL),
                    "--answers",
                    str(OIL_ANSWERS),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["data"]["ready_for_spec_review"])
            clarified = parse_research_hypothesis_proposal(output.read_bytes())
            self.assertTrue(clarified.ready_for_spec_review)
        self.assertEqual(OIL_PROPOSAL.read_bytes(), source_bytes)

    def test_not_ready_proposal_cannot_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "confirmation.json"
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "confirm-proposal",
                    "--proposal",
                    str(OIL_PROPOSAL),
                    "--output",
                    str(output),
                ]
            )
            self.assertFalse(output.exists())
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.HYPOTHESIS_PROPOSAL_NOT_READY),
        )
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["error"]["code"],
            "hypothesis_proposal_not_ready",
        )

    def test_ready_proposal_can_be_confirmed_but_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            proposal_path = Path(directory) / "proposal.json"
            confirmation_path = Path(directory) / "confirmation.json"
            _write_ready_proposal(proposal_path)
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "confirm-proposal",
                    "--proposal",
                    str(proposal_path),
                    "--output",
                    str(confirmation_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["data"]["confirmed"])
            confirmation = parse_research_hypothesis_confirmation(
                confirmation_path.read_bytes()
            )
            proposal = parse_research_hypothesis_proposal(proposal_path.read_bytes())
            self.assertEqual(
                confirmation.proposal_sha256,
                calculate_research_hypothesis_proposal_sha256(proposal),
            )
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["confirmation.json", "proposal.json"],
            )

    def test_compile_success_creates_strict_spec_and_provenance_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            proposal_path = Path(directory) / "proposal.json"
            confirmation_path = Path(directory) / "confirmation.json"
            spec_path = Path(directory) / "research-spec.json"
            _write_ready_proposal(proposal_path)
            self.assertEqual(
                _invoke(
                    [
                        "hypothesis",
                        "confirm-proposal",
                        "--proposal",
                        str(proposal_path),
                        "--output",
                        str(confirmation_path),
                    ]
                )[0],
                0,
            )
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "compile-research-spec",
                    "--proposal",
                    str(proposal_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--output",
                    str(spec_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["ok"])
            spec = parse_research_spec(spec_path.read_bytes())
            self.assertEqual(spec.claim_type.value, "association")
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                [
                    "confirmation.json",
                    "proposal.json",
                    "research-spec.json",
                    "research-spec.json.provenance.json",
                ],
            )

    def test_oil_compile_reports_unresolved_without_writing_spec(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            clarified_path = Path(directory) / "clarified.json"
            confirmation_path = Path(directory) / "confirmation.json"
            spec_path = Path(directory) / "research-spec.json"
            self.assertEqual(
                _invoke(
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
                )[0],
                0,
            )
            self.assertEqual(
                _invoke(
                    [
                        "hypothesis",
                        "confirm-proposal",
                        "--proposal",
                        str(clarified_path),
                        "--output",
                        str(confirmation_path),
                    ]
                )[0],
                0,
            )
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "compile-research-spec",
                    "--proposal",
                    str(clarified_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--output",
                    str(spec_path),
                ]
            )
            self.assertFalse(spec_path.exists())
            self.assertFalse(Path(str(spec_path) + ".provenance.json").exists())
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.RESEARCH_SPEC_UNRESOLVED),
        )
        self.assertEqual(stdout, "")
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "research_spec_unresolved")
        paths = {
            item["path"]
            for item in error["details"]["unresolved_requirements"]
        }
        self.assertIn("research_spec_inputs.variables", paths)
        self.assertNotIn("Traceback", stderr)

    def test_malformed_answers_have_stable_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            answers = Path(directory) / "answers.json"
            answers.write_text('{"answers":NaN}', encoding="utf-8")
            exit_code, stdout, stderr = _invoke(
                [
                    "hypothesis",
                    "validate-clarifications",
                    "--proposal",
                    str(OIL_PROPOSAL),
                    "--answers",
                    str(answers),
                ]
            )
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.INVALID_HYPOTHESIS_CLARIFICATIONS),
        )
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["error"]["code"],
            "invalid_hypothesis_clarifications",
        )
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
