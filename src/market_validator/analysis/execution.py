"""Authorized analysis execution (v0.4.0 Phase 2).

Consumes the single-use AnalysisAuthorization before any bundle read,
reloads the Data Ready chain, transforms variables with the existing
returns contracts, aligns on strict same-session, runs the planned
statistics, and persists an immutable, verified execution directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from market_validator.analysis.authorization import (
    ANALYSIS_AUTHORIZATION_STATEMENT,
    AnalysisAuthorization,
    AnalysisPlanConfirmation,
    calculate_analysis_authorization_sha256,
    calculate_analysis_plan_confirmation_sha256,
    validate_analysis_authorization_matches,
    validate_analysis_plan_confirmation_matches,
)
from market_validator.analysis.execution_models import (
    AlignedAnalysisDataset,
    AnalysisAuthorizationConsumptionReceipt,
    AnalysisExecutionError,
    AnalysisExecutionErrorCode,
    AnalysisExecutionManifest,
    AnalysisExecutionOutcome,
    AnalysisResult,
    CorrelationResultSummary,
    OLSResultSummary,
    PrimaryTestResult,
    TransformedObservation,
    TransformedVariableSeries,
    _derive_analysis_result_id,
    _sha256_hex,
    calculate_aligned_analysis_dataset_sha256,
    calculate_analysis_result_sha256,
    calculate_transformed_variable_series_sha256,
    canonical_model_bytes,
    fail_execution,
    parse_aligned_analysis_dataset,
    parse_analysis_execution_manifest,
    parse_analysis_execution_outcome,
    parse_analysis_result,
    parse_transformed_variable_series,
    serialize_aligned_analysis_dataset,
    serialize_analysis_execution_manifest,
    serialize_analysis_execution_outcome,
    serialize_analysis_result,
    serialize_transformed_variable_series,
)
from market_validator.analysis.planning import (
    AnalysisPlan,
    calculate_analysis_plan_sha256,
)
from market_validator.analysis.planning_generator import validate_analysis_plan
from market_validator.analysis.statistics import (
    apply_multiple_testing_correction,
    compute_ols_result,
    compute_pearson_result,
    compute_spearman_result,
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
from market_validator.data.returns import (
    transform_absolute_price_change,
    transform_price_bundle,
)
from market_validator.data.session_schedule import (
    ExplicitSessionScheduleSnapshot,
)
from market_validator.data.snapshot import (
    verify_acquisition_snapshot,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    persist_immutable_bytes,
)
from market_validator.research.enums import Transformation
from market_validator.research.models import ResearchSpec
from market_validator.research.serialization import (
    calculate_research_spec_sha256,
)

RECEIPT_SCHEMA_VERSION = "1.0"


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Authorization consumption
# ---------------------------------------------------------------------------

def _derive_receipt_id(
    authorization: AnalysisAuthorization, attempt_id: str
) -> str:
    identity = _canonical_bytes(
        {
            "attempt_id": attempt_id,
            "authorization_id": authorization.authorization_id,
            "authorization_sha256": calculate_analysis_authorization_sha256(
                authorization
            ),
        }
    )
    return _sha256_hex(identity)[:20]


def consume_analysis_authorization(
    plan: AnalysisPlan,
    confirmation: AnalysisPlanConfirmation,
    authorization: AnalysisAuthorization,
    *,
    research_spec: ResearchSpec,
    readiness_assessment: DataReadinessAssessment,
    data_ready_manifest: DataReadyManifest,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    attempt_id: str,
    receipt_path: str | Path,
    consumed_at: datetime,
) -> AnalysisAuthorizationConsumptionReceipt:
    """Six-step consumption gate; the receipt is persisted before execution."""
    try:
        validate_analysis_plan(plan)
        validate_analysis_plan_confirmation_matches(plan, confirmation)
        validate_analysis_authorization_matches(plan, confirmation, authorization)
        if plan.status != "ready" or plan.unresolved_requirements:
            fail_execution(
                AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
                "only a ready plan without unresolved requirements can be "
                "consumed",
            )
    except Exception as error:
        if isinstance(error, AnalysisExecutionError):
            raise
        raise fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "the plan, confirmation or authorization is not self-consistent",
        )
    from market_validator.data.data_plan_review import (
        calculate_data_plan_sha256,
    )

    if calculate_research_spec_sha256(research_spec) != plan.research_spec_sha256:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "research spec does not match the analysis plan",
        )
    if (
        calculate_data_plan_sha256(data_plan) != plan.data_plan_sha256
        or calculate_data_plan_sha256(data_plan)
        != data_ready_manifest.data_plan_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "the data plan does not match the authorized chain",
        )
    generated_manifest = GeneratedDataReadyManifest(
        data_ready_manifest=data_ready_manifest,
        data_ready_manifest_sha256=calculate_data_ready_manifest_sha256(
            data_ready_manifest
        ),
        readiness_assessment_sha256=calculate_data_readiness_assessment_sha256(
            readiness_assessment
        ),
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
    if (
        data_ready_manifest.research_spec_sha256
        != plan.research_spec_sha256
        or calculate_data_ready_manifest_sha256(data_ready_manifest)
        != plan.data_ready_manifest_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "the data-ready chain does not match the analysis plan",
        )
    receipt_parent = Path(receipt_path).parent
    try:
        for candidate in receipt_parent.glob("*.json"):
            try:
                existing = parse_receipt(candidate.read_bytes())
            except Exception:
                continue
            if (
                existing.authorization_id
                == authorization.authorization_id
                and existing.authorization_sha256
                == calculate_analysis_authorization_sha256(
                    authorization
                )
            ):
                fail_execution(
                    AnalysisExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
                    "this authorization has already been consumed",
                )
    except OSError:
        fail_execution(
            AnalysisExecutionErrorCode.AUTHORIZATION_RECEIPT_ERROR,
            "the consumption receipt directory cannot be inspected",
        )
    receipt = AnalysisAuthorizationConsumptionReceipt(
        receipt_schema_version="1.0",
        receipt_id=_derive_receipt_id(authorization, attempt_id),
        attempt_id=attempt_id,
        authorization_id=authorization.authorization_id,
        authorization_sha256=calculate_analysis_authorization_sha256(
            authorization
        ),
        analysis_plan_sha256=calculate_analysis_plan_sha256(plan),
        analysis_plan_confirmation_sha256=(
            calculate_analysis_plan_confirmation_sha256(confirmation)
        ),
        consumed=True,
        consumed_at=consumed_at,
    )
    try:
        persist_immutable_bytes(
            serialize_receipt(receipt), receipt_path
        )
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_execution(
                AnalysisExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
                "this authorization has already been consumed",
            )
        fail_execution(
            AnalysisExecutionErrorCode.AUTHORIZATION_RECEIPT_ERROR,
            "the consumption receipt could not be persisted",
        )
    return receipt


def serialize_receipt(receipt: AnalysisAuthorizationConsumptionReceipt) -> bytes:
    return canonical_model_bytes(receipt)


def parse_receipt(payload: bytes) -> AnalysisAuthorizationConsumptionReceipt:
    from market_validator.analysis.execution_models import parse_strict

    return parse_strict(
        payload, AnalysisAuthorizationConsumptionReceipt, "receipt"
    )


def calculate_receipt_sha256(receipt: AnalysisAuthorizationConsumptionReceipt) -> str:
    return _sha256_hex(serialize_receipt(receipt))


def validate_analysis_authorization_receipt_matches(
    authorization: AnalysisAuthorization,
    receipt: AnalysisAuthorizationConsumptionReceipt,
) -> None:
    if (
        receipt.authorization_id != authorization.authorization_id
        or receipt.authorization_sha256
        != calculate_analysis_authorization_sha256(authorization)
    ):
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_AUTHORIZATION_MISMATCH,
            "receipt does not match the authorization",
        )
    expected_receipt_id = _derive_receipt_id(
        authorization, receipt.attempt_id
    )
    if receipt.receipt_id != expected_receipt_id:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_AUTHORIZATION_MISMATCH,
            "receipt id does not match the canonical core",
        )


def persist_analysis_authorization_receipt(
    receipt: AnalysisAuthorizationConsumptionReceipt,
    output_path: str | Path,
) -> Path:
    try:
        return persist_immutable_bytes(serialize_receipt(receipt), output_path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_execution(
                AnalysisExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
                "this authorization has already been consumed",
            )
        fail_execution(
            AnalysisExecutionErrorCode.AUTHORIZATION_RECEIPT_ERROR,
            "the consumption receipt could not be persisted",
        )


# ---------------------------------------------------------------------------
# Bundle reload and transformation
# ---------------------------------------------------------------------------

def _read_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_relative_component(name: str, label: str) -> None:
    """Reject traversal, separators, and absolute components."""
    if not name or name in (".", ".."):
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            f"{label} is not a valid relative component",
        )
    if "/" in name or "\\" in name or ":" in name:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            f"{label} is not a valid relative component",
        )
    if Path(name).is_absolute():
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            f"{label} is not a valid relative component",
        )


def _bundle_path(
    snapshot_path: Path, requirement_id: str
) -> Path:
    _validate_relative_component(requirement_id, "requirement id")
    return snapshot_path / "requests" / requirement_id / "bundle.json"


def transform_analysis_variables(
    *,
    plan: AnalysisPlan,
    snapshot_path: str | Path,
) -> dict[str, TransformedVariableSeries]:
    """Reload each bound bundle (hash-checked) and transform it."""
    snapshot_root = Path(snapshot_path)
    series: dict[str, TransformedVariableSeries] = {}
    for binding in plan.variable_bindings:
        bundle_path = _bundle_path(snapshot_root, binding.requirement_id)
        if not bundle_path.is_file():
            fail_execution(
                AnalysisExecutionErrorCode.BUNDLE_RELOAD_FAILED,
                "a bound bundle is missing from the snapshot",
            )
        actual_sha256 = _read_sha256(bundle_path)
        if actual_sha256 != binding.bundle_sha256:
            fail_execution(
                AnalysisExecutionErrorCode.BUNDLE_HASH_MISMATCH,
                "a bound bundle hash does not match the analysis plan",
            )
        transformation_plan = binding.transformation_plan
        profile = transformation_plan.profile
        if profile in ("level_v1",):
            observations = _transform_level(bundle_path, binding)
        elif profile in (
            "simple_return_adjacent_v1",
            "log_return_adjacent_v1",
        ):
            observations = _transform_return(
                bundle_path, binding, profile
            )
        elif profile == "signed_first_difference_v1":
            observations = _transform_absolute_change(
                bundle_path, binding
            )
        else:
            fail_execution(
                AnalysisExecutionErrorCode.TRANSFORMATION_FAILED,
                "the transformation profile is not executable",
            )
        series[binding.variable_id] = observations
    return series


def _level_view_path(bundle_path: Path, staging_dir: str) -> Path:
    """Return the bundle path for price-transformation reuse.

    The price transformation contracts require a level (raw-observation)
    bundle. Analysis bundles may carry a non-level requirement label even
    though their observations are still raw prices; a byte-identical copy
    with the requirement relabelled to LEVEL is staged in a temporary
    directory so the original snapshot and bundle are never modified.
    """
    from market_validator.data.bundle_io import load_data_bundle

    bundle = load_data_bundle(bundle_path)
    if bundle.requirement.transformation is Transformation.LEVEL:
        return bundle_path
    relabelled = bundle.model_copy(
        deep=True,
        update={
            "requirement": bundle.requirement.model_copy(
                update={
                    "transformation": Transformation.LEVEL,
                    "required_pre_sample_periods": 0,
                }
            )
        },
    )
    view_path = Path(staging_dir) / "bundle.json"
    from market_validator.data.bundle_io import serialize_data_bundle

    view_path.write_bytes(serialize_data_bundle(relabelled))
    return view_path


def _transform_level(
    bundle_path: Path, binding
) -> TransformedVariableSeries:
    from market_validator.data.bundle_io import load_data_bundle

    try:
        bundle = load_data_bundle(bundle_path)
    except Exception:
        fail_execution(
            AnalysisExecutionErrorCode.TRANSFORMATION_FAILED,
            "the bound bundle could not be reloaded",
        )
    input_count = len(bundle.observations)
    observations = [
        TransformedObservation(
            session_date=observation.session_date,
            source_session_date=observation.session_date,
            value=observation.value,
            available_time=observation.available_time.isoformat(),
            source_observation_time=observation.observation_time.isoformat(),
        )
        for observation in bundle.observations
    ]
    warnings = list(binding.warnings)
    series = TransformedVariableSeries(
        series_schema_version="1.0",
        variable_id=binding.variable_id,
        role=binding.role,
        instrument_id=binding.instrument_id,
        source_bundle_sha256=binding.bundle_sha256,
        transformation_profile=binding.transformation_plan.profile,
        lag_periods=binding.transformation_plan.lag_periods,
        availability_lag_periods=(
            binding.transformation_plan.availability_lag_periods
        ),
        input_observation_count=input_count,
        output_observation_count=len(observations),
        excluded_count=0,
        warnings=warnings,
        observations=observations,
        series_sha256="0" * 64,
    )
    return series.model_copy(
        update={
            "series_sha256": calculate_transformed_variable_series_sha256(
                series
            )
        }
    )


def _transform_return(
    bundle_path: Path, binding, profile: str
) -> TransformedVariableSeries:
    transformation = (
        Transformation.SIMPLE_RETURN
        if profile == "simple_return_adjacent_v1"
        else Transformation.LOG_RETURN
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="analysis-level-view-"
        ) as view_dir:
            result = transform_price_bundle(
                _level_view_path(bundle_path, view_dir), transformation
            )
    except Exception as error:
        fail_execution(
            AnalysisExecutionErrorCode.TRANSFORMATION_FAILED,
            "the return transformation failed for the bound bundle",
        )
    observations = []
    for item in result.returns:
        observations.append(
            TransformedObservation(
                session_date=item.end_session_date,
                source_session_date=item.end_session_date,
                value=item.value,
                available_time=item.available_time.isoformat(),
                source_observation_time=item.end_observation_time.isoformat(),
            )
        )
    excluded = result.excluded_gap_count
    series = TransformedVariableSeries(
        series_schema_version="1.0",
        variable_id=binding.variable_id,
        role=binding.role,
        instrument_id=binding.instrument_id,
        source_bundle_sha256=binding.bundle_sha256,
        transformation_profile=profile,
        lag_periods=binding.transformation_plan.lag_periods,
        availability_lag_periods=(
            binding.transformation_plan.availability_lag_periods
        ),
        input_observation_count=result.input_observation_count,
        output_observation_count=len(observations),
        excluded_count=excluded,
        warnings=[
            warning.message
            for warning in result.gap_warnings
        ],
        observations=observations,
        series_sha256="0" * 64,
    )
    return series.model_copy(
        update={
            "series_sha256": calculate_transformed_variable_series_sha256(
                series
            )
        }
    )


def _transform_absolute_change(
    bundle_path: Path, binding
) -> TransformedVariableSeries:
    try:
        with tempfile.TemporaryDirectory(
            prefix="analysis-level-view-"
        ) as view_dir:
            result = transform_absolute_price_change(
                _level_view_path(bundle_path, view_dir)
            )
    except Exception as error:
        fail_execution(
            AnalysisExecutionErrorCode.TRANSFORMATION_FAILED,
            "the signed-difference transformation failed for the bound "
            "bundle",
        )
    observations = []
    for item in result.price_changes:
        observations.append(
            TransformedObservation(
                session_date=item.end_session_date,
                source_session_date=item.end_session_date,
                value=item.value,
                available_time=item.available_time.isoformat(),
                source_observation_time=item.end_observation_time.isoformat(),
            )
        )
    excluded = result.excluded_gap_count + result.other_exclusion_count
    series = TransformedVariableSeries(
        series_schema_version="1.0",
        variable_id=binding.variable_id,
        role=binding.role,
        instrument_id=binding.instrument_id,
        source_bundle_sha256=binding.bundle_sha256,
        transformation_profile=binding.transformation_plan.profile,
        lag_periods=binding.transformation_plan.lag_periods,
        availability_lag_periods=(
            binding.transformation_plan.availability_lag_periods
        ),
        input_observation_count=result.candidate_pair_count + 1,
        output_observation_count=len(observations),
        excluded_count=excluded,
        warnings=[
            warning.message for warning in result.gap_warnings
        ],
        observations=observations,
        series_sha256="0" * 64,
    )
    return series.model_copy(
        update={
            "series_sha256": calculate_transformed_variable_series_sha256(
                series
            )
        }
    )


# ---------------------------------------------------------------------------
# Snapshot binding verification
# ---------------------------------------------------------------------------

def _verify_snapshot_binding(
    *,
    snapshot_path: Path,
    plan: AnalysisPlan,
    data_plan: DataPlan,
) -> dict[str, object]:
    """Bind the persisted snapshot directory to the plan without trusting
    any caller-supplied ready flags."""
    from market_validator.data.snapshot import (
        parse_snapshot_manifest,
        parse_execution_outcome,
    )

    manifest_path = snapshot_path / "manifest.json"
    outcome_path = snapshot_path / "outcome.json"
    if not manifest_path.is_file() or not outcome_path.is_file():
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "snapshot manifest or outcome is missing",
        )
    manifest_sha256 = _read_sha256(manifest_path)
    if manifest_sha256 != plan.snapshot_manifest_sha256:
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "snapshot manifest hash does not match the analysis plan",
        )
    try:
        manifest = parse_snapshot_manifest(manifest_path.read_bytes())
        outcome = parse_execution_outcome(outcome_path.read_bytes())
    except Exception as error:
        raise fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "snapshot artifacts failed strict reload",
        )
    if (
        manifest.acquisition_request_plan_sha256
        != plan.acquisition_request_plan_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "snapshot plan binding does not match the analysis plan",
        )
    if (
        outcome.snapshot_id != manifest.snapshot_id
        or outcome.attempt_id != manifest.attempt_id
        or outcome.acquisition_request_plan_sha256
        != manifest.acquisition_request_plan_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "snapshot outcome is inconsistent with its manifest",
        )
    return {"snapshot_path": snapshot_path, "manifest_sha256": manifest_sha256}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _z_transform(r: float) -> float:
    """atanh(r) with the perfect-correlation boundary handled; never NaN."""
    import math as _math

    clamped = max(-0.9999999999999999, min(0.9999999999999999, r))
    return round(_math.atanh(clamped), 12)


def _z_transform_se(sample_size: int) -> float:
    """Fisher-z standard error 1/sqrt(n-3)."""
    import math as _math

    return round(1.0 / _math.sqrt(sample_size - 3), 12)


def _conclusion(
    adjusted_p_value: float,
    estimate: float,
    direction: str,
    significance_level: float,
    minimum_effect_size: float,
) -> str:
    significant = adjusted_p_value <= significance_level
    if not significant:
        return "inconclusive"
    if direction == "positive":
        direction_satisfied = estimate > 0
    elif direction == "negative":
        direction_satisfied = estimate < 0
    else:
        direction_satisfied = True
    effect_reached = abs(estimate) >= minimum_effect_size
    if direction_satisfied and effect_reached:
        return "supported"
    return "not_supported"


def _build_model_summary(
    plan: AnalysisPlan,
    dataset: AlignedAnalysisDataset,
) -> CorrelationResultSummary | OLSResultSummary:
    outcome = [row.outcome_value for row in dataset.rows]
    binding_by_id = {
        binding.variable_id: binding for binding in plan.variable_bindings
    }
    predictor_values = {
        variable_id: [row.predictor_values.get(variable_id) for row in dataset.rows]
        for variable_id in dataset.variable_ids
        if variable_id in binding_by_id
        and binding_by_id[variable_id].role == "predictor"
    }
    control_values = {
        variable_id: [row.control_values.get(variable_id) for row in dataset.rows]
        for variable_id in dataset.variable_ids
        if variable_id in binding_by_id
        and binding_by_id[variable_id].role == "control"
    }
    method = plan.method
    if method == "pearson_correlation":
        predictor_id = next(
            binding.variable_id
            for binding in plan.variable_bindings
            if binding.role == "predictor"
        )
        x = [row.predictor_values[predictor_id] for row in dataset.rows]
        result = compute_pearson_result(
            x,
            outcome,
            significance_level=plan.primary_tests[0].significance_level,
        )
        return CorrelationResultSummary(
            method="pearson_correlation",
            coefficient=result["coefficient"],
            sample_size=result["sample_size"],
            t_statistic=result["t_statistic"],
            degrees_of_freedom=result["degrees_of_freedom"],
            p_value=result["p_value"],
            confidence_interval_lower=result["confidence_interval_lower"],
            confidence_interval_upper=result["confidence_interval_upper"],
            z_transform=_z_transform(result["coefficient"]),
            z_transform_standard_error=_z_transform_se(
                result["sample_size"]
            ),
        )
    if method == "spearman_correlation":
        predictor_id = next(
            binding.variable_id
            for binding in plan.variable_bindings
            if binding.role == "predictor"
        )
        x = [row.predictor_values[predictor_id] for row in dataset.rows]
        result = compute_spearman_result(
            x,
            outcome,
            significance_level=plan.primary_tests[0].significance_level,
        )
        return CorrelationResultSummary(
            method="spearman_correlation",
            coefficient=result["coefficient"],
            sample_size=result["sample_size"],
            t_statistic=result["t_statistic"],
            degrees_of_freedom=result["degrees_of_freedom"],
            p_value=result["p_value"],
            confidence_interval_lower=result["confidence_interval_lower"],
            confidence_interval_upper=result["confidence_interval_upper"],
            z_transform=_z_transform(result["coefficient"]),
            z_transform_standard_error=_z_transform_se(
                result["sample_size"]
            ),
            tie_method="average_rank_v1",
            inference_profile="spearman_t_approximation_v1",
        )
    # OLS: explicit decisions recorded in the plan
    include_intercept = plan.include_intercept
    if include_intercept is None:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "the analysis plan does not record an intercept decision",
        )
    profile = plan.method_profile
    covariance = {
        "ols_classic_v1": "classic",
        "ols_hc1_v1": "hc1",
        "ols_newey_west_v1": "newey_west",
    }.get(profile)
    if covariance is None:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "the method profile is not an executable OLS profile",
        )
    max_lags = plan.newey_west_max_lags
    if profile == "ols_newey_west_v1" and max_lags is None:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "the Newey-West plan does not record an explicit max lag",
        )
    x_columns = dict(predictor_values)
    x_columns.update(control_values)
    fit = compute_ols_result(
        x_columns,
        outcome,
        include_intercept=include_intercept,
        covariance_estimator=covariance,
        newey_west_max_lags=max_lags,
        significance_level=plan.primary_tests[0].significance_level,
    )
    return OLSResultSummary(
        method="ols",
        covariance_estimator=covariance,
        newey_west_max_lags=max_lags,
        n_observations=fit["n_observations"],
        n_parameters=fit["n_parameters"],
        include_intercept=include_intercept,
        coefficients=fit["coefficients"],
        standard_errors=fit["standard_errors"],
        t_statistics=fit["t_statistics"],
        p_values=fit["p_values"],
        confidence_intervals=fit["confidence_intervals"],
        residuals=fit["residuals"],
        sse=fit["sse"],
        degrees_of_freedom=fit["degrees_of_freedom"],
        r_squared=fit["r_squared"],
        adjusted_r_squared=fit["adjusted_r_squared"],
    )


def execute_authorized_analysis(
    *,
    plan: AnalysisPlan,
    confirmation: AnalysisPlanConfirmation,
    authorization: AnalysisAuthorization,
    research_spec: ResearchSpec,
    readiness_assessment: DataReadinessAssessment,
    data_ready_manifest: DataReadyManifest,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    schedule_snapshots: Mapping[str, ExplicitSessionScheduleSnapshot],
    snapshot_path: str | Path,
    attempt_id: str,
    receipt_path: str | Path,
    result_root: str | Path,
    consumed_at: datetime,
    outcome_created_at: datetime,
) -> tuple[AnalysisAuthorizationConsumptionReceipt, AnalysisExecutionOutcome]:
    """Full gate: consume -> verify chain -> transform -> align -> compute."""
    receipt = consume_analysis_authorization(
        plan=plan,
        confirmation=confirmation,
        authorization=authorization,
        research_spec=research_spec,
        readiness_assessment=readiness_assessment,
        data_ready_manifest=data_ready_manifest,
        generated_plan=generated_plan,
        data_plan=data_plan,
        instrument_registry=instrument_registry,
        calendar_registry=calendar_registry,
        attempt_id=attempt_id,
        receipt_path=receipt_path,
        consumed_at=consumed_at,
    )
    try:
        snapshot = _verify_snapshot_binding(
            snapshot_path=Path(snapshot_path),
            plan=plan,
            data_plan=data_plan,
        )
    except Exception as error:
        fail_execution(
            AnalysisExecutionErrorCode.DATA_READY_VALIDATION_FAILED,
            "the acquisition snapshot does not match the authorized chain",
        )
    transformed = transform_analysis_variables(
        plan=plan, snapshot_path=snapshot["snapshot_path"]
    )
    outcome_requirement = next(
        item
        for item in data_plan.requirements
        if item.variable_id == plan.alignment_plan.target_variable_id
    )
    from market_validator.analysis.alignment import align_analysis_series

    dataset = align_analysis_series(
        plan=plan,
        transformed_series=transformed,
        schedule_snapshots=schedule_snapshots,
        sample_start=outcome_requirement.start_date,
        sample_end=outcome_requirement.end_date,
    )
    model_summary = _build_model_summary(plan, dataset)
    result = _assemble_result(
        plan=plan,
        receipt=receipt,
        dataset=dataset,
        model_summary=model_summary,
        attempt_id=attempt_id,
    )
    outcome = persist_analysis_execution(
        attempt_id=attempt_id,
        plan=plan,
        receipt=receipt,
        snapshot=snapshot,
        transformed_series=transformed,
        dataset=dataset,
        result=result,
        result_root=result_root,
        created_at=outcome_created_at,
    )
    return receipt, outcome


def _assemble_result(
    *,
    plan: AnalysisPlan,
    receipt: AnalysisAuthorizationConsumptionReceipt,
    dataset: AlignedAnalysisDataset,
    model_summary: CorrelationResultSummary | OLSResultSummary,
    attempt_id: str,
) -> AnalysisResult:
    if not plan.primary_tests:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_PLAN_MISMATCH,
            "the plan declares no primary tests",
        )
    significance_level = plan.primary_tests[0].significance_level
    p_values = []
    if isinstance(model_summary, CorrelationResultSummary):
        p_values.append(model_summary.p_value)
    else:
        p_values = [
            model_summary.p_values[test.target_variable_id]
            for test in plan.primary_tests
        ]
    correction = plan.multiple_testing_plan.correction
    adjusted = apply_multiple_testing_correction(p_values, correction)
    primary_results: list[PrimaryTestResult] = []
    if isinstance(model_summary, CorrelationResultSummary):
        test = plan.primary_tests[0]
        primary_results.append(
            PrimaryTestResult(
                test_id=test.test_id,
                target_variable_id=test.target_variable_id,
                parameter=test.parameter,
                estimate=model_summary.coefficient,
                standard_error=model_summary.z_transform_standard_error,
                test_statistic=model_summary.t_statistic,
                degrees_of_freedom=model_summary.degrees_of_freedom,
                raw_p_value=model_summary.p_value,
                adjusted_p_value=adjusted[0],
                confidence_interval_lower=(
                    model_summary.confidence_interval_lower
                ),
                confidence_interval_upper=(
                    model_summary.confidence_interval_upper
                ),
                direction=test.direction,
                significance_level=test.significance_level,
                minimum_effect_size=test.minimum_effect_size,
                conclusion=_conclusion(
                    adjusted[0],
                    model_summary.coefficient,
                    test.direction,
                    test.significance_level,
                    test.minimum_effect_size,
                ),
            )
        )
    else:
        for index, test in enumerate(plan.primary_tests):
            variable_id = test.target_variable_id
            estimate = model_summary.coefficients[variable_id]
            primary_results.append(
                PrimaryTestResult(
                    test_id=test.test_id,
                    target_variable_id=variable_id,
                    parameter=test.parameter,
                    estimate=estimate,
                    standard_error=model_summary.standard_errors[variable_id],
                    test_statistic=model_summary.t_statistics[variable_id],
                    degrees_of_freedom=model_summary.degrees_of_freedom,
                    raw_p_value=model_summary.p_values[variable_id],
                    adjusted_p_value=adjusted[index],
                    confidence_interval_lower=(
                        model_summary.confidence_intervals[variable_id][
                            "lower"
                        ]
                    ),
                    confidence_interval_upper=(
                        model_summary.confidence_intervals[variable_id][
                            "upper"
                        ]
                    ),
                    direction=test.direction,
                    significance_level=test.significance_level,
                    minimum_effect_size=test.minimum_effect_size,
                    conclusion=_conclusion(
                        adjusted[index],
                        estimate,
                        test.direction,
                        test.significance_level,
                        test.minimum_effect_size,
                    ),
                )
            )
    result = AnalysisResult(
        analysis_result_schema_version="1.0",
        analysis_result_id="pending",
        attempt_id=attempt_id,
        analysis_plan_sha256=calculate_analysis_plan_sha256(plan),
        authorization_receipt_sha256=calculate_receipt_sha256(receipt),
        data_ready_manifest_sha256=plan.data_ready_manifest_sha256,
        aligned_dataset_sha256=calculate_aligned_analysis_dataset_sha256(
            dataset
        ),
        method=plan.method,
        method_profile=plan.method_profile,
        numeric_profile=plan.numeric_profile,
        sample_size=dataset.retained_row_count,
        primary_test_results=primary_results,
        model_summary=model_summary,
        warnings=list(plan.warnings),
        limitations=[
            "no robustness was executed",
            "no LLM interpretation was generated",
            "result is not investment advice",
        ],
    )
    result_id = _derive_analysis_result_id(result)
    return result.model_copy(update={"analysis_result_id": result_id})


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_analysis_execution(
    *,
    attempt_id: str,
    plan: AnalysisPlan,
    receipt: AnalysisAuthorizationConsumptionReceipt,
    snapshot: dict[str, object],
    transformed_series: Mapping[str, TransformedVariableSeries],
    dataset: AlignedAnalysisDataset,
    result: AnalysisResult,
    result_root: str | Path,
    created_at: datetime,
) -> AnalysisExecutionOutcome:
    """Stage on the same filesystem, hash-verify, then atomically rename."""
    root = Path(result_root)
    _validate_relative_component(attempt_id, "attempt id")
    final_dir = root / "analysis-runs" / attempt_id
    if final_dir.exists():
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_OUTPUT_CONFLICT,
            "the execution directory already exists",
        )
    parent = final_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        str(final_dir) + f".staging-{attempt_id[:8]}"
    )
    if staging.exists():
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True)
        transformed_dir = staging / "transformed"
        transformed_dir.mkdir()
        files: dict[str, str] = {}
        for variable_id, series in sorted(transformed_series.items()):
            _validate_relative_component(variable_id, "variable id")
            payload = serialize_transformed_variable_series(series)
            relative = f"transformed/{variable_id}.json"
            (staging / relative).write_bytes(payload)
            files[relative] = _sha256_hex(payload)
        dataset_payload = serialize_aligned_analysis_dataset(dataset)
        (staging / "aligned-dataset.json").write_bytes(dataset_payload)
        files["aligned-dataset.json"] = _sha256_hex(dataset_payload)
        result_payload = serialize_analysis_result(result)
        (staging / "analysis-result.json").write_bytes(result_payload)
        files["analysis-result.json"] = _sha256_hex(result_payload)
        outcome = AnalysisExecutionOutcome(
            outcome_schema_version="1.0",
            attempt_id=attempt_id,
            authorization_receipt_sha256=calculate_receipt_sha256(receipt),
            status="completed",
            result_sha256=calculate_analysis_result_sha256(result),
            dataset_sha256=calculate_aligned_analysis_dataset_sha256(
                dataset
            ),
            error_code=None,
            error_message=None,
            created_at=created_at,
        )
        manifest = AnalysisExecutionManifest(
            manifest_schema_version="1.0",
            attempt_id=attempt_id,
            analysis_plan_sha256=calculate_analysis_plan_sha256(plan),
            authorization_receipt_sha256=calculate_receipt_sha256(receipt),
            snapshot_manifest_sha256=snapshot["manifest_sha256"],
            files=files,
            created_at=created_at,
        )
        outcome_payload = serialize_analysis_execution_outcome(outcome)
        (staging / "execution-outcome.json").write_bytes(outcome_payload)
        files["execution-outcome.json"] = _sha256_hex(outcome_payload)
        manifest_payload = serialize_analysis_execution_manifest(manifest)
        (staging / "execution-manifest.json").write_bytes(manifest_payload)
        files["execution-manifest.json"] = _sha256_hex(manifest_payload)
        # hash-verify every staged file
        for relative, expected in files.items():
            actual = _read_sha256(staging / relative)
            if actual != expected:
                fail_execution(
                    AnalysisExecutionErrorCode.ANALYSIS_OUTPUT_ERROR,
                    "staged execution file failed hash verification",
                )
        os.rename(staging, final_dir)
    except AnalysisExecutionError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_OUTPUT_ERROR,
            "analysis execution could not be persisted",
        )
    return outcome


def verify_persisted_analysis_execution(
    result_root: str | Path,
    attempt_id: str,
    *,
    expected_analysis_plan_sha256: str | None = None,
    expected_receipt_sha256: str | None = None,
) -> AnalysisExecutionOutcome:
    """Reload and hash-verify every file in a persisted execution."""
    root = Path(result_root)
    _validate_relative_component(attempt_id, "attempt id")
    final_dir = root / "analysis-runs" / attempt_id
    manifest_path = final_dir / "execution-manifest.json"
    if not manifest_path.is_file():
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
            "execution manifest is missing",
        )
    try:
        manifest = parse_analysis_execution_manifest(
            manifest_path.read_bytes()
        )
        outcome = parse_analysis_execution_outcome(
            (final_dir / "execution-outcome.json").read_bytes()
        )
    except Exception as error:
        raise fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
            "execution artifacts failed strict reload",
        )
    if manifest.attempt_id != attempt_id or outcome.attempt_id != attempt_id:
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
            "execution artifacts do not match the attempt",
        )
    for relative, expected in manifest.files.items():
        parts = Path(relative).parts
        if not parts:
            fail_execution(
                AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
                "manifest file key is empty",
            )
        for part in parts:
            _validate_relative_component(part, "manifest file")
        if len(parts) == 2 and parts[0] != "transformed":
            fail_execution(
                AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
                "manifest file key is outside the allowed layout",
            )
        if len(parts) > 2:
            fail_execution(
                AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
                "manifest file key is outside the allowed layout",
            )
        path = final_dir / relative
        if not path.is_file() or _read_sha256(path) != expected:
            fail_execution(
                AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
                "an execution artifact failed hash verification",
            )
    if (
        expected_analysis_plan_sha256 is not None
        and manifest.analysis_plan_sha256 != expected_analysis_plan_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
            "execution plan hash does not match the expected plan",
        )
    if (
        expected_receipt_sha256 is not None
        and manifest.authorization_receipt_sha256 != expected_receipt_sha256
    ):
        fail_execution(
            AnalysisExecutionErrorCode.ANALYSIS_RESULT_MISMATCH,
            "execution receipt hash does not match the expected receipt",
        )
    return outcome


__all__ = [
    "calculate_receipt_sha256",
    "consume_analysis_authorization",
    "execute_authorized_analysis",
    "parse_receipt",
    "persist_analysis_authorization_receipt",
    "persist_analysis_execution",
    "serialize_receipt",
    "transform_analysis_variables",
    "validate_analysis_authorization_receipt_matches",
    "verify_persisted_analysis_execution",
]
