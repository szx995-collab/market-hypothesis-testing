"""Subprocess tests for the ResearchSpec CLI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OIL_EXAMPLE = (
    REPOSITORY_ROOT
    / "examples"
    / "research_specs"
    / "oil_to_a_share_energy.json"
)
INVALID_EXAMPLE = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "invalid_research_spec.json"
)


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    return subprocess.run(
        [sys.executable, "-m", "market_validator", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class ResearchSpecCommandTest(unittest.TestCase):
    def test_valid_file_returns_zero_and_summary(self) -> None:
        completed = run_cli("spec", "validate", str(OIL_EXAMPLE))
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["spec_id"], "oil-to-a-share-energy-v1")
        self.assertEqual(payload["schema_version"], "1.0")

    def test_invalid_file_returns_two_and_structured_errors(self) -> None:
        completed = run_cli("spec", "validate", str(INVALID_EXAMPLE))
        rendered = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(rendered["valid"])
        self.assertEqual(rendered["errors"][0]["path"], "spec_id")
        self.assertEqual(rendered["errors"][0]["code"], "missing")
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_schema_command_returns_json_schema_without_stderr(self) -> None:
        completed = run_cli("spec", "schema")
        schema = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(schema["title"], "ResearchSpec")
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
