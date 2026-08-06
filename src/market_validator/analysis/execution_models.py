"""Analysis execution artifacts (v0.4.0 Phase 2).

Deterministic, immutable, canonical-JSON artifacts produced by the generic
analysis executor: authorization consumption receipt, transformed variable
series, aligned dataset, and the final analysis result. No statistics are
computed in this module.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from typing import Annotated, Literal, NoReturn

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
)

from market_validator.research.models import StrictResearchModel

EXECUTION_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class AnalysisExecutionError(ValueError):
    """Safe structured failure; never embeds raw data or secrets."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class AnalysisExecutionErrorCode:
    INVALID_ANALYSIS_EXECUTION_INPUT = "invalid_analysis_execution_input"
    ANALYSIS_PLAN_MISMATCH = "analysis_plan_mismatch"
    ANALYSIS_CONFIRMATION_MISMATCH = "analysis_confirmation_mismatch"
    ANALYSIS_AUTHORIZATION_MISMATCH = "analysis_authorization_mismatch"
    AUTHORIZATION_ALREADY_CONSUMED = "authorization_already_consumed"
    AUTHORIZATION_RECEIPT_ERROR = "authorization_receipt_error"
    DATA_READY_VALIDATION_FAILED = "data_ready_validation_failed"
    BUNDLE_RELOAD_FAILED = "bundle_reload_failed"
    BUNDLE_HASH_MISMATCH = "bundle_hash_mismatch"
    TRANSFORMATION_FAILED = "transformation_failed"
    ALIGNMENT_FAILED = "alignment_failed"
    LOOKAHEAD_DETECTED = "lookahead_detected"
    MISSING_DATA_ERROR = "missing_data_error"
    INSUFFICIENT_USABLE_OBSERVATIONS = "insufficient_usable_observations"
    ZERO_VARIANCE = "zero_variance"
    SINGULAR_DESIGN_MATRIX = "singular_design_matrix"
    INVALID_DEGREES_OF_FREEDOM = "invalid_degrees_of_freedom"
    NON_FINITE_STATISTIC = "non_finite_statistic"
    ANALYSIS_RESULT_MISMATCH = "analysis_result_mismatch"
    ANALYSIS_OUTPUT_CONFLICT = "analysis_output_conflict"
    ANALYSIS_OUTPUT_ERROR = "analysis_output_error"


def fail_execution(code: str, message: str) -> NoReturn:
    raise AnalysisExecutionError(code, message)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> NoReturn:
    raise ValueError(value)


def _strict_json_load(payload: bytes) -> None:
    json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_number,
    )


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


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_model_bytes(model: StrictResearchModel) -> bytes:
    return _canonical_bytes(
        model.model_dump(mode="json", exclude_computed_fields=True)
    )


def parse_strict(payload: bytes, model_type, label: str):
    try:
        _strict_json_load(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            f"{label} failed strict JSON validation",
        )
    try:
        return model_type.model_validate_json(payload)
    except ValidationError:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            f"{label} failed strict domain validation",
        )


# ---------------------------------------------------------------------------
# Authorization consumption receipt
# ---------------------------------------------------------------------------

