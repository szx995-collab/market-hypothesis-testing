"""Subprocess acceptance tests for offline-safe FRED CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_KEY = "k7" * 16


def run_cli(*arguments: str, api_key: str | None = None):
    environment = os.environ.copy()
    environment.pop("FRED_API_KEY", None)
    if api_key is not None:
        environment["FRED_API_KEY"] = api_key
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
        timeout=15,
    )


class FredCliTest(unittest.TestCase):
    def test_status_is_local_only_and_never_displays_key(self) -> None:
        completed = run_cli("data", "fred", "status", api_key=SENTINEL_KEY)
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["configured"])
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["network_tested"])
        self.assertNotIn(SENTINEL_KEY, completed.stdout + completed.stderr)

    def test_dry_run_succeeds_without_key_and_never_requests_network(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_wti_spot_initial.json",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_requested"])
        self.assertEqual(payload["series_id"], "DCOILWTICO")
        self.assertEqual(payload["public_parameters"]["output_type"], 4)
        self.assertEqual(
            payload["public_parameters"]["realtime_start"], "1776-07-04"
        )
        self.assertEqual(
            payload["public_parameters"]["realtime_end"], "9999-12-31"
        )

    def test_dry_run_with_key_still_does_not_expose_or_use_it(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_usd_broad_initial.json",
            api_key=SENTINEL_KEY,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(payload["network_requested"])
        self.assertEqual(payload["series_id"], "DTWEXBGS")
        self.assertNotIn(SENTINEL_KEY, completed.stdout + completed.stderr)

    def test_interactive_flag_without_live_remains_dry_and_opens_no_prompt(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_wti_spot_initial.json",
            "--interactive",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_requested"])

    def test_live_with_invalid_environment_key_fails_without_prompt_or_network(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_wti_spot_initial.json",
            "--live",
            "--interactive",
            api_key="invalid",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(payload["code"], "credential_invalid")
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_live_without_key_fails_before_network_with_structured_json(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_wti_spot_initial.json",
            "--live",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["code"], "credential_missing")
        self.assertEqual(payload["errors"][0]["code"], "credential_missing")
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertNotIn("environment", completed.stdout.casefold())

    def test_invalid_requirement_has_domain_exit_code_without_traceback(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/research_specs/oil_to_a_share_energy.json",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(payload["valid"])
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
