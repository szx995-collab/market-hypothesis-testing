"""Subprocess tests for JSON-only offline data commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    return subprocess.run(
        [sys.executable, "-m", "market_validator", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class DataCommandTest(unittest.TestCase):
    def test_registry_validate(self) -> None:
        completed = run_cli("data", "registry", "validate")
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["provider_mapping_count"], 4)

    def test_calendars_are_metadata_only(self) -> None:
        completed = run_cli("data", "calendars")
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(payload["schedule_calculation_implemented"])

    def test_local_csv_and_fred_providers_are_listed(self) -> None:
        completed = run_cli("data", "providers")
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(payload["providers"]), 2)
        by_id = {
            item["capabilities"]["provider_id"]: item
            for item in payload["providers"]
        }
        capabilities = by_id["local_csv"]["capabilities"]
        self.assertEqual(capabilities["provider_id"], "local_csv")
        self.assertFalse(capabilities["requires_authentication"])
        self.assertFalse(capabilities["requires_network"])
        self.assertTrue(capabilities["supports_local_files"])
        fred = by_id["fred"]
        self.assertTrue(fred["capabilities"]["requires_authentication"])
        self.assertTrue(fred["capabilities"]["requires_network"])
        self.assertFalse(fred["status"]["network_tested"])

    def test_both_examples_generate_data_plans(self) -> None:
        for filename in ("oil_to_a_share_energy.json", "nikkei_to_us_market.json"):
            with self.subTest(filename=filename):
                completed = run_cli(
                    "data", "plan", f"examples/research_specs/{filename}"
                )
                payload = json.loads(completed.stdout)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(len(payload["requirements"]), 2)
                self.assertTrue(payload["unresolved_instruments"])

    def test_valid_csv_returns_zero(self) -> None:
        completed = run_cli(
            "data", "csv", "inspect", "tests/fixtures/valid_market_data.csv"
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["quality"]["status"], "pass")

    def test_invalid_csv_returns_two_without_traceback(self) -> None:
        completed = run_cli(
            "data", "csv", "inspect", "tests/fixtures/invalid_market_data.csv"
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["quality"]["status"], "fail")
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
