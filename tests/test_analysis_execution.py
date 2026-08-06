"""Authorized analysis execution tests (v0.4.0 Phase 2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from market_validator.analysis.authorization import (
    confirm_analysis_plan,
    create_analysis_authorization,
)
from market_validator.analysis.execution import (
    calculate_receipt_sha256,
    consume_analysis_authorization,
    execute_authorized_analysis,
    persist_analysis_authorization_receipt,
    validate_analysis_authorization_receipt_matches,
    verify_persisted_analysis_execution,
)
from market_validator.analysis.execution_models import (
    AnalysisExecutionError,
    AnalysisExecutionErrorCode,
)
from market_validator.analysis.planning import (
    METHOD_PROFILE_OLS_CLASSIC,
    METHOD_PROFILE_OLS_HC1,
    METHOD_PROFILE_OLS_NEWEY_WEST,
    METHOD_PROFILE_PEARSON,
    METHOD_PROFILE_SPEARMAN,
    TRANSFORMATION_PROFILE_LEVEL,
    TRANSFORMATION_PROFILE_LOG_RETURN,
    TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE,
    TRANSFORMATION_PROFILE_SIMPLE_RETURN,
    AnalysisPlanDecisions,
)
from market_validator.analysis.planning_generator import (
    generate_analysis_plan,
)
from market_validator.research.enums import (
    ModelMethod,
    Transformation,
)
from tests.test_analysis_planning import _decisions, _make_context
from tests.test_data_readiness import _make_schedule_snapshot

T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
TRANSFORMATION_PROFILES = {
    Transformation.LEVEL: TRANSFORMATION_PROFILE_LEVEL,
    Transformation.SIMPLE_RETURN: TRANSFORMATION_PROFILE_SIMPLE_RETURN,
    Transformation.LOG_RETURN: TRANSFORMATION_PROFILE_LOG_RETURN,
    Transformation.DIFFERENCE: TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE,
}


def _build_execution(
    tmp: Path,
    *,
    method: ModelMethod = ModelMethod.PEARSON_CORRELATION,
    method_profile: str = METHOD_PROFILE_PEARSON,
    outcome_transformation=None,
    predictor_transformation=None,
    decisions_overrides: dict | None = None,
    plan_minimum: int = 4,
    lag_periods: int = 0,
    confirm: bool = True,
):
    varying_values = [str(100.0 + index * 0.7) for index in range(9)]
    ctx = _make_context(
        tmp,
        method=method,
        outcome_transformation=outcome_transformation,
        predictor_transformation=predictor_transformation,
        minimum_observations=plan_minimum,
        observation_values=varying_values,
    )
    transformation_profiles = {}
    if outcome_transformation is not None:
        transformation_profiles[ctx["spec"].outcome.variable_id] = (
            TRANSFORMATION_PROFILES[outcome_transformation]
        )
    if predictor_transformation is not None:
        transformation_profiles[
            ctx["spec"].predictors[0].variable_id
        ] = TRANSFORMATION_PROFILES[predictor_transformation]
    overrides = dict(decisions_overrides or {})
    if lag_periods:
        base = _decisions(
            ctx["spec"],
            method_profile=method_profile,
            transformation_profiles=transformation_profiles,
        )
        overrides["transformation_decisions"] = {
            variable_id: decision.model_copy(
                update={
                    "lag_periods": lag_periods,
                    "availability_lag_periods": lag_periods,
                }
            )
            for variable_id, decision in base.transformation_decisions.items()
        }
    decisions = _decisions(
        ctx["spec"],
        method_profile=method_profile,
        transformation_profiles=transformation_profiles,
        **overrides,
    )
    generated = generate_analysis_plan(
        research_spec=ctx["spec"],
        readiness_assessment=ctx["assessment"],
        data_ready_manifest=ctx["manifest"],
        generated_plan=ctx["chain"]["plan"],
        data_plan=ctx["chain"]["data_plan"],
        instrument_registry=ctx["chain"]["instruments"],
        calendar_registry=ctx["chain"]["calendars"],
        decisions=decisions,
    )
    confirmation = None
    authorization = None
    if confirm:
        confirmation = confirm_analysis_plan(generated.analysis_plan, confirmed_at=T0)
        authorization = create_analysis_authorization(
            generated.analysis_plan, confirmation, authorized_at=T0
        )
    return {
        "ctx": ctx,
        "plan": generated.analysis_plan,
        "confirmation": confirmation,
        "authorization": authorization,
        "spec": ctx["spec"],
        "assessment": ctx["assessment"],
        "manifest": ctx["manifest"],
        "generated_plan": ctx["chain"]["plan"],
        "data_plan": ctx["chain"]["data_plan"],
        "instruments": ctx["chain"]["instruments"],
        "calendars": ctx["chain"]["calendars"],
        "schedule_snapshots": {
            _make_schedule_snapshot().schedule_adapter_id: (
                _make_schedule_snapshot()
            )
        },
        "snapshot_path": ctx["verified"].snapshot_path,
    }


def _execute(tmp: Path, *, method=ModelMethod.PEARSON_CORRELATION,
             method_profile=METHOD_PROFILE_PEARSON, **kwargs):
    build = _build_execution(
        tmp, method=method, method_profile=method_profile, **kwargs
    )
    receipt_path = Path(tmp) / "analysis-receipt.json"
    result_root = Path(tmp) / "results"
    _consumed_receipt, outcome = execute_authorized_analysis(
        plan=build["plan"],
        confirmation=build["confirmation"],
        authorization=build["authorization"],
        research_spec=build["spec"],
        readiness_assessment=build["assessment"],
        data_ready_manifest=build["manifest"],
        generated_plan=build["generated_plan"],
        data_plan=build["data_plan"],
        instrument_registry=build["instruments"],
        calendar_registry=build["calendars"],
        schedule_snapshots=build["schedule_snapshots"],
        snapshot_path=build["snapshot_path"],
        attempt_id="attempt-exec-1",
        receipt_path=receipt_path,
        result_root=result_root,
        consumed_at=T0,
        outcome_created_at=T0,
    )
    return build, outcome, receipt_path, result_root


class HappyPathTest(unittest.TestCase):
    def test_pearson_level_full_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, receipt_path, result_root = _execute(Path(tmp))
            self.assertEqual(outcome.status, "completed")
            self.assertIsNotNone(outcome.result_sha256)
            self.assertTrue(receipt_path.is_file())
            manifest_dir = result_root / "analysis-runs" / "attempt-exec-1"
            self.assertTrue((manifest_dir / "execution-manifest.json").is_file())
            self.assertTrue((manifest_dir / "analysis-result.json").is_file())
            self.assertTrue(
                (manifest_dir / "aligned-dataset.json").is_file()
            )
            transformed = manifest_dir / "transformed"
            self.assertTrue(any(transformed.glob("*.json")))
            verified_outcome = verify_persisted_analysis_execution(
                result_root, "attempt-exec-1"
            )
            self.assertEqual(verified_outcome.status, "completed")

    def test_spearman_ties_full_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(
                Path(tmp),
                method=ModelMethod.SPEARMAN_CORRELATION,
                method_profile=METHOD_PROFILE_SPEARMAN,
            )
            self.assertEqual(outcome.status, "completed")
            from market_validator.analysis.execution_models import (
                parse_analysis_result,
            )

            result = parse_analysis_result(
                (
                    Path(tmp)
                    / "results"
                    / "analysis-runs"
                    / "attempt-exec-1"
                    / "analysis-result.json"
                ).read_bytes()
            )
            self.assertEqual(result.method, "spearman_correlation")
            self.assertEqual(
                result.model_summary.tie_method, "average_rank_v1"
            )
            self.assertEqual(
                result.model_summary.inference_profile,
                "spearman_t_approximation_v1",
            )

    def test_ols_classic_hc1_newey_west(self):
        for profile in (
            METHOD_PROFILE_OLS_CLASSIC,
            METHOD_PROFILE_OLS_HC1,
            METHOD_PROFILE_OLS_NEWEY_WEST,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                build, outcome, _receipt, _root = _execute(
                    Path(tmp),
                    method=ModelMethod.OLS,
                    method_profile=profile,
                    decisions_overrides={
                        "newey_west_max_lags": (
                            2 if profile == METHOD_PROFILE_OLS_NEWEY_WEST
                            else None
                        )
                    },
                )
            self.assertEqual(outcome.status, "completed", profile)

    def test_simple_return_transformation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(
                Path(tmp),
                outcome_transformation=Transformation.SIMPLE_RETURN,
                predictor_transformation=Transformation.SIMPLE_RETURN,
            )
        self.assertEqual(outcome.status, "completed")

    def test_log_return_transformation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(
                Path(tmp),
                outcome_transformation=Transformation.LOG_RETURN,
                predictor_transformation=Transformation.LOG_RETURN,
            )
        self.assertEqual(outcome.status, "completed")

    def test_signed_difference_transformation(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(
                Path(tmp),
                outcome_transformation=Transformation.DIFFERENCE,
                predictor_transformation=Transformation.DIFFERENCE,
            )
        self.assertEqual(outcome.status, "completed")


class AlignmentSafetyTest(unittest.TestCase):
    def test_drop_policy_records_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(Path(tmp))
            from market_validator.analysis.execution_models import (
                parse_aligned_analysis_dataset,
            )

            dataset = parse_aligned_analysis_dataset(
                (
                    Path(tmp)
                    / "results"
                    / "analysis-runs"
                    / "attempt-exec-1"
                    / "aligned-dataset.json"
                ).read_bytes()
            )
            self.assertEqual(
                dataset.retained_row_count + dataset.dropped_row_count,
                dataset.candidate_row_count,
            )
            self.assertGreaterEqual(dataset.retained_row_count, 2)
            # every retained row is complete and auditable
            for row in dataset.rows:
                self.assertIn("source_session_dates", row.model_dump())
                self.assertIn("available_times", row.model_dump())
                self.assertEqual(
                    len(row.source_session_dates),
                    len(row.available_times),
                )

    def test_error_policy_fails_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(
                Path(tmp),
                decisions_overrides={
                    "same_market_missing_data_policy": "error_v1"
                },
                lag_periods=2,
            )
            with self.assertRaises(AnalysisExecutionError):
                execute_authorized_analysis(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    schedule_snapshots=build["schedule_snapshots"],
                    snapshot_path=build["snapshot_path"],
                    attempt_id="attempt-error",
                    receipt_path=Path(tmp) / "receipt-error.json",
                    result_root=Path(tmp) / "results-error",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )

    def test_keep_missing_stays_draft(self):
        # planner-level contract: keep_missing must never produce a ready plan
        from market_validator.analysis.planning_generator import (
            analysis_plan_readiness_blockers,
        )

        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(
                Path(tmp),
                decisions_overrides={
                    "same_market_missing_data_policy": "keep_missing_v1"
                },
                confirm=False,
            )
            blockers = analysis_plan_readiness_blockers(build["plan"])
            self.assertIn(
                "analysis_missing_policy_not_executable", blockers
            )

    def test_minimum_observations_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(Path(tmp), plan_minimum=10000)
            with self.assertRaises(AnalysisExecutionError) as caught:
                execute_authorized_analysis(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    schedule_snapshots=build["schedule_snapshots"],
                    snapshot_path=build["snapshot_path"],
                    attempt_id="attempt-min",
                    receipt_path=Path(tmp) / "receipt-min.json",
                    result_root=Path(tmp) / "results-min",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )
            self.assertEqual(
                caught.exception.code,
                AnalysisExecutionErrorCode.INSUFFICIENT_USABLE_OBSERVATIONS,
            )

    def test_session_lag_not_natural_days(self):
        # effective_shift consumes schedule sessions: with lag_periods=1 the
        # predictor source session must be the previous target session, not
        # the previous calendar day
        with tempfile.TemporaryDirectory() as tmp:
            build, outcome, _receipt, _root = _execute(
                Path(tmp), lag_periods=1
            )
            from market_validator.analysis.execution_models import (
                parse_aligned_analysis_dataset,
            )

            dataset = parse_aligned_analysis_dataset(
                (
                    Path(tmp)
                    / "results"
                    / "analysis-runs"
                    / "attempt-exec-1"
                    / "aligned-dataset.json"
                ).read_bytes()
            )
            from tests.test_data_readiness import _make_schedule_snapshot
            from market_validator.data.session_schedule import (
                ExplicitSessionScheduleAdapter,
            )

            adapter = ExplicitSessionScheduleAdapter(_make_schedule_snapshot())
            self.assertGreaterEqual(dataset.retained_row_count, 1)
            row = dataset.rows[0]
            predictor_id = next(
                binding.variable_id
                for binding in build["plan"].variable_bindings
                if binding.role == "predictor"
            )
            previous = adapter.previous_sessions(
                row.target_session_date, 2
            )[0]
            self.assertEqual(
                row.source_session_dates[predictor_id], previous
            )
            self.assertNotEqual(
                row.source_session_dates[predictor_id],
                row.target_session_date,
            )


class AuthorizationTest(unittest.TestCase):
    def test_receipt_persisted_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(Path(tmp))
            receipt_path = Path(tmp) / "analysis-receipt.json"
            receipt = consume_analysis_authorization(
                plan=build["plan"],
                confirmation=build["confirmation"],
                authorization=build["authorization"],
                research_spec=build["spec"],
                readiness_assessment=build["assessment"],
                data_ready_manifest=build["manifest"],
                generated_plan=build["generated_plan"],
                data_plan=build["data_plan"],
                instrument_registry=build["instruments"],
                calendar_registry=build["calendars"],
                attempt_id="attempt-receipt",
                receipt_path=receipt_path,
                consumed_at=T0,
            )
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(receipt.consumed, True)
            validate_analysis_authorization_receipt_matches(
                build["authorization"], receipt
            )

    def test_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _outcome, receipt_path, _root = _execute(Path(tmp))
            with self.assertRaises(AnalysisExecutionError) as caught:
                consume_analysis_authorization(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    attempt_id="attempt-reuse",
                    receipt_path=receipt_path,
                    consumed_at=T0,
                )
            self.assertEqual(
                caught.exception.code,
                AnalysisExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
            )

    def test_failed_execution_still_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(
                Path(tmp),
                decisions_overrides={
                    "same_market_missing_data_policy": "error_v1"
                },
            )
            receipt_path = Path(tmp) / "analysis-receipt.json"
            try:
                execute_authorized_analysis(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    schedule_snapshots=build["schedule_snapshots"],
                    snapshot_path=build["snapshot_path"],
                    attempt_id="attempt-fail",
                    receipt_path=receipt_path,
                    result_root=Path(tmp) / "results-fail",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )
            except AnalysisExecutionError:
                pass
            self.assertTrue(receipt_path.is_file(), "receipt persists on failure")

    def test_stale_plan_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(Path(tmp))
            stale = build["plan"].model_copy(
                update={"analysis_plan_id": "0" * 32}
            )
            with self.assertRaises(AnalysisExecutionError) as caught:
                consume_analysis_authorization(
                    plan=stale,
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    attempt_id="attempt-stale",
                    receipt_path=Path(tmp) / "receipt-stale.json",
                    consumed_at=T0,
                )
            self.assertEqual(
                caught.exception.code,
                AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            )


class PersistenceTest(unittest.TestCase):
    def test_tampering_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _outcome, _receipt, result_root = _execute(Path(tmp))
            result_path = (
                result_root
                / "analysis-runs"
                / "attempt-exec-1"
                / "analysis-result.json"
            )
            content = json.loads(result_path.read_text(encoding="utf-8"))
            content["warnings"] = ["tampered"]
            result_path.write_text(
                json.dumps(content), encoding="utf-8"
            )
            with self.assertRaises(AnalysisExecutionError) as caught:
                verify_persisted_analysis_execution(
                    result_root, "attempt-exec-1"
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
        )

    def test_output_conflict_on_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _outcome, _receipt, _root = _execute(Path(tmp))
            # a fresh authorization for the same attempt id must hit the
            # create-only result directory conflict
            (Path(tmp) / "second").mkdir(exist_ok=True)
            second = _build_execution(
                Path(tmp) / "second",
                method=ModelMethod.PEARSON_CORRELATION,
            )
            with self.assertRaises(AnalysisExecutionError) as caught:
                execute_authorized_analysis(
                    plan=second["plan"],
                    confirmation=second["confirmation"],
                    authorization=second["authorization"],
                    research_spec=second["spec"],
                    readiness_assessment=second["assessment"],
                    data_ready_manifest=second["manifest"],
                    generated_plan=second["generated_plan"],
                    data_plan=second["data_plan"],
                    instrument_registry=second["instruments"],
                    calendar_registry=second["calendars"],
                    schedule_snapshots=second["schedule_snapshots"],
                    snapshot_path=second["snapshot_path"],
                    attempt_id="attempt-exec-1",
                    receipt_path=Path(tmp) / "second-receipt.json",
                    result_root=Path(tmp) / "results",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )
            self.assertEqual(
                caught.exception.code,
                AnalysisExecutionErrorCode.ANALYSIS_OUTPUT_CONFLICT,
            )

    def test_traversal_attempt_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(Path(tmp))
            with self.assertRaises(AnalysisExecutionError) as caught:
                execute_authorized_analysis(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    schedule_snapshots=build["schedule_snapshots"],
                    snapshot_path=build["snapshot_path"],
                    attempt_id="../escape",
                    receipt_path=Path(tmp) / "traversal-receipt.json",
                    result_root=Path(tmp) / "results",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
        )

    def test_tampered_data_plan_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = _build_execution(Path(tmp))
            tampered = build["data_plan"].model_copy(
                update={
                    "requirements": [
                        requirement.model_copy(
                            update={"start_date": date(2019, 1, 1)}
                        )
                        for requirement in build["data_plan"].requirements
                    ]
                }
            )
            with self.assertRaises(AnalysisExecutionError) as caught:
                execute_authorized_analysis(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=tampered,
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    schedule_snapshots=build["schedule_snapshots"],
                    snapshot_path=build["snapshot_path"],
                    attempt_id="attempt-tampered-plan",
                    receipt_path=Path(tmp) / "tampered-receipt.json",
                    result_root=Path(tmp) / "results-tampered",
                    consumed_at=T0,
                    outcome_created_at=T0,
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
        )

    def test_authorization_keyed_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _outcome, _receipt, _root = _execute(Path(tmp))
            with self.assertRaises(AnalysisExecutionError) as caught:
                consume_analysis_authorization(
                    plan=build["plan"],
                    confirmation=build["confirmation"],
                    authorization=build["authorization"],
                    research_spec=build["spec"],
                    readiness_assessment=build["assessment"],
                    data_ready_manifest=build["manifest"],
                    generated_plan=build["generated_plan"],
                    data_plan=build["data_plan"],
                    instrument_registry=build["instruments"],
                    calendar_registry=build["calendars"],
                    attempt_id="attempt-other-path",
                    receipt_path=Path(tmp) / "other-receipt.json",
                    consumed_at=T0,
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
        )

    def test_no_provider_or_network_calls(self):
        import importlib

        source = importlib.import_module(
            "market_validator.analysis.execution"
        ).__file__
        text = Path(source).read_text(encoding="utf-8")
        for forbidden in (
            "fred_provider",
            "csv_provider",
            "credentials.resolver",
            "requests",
            "urllib",
            "http",
        ):
            self.assertNotIn(f"import {forbidden}", text)
            self.assertNotIn(f"from market_validator.{forbidden}", text)


if __name__ == "__main__":
    unittest.main()
