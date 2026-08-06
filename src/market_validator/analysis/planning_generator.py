"""Generator, readiness blockers, validation, and persistence for analysis plans."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from market_validator.analysis.planning import (
    ALIGNMENT_PROFILE_STRICT_SAME_SESSION,
    AnalysisPlan,
    AnalysisPlanDecisions,
    AnalysisPlanningErrorCode,
    AnalysisPlanningStage,
    AnalysisVariableBinding,
    AlignmentPlan,
    GeneratedAnalysisPlan,
    METHOD_PROFILE_OLS_CLASSIC,
    METHOD_PROFILE_OLS_HC1,
    METHOD_PROFILE_OLS_NEWEY_WEST,
    METHOD_PROFILE_PEARSON,
    METHOD_PROFILE_SPEARMAN,
    NUMERIC_PROFILE,
    MultipleTestingPlan,
    PrimaryTestSpec,
    TRANSFORMATION_PROFILE_LEVEL,
    TRANSFORMATION_PROFILE_LOG_RETURN,
    TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE,
    TRANSFORMATION_PROFILE_SIMPLE_RETURN,
    TransformationPlan,
    UnresolvedAnalysisRequirement,
    UnresolvedAnalysisRequirementCode,
    _canonical_bytes,
    _derive_analysis_plan_id,
    _sha256_hex,
    calculate_analysis_plan_sha256,
    fail_planning,
    parse_analysis_plan,
    serialize_analysis_plan,
)
from market_validator.data.acquisition_request import (
    GeneratedAcquisitionRequestPlan,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import DataPlan
from market_validator.data.readiness import (
    DataReadyManifest,
    DataReadinessAssessment,
    GeneratedDataReadyManifest,
    calculate_data_ready_manifest_sha256,
    calculate_data_readiness_assessment_sha256,
    validate_data_ready_manifest_matches,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.session_schedule import (
    ExplicitSessionScheduleSnapshot,
    SessionScheduleError,
    calculate_explicit_session_schedule_snapshot_sha256,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    persist_immutable_bytes,
)
from market_validator.research.enums import (
    ClaimType,
    Direction,
    JoinPolicy,
    MissingDataPolicy,
    ModelMethod,
    MultipleTestingCorrection,
    TargetSession,
    Transformation,
    VariableRole,
)
from market_validator.research.models import ResearchSpec
from market_validator.research.serialization import (
    calculate_research_spec_sha256,
)

_PROFILE_TO_METHOD = {
    METHOD_PROFILE_PEARSON: ModelMethod.PEARSON_CORRELATION,
    METHOD_PROFILE_SPEARMAN: ModelMethod.SPEARMAN_CORRELATION,
    METHOD_PROFILE_OLS_CLASSIC: ModelMethod.OLS,
    METHOD_PROFILE_OLS_HC1: ModelMethod.OLS,
    METHOD_PROFILE_OLS_NEWEY_WEST: ModelMethod.OLS,
}

_PROFILE_TO_COVARIANCE = {
    METHOD_PROFILE_OLS_CLASSIC: "classic",
    METHOD_PROFILE_OLS_HC1: "hc1",
    METHOD_PROFILE_OLS_NEWEY_WEST: "newey_west",
}

_TRANSFORMATION_TO_PROFILE = {
    Transformation.LEVEL: TRANSFORMATION_PROFILE_LEVEL,
    Transformation.SIMPLE_RETURN: TRANSFORMATION_PROFILE_SIMPLE_RETURN,
    Transformation.LOG_RETURN: TRANSFORMATION_PROFILE_LOG_RETURN,
    Transformation.DIFFERENCE: TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE,
}

_UNSUPPORTED_TRANSFORMATIONS = {
    Transformation.PCT_CHANGE,
    Transformation.ROLLING_MEAN,
    Transformation.ZSCORE,
}

_MISSING_POLICY_MAP = {
    "drop_observation_v1": MissingDataPolicy.DROP_OBSERVATION.value,
    "error_v1": MissingDataPolicy.ERROR.value,
    "keep_missing_v1": MissingDataPolicy.KEEP_MISSING.value,
}

_PROFILE_FIRST_VALUE_POLICY = {
    TRANSFORMATION_PROFILE_LEVEL: "retain_first_v1",
    TRANSFORMATION_PROFILE_SIMPLE_RETURN: "omit_first_v1",
    TRANSFORMATION_PROFILE_LOG_RETURN: "omit_first_v1",
    TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE: "omit_first_v1",
}

_PROFILE_OUTPUT_UNIT_RULE = {
    TRANSFORMATION_PROFILE_LEVEL: "original_units_v1",
    TRANSFORMATION_PROFILE_SIMPLE_RETURN: "fraction_ratio_v1",
    TRANSFORMATION_PROFILE_LOG_RETURN: "log_ratio_v1",
    TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE: "absolute_difference_v1",
}


def _unresolved(
    code: str, message: str, variable_id: str | None = None
) -> UnresolvedAnalysisRequirement:
    return UnresolvedAnalysisRequirement(
        code=code, message=message, variable_id=variable_id
    )


def _verify_data_ready(
    *,
    research_spec: ResearchSpec,
    readiness_assessment: DataReadinessAssessment,
    data_ready_manifest: DataReadyManifest,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> None:
    """Steps 1-9: revalidate Data Ready before trusting any input."""
    spec_sha256 = calculate_research_spec_sha256(research_spec)
    assessment_sha256 = calculate_data_readiness_assessment_sha256(
        readiness_assessment
    )
    manifest_sha256 = calculate_data_ready_manifest_sha256(
        data_ready_manifest
    )
    generated_manifest = GeneratedDataReadyManifest(
        data_ready_manifest=data_ready_manifest,
        data_ready_manifest_sha256=manifest_sha256,
        readiness_assessment_sha256=assessment_sha256,
        snapshot_manifest_sha256=data_ready_manifest.snapshot_manifest_sha256,
        data_plan_sha256=data_ready_manifest.data_plan_sha256,
        acquisition_request_plan_sha256=(
            data_ready_manifest.acquisition_request_plan_sha256
        ),
        research_spec_sha256=data_ready_manifest.research_spec_sha256,
        data_plan_confirmation_sha256=(
            data_ready_manifest.data_plan_confirmation_sha256
        ),
        source_selection_sha256=data_ready_manifest.source_selection_sha256,
        source_selection_confirmation_sha256=(
            data_ready_manifest.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=(
            data_ready_manifest.instrument_registry_sha256
        ),
        calendar_registry_sha256=(
            data_ready_manifest.calendar_registry_sha256
        ),
    )
    validate_data_ready_manifest_matches(
        generated_manifest,
        readiness_assessment,
        generated_plan,
        data_plan,
        instrument_registry,
        calendar_registry,
    )


def _build_variable_bindings(
    research_spec: ResearchSpec,
    data_ready_manifest: DataReadyManifest,
    decisions: AnalysisPlanDecisions,
    data_plan: DataPlan,
) -> tuple[list[AnalysisVariableBinding], list[UnresolvedAnalysisRequirement]]:
    """Steps 10-12: variable-to-bundle mapping and transformation plans."""
    variables: list[tuple[str, VariableRole]] = [
        (research_spec.outcome.variable_id, VariableRole.OUTCOME)
    ]
    variables.extend(
        (variable.variable_id, VariableRole.PREDICTOR)
        for variable in research_spec.predictors
    )
    variables.extend(
        (variable.variable_id, VariableRole.CONTROL)
        for variable in research_spec.controls
    )
    by_id = {variable_id: role for variable_id, role in variables}
    if len(by_id) != len(variables):
        duplicates = sorted(
            {
                variable_id
                for variable_id, _ in variables
                if list(by_id).count(variable_id) > 1
            }
        )
        unresolved = [
            _unresolved(
                UnresolvedAnalysisRequirementCode.DUPLICATE_VARIABLE_BUNDLE,
                "a variable appears in more than one ResearchSpec role",
                duplicates[0] if duplicates else None,
            )
        ]
        return [], unresolved

    bundles_by_variable: dict[str, list[object]] = {}
    for record in data_ready_manifest.bundles:
        bundles_by_variable.setdefault(record.variable_id, []).append(record)

    spec_variables = {
        variable.variable_id for variable in research_spec.predictors
    } | {research_spec.outcome.variable_id} | {
        variable.variable_id for variable in research_spec.controls
    }
    unresolved: list[UnresolvedAnalysisRequirement] = []
    unknown = sorted(set(bundles_by_variable) - spec_variables)
    for variable_id in unknown:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.UNEXPECTED_VARIABLE_BUNDLE,
                "DataReadyManifest binds a bundle for an unknown variable",
                variable_id,
            )
        )

    declared_ids = set(decisions.transformation_decisions)
    unknown_decision_ids = sorted(declared_ids - set(by_id))
    for variable_id in unknown_decision_ids:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "transformation decision refers to an unknown variable",
                variable_id,
            )
        )

    bindings: list[AnalysisVariableBinding] = []
    for variable_id, role in variables:
        records = bundles_by_variable.get(variable_id, [])
        if not records:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.MISSING_VARIABLE_BUNDLE,
                    "no DataReady bundle exists for the variable",
                    variable_id,
                )
            )
            continue
        if len(records) > 1:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.DUPLICATE_VARIABLE_BUNDLE,
                    "more than one DataReady bundle binds the variable",
                    variable_id,
                )
            )
            continue
        record = records[0]
        spec_variable = next(
            variable
            for variable in (
                [research_spec.outcome]
                + research_spec.predictors
                + research_spec.controls
            )
            if variable.variable_id == variable_id
        )
        if record.instrument_id != spec_variable.instrument.instrument_id:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.MISSING_VARIABLE_BUNDLE,
                    "bundle instrument does not match the ResearchSpec",
                    variable_id,
                )
            )
            continue
        requirement = next(
            (
                item
                for item in data_plan.requirements
                if item.requirement_id == record.requirement_id
            ),
            None,
        )
        if requirement is None or requirement.field != spec_variable.field:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.MISSING_VARIABLE_BUNDLE,
                    "bundle field does not match the ResearchSpec",
                    variable_id,
                )
            )
            continue
        transformation_plan = _build_transformation_plan(
            variable_id, spec_variable, decisions, unresolved
        )
        if transformation_plan is None:
            continue
        bindings.append(
            AnalysisVariableBinding(
                variable_id=variable_id,
                role=role.value,
                instrument_id=record.instrument_id,
                field=requirement.field,
                requirement_id=record.requirement_id,
                provider_id=record.provider_id,
                bundle_relative_path=record.bundle_relative_path,
                bundle_sha256=record.bundle_sha256,
                source_content_sha256=record.source_content_sha256,
                session_schedule_sha256=record.session_schedule_sha256,
                expected_sample_sessions_sha256=(
                    record.expected_sample_sessions_sha256
                ),
                observed_sample_sessions_sha256=(
                    record.observed_sample_sessions_sha256
                ),
                expected_pre_sample_sessions_sha256=(
                    record.expected_pre_sample_sessions_sha256
                ),
                observed_pre_sample_sessions_sha256=(
                    record.observed_pre_sample_sessions_sha256
                ),
                quality_status=record.quality_status,
                quality_issue_codes=list(record.quality_issue_codes),
                warnings=list(record.warnings),
                transformation_plan=transformation_plan,
            )
        )
    return bindings, unresolved


def _build_transformation_plan(
    variable_id: str,
    spec_variable,
    decisions: AnalysisPlanDecisions,
    unresolved: list[UnresolvedAnalysisRequirement],
) -> TransformationPlan | None:
    decision = decisions.transformation_decisions.get(variable_id)
    if decision is None or decision.profile is None:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "no explicit transformation decision for the variable",
                variable_id,
            )
        )
        return None
    transformation = spec_variable.transformation
    if transformation in _UNSUPPORTED_TRANSFORMATIONS:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.TRANSFORMATION_CONTRACT_UNRESOLVED,
                "the ResearchSpec transformation has no executable contract",
                variable_id,
            )
        )
        return None
    expected_profile = _TRANSFORMATION_TO_PROFILE.get(transformation)
    if expected_profile is None or decision.profile != expected_profile:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.TRANSFORMATION_CONTRACT_UNRESOLVED,
                "the transformation profile does not match the ResearchSpec "
                "transformation",
                variable_id,
            )
        )
        return None
    if (
        transformation is not Transformation.LEVEL
        and decision.rolling_window_periods is not None
    ):
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "rolling windows are not part of this transformation profile",
                variable_id,
            )
        )
        return None
    return TransformationPlan(
        variable_id=variable_id,
        research_spec_transformation=transformation.value,
        profile=decision.profile,
        rolling_window_periods=decision.rolling_window_periods,
        lag_periods=decision.lag_periods,
        availability_lag_periods=decision.availability_lag_periods,
        required_pre_sample_periods=decision.required_pre_sample_periods,
        first_value_policy=_PROFILE_FIRST_VALUE_POLICY[decision.profile],
        gap_policy="provider_reported_missing_excluded_v1",
        output_unit_rule=_PROFILE_OUTPUT_UNIT_RULE[decision.profile],
    )


def _build_alignment_plan(
    research_spec: ResearchSpec,
    decisions: AnalysisPlanDecisions,
    bindings: list[AnalysisVariableBinding],
    data_plan: DataPlan,
    unresolved: list[UnresolvedAnalysisRequirement],
) -> AlignmentPlan | None:
    outcome = next(
        (
            binding
            for binding in bindings
            if binding.role == VariableRole.OUTCOME.value
        ),
        None,
    )
    if outcome is None:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ALIGNMENT_CONTRACT_MISSING,
                "no outcome binding is available for alignment planning",
            )
        )
        return None
    alignment = research_spec.alignment
    if alignment is not None:
        calendar_id = alignment.target_calendar
        max_staleness = alignment.max_staleness_days
        if (
            alignment.join_policy is not JoinPolicy.STRICT_MATCH
            or max_staleness != 0
        ):
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.ALIGNMENT_CONTRACT_MISSING,
                    "only strict same-session alignment is executable",
                )
            )
        if alignment.missing_data_policy is MissingDataPolicy.KEEP_MISSING:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.ANALYSIS_MISSING_POLICY_NOT_EXECUTABLE,
                    "keep-missing is not executable by the current analysis "
                    "engine",
                )
            )
        cutoff = alignment.information_cutoff
        if (
            cutoff is not None
            and cutoff.type.value
            in ("before_target_open", "before_target_close")
        ):
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.INFORMATION_CUTOFF_NOT_EXECUTABLE,
                    "before-open and before-close cutoffs have no precise "
                    "session-time contract in the calendar registry",
                )
            )
        plan = AlignmentPlan(
            target_variable_id=outcome.variable_id,
            target_calendar_id=calendar_id,
            target_session_schedule_sha256=(
                outcome.session_schedule_sha256
            ),
            target_session=alignment.target_session.value,
            information_cutoff=(
                alignment.information_cutoff.model_dump(mode="json")
                if alignment.information_cutoff is not None
                else None
            ),
            join_policy=alignment.join_policy.value,
            max_staleness_days=max_staleness,
            missing_data_policy=alignment.missing_data_policy.value,
            predictor_lag_periods=max(
                (binding.transformation_plan.lag_periods for binding in bindings),
                default=0,
            ),
            availability_lag_periods=max(
                (
                    binding.transformation_plan.availability_lag_periods
                    for binding in bindings
                ),
                default=0,
            ),
            no_lookahead=True,
            alignment_profile=ALIGNMENT_PROFILE_STRICT_SAME_SESSION,
        )
        return plan
    if (
        decisions.same_market_join_policy
        != ALIGNMENT_PROFILE_STRICT_SAME_SESSION
        or decisions.same_market_missing_data_policy is None
    ):
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ALIGNMENT_CONTRACT_MISSING,
                "alignment contract requires explicit strict same-session "
                "decisions",
            )
        )
        return None
    if decisions.same_market_missing_data_policy == "keep_missing_v1":
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_MISSING_POLICY_NOT_EXECUTABLE,
                "keep-missing is not executable by the current analysis "
                "engine",
            )
        )
        return None
    requirement = next(
        item
        for item in data_plan.requirements
        if item.requirement_id == outcome.requirement_id
    )
    return AlignmentPlan(
        target_variable_id=outcome.variable_id,
        target_calendar_id=requirement.calendar_id,
        target_session_schedule_sha256=outcome.session_schedule_sha256,
        target_session=TargetSession.REGULAR_SESSION.value,
        information_cutoff=None,
        join_policy=JoinPolicy.STRICT_MATCH.value,
        max_staleness_days=0,
        missing_data_policy=_MISSING_POLICY_MAP[
            decisions.same_market_missing_data_policy
        ],
        predictor_lag_periods=max(
            (binding.transformation_plan.lag_periods for binding in bindings),
            default=0,
        ),
        availability_lag_periods=max(
            (
                binding.transformation_plan.availability_lag_periods
                for binding in bindings
            ),
            default=0,
        ),
        no_lookahead=True,
        alignment_profile=ALIGNMENT_PROFILE_STRICT_SAME_SESSION,
    )


def _build_primary_tests(
    research_spec: ResearchSpec,
    decisions: AnalysisPlanDecisions,
    unresolved: list[UnresolvedAnalysisRequirement],
) -> tuple[list[PrimaryTestSpec], MultipleTestingPlan]:
    spec_model = research_spec.model
    method = spec_model.method
    if decisions.method_profile is None:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "no method profile was provided",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    predictor_ids = [variable.variable_id for variable in research_spec.predictors]
    controls = research_spec.controls

    if method is ModelMethod.LEAD_LAG_REGRESSION:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.UNSUPPORTED_ANALYSIS_METHOD,
                "lead-lag regression has no executable contract",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    if research_spec.claim_type is ClaimType.PREDICTIVE:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.UNSUPPORTED_CLAIM_TYPE,
                "predictive claims are not executable",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    if _PROFILE_TO_METHOD.get(decisions.method_profile) is not method:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.METHOD_PROFILE_MISMATCH,
                "the method profile does not match the ResearchSpec method",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )

    if method in (ModelMethod.PEARSON_CORRELATION, ModelMethod.SPEARMAN_CORRELATION):
        parameter = (
            "pearson_r"
            if method is ModelMethod.PEARSON_CORRELATION
            else "spearman_rho"
        )
        if len(predictor_ids) != 1 or controls:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.PRIMARY_TEST_VARIABLE_INVALID,
                    "correlation profiles require exactly one predictor and "
                    "no controls",
                )
            )
            return [], MultipleTestingPlan(
                family_id=f"{research_spec.spec_id}.primary",
                test_ids=[],
                correction=spec_model.multiple_testing_correction.value,
                family_size=0,
            )
        if decisions.primary_test_variable_ids != [predictor_ids[0]]:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.PRIMARY_TEST_VARIABLE_INVALID,
                    "the primary test variable must be the single predictor",
                )
            )
            return [], MultipleTestingPlan(
                family_id=f"{research_spec.spec_id}.primary",
                test_ids=[],
                correction=spec_model.multiple_testing_correction.value,
                family_size=0,
            )
        if (
            decisions.include_intercept is not None
            or decisions.covariance_estimator is not None
            or decisions.newey_west_max_lags is not None
        ):
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                    "correlation profiles must leave intercept, covariance "
                    "and HAC lags null",
                )
            )
            return [], MultipleTestingPlan(
                family_id=f"{research_spec.spec_id}.primary",
                test_ids=[],
                correction=spec_model.multiple_testing_correction.value,
                family_size=0,
            )
        target_variable_id = predictor_ids[0]
        tests = [
            PrimaryTestSpec(
                test_id=_derive_test_id(
                    research_spec, target_variable_id, parameter
                ),
                target_variable_id=target_variable_id,
                parameter=parameter,
                null_value=0,
                direction=spec_model.direction.value,
                significance_level=spec_model.significance_level,
                minimum_effect_size=spec_model.minimum_effect_size,
                multiple_testing_family_id=(
                    f"{research_spec.spec_id}.primary"
                ),
            )
        ]
        return tests, MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[test.test_id for test in tests],
            correction=spec_model.multiple_testing_correction.value,
            family_size=len(tests),
        )

    # OLS
    if not predictor_ids:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.PRIMARY_TEST_VARIABLE_INVALID,
                "OLS requires at least one predictor",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    primary_ids = decisions.primary_test_variable_ids
    if (
        not primary_ids
        or set(primary_ids) - set(predictor_ids)
        or len(set(primary_ids)) != len(primary_ids)
    ):
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.PRIMARY_TEST_VARIABLE_INVALID,
                "primary test variables must be an explicit subset of the "
                "predictors",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    if decisions.include_intercept is None:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "OLS requires an explicit intercept decision",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    expected_covariance = _PROFILE_TO_COVARIANCE[decisions.method_profile]
    if decisions.covariance_estimator != expected_covariance:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.METHOD_PROFILE_MISMATCH,
                "the covariance estimator does not match the method profile",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    if decisions.method_profile == METHOD_PROFILE_OLS_NEWEY_WEST:
        if decisions.newey_west_max_lags is None:
            unresolved.append(
                _unresolved(
                    UnresolvedAnalysisRequirementCode.NEWEY_WEST_LAG_MISSING,
                    "Newey-West requires an explicit maximum lag",
                )
            )
            return [], MultipleTestingPlan(
                family_id=f"{research_spec.spec_id}.primary",
                test_ids=[],
                correction=spec_model.multiple_testing_correction.value,
                family_size=0,
            )
    elif decisions.newey_west_max_lags is not None:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.ANALYSIS_DECISION_MISSING,
                "HAC lags are only valid for the Newey-West profile",
            )
        )
        return [], MultipleTestingPlan(
            family_id=f"{research_spec.spec_id}.primary",
            test_ids=[],
            correction=spec_model.multiple_testing_correction.value,
            family_size=0,
        )
    tests = []
    for variable_id in primary_ids:
        tests.append(
            PrimaryTestSpec(
                test_id=_derive_test_id(
                    research_spec, variable_id, "ols_coefficient"
                ),
                target_variable_id=variable_id,
                parameter="ols_coefficient",
                null_value=0,
                direction=spec_model.direction.value,
                significance_level=spec_model.significance_level,
                minimum_effect_size=spec_model.minimum_effect_size,
                multiple_testing_family_id=(
                    f"{research_spec.spec_id}.primary"
                ),
            )
        )
    return tests, MultipleTestingPlan(
        family_id=f"{research_spec.spec_id}.primary",
        test_ids=[test.test_id for test in tests],
        correction=spec_model.multiple_testing_correction.value,
        family_size=len(tests),
    )


def _derive_test_id(
    research_spec: ResearchSpec, variable_id: str, parameter: str
) -> str:
    identity = _canonical_bytes(
        {
            "spec_id": research_spec.spec_id,
            "variable_id": variable_id,
            "parameter": parameter,
        }
    )
    return _sha256_hex(identity)[:16]


def generate_analysis_plan(
    *,
    research_spec: ResearchSpec,
    readiness_assessment: DataReadinessAssessment,
    data_ready_manifest: DataReadyManifest,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    decisions: AnalysisPlanDecisions,
) -> GeneratedAnalysisPlan:
    """Deterministically compile an AnalysisPlan (16 fixed steps)."""
    _verify_data_ready(
        research_spec=research_spec,
        readiness_assessment=readiness_assessment,
        data_ready_manifest=data_ready_manifest,
        generated_plan=generated_plan,
        data_plan=data_plan,
        instrument_registry=instrument_registry,
        calendar_registry=calendar_registry,
    )
    unresolved: list[UnresolvedAnalysisRequirement] = []
    warnings: list[str] = []

    spec_sha256 = calculate_research_spec_sha256(research_spec)
    if data_ready_manifest.research_spec_sha256 != spec_sha256:
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.RESEARCH_SPEC_MISMATCH,
                "DataReadyManifest research-spec hash does not match the "
                "provided ResearchSpec",
            )
        )

    if data_ready_manifest.session_schedule_sha256s != (
        readiness_assessment.session_schedule_sha256s
    ):
        unresolved.append(
            _unresolved(
                UnresolvedAnalysisRequirementCode.DATA_READY_MANIFEST_MISMATCH,
                "manifest and assessment session schedule hashes disagree",
            )
        )

    bindings, binding_unresolved = _build_variable_bindings(
        research_spec, data_ready_manifest, decisions, data_plan
    )
    unresolved.extend(binding_unresolved)

    alignment_plan = _build_alignment_plan(
        research_spec,
        decisions,
        bindings,
        data_plan,
        unresolved,
    )
    primary_tests, multiple_testing_plan = _build_primary_tests(
        research_spec, decisions, unresolved
    )

    if research_spec.robustness_checks:
        warnings.append(
            UnresolvedAnalysisRequirementCode.ROBUSTNESS_EXECUTION_NOT_PLANNED
            + ": robustness execution is not planned in this phase"
        )
    robustness_names = [
        check.value for check in research_spec.robustness_checks
    ]
    robustness_sha256 = _sha256_hex(_canonical_bytes(sorted(robustness_names)))

    status = "draft" if unresolved else "ready"
    spec_sha256 = calculate_research_spec_sha256(research_spec)
    manifest_sha256 = calculate_data_ready_manifest_sha256(
        data_ready_manifest
    )
    assessment_sha256 = calculate_data_readiness_assessment_sha256(
        readiness_assessment
    )
    plan = AnalysisPlan(
        analysis_plan_schema_version="1.0",
        analysis_plan_id="pending",
        status=status,
        research_spec_sha256=spec_sha256,
        data_ready_manifest_sha256=manifest_sha256,
        readiness_assessment_sha256=assessment_sha256,
        snapshot_manifest_sha256=data_ready_manifest.snapshot_manifest_sha256,
        data_plan_sha256=data_ready_manifest.data_plan_sha256,
        acquisition_request_plan_sha256=(
            data_ready_manifest.acquisition_request_plan_sha256
        ),
        instrument_registry_sha256=(
            data_ready_manifest.instrument_registry_sha256
        ),
        calendar_registry_sha256=(
            data_ready_manifest.calendar_registry_sha256
        ),
        session_schedule_sha256s=dict(
            data_ready_manifest.session_schedule_sha256s
        ),
        claim_type=research_spec.claim_type.value,
        method=research_spec.model.method.value,
        method_profile=decisions.method_profile or "unresolved",
        include_intercept=decisions.include_intercept,
        newey_west_max_lags=decisions.newey_west_max_lags,
        human_formula=research_spec.model.formula,
        human_null_hypothesis=research_spec.model.null_hypothesis,
        human_alternative_hypothesis=research_spec.model.alternative_hypothesis,
        variable_bindings=bindings,
        alignment_plan=alignment_plan,
        primary_tests=primary_tests,
        multiple_testing_plan=multiple_testing_plan,
        minimum_usable_observations=(
            research_spec.sample.minimum_observations
        ),
        numeric_profile=NUMERIC_PROFILE,
        declared_robustness_checks=robustness_names,
        declared_robustness_checks_sha256=robustness_sha256,
        unresolved_requirements=unresolved,
        warnings=sorted(set(warnings)),
    )
    plan_id = _derive_analysis_plan_id(plan)
    plan = plan.model_copy(update={"analysis_plan_id": plan_id})
    plan_sha256 = calculate_analysis_plan_sha256(plan)
    return GeneratedAnalysisPlan(
        analysis_plan=plan,
        analysis_plan_sha256=plan_sha256,
        research_spec_sha256=spec_sha256,
        data_ready_manifest_sha256=manifest_sha256,
        readiness_assessment_sha256=assessment_sha256,
        snapshot_manifest_sha256=data_ready_manifest.snapshot_manifest_sha256,
    )


def analysis_plan_readiness_blockers(plan: AnalysisPlan) -> list[str]:
    """Deterministic blockers before the plan can be confirmed."""
    blockers = [
        item.code for item in plan.unresolved_requirements
    ]
    if plan.status != "ready":
        blockers.append("analysis_plan_not_ready")
    return sorted(set(blockers))


def validate_analysis_plan(plan: AnalysisPlan) -> None:
    """Strict revalidation of an AnalysisPlan (round-trip + id + status)."""
    restored = parse_analysis_plan(serialize_analysis_plan(plan))
    if restored != plan:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_MISMATCH,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "analysis plan does not round-trip exactly",
        )
    expected_id = _derive_analysis_plan_id(plan)
    if plan.analysis_plan_id != expected_id:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_MISMATCH,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "analysis plan id does not match the canonical core",
        )


def persist_analysis_plan(
    plan: AnalysisPlan,
    output_path: str | Path,
) -> Path:
    """Create-only persistence with provenance sidecar and reload check."""
    payload = serialize_analysis_plan(plan)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_planning(
                AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_CONFLICT,
                AnalysisPlanningStage.PLAN_OUTPUT,
                "analysis plan output path already exists with different "
                "content",
            )
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "analysis plan could not be persisted",
        )
    try:
        restored = parse_analysis_plan(persisted.read_bytes())
    except Exception:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "persisted analysis plan failed reload validation",
        )
    if restored != plan:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "persisted analysis plan does not match the canonical bytes",
        )
    provenance = persisted.with_name(persisted.name + ".provenance.json")
    provenance_payload = (
        _canonical_bytes(
            {
                "artifact_type": "analysis_plan",
                "sha256": calculate_analysis_plan_sha256(plan),
                "persisted_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    )
    try:
        persist_immutable_bytes(provenance_payload, provenance)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_planning(
                AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_CONFLICT,
                AnalysisPlanningStage.PLAN_OUTPUT,
                "analysis plan provenance path conflicts with existing "
                "content; no artifact was deleted",
            )
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.PLAN_OUTPUT,
            "analysis plan provenance could not be persisted; no artifact "
            "was deleted",
        )
    return persisted


__all__ = [
    "analysis_plan_readiness_blockers",
    "generate_analysis_plan",
    "persist_analysis_plan",
    "validate_analysis_plan",
]
