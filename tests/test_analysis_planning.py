"""Analysis planning contract tests (v0.4.0 Phase 1, A-G/J-L)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_validator.analysis.planning import (
    AnalysisPlan,
    AnalysisPlanDecisions,
    AnalysisPlanningError,
    AnalysisPlanningErrorCode,
    METHOD_PROFILE_OLS_CLASSIC,
    METHOD_PROFILE_OLS_HC1,
    METHOD_PROFILE_OLS_NEWEY_WEST,
    METHOD_PROFILE_PEARSON,
    METHOD_PROFILE_SPEARMAN,
    TRANSFORMATION_PROFILE_LEVEL,
    UnresolvedAnalysisRequirementCode,
    analysis_plan_schema,
    calculate_analysis_plan_sha256,
    parse_analysis_plan,
    serialize_analysis_plan,
)
from market_validator.analysis.planning_generator import (
    analysis_plan_readiness_blockers,
    generate_analysis_plan,
    persist_analysis_plan,
    validate_analysis_plan,
)
from market_validator.research.enums import ModelMethod
from market_validator.research.models import ResearchSpec
from market_validator.research.serialization import (
    calculate_research_spec_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_context(
    tmp: Path,
    *,
    method: ModelMethod = ModelMethod.PEARSON_CORRELATION,
    outcome_transformation=None,
    predictor_transformation=None,
    minimum_observations=None,
    observation_values=None,
):
    """Build a verified Data Ready chain plus a spec, returning inputs."""
    from tests.test_data_readiness import (
        _calendar_registry_with_adapter,
        _fred_registry,
        _ready_snapshot,
        _assess,
        _make_schedule_snapshot,
    )
    from market_validator.data.data_plan_review import (
        confirm_data_plan,
        generate_data_plan,
    )
    from market_validator.research.serialization import parse_research_spec

    from tests.test_data_readiness import _short_window_spec
    from market_validator.research.enums import Transformation as _T

    base_spec = _short_window_spec()
    transformed_needs_pre_sample = any(
        transformation
        in (
            _T.SIMPLE_RETURN,
            _T.LOG_RETURN,
            _T.DIFFERENCE,
        )
        for transformation in (
            outcome_transformation,
            predictor_transformation,
        )
        if transformation is not None
    )
    verified, chain, _transport = _ready_snapshot(
        tmp,
        pre_sample_periods=1 if transformed_needs_pre_sample else 0,
        values=observation_values,
    )

    spec = _short_window_spec()
    needs_rebuild = method is not ModelMethod.PEARSON_CORRELATION or (
        outcome_transformation is not None
        or predictor_transformation is not None
        or minimum_observations is not None
    )
    if needs_rebuild:
        updates = {}
        if method is not ModelMethod.PEARSON_CORRELATION:
            updates["model"] = spec.model.model_copy(
                update={"method": method}
            )
        outcome = spec.outcome
        if outcome_transformation is not None:
            outcome = outcome.model_copy(
                update={"transformation": outcome_transformation}
            )
        predictors = spec.predictors
        if predictor_transformation is not None:
            predictors = [
                predictor.model_copy(
                    update={
                        "transformation": predictor_transformation
                    }
                )
                for predictor in predictors
            ]
        updates["outcome"] = outcome
        updates["predictors"] = predictors
        if minimum_observations is not None:
            updates["sample"] = spec.sample.model_copy(
                update={"minimum_observations": minimum_observations}
            )
        spec = spec.model_copy(update=updates)
        # ResearchSpec is immutable once bound; rebuild the chain for a
        # different method so the manifest binds the new spec hash.
        calendars = chain["calendars"]
        instruments = chain["instruments"]
        generated = generate_data_plan(spec, instruments, calendars)
        confirmation = confirm_data_plan(
            generated, instruments, confirmed_at=datetime(
                2026, 8, 6, tzinfo=timezone.utc
            )
        )
        from market_validator.data.source_selection import (
            SourceSelectionDecision,
            confirm_source_selection,
            generate_source_selection,
        )

        decisions = []
        for requirement in generated.data_plan.requirements:
            mapping = instruments.get(requirement.instrument_id).provider_mappings[0]
            decisions.append(
                SourceSelectionDecision(
                    requirement_id=requirement.requirement_id,
                    provider_id=mapping.provider_id,
                    provider_symbol=mapping.provider_symbol,
                    dataset_or_endpoint=mapping.dataset_or_endpoint,
                )
            )
        selection = generate_source_selection(
            generated, confirmation, instruments, calendars, decisions
        )
        selection_confirmation = confirm_source_selection(
            selection,
            instruments,
            calendars,
            confirmed_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        )
        from market_validator.data.acquisition_request import (
            generate_acquisition_request_plan,
        )

        plan = generate_acquisition_request_plan(
            selection,
            selection_confirmation,
            generated.data_plan,
            instruments,
            calendars,
            chain["capabilities"],
            session_adapters={
                _make_schedule_snapshot().schedule_adapter_id: (
                    _make_session_adapter_callback()
                )
            },
        )
        from market_validator.data.access_authorization import (
            create_data_access_authorization,
        )

        authorization = create_data_access_authorization(
            plan,
            instruments,
            calendars,
            chain["capabilities"],
            [r.requirement_id for r in plan.acquisition_request_plan.requests],
            authorized_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        )
        from market_validator.data.execution import (
            execute_authorized_acquisition,
        )

        (tmp / "snapshots2").mkdir(exist_ok=True)
        verified = execute_authorized_acquisition(
            generated_plan=plan,
            authorization=authorization,
            data_plan=generated.data_plan,
            instrument_registry=instruments,
            calendar_registry=calendars,
            capability_snapshots=chain["capabilities"],
            attempt_id="attempt-method",
            receipt_path=tmp / "receipt-method.json",
            snapshot_root=tmp / "snapshots2",
            adapters=chain["adapters"],
        )
        chain = {
            "plan": plan,
            "data_plan": generated.data_plan,
            "instruments": instruments,
            "calendars": calendars,
            "capabilities": chain["capabilities"],
            "adapters": chain["adapters"],
            "spec": spec,
        }
    assessment = _assess(tmp, verified, chain)
    from market_validator.data.readiness import create_data_ready_manifest

    manifest = create_data_ready_manifest(
        assessment=assessment,
        generated_plan=chain["plan"],
    )
    return {
        "spec": spec,
        "assessment": assessment,
        "manifest": manifest.data_ready_manifest,
        "generated_manifest": manifest,
        "chain": chain,
        "verified": verified,
    }


def _make_session_adapter_callback():
    from tests.test_data_readiness import _make_schedule_snapshot
    from market_validator.data.session_schedule import (
        ExplicitSessionScheduleAdapter,
    )

    schedule = _make_schedule_snapshot()
    adapter = ExplicitSessionScheduleAdapter(schedule)
    return lambda start, periods: adapter.previous_sessions(
        start, periods
    )[-1]


def _decisions(
    spec,
    *,
    method_profile=METHOD_PROFILE_PEARSON,
    transformation_profiles=None,
    **overrides,
):
    variables = [spec.outcome] + spec.predictors + spec.controls
    transformation_profiles = transformation_profiles or {}
    transformation_decisions = {
        variable.variable_id: _transformation_decision(
            variable, profile=transformation_profiles.get(
                variable.variable_id, TRANSFORMATION_PROFILE_LEVEL
            )
        )
        for variable in variables
    }
    is_correlation = method_profile in (
        METHOD_PROFILE_PEARSON,
        METHOD_PROFILE_SPEARMAN,
    )
    payload = dict(
        method_profile=method_profile,
        primary_test_variable_ids=(
            [spec.predictors[0].variable_id]
            if is_correlation
            else (
                [variable.variable_id for variable in spec.predictors]
                if method_profile is not None
                else None
            )
        ),
        include_intercept=(
            None if is_correlation or method_profile is None else True
        ),
        covariance_estimator=(
            None
            if is_correlation or method_profile is None
            else {"ols_classic_v1": "classic", "ols_hc1_v1": "hc1", "ols_newey_west_v1": "newey_west"}[method_profile]
        ),
        newey_west_max_lags=(
            2 if method_profile == METHOD_PROFILE_OLS_NEWEY_WEST else None
        ),
        same_market_join_policy="strict_same_session_v1",
        same_market_missing_data_policy="drop_observation_v1",
        transformation_decisions=transformation_decisions,
    )
    payload.update(overrides)
    return AnalysisPlanDecisions(**payload)


def _transformation_decision(variable, *, profile=TRANSFORMATION_PROFILE_LEVEL):
    from market_validator.analysis.planning import TransformationDecision

    return TransformationDecision(
        variable_id=variable.variable_id,
        profile=profile,
        lag_periods=variable.lag_periods,
        availability_lag_periods=variable.availability_lag_periods,
        required_pre_sample_periods=0,
        rolling_window_periods=None,
    )


class HappyPathTest(unittest.TestCase):
    def test_pearson_plan_ready_confirmable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        plan = generated.analysis_plan
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.method, "pearson_correlation")
        self.assertEqual(plan.method_profile, METHOD_PROFILE_PEARSON)
        self.assertFalse(plan.unresolved_requirements)
        self.assertEqual(len(plan.variable_bindings), 2)
        self.assertEqual(len(plan.primary_tests), 1)
        self.assertEqual(plan.primary_tests[0].parameter, "pearson_r")
        self.assertEqual(plan.alignment_plan.join_policy, "strict_match")
        self.assertEqual(plan.alignment_plan.no_lookahead, True)
        self.assertEqual(plan.alignment_plan.max_staleness_days, 0)
        validate_analysis_plan(plan)
        self.assertFalse(analysis_plan_readiness_blockers(plan))
        # bindings carry complete session evidence
        for binding in plan.variable_bindings:
            self.assertEqual(len(binding.session_schedule_sha256), 64)
            self.assertEqual(
                len(binding.expected_sample_sessions_sha256), 64
            )
            self.assertEqual(
                len(binding.observed_pre_sample_sessions_sha256), 64
            )

    def test_spearman_plan_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(
                Path(tmp), method=ModelMethod.SPEARMAN_CORRELATION
            )
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(
                    ctx["spec"], method_profile=METHOD_PROFILE_SPEARMAN
                ),
            )
        self.assertEqual(generated.analysis_plan.status, "ready")
        self.assertEqual(
            generated.analysis_plan.primary_tests[0].parameter,
            "spearman_rho",
        )

    def test_ols_profiles_ready(self):
        for profile, covariance in (
            (METHOD_PROFILE_OLS_CLASSIC, "classic"),
            (METHOD_PROFILE_OLS_HC1, "hc1"),
            (METHOD_PROFILE_OLS_NEWEY_WEST, "newey_west"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                ctx = _make_context(Path(tmp), method=ModelMethod.OLS)
                generated = generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=ctx["manifest"],
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(
                        ctx["spec"], method_profile=profile
                    ),
                )
            plan = generated.analysis_plan
            self.assertEqual(plan.status, "ready", profile)
            self.assertEqual(plan.method_profile, profile)
            self.assertEqual(plan.primary_tests[0].parameter, "ols_coefficient")


class UnsupportedScopeTest(unittest.TestCase):
    def _run_with(self, tmp, **mutations):
        ctx = _make_context(Path(tmp))
        spec = ctx["spec"].model_copy(
            update={
                "model": ctx["spec"].model.model_copy(update=mutations)
            }
        ) if mutations.get("method") or mutations.get("claim_type") is None else ctx["spec"]
        return ctx, spec

    def test_predictive_claim_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            from market_validator.research.enums import ClaimType

            spec = ctx["spec"].model_copy(update={"claim_type": ClaimType.PREDICTIVE})
            generated = generate_analysis_plan(
                research_spec=spec,
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        plan = generated.analysis_plan
        self.assertEqual(plan.status, "draft")
        codes = {item.code for item in plan.unresolved_requirements}
        self.assertIn(
            UnresolvedAnalysisRequirementCode.UNSUPPORTED_CLAIM_TYPE, codes
        )

    def test_lead_lag_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            spec = ctx["spec"].model_copy(
                update={
                    "model": ctx["spec"].model.model_copy(
                        update={"method": ModelMethod.LEAD_LAG_REGRESSION}
                    )
                }
            )
            generated = generate_analysis_plan(
                research_spec=spec,
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        self.assertEqual(generated.analysis_plan.status, "draft")
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(
            UnresolvedAnalysisRequirementCode.UNSUPPORTED_ANALYSIS_METHOD,
            codes,
        )

    def test_pct_change_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            from market_validator.research.enums import Transformation

            spec = ctx["spec"].model_copy(
                update={
                    "outcome": ctx["spec"].outcome.model_copy(
                        update={"transformation": Transformation.PCT_CHANGE}
                    )
                }
            )
            generated = generate_analysis_plan(
                research_spec=spec,
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        self.assertEqual(generated.analysis_plan.status, "draft")
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(
            UnresolvedAnalysisRequirementCode.TRANSFORMATION_CONTRACT_UNRESOLVED,
            codes,
        )

    def test_rolling_mean_and_zscore_draft(self):
        from market_validator.research.enums import Transformation

        for transformation in (Transformation.ROLLING_MEAN, Transformation.ZSCORE):
            with tempfile.TemporaryDirectory() as tmp:
                ctx = _make_context(Path(tmp))
                spec = ctx["spec"].model_copy(
                    update={
                        "outcome": ctx["spec"].outcome.model_copy(
                            update={
                                "transformation": transformation,
                                "rolling_window_periods": (
                                    5
                                    if transformation
                                    in (
                                        Transformation.ROLLING_MEAN,
                                        Transformation.ZSCORE,
                                    )
                                    else None
                                ),
                            }
                        )
                    }
                )
                generated = generate_analysis_plan(
                    research_spec=spec,
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=ctx["manifest"],
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )
            self.assertEqual(generated.analysis_plan.status, "draft")


class DecisionsTest(unittest.TestCase):
    def test_missing_method_profile_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"], method_profile=None),
            )
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING, codes)
        self.assertEqual(generated.analysis_plan.status, "draft")

    def test_method_profile_mismatch_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(
                    ctx["spec"], method_profile=METHOD_PROFILE_OLS_CLASSIC
                ),
            )
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.METHOD_PROFILE_MISMATCH, codes)

    def test_automatic_primary_predictor_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"], primary_test_variable_ids=None),
            )
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.PRIMARY_TEST_VARIABLE_INVALID, codes)

    def test_pearson_with_intercept_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"], include_intercept=True),
            )
        self.assertEqual(generated.analysis_plan.status, "draft")

    def test_newey_west_lag_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp), method=ModelMethod.OLS)
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(
                    ctx["spec"],
                    method_profile=METHOD_PROFILE_OLS_NEWEY_WEST,
                    newey_west_max_lags=None,
                ),
            )
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.NEWEY_WEST_LAG_MISSING, codes)

    def test_classic_with_hac_lag_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp), method=ModelMethod.OLS)
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(
                    ctx["spec"],
                    method_profile=METHOD_PROFILE_OLS_CLASSIC,
                    newey_west_max_lags=2,
                ),
            )
        self.assertEqual(generated.analysis_plan.status, "draft")

    def test_unknown_decision_variable_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            decisions = _decisions(ctx["spec"])
            extra = _transformation_decision(
                ctx["spec"].predictors[0]
            ).model_copy(update={"variable_id": "unknown.decision.variable"})
            decisions = decisions.model_copy(
                update={
                    "transformation_decisions": {
                        **decisions.transformation_decisions,
                        "unknown.decision.variable": extra,
                    }
                }
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
        self.assertEqual(generated.analysis_plan.status, "draft")
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING, codes)

    def test_unknown_decision_field_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            decisions = _decisions(ctx["spec"])
            payload = json.loads(
                decisions.model_dump_json().encode("utf-8")
            )
            payload["unexpected_decision"] = 1
            from market_validator.analysis.planning import (
                AnalysisPlanDecisions,
            )

            with self.assertRaises(Exception):
                AnalysisPlanDecisions.model_validate_json(
                    json.dumps(payload).encode("utf-8")
                )


class AlignmentTest(unittest.TestCase):
    def test_spec_alignment_preserved_field_by_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            from market_validator.research.models import (
                AlignmentSpec,
                InformationCutoffSpec,
            )
            from market_validator.research.enums import (
                InformationCutoffType,
                JoinPolicy,
                MissingDataPolicy,
                TargetSession,
            )

            alignment = AlignmentSpec(
                target_market="Synthetic Test Market",
                target_timezone="UTC",
                target_calendar="synthetic.test.equity",
                target_session=TargetSession.REGULAR_SESSION,
                information_cutoff=InformationCutoffSpec(
                    type=InformationCutoffType.BEFORE_TARGET_OPEN,
                    local_time=None,
                    timezone=None,
                ),
                join_policy=JoinPolicy.STRICT_MATCH,
                max_staleness_days=0,
                missing_data_policy=MissingDataPolicy.DROP_OBSERVATION,
            )
            spec = ctx["spec"].model_copy(update={"alignment": alignment})
            generated = generate_analysis_plan(
                research_spec=spec,
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        plan = generated.analysis_plan
        # the changed spec invalidates the manifest binding (draft), but the
        # alignment contract must still be preserved field by field
        self.assertEqual(plan.status, "draft")
        self.assertIn(
            UnresolvedAnalysisRequirementCode.RESEARCH_SPEC_MISMATCH,
            {item.code for item in plan.unresolved_requirements},
        )
        self.assertIsNotNone(plan.alignment_plan)
        self.assertEqual(plan.alignment_plan.target_calendar_id, "synthetic.test.equity")
        self.assertEqual(plan.alignment_plan.join_policy, "strict_match")
        self.assertEqual(plan.alignment_plan.max_staleness_days, 0)
        self.assertEqual(plan.alignment_plan.missing_data_policy, "drop_observation")
        self.assertEqual(plan.alignment_plan.information_cutoff["type"], "before_target_open")

    def test_alignment_null_without_decisions_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(
                    ctx["spec"], same_market_join_policy=None
                ),
            )
        self.assertEqual(generated.analysis_plan.status, "draft")
        codes = {
            item.code
            for item in generated.analysis_plan.unresolved_requirements
        }
        self.assertIn(UnresolvedAnalysisRequirementCode.ALIGNMENT_CONTRACT_MISSING, codes)

    def test_explicit_strict_same_session_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
        self.assertEqual(generated.analysis_plan.status, "ready")
        self.assertEqual(
            generated.analysis_plan.alignment_plan.alignment_profile,
            "strict_same_session_v1",
        )


class BindingsTest(unittest.TestCase):
    def test_hash_changes_invalidate_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            base = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            changed = ctx["manifest"].model_copy(
                update={
                    "bundles": [
                        record.model_copy(
                            update={"bundle_sha256": "0" * 64}
                        )
                        if idx == 0
                        else record
                        for idx, record in enumerate(ctx["manifest"].bundles)
                    ]
                }
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=changed,
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )
        self.assertIsNotNone(base.analysis_plan_sha256)

    def test_missing_variable_bundle_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            manifest = ctx["manifest"].model_copy(
                update={
                    "bundles": [
                        record
                        for record in ctx["manifest"].bundles
                        if record.variable_id
                        != ctx["spec"].predictors[0].variable_id
                    ]
                }
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=manifest,
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )

    def test_unknown_bundle_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            extra = ctx["manifest"].bundles[0].model_copy(
                update={"variable_id": "unknown.variable"}
            )
            manifest = ctx["manifest"].model_copy(
                update={"bundles": list(ctx["manifest"].bundles) + [extra]}
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=manifest,
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )

    def test_duplicate_bundle_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            duplicate = ctx["manifest"].bundles[0].model_copy(
                update={"bundle_sha256": "1" * 64}
            )
            manifest = ctx["manifest"].model_copy(
                update={"bundles": list(ctx["manifest"].bundles) + [duplicate]}
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=manifest,
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )

    def test_manifest_spec_mismatch_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            manifest = ctx["manifest"].model_copy(
                update={"research_spec_sha256": "0" * 64}
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=ctx["assessment"],
                    data_ready_manifest=manifest,
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )

    def test_schedule_hash_change_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            assessment = ctx["assessment"].model_copy(
                update={
                    "session_schedule_sha256s": {
                        key: "0" * 64
                        for key in ctx["assessment"].session_schedule_sha256s
                    }
                }
            )
            with self.assertRaises(Exception):
                generate_analysis_plan(
                    research_spec=ctx["spec"],
                    readiness_assessment=assessment,
                    data_ready_manifest=ctx["manifest"],
                    generated_plan=ctx["chain"]["plan"],
                    data_plan=ctx["chain"]["data_plan"],
                    instrument_registry=ctx["chain"]["instruments"],
                    calendar_registry=ctx["chain"]["calendars"],
                    decisions=_decisions(ctx["spec"]),
                )


class DeterminismTest(unittest.TestCase):
    def test_canonical_round_trip_and_stable_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            payload = serialize_analysis_plan(generated.analysis_plan)
            restored = parse_analysis_plan(payload)
        self.assertEqual(restored, generated.analysis_plan)
        self.assertEqual(
            calculate_analysis_plan_sha256(restored),
            generated.analysis_plan_sha256,
        )
        self.assertEqual(restored.analysis_plan_id, generated.analysis_plan.analysis_plan_id)

    def test_content_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            base = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            changed = base.analysis_plan.model_copy(
                update={"warnings": ["extra"]}
            )
        self.assertNotEqual(
            calculate_analysis_plan_sha256(changed),
            base.analysis_plan_sha256,
        )

    def test_strict_json_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            payload = serialize_analysis_plan(generated.analysis_plan)
            text = payload.decode("utf-8")
        with self.assertRaises(Exception):
            parse_analysis_plan(b'{"analysis_plan_schema_version": "1.0", "analysis_plan_schema_version": "1.0"}')
        with self.assertRaises(Exception):
            parse_analysis_plan(b"[1,2]")
        with self.assertRaises(Exception):
            parse_analysis_plan(
                text.replace('"status":"ready"', '"status":"bogus"').encode("utf-8")
            )
        with self.assertRaises(Exception):
            parse_analysis_plan(
                text.replace('"status"', '"unknown_field"').encode("utf-8")
            )


class PersistenceTest(unittest.TestCase):
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            path = Path(tmp) / "plan.json"
            persisted = persist_analysis_plan(generated.analysis_plan, path)
            restored = parse_analysis_plan(persisted.read_bytes())
        self.assertEqual(restored, generated.analysis_plan)

    def test_persist_provenance_conflict_preserves_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            path = Path(tmp) / "plan.json"
            persist_analysis_plan(generated.analysis_plan, path)
            provenance = Path(tmp) / "plan.json.provenance.json"
            provenance.write_bytes(b'{"attacker": true}')
            with self.assertRaises(AnalysisPlanningError) as caught:
                persist_analysis_plan(generated.analysis_plan, path)
            self.assertEqual(
                caught.exception.code,
                AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_CONFLICT,
            )
            self.assertTrue(path.exists(), "plan artifact must survive")

    def test_persist_conflict_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"]),
            )
            path = Path(tmp) / "plan.json"
            persist_analysis_plan(generated.analysis_plan, path)
            changed = generated.analysis_plan.model_copy(
                update={"warnings": ["different"]}
            )
            with self.assertRaises(AnalysisPlanningError):
                persist_analysis_plan(changed, path)


class NoSideEffectsTest(unittest.TestCase):
    def test_planning_never_calls_execution_apis(self):
        from unittest import mock
        from market_validator.analysis import planning_generator

        forbidden = [
            "market_validator.analysis.price_change_volatility",
            "market_validator.workflow",
            "market_validator.data.execution",
            "market_validator.credentials.resolver",
            "market_validator.data.bundle_io",
        ]
        import importlib

        source = importlib.import_module(
            "market_validator.analysis.planning_generator"
        ).__file__
        text = Path(source).read_text(encoding="utf-8")
        for module in forbidden:
            self.assertNotIn(
                f"import {module}", text, f"must not import {module}"
            )
            self.assertNotIn(
                f"from {module} import", text, f"must not import {module}"
            )


if __name__ == "__main__":
    unittest.main()
