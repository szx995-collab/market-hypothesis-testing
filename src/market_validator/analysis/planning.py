"""Offline deterministic analysis planning (v0.4.0 Phase 1).

Compiles a ResearchSpec + verified DataReadyManifest + explicit
AnalysisPlanDecisions into an exact, immutable AnalysisPlan. No statistics
are computed, no transformations are executed, and no alignment is run in
this module.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, NoReturn

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from market_validator.data.session_schedule import (
    ExplicitSessionScheduleSnapshot,
    SessionScheduleError,
    calculate_explicit_session_schedule_snapshot_sha256,
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
)
from market_validator.research.models import (
    ResearchSpec,
    StrictResearchModel,
)

ANALYSIS_PLAN_SCHEMA_VERSION = "1.0"
ANALYSIS_DECISIONS_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]

METHOD_PROFILE_PEARSON = "pearson_correlation_v1"
METHOD_PROFILE_SPEARMAN = "spearman_correlation_v1"
METHOD_PROFILE_OLS_CLASSIC = "ols_classic_v1"
METHOD_PROFILE_OLS_HC1 = "ols_hc1_v1"
METHOD_PROFILE_OLS_NEWEY_WEST = "ols_newey_west_v1"
MethodProfile = Literal[
    "pearson_correlation_v1",
    "spearman_correlation_v1",
    "ols_classic_v1",
    "ols_hc1_v1",
    "ols_newey_west_v1",
]

TRANSFORMATION_PROFILE_LEVEL = "level_v1"
TRANSFORMATION_PROFILE_SIMPLE_RETURN = "simple_return_adjacent_v1"
TRANSFORMATION_PROFILE_LOG_RETURN = "log_return_adjacent_v1"
TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE = "signed_first_difference_v1"
TransformationProfile = Literal[
    "level_v1",
    "simple_return_adjacent_v1",
    "log_return_adjacent_v1",
    "signed_first_difference_v1",
]

ALIGNMENT_PROFILE_STRICT_SAME_SESSION = "strict_same_session_v1"

NUMERIC_PROFILE = "python_binary64_deterministic_v1"


class AnalysisPlanningErrorCode:
    INVALID_ANALYSIS_PLAN_INPUT = "invalid_analysis_plan_input"
    DATA_READY_MANIFEST_MISMATCH = "data_ready_manifest_mismatch"
    RESEARCH_SPEC_MISMATCH = "research_spec_mismatch"
    ANALYSIS_PLAN_NOT_READY = "analysis_plan_not_ready"
    ANALYSIS_PLAN_MISMATCH = "analysis_plan_mismatch"
    ANALYSIS_DECISION_INVALID = "analysis_decision_invalid"
    UNSUPPORTED_ANALYSIS_METHOD = "unsupported_analysis_method"
    UNSUPPORTED_CLAIM_TYPE = "unsupported_claim_type"
    ALIGNMENT_CONTRACT_MISSING = "alignment_contract_missing"
    TRANSFORMATION_CONTRACT_UNRESOLVED = "transformation_contract_unresolved"
    PRIMARY_TEST_INVALID = "primary_test_invalid"
    MULTIPLE_TESTING_PLAN_INVALID = "multiple_testing_plan_invalid"
    INVALID_ANALYSIS_PLAN_CONFIRMATION = "invalid_analysis_plan_confirmation"
    ANALYSIS_PLAN_CONFIRMATION_MISMATCH = "analysis_plan_confirmation_mismatch"
    INVALID_ANALYSIS_AUTHORIZATION = "invalid_analysis_authorization"
    ANALYSIS_AUTHORIZATION_MISMATCH = "analysis_authorization_mismatch"
    ANALYSIS_ACCESS_SCOPE_FORBIDDEN = "analysis_access_scope_forbidden"
    ANALYSIS_OUTPUT_CONFLICT = "analysis_output_conflict"
    ANALYSIS_OUTPUT_ERROR = "analysis_output_error"


class AnalysisPlanningStage:
    INPUT_VALIDATION = "input_validation"
    DATA_READY_VALIDATION = "data_ready_validation"
    VARIABLE_BINDING = "variable_binding"
    TRANSFORMATION_PLANNING = "transformation_planning"
    ALIGNMENT_PLANNING = "alignment_planning"
    METHOD_PLANNING = "method_planning"
    PRIMARY_TEST_PLANNING = "primary_test_planning"
    PLAN_OUTPUT = "plan_output"
    CONFIRMATION_VALIDATION = "confirmation_validation"
    AUTHORIZATION_VALIDATION = "authorization_validation"
    AUTHORIZATION_OUTPUT = "authorization_output"


class UnresolvedAnalysisRequirementCode:
    DATA_READY_MANIFEST_MISMATCH = "data_ready_manifest_mismatch"
    RESEARCH_SPEC_MISMATCH = "research_spec_mismatch"
    MISSING_VARIABLE_BUNDLE = "missing_variable_bundle"
    DUPLICATE_VARIABLE_BUNDLE = "duplicate_variable_bundle"
    UNEXPECTED_VARIABLE_BUNDLE = "unexpected_variable_bundle"
    ANALYSIS_DECISION_MISSING = "analysis_decision_missing"
    METHOD_PROFILE_MISMATCH = "method_profile_mismatch"
    UNSUPPORTED_CLAIM_TYPE = "unsupported_claim_type"
    UNSUPPORTED_ANALYSIS_METHOD = "unsupported_analysis_method"
    PRIMARY_TEST_VARIABLE_INVALID = "primary_test_variable_invalid"
    FORMULA_VARIABLE_BINDING_MISMATCH = "formula_variable_binding_mismatch"
    ALIGNMENT_CONTRACT_MISSING = "alignment_contract_missing"
    TRANSFORMATION_CONTRACT_UNRESOLVED = "transformation_contract_unresolved"
    NEWEY_WEST_LAG_MISSING = "newey_west_lag_missing"
    MULTIPLE_TESTING_FAMILY_INVALID = "multiple_testing_family_invalid"
    MINIMUM_OBSERVATION_CONTRACT_INVALID = (
        "minimum_observation_contract_invalid"
    )
    ROBUSTNESS_EXECUTION_NOT_PLANNED = "robustness_execution_not_planned"


class AnalysisPlanningError(ValueError):
    """Safe structured failure; never embeds raw data or secrets."""

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        requirement_id: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.safe_message = message
        self.requirement_id = requirement_id
        super().__init__(message)


def fail_planning(code: str, stage: str, message: str) -> NoReturn:
    raise AnalysisPlanningError(code, stage, message)


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


def _canonical_model_bytes(model: StrictResearchModel) -> bytes:
    return _canonical_bytes(
        model.model_dump(mode="json", exclude_computed_fields=True)
    )


def _parse_strict(payload: bytes, model_type, label: str):
    try:
        _strict_json_load(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        fail_planning(
            AnalysisPlanningErrorCode.INVALID_ANALYSIS_PLAN_INPUT,
            AnalysisPlanningStage.INPUT_VALIDATION,
            f"{label} failed strict JSON validation",
        )
    try:
        return model_type.model_validate_json(payload)
    except ValidationError:
        fail_planning(
            AnalysisPlanningErrorCode.INVALID_ANALYSIS_PLAN_INPUT,
            AnalysisPlanningStage.INPUT_VALIDATION,
            f"{label} failed strict domain validation",
        )


# ---------------------------------------------------------------------------
# AnalysisPlanDecisions
# ---------------------------------------------------------------------------

class TransformationDecision(StrictResearchModel):
    """Explicit per-variable transformation decision (no hidden defaults)."""

    variable_id: Identifier
    profile: TransformationProfile | None = None
    lag_periods: int = Field(ge=0)
    availability_lag_periods: int = Field(ge=0)
    required_pre_sample_periods: int = Field(ge=0)
    rolling_window_periods: int | None = Field(default=None, ge=1)


class AnalysisPlanDecisions(StrictResearchModel):
    """Strict whitelist of execution-level decisions.

    Every field must be explicit or explicitly null; there are no hidden
    defaults that could change the analysis.
    """

    decisions_schema_version: Literal["1.0"] = "1.0"
    method_profile: MethodProfile | None = None
    primary_test_variable_ids: list[Identifier] | None = None
    include_intercept: bool | None = None
    covariance_estimator: Literal["classic", "hc1", "newey_west"] | None = (
        None
    )
    newey_west_max_lags: int | None = Field(default=None, ge=0)
    same_market_join_policy: Literal["strict_same_session_v1"] | None = None
    same_market_missing_data_policy: Literal[
        "drop_observation_v1", "error_v1", "keep_missing_v1"
    ] | None = None
    transformation_decisions: dict[Identifier, TransformationDecision] = (
        Field(default_factory=dict)
    )


# ---------------------------------------------------------------------------
# Derived plan models
# ---------------------------------------------------------------------------

class TransformationPlan(StrictResearchModel):
    """Versioned, fixed transformation contract for one variable."""

    variable_id: Identifier
    research_spec_transformation: str
    profile: TransformationProfile
    rolling_window_periods: int | None = None
    lag_periods: int = Field(ge=0)
    availability_lag_periods: int = Field(ge=0)
    required_pre_sample_periods: int = Field(ge=0)
    first_value_policy: Literal["retain_first_v1", "omit_first_v1"]
    gap_policy: Literal["provider_reported_missing_excluded_v1"]
    output_unit_rule: Literal[
        "original_units_v1", "fraction_ratio_v1", "log_ratio_v1",
        "absolute_difference_v1",
    ]


class AnalysisVariableBinding(StrictResearchModel):
    """Exact binding of one ResearchSpec variable to one DataReady bundle."""

    variable_id: Identifier
    role: str
    instrument_id: Identifier
    field: str
    requirement_id: Identifier
    provider_id: str
    bundle_relative_path: str
    bundle_sha256: Sha256Hex
    source_content_sha256: Sha256Hex
    session_schedule_sha256: Sha256Hex
    expected_sample_sessions_sha256: Sha256Hex
    observed_sample_sessions_sha256: Sha256Hex
    expected_pre_sample_sessions_sha256: Sha256Hex
    observed_pre_sample_sessions_sha256: Sha256Hex
    quality_status: str
    quality_issue_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    transformation_plan: TransformationPlan


class AlignmentPlan(StrictResearchModel):
    """Exact alignment contract; no-lookahead is fixed true."""

    target_variable_id: Identifier
    target_calendar_id: Identifier
    target_session_schedule_sha256: Sha256Hex
    target_session: str
    information_cutoff: dict[str, object] | None = None
    join_policy: str
    max_staleness_days: int = Field(ge=0)
    missing_data_policy: str
    predictor_lag_periods: int = Field(ge=0)
    availability_lag_periods: int = Field(ge=0)
    no_lookahead: Literal[True] = True
    alignment_profile: str


class PrimaryTestSpec(StrictResearchModel):
    """Deterministic primary test contract; formula text is evidence only."""

    test_id: Identifier
    target_variable_id: Identifier
    parameter: Literal["pearson_r", "spearman_rho", "ols_coefficient"]
    null_value: Literal[0] = 0
    direction: str
    significance_level: float = Field(gt=0, lt=1)
    minimum_effect_size: float = Field(ge=0)
    multiple_testing_family_id: Identifier


class MultipleTestingPlan(StrictResearchModel):
    """Family plan; correction must equal the ResearchSpec declaration."""

    family_id: Identifier
    test_ids: list[Identifier] = Field(default_factory=list)
    correction: str
    family_size: int = Field(ge=0)


class UnresolvedAnalysisRequirement(StrictResearchModel):
    code: str
    message: str
    variable_id: Identifier | None = None


class AnalysisPlan(StrictResearchModel):
    """Immutable, deterministic analysis execution contract."""

    analysis_plan_schema_version: Literal["1.0"] = "1.0"
    analysis_plan_id: Identifier
    status: Literal["draft", "ready"]
    research_spec_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex
    readiness_assessment_sha256: Sha256Hex
    snapshot_manifest_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    session_schedule_sha256s: dict[str, Sha256Hex]
    claim_type: str
    method: str
    method_profile: str
    human_formula: str
    human_null_hypothesis: str
    human_alternative_hypothesis: str
    variable_bindings: list[AnalysisVariableBinding] = Field(min_length=1)
    alignment_plan: AlignmentPlan | None = None
    primary_tests: list[PrimaryTestSpec] = Field(default_factory=list)
    multiple_testing_plan: MultipleTestingPlan
    minimum_usable_observations: int = Field(ge=0)
    numeric_profile: str
    declared_robustness_checks: list[str] = Field(default_factory=list)
    declared_robustness_checks_sha256: Sha256Hex
    unresolved_requirements: list[UnresolvedAnalysisRequirement] = Field(
        default_factory=list
    )
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_consistency(self) -> "AnalysisPlan":
        if self.status == "ready" and self.unresolved_requirements:
            raise ValueError(
                "a ready plan must not carry unresolved requirements"
            )
        return self


class GeneratedAnalysisPlan(StrictResearchModel):
    analysis_plan: AnalysisPlan
    analysis_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex
    readiness_assessment_sha256: Sha256Hex
    snapshot_manifest_sha256: Sha256Hex


def serialize_analysis_plan(plan: AnalysisPlan) -> bytes:
    return _canonical_model_bytes(plan)


def parse_analysis_plan(payload: bytes) -> AnalysisPlan:
    return _parse_strict(payload, AnalysisPlan, "AnalysisPlan")


def calculate_analysis_plan_sha256(plan: AnalysisPlan) -> str:
    return _sha256_hex(serialize_analysis_plan(plan))


def _derive_analysis_plan_id(plan: AnalysisPlan) -> str:
    pending = plan.model_copy(update={"analysis_plan_id": "pending"})
    return _sha256_hex(serialize_analysis_plan(pending))[:32]


def analysis_plan_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "analysis_plan_schema_version": {"const": "1.0"},
            "status": {"enum": ["draft", "ready"]},
        },
        "required": ["analysis_plan_schema_version", "status"],
    }


__all__ = [
    "ALIGNMENT_PROFILE_STRICT_SAME_SESSION",
    "ANALYSIS_DECISIONS_SCHEMA_VERSION",
    "ANALYSIS_PLAN_SCHEMA_VERSION",
    "AnalysisPlan",
    "AnalysisPlanDecisions",
    "AnalysisPlanningError",
    "AnalysisPlanningErrorCode",
    "AnalysisPlanningStage",
    "AnalysisVariableBinding",
    "AlignmentPlan",
    "GeneratedAnalysisPlan",
    "METHOD_PROFILE_OLS_CLASSIC",
    "METHOD_PROFILE_OLS_HC1",
    "METHOD_PROFILE_OLS_NEWEY_WEST",
    "METHOD_PROFILE_PEARSON",
    "METHOD_PROFILE_SPEARMAN",
    "MultipleTestingPlan",
    "NUMERIC_PROFILE",
    "PrimaryTestSpec",
    "TRANSFORMATION_PROFILE_LEVEL",
    "TRANSFORMATION_PROFILE_LOG_RETURN",
    "TRANSFORMATION_PROFILE_SIGNED_DIFFERENCE",
    "TRANSFORMATION_PROFILE_SIMPLE_RETURN",
    "TransformationDecision",
    "TransformationPlan",
    "UnresolvedAnalysisRequirement",
    "UnresolvedAnalysisRequirementCode",
    "analysis_plan_schema",
    "calculate_analysis_plan_sha256",
    "fail_planning",
    "parse_analysis_plan",
    "serialize_analysis_plan",
]
