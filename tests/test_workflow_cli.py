"""Offline subprocess and error-mapping tests for the workflow CLI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import unittest
from unittest import mock

from market_validator.analysis.price_change_volatility import (
    PriceChangeVolatilityParameters,
    VolatilityConclusion,
)
from market_validator.cli import main
from market_validator.data.bundle_io import serialize_data_bundle
from market_validator.data.models import (
    DataBundle,
    DataQualityIssue,
    DataQualityReport,
    DataRequirement,
    DataSourceMetadata,
    Observation,
    QualitySeverity,
    QualityStatus,
    TimePrecision,
)
from market_validator.research.enums import DataRevisionMode
from market_validator.workflow import (
    MarketValidationWorkflowPlan,
    WorkflowErrorCode,
    WorkflowExecutionError,
    WorkflowFailure,
    WorkflowStage,
)
from market_validator.workflow_cli import (
    WORKFLOW_ERROR_EXIT_CODES,
    WorkflowCliExitCode,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_workflow_cli"
WTI_REQUEST_ID = "fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12"
WTI_BUNDLE_SHA256 = "94e89062727b475168dc5605bef4b523e6046786f83474bca0334652a7fc3c7b"
WTI_MANIFEST_SHA256 = "5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e"
WTI_ARTIFACT_ID = "price-change-volatility-0796a66788e5cfd71dff6a3222dd5cab"
WTI_RESULT_SHA256 = "0796a66788e5cfd71dff6a3222dd5cab6062ab73d7b5dd9e1ccfbe9bbdc12435"
WTI_REPORT_SHA256 = "c6e0d4a7d2c8e57c9da8985f904de8bcec6d14f1be35d8893ea296aeb277967b"
WTI_BUNDLE_PATH = (
    ROOT
    / ".market_validator"
    / "data"
    / "bundles"
    / "fred"
    / f"{WTI_REQUEST_ID}.json"
)
WTI_PLAN_PATH = (
    ROOT
    / "examples"
    / "workflow_plans"
    / "fred_wti_price_change_volatility.json"
)


def _console_script_path() -> Path:
    discovered = shutil.which("market-validator")
    if discovered:
        return Path(discovered)
    scripts = Path(sysconfig.get_path("scripts"))
    filename = "market-validator.exe" if os.name == "nt" else "market-validator"
    return scripts / filename


class WorkflowCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.bundle_path = self.root / "fred-cli-workflow-test.json"
        self.artifact_root = self.root / "artifacts"
        bundle_bytes = self._make_bundle_bytes()
        self.bundle_path.write_bytes(bundle_bytes)
        self.bundle_sha256 = self._sha_bytes(bundle_bytes)
        self.plan = MarketValidationWorkflowPlan(
            expected_source_request_id=self.bundle_path.stem,
            expected_source_bundle_sha256=self.bundle_sha256,
            parameters=PriceChangeVolatilityParameters(),
        )
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(
            self.plan.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    @staticmethod
    def _sha_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _sha_file(cls, path: Path) -> str:
        return cls._sha_bytes(path.read_bytes())

    @staticmethod
    def _observation(session_date: date, value: float) -> Observation:
        observation_time = datetime.combine(
            session_date, datetime.min.time(), tzinfo=timezone.utc
        )
        return Observation(
            instrument_id="global.crude_oil.wti_spot",
            field="value",
            value=value,
            observation_time=observation_time,
            available_time=observation_time + timedelta(hours=12),
            session_date=session_date,
            timezone="UTC",
            currency="USD",
            unit="Dollars per Barrel",
            observation_precision=TimePrecision.DATE,
            availability_precision=TimePrecision.TIMESTAMP,
            vintage_date=session_date,
            revision_policy=DataRevisionMode.INITIAL_RELEASE,
            availability_assumption="Synthetic offline CLI fixture.",
        )

    def _group(
        self,
        start_date: date,
        changes: list[float],
        *,
        initial_price: float,
    ) -> list[Observation]:
        observations = [self._observation(start_date, initial_price)]
        current_date = start_date
        current_price = initial_price
        for change in changes:
            current_date += timedelta(days=1)
            current_price += change
            observations.append(self._observation(current_date, current_price))
        return observations

    @staticmethod
    def _requirement(observations: list[Observation]) -> DataRequirement:
        payload = json.loads(
            (
                ROOT
                / "examples"
                / "data_requirements"
                / "fred_wti_spot_initial.json"
            ).read_text(encoding="utf-8")
        )
        payload["start_date"] = min(
            item.session_date for item in observations
        ).isoformat()
        payload["end_date"] = max(
            item.session_date for item in observations
        ).isoformat()
        return DataRequirement.model_validate_json(json.dumps(payload))

    def _make_bundle_bytes(self) -> bytes:
        changes = [1.0, -2.0, 3.0, -4.0, 5.0]
        shock = self._group(date(2020, 3, 1), changes, initial_price=100.0)
        reference = self._group(date(2021, 1, 1), changes, initial_price=200.0)
        observations = shock + reference
        missing_row = len(shock) + 2
        bundle = DataBundle(
            requirement=self._requirement(observations),
            observations=observations,
            source=DataSourceMetadata(
                provider_id="fred",
                dataset_id="DCOILWTICO",
                provider_symbol="DCOILWTICO",
                source_uri="https://api.stlouisfed.org/fred/series/observations",
                retrieved_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                public_request_parameters={"series_id": "DCOILWTICO"},
                content_sha256="b" * 64,
                license_note="Synthetic offline CLI fixture.",
                is_fallback=False,
            ),
            quality=DataQualityReport(
                status=QualityStatus.WARN,
                rows_read=len(observations) + 1,
                observations_parsed=len(observations),
                coverage_start=min(item.observation_time for item in observations),
                coverage_end=max(item.observation_time for item in observations),
                issues=[
                    DataQualityIssue(
                        code="provider_missing_value",
                        severity=QualitySeverity.WARNING,
                        message="Synthetic gap between CLI workflow groups.",
                        row_number=missing_row,
                    )
                ],
            ),
        )
        return serialize_data_bundle(bundle)

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        source_path = str(ROOT / "src")
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not existing_pythonpath
            else os.pathsep.join((source_path, existing_pythonpath))
        )
        return environment

    def _subprocess(
        self,
        command: list[str],
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *arguments],
            cwd=ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )

    def _module_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._subprocess(
            [sys.executable, "-m", "market_validator"],
            *arguments,
        )

    def _run_arguments(self) -> tuple[str, ...]:
        return (
            "run",
            "--plan",
            str(self.plan_path),
            "--bundle",
            str(self.bundle_path),
            "--artifact-root",
            str(self.artifact_root),
        )

    def test_module_and_console_script_entrypoints_match(self) -> None:
        console = _console_script_path()
        self.assertTrue(console.is_file(), f"console script not installed: {console}")
        module = self._module_cli("validate-plan", str(self.plan_path))
        installed = self._subprocess(
            [str(console)],
            "validate-plan",
            str(self.plan_path),
        )
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(module.stdout, installed.stdout)
        self.assertEqual(module.stderr, installed.stderr)

    def test_help_and_missing_arguments(self) -> None:
        help_result = self._module_cli("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("validate-plan", help_result.stdout)
        self.assertIn("verify-artifact", help_result.stdout)

        missing = self._module_cli("run")
        self.assertEqual(missing.returncode, WorkflowCliExitCode.CLI_INPUT_ERROR)
        self.assertEqual(missing.stdout, "")
        error = json.loads(missing.stderr)
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "cli_input_error")
        self.assertNotIn("Traceback", missing.stderr)

    def test_validate_plan_success_does_not_run_or_write_artifact(self) -> None:
        completed = self._module_cli("validate-plan", str(self.plan_path))
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["analysis_type"], "price_change_volatility")
        self.assertFalse(self.artifact_root.exists())

    def test_malformed_unknown_field_and_unknown_analysis_type(self) -> None:
        malformed = self.root / "malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        malformed_result = self._module_cli("validate-plan", str(malformed))
        self.assertEqual(
            malformed_result.returncode,
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
        self.assertEqual(json.loads(malformed_result.stderr)["error"]["code"], "cli_input_error")

        base = json.loads(self.plan_path.read_text("utf-8"))
        for name, mutation in (
            ("unknown-field", {"unexpected": True}),
            ("unknown-analysis", {"analysis_type": "unknown"}),
        ):
            candidate = dict(base)
            candidate.update(mutation)
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.subTest(name=name):
                result = self._module_cli("validate-plan", str(path))
                self.assertEqual(result.returncode, WorkflowCliExitCode.INVALID_PLAN)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"],
                    WorkflowErrorCode.INVALID_PLAN.value,
                )
                self.assertNotIn("Traceback", result.stderr)

    def test_nonregular_and_symbolic_plan_paths_are_rejected(self) -> None:
        directory_result = self._module_cli("validate-plan", str(self.root))
        self.assertEqual(
            directory_result.returncode,
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "market_validator.workflow_cli._path_is_symlink",
            return_value=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["validate-plan", str(self.plan_path)])
        self.assertEqual(code, WorkflowCliExitCode.CLI_INPUT_ERROR)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "cli_input_error")

    def test_complete_workflow_insufficient_evidence_is_success(self) -> None:
        completed = self._module_cli(*self._run_arguments())
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["workflow_status"], "completed")
        self.assertEqual(
            payload["data"]["final_conclusion"],
            VolatilityConclusion.INSUFFICIENT_EVIDENCE.value,
        )

    def test_all_workflow_failure_codes_have_stable_exit_mapping(self) -> None:
        for error_code, exit_code in WORKFLOW_ERROR_EXIT_CODES.items():
            with self.subTest(error_code=error_code):
                failure = WorkflowFailure(
                    code=error_code,
                    stage=WorkflowStage.ANALYSIS,
                    message="synthetic workflow failure",
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch(
                    "market_validator.workflow_cli.run_market_validation_workflow",
                    side_effect=WorkflowExecutionError(failure),
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    actual = main(list(self._run_arguments()))
                self.assertEqual(actual, exit_code)
                self.assertEqual(stdout.getvalue(), "")
                rendered = json.loads(stderr.getvalue())
                self.assertEqual(rendered["error"]["code"], error_code.value)
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_unexpected_internal_error_has_no_traceback(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "market_validator.workflow_cli.run_market_validation_workflow",
            side_effect=RuntimeError("sensitive implementation detail"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(self._run_arguments()))
        self.assertEqual(code, WorkflowCliExitCode.INTERNAL_ERROR)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("sensitive implementation detail", stderr.getvalue())

    def test_verify_artifact_success_and_wrong_external_hash(self) -> None:
        completed = self._module_cli(*self._run_arguments())
        data = json.loads(completed.stdout)["data"]
        verified = self._module_cli(
            "verify-artifact",
            "--artifact",
            data["artifact_path"],
            "--expected-manifest-sha256",
            data["manifest_sha256"],
        )
        verified_payload = json.loads(verified.stdout)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(verified.stderr, "")
        self.assertEqual(
            verified_payload["data"]["manifest"]["artifact_id"],
            data["artifact_id"],
        )

        rejected = self._module_cli(
            "verify-artifact",
            "--artifact",
            data["artifact_path"],
            "--expected-manifest-sha256",
            "0" * 64,
        )
        self.assertEqual(
            rejected.returncode,
            WorkflowCliExitCode.ARTIFACT_VERIFICATION_FAILED,
        )
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(
            json.loads(rejected.stderr)["error"]["code"],
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED.value,
        )
        self.assertNotIn("Traceback", rejected.stderr)

    def test_repeated_cli_run_preserves_artifact_bytes_hashes_and_mtimes(self) -> None:
        first = self._module_cli(*self._run_arguments())
        first_data = json.loads(first.stdout)["data"]
        artifact_path = Path(first_data["artifact_path"])
        names = ("result.json", "report.md", "manifest.json")
        before = {
            name: (
                (artifact_path / name).read_bytes(),
                (artifact_path / name).stat().st_mtime_ns,
            )
            for name in names
        }

        second = self._module_cli(*self._run_arguments())
        second_data = json.loads(second.stdout)["data"]
        after = {
            name: (
                (artifact_path / name).read_bytes(),
                (artifact_path / name).stat().st_mtime_ns,
            )
            for name in names
        }
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first_data["artifact_id"], second_data["artifact_id"])
        self.assertEqual(
            first_data["manifest_sha256"], second_data["manifest_sha256"]
        )
        self.assertEqual(before, after)

    @unittest.skipUnless(
        WTI_BUNDLE_PATH.exists() and WTI_PLAN_PATH.exists(),
        "local verified WTI snapshot absent",
    )
    def test_wti_golden_cli_and_source_files_remain_unchanged(self) -> None:
        source_paths = (
            WTI_BUNDLE_PATH,
            ROOT
            / ".market_validator"
            / "data"
            / "manifests"
            / "fred"
            / f"{WTI_REQUEST_ID}.json",
            ROOT
            / ".market_validator"
            / "data"
            / "raw"
            / "fred"
            / f"{WTI_REQUEST_ID}-series.json",
            ROOT
            / ".market_validator"
            / "data"
            / "raw"
            / "fred"
            / f"{WTI_REQUEST_ID}-observations-0000.json",
        )
        before = {path: self._sha_file(path) for path in source_paths}
        completed = self._module_cli(
            "run",
            "--plan",
            str(WTI_PLAN_PATH),
            "--bundle",
            str(WTI_BUNDLE_PATH),
            "--artifact-root",
            str(ROOT / ".market_validator" / "analysis_artifacts"),
        )
        after = {path: self._sha_file(path) for path in source_paths}
        data = json.loads(completed.stdout)["data"]

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(data["artifact_id"], WTI_ARTIFACT_ID)
        self.assertEqual(data["manifest_sha256"], WTI_MANIFEST_SHA256)
        self.assertEqual(data["files"]["result"]["sha256"], WTI_RESULT_SHA256)
        self.assertEqual(data["files"]["report"]["sha256"], WTI_REPORT_SHA256)
        self.assertEqual(data["final_conclusion"], "supported")
        self.assertEqual(len(data["result"]["errors"]), 0)
        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