class AnalysisAuthorizationConsumptionReceipt(StrictResearchModel):
    receipt_schema_version: Literal["1.0"] = "1.0"
    receipt_id: Identifier
    attempt_id: Identifier
    authorization_id: Identifier
    authorization_sha256: Sha256Hex
    analysis_plan_sha256: Sha256Hex
    analysis_plan_confirmation_sha256: Sha256Hex
    consumed: Literal[True] = True
    consumed_at: datetime

    @field_validator("consumed_at")
    @classmethod
    def validate_consumed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("consumed_at must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @field_serializer("consumed_at", when_used="json")
    def serialize_consumed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Transformed variable series
# ---------------------------------------------------------------------------

class TransformedObservation(StrictResearchModel):
    session_date: date
    source_session_date: date
    value: float = Field(allow_inf_nan=False)
    available_time: str
    source_observation_time: str


class TransformedVariableSeries(StrictResearchModel):
    series_schema_version: Literal["1.0"] = "1.0"
    variable_id: Identifier
    role: str
    instrument_id: Identifier
    source_bundle_sha256: Sha256Hex
    transformation_profile: str
    lag_periods: int = Field(ge=0)
    availability_lag_periods: int = Field(ge=0)
    input_observation_count: int = Field(ge=0)
    output_observation_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    observations: list[TransformedObservation] = Field(default_factory=list)
    series_sha256: Sha256Hex


def serialize_transformed_variable_series(series: TransformedVariableSeries) -> bytes:
    return canonical_model_bytes(series)


def parse_transformed_variable_series(payload: bytes) -> TransformedVariableSeries:
    return parse_strict(
        payload, TransformedVariableSeries, "TransformedVariableSeries"
    )


def calculate_transformed_variable_series_sha256(
    series: TransformedVariableSeries,
) -> str:
    return _sha256_hex(serialize_transformed_variable_series(series))


# ---------------------------------------------------------------------------
# Aligned analysis dataset
# ---------------------------------------------------------------------------

class AlignedAnalysisRow(StrictResearchModel):
    target_session_date: date
    outcome_value: float = Field(allow_inf_nan=False)
    predictor_values: dict[str, float] = Field(default_factory=dict)
    control_values: dict[str, float] = Field(default_factory=dict)
    source_session_dates: dict[str, date] = Field(default_factory=dict)
    available_times: dict[str, str] = Field(default_factory=dict)


class AlignedAnalysisDataset(StrictResearchModel):
    dataset_schema_version: Literal["1.0"] = "1.0"
    analysis_plan_sha256: Sha256Hex
    target_variable_id: Identifier
    variable_ids: list[Identifier] = Field(min_length=1)
    candidate_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    dropped_row_count: int = Field(ge=0)
    drop_reason_counts: dict[str, int] = Field(default_factory=dict)
    sample_start: date
    sample_end: date
    rows: list[AlignedAnalysisRow] = Field(default_factory=list)
    dataset_sha256: Sha256Hex


def serialize_aligned_analysis_dataset(
    dataset: AlignedAnalysisDataset,
) -> bytes:
    return canonical_model_bytes(dataset)


def parse_aligned_analysis_dataset(payload: bytes) -> AlignedAnalysisDataset:
    return parse_strict(payload, AlignedAnalysisDataset, "AlignedAnalysisDataset")


def calculate_aligned_analysis_dataset_sha256(
    dataset: AlignedAnalysisDataset,
) -> str:
    return _sha256_hex(serialize_aligned_analysis_dataset(dataset))


# ---------------------------------------------------------------------------
# Statistical summaries (discriminated by method)
# ---------------------------------------------------------------------------

class CorrelationResultSummary(StrictResearchModel):
    method: Literal["pearson_correlation", "spearman_correlation"]
    coefficient: float = Field(allow_inf_nan=False)
    sample_size: int = Field(ge=0)
    t_statistic: float = Field(allow_inf_nan=False)
    degrees_of_freedom: int
    p_value: float = Field(ge=0, le=1)
    confidence_interval_lower: float = Field(allow_inf_nan=False)
    confidence_interval_upper: float = Field(allow_inf_nan=False)
    z_transform: float = Field(allow_inf_nan=False)
    z_transform_standard_error: float = Field(allow_inf_nan=False)
    tie_method: Literal["average_rank_v1"] | None = None
    inference_profile: Literal["spearman_t_approximation_v1"] | None = None


class OLSResultSummary(StrictResearchModel):
    method: Literal["ols"]
    covariance_estimator: Literal["classic", "hc1", "newey_west"]
    newey_west_max_lags: int | None = None
    n_observations: int = Field(ge=0)
    n_parameters: int = Field(ge=0)
    include_intercept: bool
    coefficients: dict[str, float]
    standard_errors: dict[str, float]
    t_statistics: dict[str, float]
    p_values: dict[str, float]
    confidence_intervals: dict[str, dict[str, float]]
    residuals: list[float]
    sse: float = Field(allow_inf_nan=False)
    degrees_of_freedom: int
    r_squared: float = Field(allow_inf_nan=False)
    adjusted_r_squared: float = Field(allow_inf_nan=False)


# ---------------------------------------------------------------------------
# Primary test result
# ---------------------------------------------------------------------------

class PrimaryTestResult(StrictResearchModel):
    test_id: Identifier
    target_variable_id: Identifier
    parameter: Literal["pearson_r", "spearman_rho", "ols_coefficient"]
    estimate: float = Field(allow_inf_nan=False)
    standard_error: float = Field(allow_inf_nan=False)
    test_statistic: float = Field(allow_inf_nan=False)
    degrees_of_freedom: int
    raw_p_value: float = Field(ge=0, le=1)
    adjusted_p_value: float = Field(ge=0, le=1)
    confidence_interval_lower: float = Field(allow_inf_nan=False)
    confidence_interval_upper: float = Field(allow_inf_nan=False)
    direction: str
    significance_level: float = Field(gt=0, lt=1)
    minimum_effect_size: float = Field(ge=0)
    conclusion: Literal["supported", "not_supported", "inconclusive"]


# ---------------------------------------------------------------------------
# Analysis result
# ---------------------------------------------------------------------------

class AnalysisResult(StrictResearchModel):
    analysis_result_schema_version: Literal["1.0"] = "1.0"
    analysis_result_id: Identifier
    attempt_id: Identifier
    analysis_plan_sha256: Sha256Hex
    authorization_receipt_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex
    aligned_dataset_sha256: Sha256Hex
    method: str
    method_profile: str
    numeric_profile: str
    sample_size: int = Field(ge=0)
    primary_test_results: list[PrimaryTestResult] = Field(
        default_factory=list
    )
    model_summary: CorrelationResultSummary | OLSResultSummary
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def serialize_analysis_result(result: AnalysisResult) -> bytes:
    return canonical_model_bytes(result)


def parse_analysis_result(payload: bytes) -> AnalysisResult:
    return parse_strict(payload, AnalysisResult, "AnalysisResult")


def calculate_analysis_result_sha256(result: AnalysisResult) -> str:
    return _sha256_hex(serialize_analysis_result(result))


def _derive_analysis_result_id(result: AnalysisResult) -> str:
    pending = result.model_copy(update={"analysis_result_id": "pending"})
    return _sha256_hex(serialize_analysis_result(pending))[:32]


# ---------------------------------------------------------------------------
# Execution outcome and manifest
# ---------------------------------------------------------------------------

class AnalysisExecutionOutcome(StrictResearchModel):
    outcome_schema_version: Literal["1.0"] = "1.0"
    attempt_id: Identifier
    authorization_receipt_sha256: Sha256Hex
    status: Literal["completed", "failed"]
    result_sha256: Sha256Hex | None = None
    dataset_sha256: Sha256Hex | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware UTC")
        return value

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


class AnalysisExecutionManifest(StrictResearchModel):
    manifest_schema_version: Literal["1.0"] = "1.0"
    attempt_id: Identifier
    analysis_plan_sha256: Sha256Hex
    authorization_receipt_sha256: Sha256Hex
    snapshot_manifest_sha256: Sha256Hex
    files: dict[str, Sha256Hex]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware UTC")
        return value

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


def serialize_analysis_execution_outcome(outcome: AnalysisExecutionOutcome) -> bytes:
    return canonical_model_bytes(outcome)


def parse_analysis_execution_outcome(payload: bytes) -> AnalysisExecutionOutcome:
    return parse_strict(payload, AnalysisExecutionOutcome, "AnalysisExecutionOutcome")


def serialize_analysis_execution_manifest(manifest: AnalysisExecutionManifest) -> bytes:
    return canonical_model_bytes(manifest)


def parse_analysis_execution_manifest(payload: bytes) -> AnalysisExecutionManifest:
    return parse_strict(payload, AnalysisExecutionManifest, "AnalysisExecutionManifest")


def analysis_execution_schemas() -> dict[str, object]:
    return {
        "receipt": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "receipt_schema_version": {"const": "1.0"},
                "consumed": {"const": True},
            },
            "required": ["receipt_schema_version", "consumed"],
        },
        "analysis_result": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "analysis_result_schema_version": {"const": "1.0"},
            },
            "required": ["analysis_result_schema_version"],
        },
    }


__all__ = [
    "AlignedAnalysisDataset",
    "AlignedAnalysisRow",
    "AnalysisAuthorizationConsumptionReceipt",
    "AnalysisExecutionError",
    "AnalysisExecutionErrorCode",
    "AnalysisExecutionManifest",
    "AnalysisExecutionOutcome",
    "AnalysisResult",
    "CorrelationResultSummary",
    "EXECUTION_SCHEMA_VERSION",
    "OLSResultSummary",
    "PrimaryTestResult",
    "TransformedObservation",
    "TransformedVariableSeries",
    "analysis_execution_schemas",
    "calculate_aligned_analysis_dataset_sha256",
    "calculate_analysis_result_sha256",
    "calculate_transformed_variable_series_sha256",
    "canonical_model_bytes",
    "fail_execution",
    "parse_aligned_analysis_dataset",
    "parse_analysis_execution_manifest",
    "parse_analysis_execution_outcome",
    "parse_analysis_result",
    "parse_strict",
    "parse_transformed_variable_series",
    "serialize_aligned_analysis_dataset",
    "serialize_analysis_execution_manifest",
    "serialize_analysis_execution_outcome",
    "serialize_analysis_result",
    "serialize_transformed_variable_series",
]
