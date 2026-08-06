"""Unified data-lifecycle CLI tests (v0.3.0 Phase 5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_KEY = "SENTINEL-LIFE-CYCLE-SECRET"


def run_cli(*arguments: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    if env_extra:
        environment.update(env_extra)
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
        timeout=60,
    )


class CliBoundaryTest(unittest.TestCase):
    def test_schema_readiness_is_1_1(self) -> None:
        completed = run_cli("data-lifecycle", "schema", "readiness-assessment")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "1.1")

    def test_schema_data_ready_is_1_1(self) -> None:
        completed = run_cli("data-lifecycle", "schema", "data-ready-manifest")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "1.1")

    def test_schema_acquisition_is_1_2(self) -> None:
        completed = run_cli("data-lifecycle", "schema", "acquisition-request")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "1.2")

    def test_validate_session_schedule_fixture(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "validate",
            "session-schedule",
            str(ROOT / "examples/v0.3_data_ready/fixtures/session-schedule.json"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(len(payload["sha256"]), 64)

    def test_validate_malformed_json_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            completed = run_cli(
                "data-lifecycle", "validate", "session-schedule", str(path)
            )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["valid"])

    def test_validate_duplicate_key_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            path.write_text(
                '{"schedule_id": "a", "schedule_id": "b"}', encoding="utf-8"
            )
            completed = run_cli(
                "data-lifecycle", "validate", "session-schedule", str(path)
            )
        self.assertEqual(completed.returncode, 2)

    def test_validate_unknown_field_exit_2(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "validate",
            "session-schedule",
            str(ROOT / "examples/v0.3_data_ready/fixtures/session-schedule.json"),
        )
        self.assertEqual(completed.returncode, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.json"
            payload = json.loads(
                (
                    ROOT
                    / "examples/v0.3_data_ready/fixtures/session-schedule.json"
                ).read_text(encoding="utf-8")
            )
            payload["unexpected_field"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            completed = run_cli(
                "data-lifecycle", "validate", "session-schedule", str(path)
            )
        self.assertEqual(completed.returncode, 2)

    def test_no_traceback_on_internal_errors(self) -> None:
        completed = run_cli("data-lifecycle", "schema", "does-not-exist")
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr + completed.stdout)

    def test_secret_never_leaks_in_output(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "schema",
            "source-selection",
            env_extra={"FRED_API_KEY": SENTINEL_KEY},
        )
        self.assertNotIn(SENTINEL_KEY, completed.stdout + completed.stderr)

    def test_module_and_console_equivalent(self) -> None:
        executable_name = "market-validator.exe" if os.name == "nt" else "market-validator"
        console_script = Path(sys.executable).with_name(executable_name)
        if not console_script.exists():
            self.skipTest("console entry point not installed")
        module_result = run_cli("data-lifecycle", "schema", "session-schedule")
        console_result = subprocess.run(
            [str(console_script), "data-lifecycle", "schema", "session-schedule"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(console_result.returncode, 0, console_result.stderr)
        self.assertEqual(
            json.loads(module_result.stdout),
            json.loads(console_result.stdout),
        )


class LifecycleEndToEndCliTest(unittest.TestCase):
    """End-to-end CLI over the synthetic example artifacts."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        import tempfile

        cls.tmp = tempfile.TemporaryDirectory(prefix="cli-e2e-")
        example_dir = ROOT / "examples/v0.3_data_ready"
        spec = importlib.util.spec_from_file_location(
            "v03_run_example", example_dir / "run_example.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.run_example(Path(cls.tmp.name) / "out")
        cls.out = Path(cls.tmp.name) / "out"
        cls.artifacts = cls.out / "artifacts"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _a(self, name: str) -> str:
        return str(self.artifacts / name)

    def test_authorization_create_rejects_network_flag_without_network(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "authorization",
            "create",
            "--acquisition-plan", self._a("acquisition-request-plan.json"),
            "--instrument-registry", self._a("instrument-registry.json"),
            "--calendar-registry", self._a("calendar-registry.json"),
            "--authorize-network",
            "--output", str(self.out / "auth-2.json"),
        )
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertFalse(
            json.loads(completed.stdout)["valid"],
        )

    def test_authorization_create_requires_explicit_local_flag(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "authorization",
            "create",
            "--acquisition-plan", self._a("acquisition-request-plan.json"),
            "--instrument-registry", self._a("instrument-registry.json"),
            "--calendar-registry", self._a("calendar-registry.json"),
            "--capability", self._a("provider-capability.json"),
            "--output", str(self.out / "auth-3.json"),
        )
        self.assertEqual(completed.returncode, 2, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["status"], "local_not_authorized")

    def test_authorization_create_with_explicit_local_flag_succeeds(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "authorization",
            "create",
            "--acquisition-plan", self._a("acquisition-request-plan.json"),
            "--instrument-registry", self._a("instrument-registry.json"),
            "--calendar-registry", self._a("calendar-registry.json"),
            "--capability", self._a("provider-capability.json"),
            "--authorize-local-file-read",
            "--output", str(self.out / "auth-4.json"),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["status"], "authorized")

    def test_execution_run_rejects_consumed_authorization_with_4(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "execution",
            "run",
            "--acquisition-plan", self._a("acquisition-request-plan.json"),
            "--authorization", self._a("data-access-authorization.json"),
            "--data-plan", self._a("data-plan.json"),
            "--instrument-registry", self._a("instrument-registry.json"),
            "--calendar-registry", self._a("calendar-registry.json"),
            "--capability", self._a("provider-capability.json"),
            "--attempt-id", "cli-e2e-attempt",
            "--receipt-output", self._a("authorization-receipt.json"),
            "--snapshot-root", str(self.out / "snapshots-2"),
        )
        self.assertEqual(completed.returncode, 4, completed.stdout)

    def test_readiness_verify_succeeds_on_example_artifacts(self) -> None:
        completed = run_cli(
            "data-lifecycle",
            "readiness",
            "verify",
            "--manifest", self._a("data-ready-manifest.json"),
            "--assessment", self._a("readiness-assessment.json"),
            "--acquisition-plan", self._a("acquisition-request-plan.json"),
            "--data-plan", self._a("data-plan.json"),
            "--instrument-registry", self._a("instrument-registry.json"),
            "--calendar-registry", self._a("calendar-registry.json"),
            "--source-selection", self._a("source-selection.json"),
            "--source-selection-confirmation",
            self._a("source-selection-confirmation.json"),
            "--snapshot", self._a("snapshot-manifest.json"),
            "--session-schedule", self._a("session-schedule.json"),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["status"], "verified")

    def test_readiness_verify_rejects_tampered_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Path(tmp) / "schedule.json"
            payload = json.loads(
                (self.artifacts / "session-schedule.json").read_text(
                    encoding="utf-8"
                )
            )
            payload["schedule_id"] = "tampered"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            completed = run_cli(
                "data-lifecycle",
                "readiness",
                "verify",
                "--manifest", self._a("data-ready-manifest.json"),
                "--assessment", self._a("readiness-assessment.json"),
                "--acquisition-plan", self._a("acquisition-request-plan.json"),
                "--data-plan", self._a("data-plan.json"),
                "--instrument-registry", self._a("instrument-registry.json"),
                "--calendar-registry", self._a("calendar-registry.json"),
                "--source-selection", self._a("source-selection.json"),
                "--source-selection-confirmation",
                self._a("source-selection-confirmation.json"),
                "--snapshot", self._a("snapshot-manifest.json"),
                "--session-schedule", str(tampered),
            )
        self.assertEqual(completed.returncode, 2, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["valid"])


class LegacyFredBoundaryTest(unittest.TestCase):
    def test_fetch_help_marks_legacy(self) -> None:
        completed = run_cli("data", "fred", "--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("legacy provider diagnostic", completed.stdout.lower())

    def test_dry_run_reports_formal_lifecycle_false(self) -> None:
        completed = run_cli(
            "data",
            "fred",
            "fetch",
            "examples/data_requirements/fred_wti_spot_initial.json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["formal_lifecycle"])
        self.assertFalse(payload["data_ready"])
        self.assertFalse(payload["authorization_artifact_enforced"])


if __name__ == "__main__":
    unittest.main()
