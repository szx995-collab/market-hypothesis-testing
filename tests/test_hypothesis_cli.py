"""Offline CLI tests for hypothesis schema, validation, and proposal generation."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from market_validator.cli import main
from market_validator.workflow_cli import WorkflowCliExitCode
from tests.test_hypothesis_service import FakeBackend, _proposal_data


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.proposal.json"
)
QUESTION_FILE = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.question.txt"
)
MODEL = "test-model"


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            exit_code = main(arguments)
        except SystemExit as exc:
            exit_code = int(exc.code)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class HypothesisCliTest(unittest.TestCase):
    def test_schema_is_offline_json_and_both_entry_points_match(self) -> None:
        module = subprocess.run(
            [sys.executable, "-m", "market_validator", "hypothesis", "schema"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        script = subprocess.run(
            ["market-validator", "hypothesis", "schema"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(module.returncode, 0)
        self.assertEqual(script.returncode, 0)
        self.assertEqual(json.loads(module.stdout), json.loads(script.stdout))
        self.assertEqual(module.stderr, "")
        self.assertEqual(script.stderr, "")

    def test_validate_proposal_outputs_summary_without_confirmation(self) -> None:
        exit_code, stdout, stderr = _invoke(
            ["hypothesis", "validate-proposal", str(EXAMPLE)]
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["ready_for_spec_review"])
        self.assertEqual(len(payload["data"]["proposal_sha256"]), 64)
        self.assertNotIn("confirmation", payload["data"])
        self.assertEqual(stderr, "")

    def test_invalid_proposal_has_stable_json_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text('{"unknown":true}', encoding="utf-8")
            exit_code, stdout, stderr = _invoke(
                ["hypothesis", "validate-proposal", str(invalid)]
            )
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.INVALID_HYPOTHESIS_PROPOSAL),
        )
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["error"]["code"], "invalid_hypothesis_proposal")
        self.assertNotIn("Traceback", stderr)

    def test_propose_requires_explicit_network_authorization(self) -> None:
        exit_code, stdout, stderr = _invoke(
            [
                "propose-hypothesis",
                "--question-file",
                str(QUESTION_FILE),
                "--provider",
                "deepseek_api",
                "--model",
                MODEL,
                "--output",
                "proposal.json",
            ]
        )
        self.assertEqual(exit_code, int(WorkflowCliExitCode.CLI_INPUT_ERROR))
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "cli_input_error")

    def test_missing_key_fails_before_transport_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            with patch.dict(os.environ, {}, clear=True):
                exit_code, stdout, stderr = _invoke(
                    [
                        "propose-hypothesis",
                        "--question-file",
                        str(QUESTION_FILE),
                        "--provider",
                        "deepseek_api",
                        "--model",
                        MODEL,
                        "--output",
                        str(output),
                        "--allow-network",
                    ]
                )
            self.assertFalse(output.exists())
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.PROVIDER_CONFIGURATION_MISSING),
        )
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["error"]["code"],
            "provider_configuration_missing",
        )

    def test_fake_provider_creates_only_one_canonical_proposal(self) -> None:
        fake = FakeBackend(
            _proposal_data(),
            name="deepseek_api",
            model=MODEL,
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            with patch(
                "market_validator.hypothesis_cli.DeepSeekApiBackend",
                return_value=fake,
            ):
                exit_code, stdout, stderr = _invoke(
                    [
                        "propose-hypothesis",
                        "--question-file",
                        str(QUESTION_FILE),
                        "--provider",
                        "deepseek_api",
                        "--model",
                        MODEL,
                        "--output",
                        str(output),
                        "--allow-network",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue(json.loads(stdout)["ok"])
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["proposal.json"])
            self.assertEqual(len(fake.requests), 1)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("confirmed", persisted)
            self.assertNotIn("request_id", persisted)
            self.assertNotIn("bundle", json.dumps(persisted).casefold())

    def test_existing_output_conflict_stops_before_backend_creation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            output.write_text("existing", encoding="utf-8")
            with patch(
                "market_validator.hypothesis_cli.DeepSeekApiBackend"
            ) as constructor:
                exit_code, stdout, stderr = _invoke(
                    [
                        "propose-hypothesis",
                        "--question-file",
                        str(QUESTION_FILE),
                        "--provider",
                        "deepseek_api",
                        "--model",
                        MODEL,
                        "--output",
                        str(output),
                        "--allow-network",
                    ]
                )
            constructor.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "existing")
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.HYPOTHESIS_OUTPUT_CONFLICT),
        )
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["error"]["code"],
            "hypothesis_output_conflict",
        )

    def test_provider_invalid_output_is_not_written(self) -> None:
        payload = _proposal_data()
        payload["unknown"] = True
        fake = FakeBackend(payload, name="deepseek_api", model=MODEL)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "proposal.json"
            with patch(
                "market_validator.hypothesis_cli.DeepSeekApiBackend",
                return_value=fake,
            ):
                exit_code, stdout, stderr = _invoke(
                    [
                        "propose-hypothesis",
                        "--question-file",
                        str(QUESTION_FILE),
                        "--provider",
                        "deepseek_api",
                        "--model",
                        MODEL,
                        "--output",
                        str(output),
                        "--allow-network",
                    ]
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(
            exit_code,
            int(WorkflowCliExitCode.INVALID_HYPOTHESIS_PROPOSAL),
        )
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
