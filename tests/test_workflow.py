"""Offline end-to-end tests for the minimal deterministic workflow."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock

from market_validator.analysis.artifacts import (
    load_price_change_volatility_artifact,
)
from market_validator.analysis.price_change_volatility import (
    PriceChangeVolatilityAnalysisError,
    PriceChangeVolatilityParameters,
    VolatilityConclusion,
    compare_price_change_volatility,
)
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
    COMPLETED_STAGE_SEQUENCE,
    CompletedWorkflowRun,
    MarketValidationWorkflowPlan,
    WorkflowErrorCode,
    WorkflowExecutionError,
    run_market_validation_workflow,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_workflow"
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


class MarketValidationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.bundle_path = self.root / "fred-workflow-test.json"
        self.artifact_root = self.root / "artifacts"
        bundle_bytes = self._make_bundle_bytes()
        self.bundle_path.write_bytes(bundle_bytes)
        self.bundle_sha256 = self._sha_bytes(bundle_bytes)
        self.plan = MarketValidationWorkflowPlan(
            expected_source_request_id=self.bundle_path.stem,
            expected_source_bundle_sha256=self.bundle_sha256,
            parameters=PriceChangeVolatilityParameters(),
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
            availability_assumption="Synthetic offline workflow fixture.",
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
        shock = self._group(
            date(2020, 3, 1),
            [1.0, -2.0, 3.0, -4.0, 5.0],
            initial_price=100.0,
        )
        reference = self._group(
            date(2021, 1, 1),
            [0.5, -0.75, 1.0, -1.25, 1.5],
            initial_price=200.0,
        )
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
                license_note="Synthetic offline workflow fixture.",
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
                        message="Synthetic gap between workflow groups.",
                        row_number=missing_row,
                    )
                ],
            ),
        )
        return serialize_data_bundle(bundle)

    def _run(
        self,
        plan: MarketValidationWorkflowPlan | dict[str, object] | None = None,
        *,
        bundle_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> CompletedWorkflowRun:
        return run_market_validation_workflow(
            plan or self.plan,
            bundle_path or self.bundle_path,
            artifact_root or self.artifact_root,
        )

    def assert_workflow_error(
        self,
        code: WorkflowErrorCode,
        callable_object,
    ) -> WorkflowExecutionError:
        with self.assertRaises(WorkflowExecutionError) as context:
            callable_object()
        self.assertEqual(context.exception.failure.code, code)
        self.assertEqual(context.exception.failure.workflow_status, "failed")
        return context.exception

    def test_complete_compare_persist_strict_reload_chain(self) -> None:
        completed = self._run()

        self.assertEqual(completed.workflow_status, "completed")
        self.assertEqual(completed.completed_stages, COMPLETED_STAGE_SEQUENCE)
        self.assertEqual(completed.source_request_id, self.plan.expected_source_request_id)
        self.assertEqual(
            completed.source_bundle_sha256,
            self.plan.expected_source_bundle_sha256,
        )
        self.assertEqual(completed.result.parameters, self.plan.parameters)
        self.assertEqual(completed.final_conclusion, completed.result.final_conclusion)
        self.assertEqual(
            CompletedWorkflowRun.model_validate_json(completed.model_dump_json()),
            completed,
        )

    def test_plan_unknown_field_and_unknown_analysis_type_are_invalid_plan(self) -> None:
        base = json.loads(self.plan.model_dump_json())
        cases = []
        with_unknown = dict(base)
        with_unknown["unexpected"] = True
        cases.append(with_unknown)
        with_analysis = dict(base)
        with_analysis["analysis_type"] = "unknown_analysis"
        cases.append(with_analysis)

        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_workflow_error(
                    WorkflowErrorCode.INVALID_PLAN,
                    lambda payload=payload: self._run(payload),
                )
        self.assertFalse(self.artifact_root.exists())

    def test_request_id_and_bundle_sha_mismatch_stop_before_persistence(self) -> None:
        plans = (
            self.plan.model_copy(
                update={"expected_source_request_id": "different-request"}
            ),
            self.plan.model_copy(
                update={"expected_source_bundle_sha256": "c" * 64}
            ),
        )
        for index, plan in enumerate(plans):
            with self.subTest(plan=plan):
                artifact_root = self.root / f"mismatch-{index}"
                self.assert_workflow_error(
                    WorkflowErrorCode.SOURCE_IDENTITY_MISMATCH,
                    lambda plan=plan, artifact_root=artifact_root: self._run(
                        plan,
                        artifact_root=artifact_root,
                    ),
                )
                self.assertFalse(artifact_root.exists())

    def test_analysis_parameter_mismatch_stops_before_persistence(self) -> None:
        actual = compare_price_change_volatility(
            self.bundle_path,
            self.plan.parameters,
        )
        invalid_parameters = actual.parameters.model_copy(update={"random_seed": 1})
        mismatched = actual.model_copy(update={"parameters": invalid_parameters})
        with mock.patch(
            "market_validator.workflow.compare_price_change_volatility",
            return_value=mismatched,
        ):
            self.assert_workflow_error(
                WorkflowErrorCode.ANALYSIS_CONTRACT_MISMATCH,
                self._run,
            )
        self.assertFalse(self.artifact_root.exists())

    def test_analysis_failure_does_not_publish_success_artifact(self) -> None:
        with mock.patch(
            "market_validator.workflow.compare_price_change_volatility",
            side_effect=PriceChangeVolatilityAnalysisError("synthetic failure"),
        ):
            self.assert_workflow_error(
                WorkflowErrorCode.ANALYSIS_FAILED,
                self._run,
            )
        self.assertFalse(self.artifact_root.exists())

    def test_persistence_is_followed_by_strict_reload_with_manifest_hash(self) -> None:
        with mock.patch(
            "market_validator.workflow.load_price_change_volatility_artifact",
            wraps=load_price_change_volatility_artifact,
        ) as strict_reload:
            completed = self._run()

        strict_reload.assert_called_once_with(
            completed.artifact_path,
            expected_manifest_sha256=completed.manifest_sha256,
        )
        self.assertEqual(completed.files.manifest.sha256, completed.manifest_sha256)

    def test_strict_reload_result_mismatch_is_verification_failure(self) -> None:
        original_loader = load_price_change_volatility_artifact

        def changed_loader(*args, **kwargs):
            loaded = original_loader(*args, **kwargs)
            changed_source = loaded.result.source.model_copy(
                update={"request_id": "changed-after-load"}
            )
            changed_result = loaded.result.model_copy(update={"source": changed_source})
            return loaded.model_copy(update={"result": changed_result})

        with mock.patch(
            "market_validator.workflow.load_price_change_volatility_artifact",
            side_effect=changed_loader,
        ):
            self.assert_workflow_error(
                WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
                self._run,
            )

    def test_external_manifest_hash_accepts_match_and_rejects_mismatch(self) -> None:
        first = self._run()
        correct_plan = self.plan.model_copy(
            update={"expected_artifact_manifest_sha256": first.manifest_sha256}
        )
        repeated = self._run(correct_plan)
        self.assertEqual(repeated.manifest_sha256, first.manifest_sha256)

        wrong_plan = self.plan.model_copy(
            update={"expected_artifact_manifest_sha256": "0" * 64}
        )
        self.assert_workflow_error(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            lambda: self._run(wrong_plan),
        )

    def test_artifact_conflict_is_structured_and_does_not_overwrite(self) -> None:
        completed = self._run()
        report_path = completed.artifact_path / "report.md"
        tampered = report_path.read_bytes() + b"tampered"
        report_path.write_bytes(tampered)

        self.assert_workflow_error(WorkflowErrorCode.ARTIFACT_CONFLICT, self._run)
        self.assertEqual(report_path.read_bytes(), tampered)

    def test_overlapping_traversal_and_symlink_paths_are_rejected(self) -> None:
        self.assert_workflow_error(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            lambda: self._run(artifact_root=self.bundle_path.parent),
        )
        traversal = self.root / ".." / self.root.name / self.bundle_path.name
        self.assert_workflow_error(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            lambda: self._run(bundle_path=traversal),
        )
        with mock.patch(
            "market_validator.workflow._path_is_symlink",
            return_value=True,
        ):
            self.assert_workflow_error(
                WorkflowErrorCode.WORKFLOW_PATH_ERROR,
                self._run,
            )
        self.assertFalse(self.artifact_root.exists())

    def test_repeated_workflow_preserves_bytes_hashes_and_mtimes(self) -> None:
        first = self._run()
        names = ("result.json", "report.md", "manifest.json")
        before = {
            name: (
                (first.artifact_path / name).read_bytes(),
                (first.artifact_path / name).stat().st_mtime_ns,
            )
            for name in names
        }

        second = self._run()
        after = {
            name: (
                (second.artifact_path / name).read_bytes(),
                (second.artifact_path / name).stat().st_mtime_ns,
            )
            for name in names
        }
        self.assertEqual(second.artifact_id, first.artifact_id)
        self.assertEqual(second.manifest_sha256, first.manifest_sha256)
        self.assertEqual(after, before)

    @unittest.skipUnless(WTI_BUNDLE_PATH.exists(), "local verified WTI snapshot absent")
    def test_real_wti_golden_workflow_and_source_files_remain_unchanged(self) -> None:
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
        plan = MarketValidationWorkflowPlan(
            expected_source_request_id=WTI_REQUEST_ID,
            expected_source_bundle_sha256=WTI_BUNDLE_SHA256,
            parameters=PriceChangeVolatilityParameters(),
            expected_artifact_manifest_sha256=WTI_MANIFEST_SHA256,
        )

        completed = run_market_validation_workflow(
            plan,
            WTI_BUNDLE_PATH,
            ROOT / ".market_validator" / "analysis_artifacts",
        )
        after = {path: self._sha_file(path) for path in source_paths}

        self.assertEqual(completed.artifact_id, WTI_ARTIFACT_ID)
        self.assertEqual(completed.files.result.sha256, WTI_RESULT_SHA256)
        self.assertEqual(completed.files.report.sha256, WTI_REPORT_SHA256)
        self.assertEqual(completed.manifest_sha256, WTI_MANIFEST_SHA256)
        self.assertEqual(completed.final_conclusion, VolatilityConclusion.SUPPORTED)
        self.assertEqual(len(completed.result.errors), 0)
        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
