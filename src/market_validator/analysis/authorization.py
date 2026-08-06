"""AnalysisPlanConfirmation and AnalysisAuthorization (v0.4.0 Phase 1).

A confirmation binds the user to one exact AnalysisPlan; an authorization
permits exactly one deterministic execution attempt of that confirmed plan.
Neither computes statistics, runs robustness, or generates reports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from market_validator.analysis.planning import (
    AnalysisPlan,
    AnalysisPlanningErrorCode,
    AnalysisPlanningStage,
    Sha256Hex,
    _canonical_bytes,
    _sha256_hex,
    calculate_analysis_plan_sha256,
    fail_planning,
    parse_analysis_plan,
    serialize_analysis_plan,
)
from market_validator.analysis.planning_generator import validate_analysis_plan
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    persist_immutable_bytes,
)
from market_validator.research.models import StrictResearchModel

CONFIRMATION_SCHEMA_VERSION = "1.0"
AUTHORIZATION_SCHEMA_VERSION = "1.0"
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]

ANALYSIS_PLAN_CONFIRMATION_STATEMENT = (
    "I explicitly confirm this exact AnalysisPlan for a separately "
    "authorized analysis execution."
)

ANALYSIS_AUTHORIZATION_STATEMENT = (
    "I explicitly authorize one deterministic execution attempt of this "
    "exact confirmed AnalysisPlan."
)


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
    import json

    json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_number,
    )


def _parse_strict(payload: bytes, model_type, label: str):
    import json

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


def _canonical_model_bytes(model: StrictResearchModel) -> bytes:
    import json

    return (
        json.dumps(
            model.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class AnalysisPlanConfirmation(StrictResearchModel):
    """Explicit user confirmation bound to one exact AnalysisPlan."""

    confirmation_schema_version: Literal["1.0"] = "1.0"
    analysis_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex
    confirmed: Literal[True] = True
    confirmation_statement: str = ANALYSIS_PLAN_CONFIRMATION_STATEMENT
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def validate_confirmed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("confirmed_at must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @field_validator("confirmation_statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        if value != ANALYSIS_PLAN_CONFIRMATION_STATEMENT:
            raise ValueError("confirmation statement is fixed")
        return value

    @field_serializer("confirmed_at", when_used="json")
    def serialize_confirmed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )


class AnalysisAuthorization(StrictResearchModel):
    """Single-use permission for one deterministic execution attempt."""

    authorization_schema_version: Literal["1.0"] = "1.0"
    authorization_id: Identifier
    analysis_plan_sha256: Sha256Hex
    analysis_plan_confirmation_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex
    authorized_primary_test_ids: list[Identifier]
    analysis_execution_authorized: Literal[True] = True
    robustness_execution_authorized: Literal[False] = False
    report_generation_authorized: Literal[False] = False
    network_access_authorized: Literal[False] = False
    provider_access_authorized: Literal[False] = False
    data_mutation_authorized: Literal[False] = False
    automatic_retry_authorized: Literal[False] = False
    fallback_authorized: Literal[False] = False
    single_use: Literal[True] = True
    authorization_statement: str = ANALYSIS_AUTHORIZATION_STATEMENT
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def validate_authorized_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorized_at must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @field_validator("authorization_statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        if value != ANALYSIS_AUTHORIZATION_STATEMENT:
            raise ValueError("authorization statement is fixed")
        return value

    @field_serializer("authorized_at", when_used="json")
    def serialize_authorized_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )


# ---------------------------------------------------------------------------
# Confirmation API
# ---------------------------------------------------------------------------

def confirm_analysis_plan(
    plan: AnalysisPlan,
    *,
    confirmed_at: datetime,
) -> AnalysisPlanConfirmation:
    """Confirm a Ready AnalysisPlan with no unresolved requirements."""
    validate_analysis_plan(plan)
    if plan.status != "ready":
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_NOT_READY,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "only a ready analysis plan can be confirmed",
        )
    if plan.unresolved_requirements:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_NOT_READY,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "an analysis plan with unresolved requirements cannot be "
            "confirmed",
        )
    plan_sha256 = calculate_analysis_plan_sha256(plan)
    return AnalysisPlanConfirmation(
        confirmation_schema_version="1.0",
        analysis_plan_sha256=plan_sha256,
        research_spec_sha256=plan.research_spec_sha256,
        data_ready_manifest_sha256=plan.data_ready_manifest_sha256,
        confirmed=True,
        confirmation_statement=ANALYSIS_PLAN_CONFIRMATION_STATEMENT,
        confirmed_at=confirmed_at,
    )


def validate_analysis_plan_confirmation_matches(
    plan: AnalysisPlan,
    confirmation: AnalysisPlanConfirmation,
) -> None:
    """Recompute and compare every bound field."""
    if (
        confirmation.analysis_plan_sha256
        != calculate_analysis_plan_sha256(plan)
    ):
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_CONFIRMATION_MISMATCH,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "confirmation does not match the analysis plan",
        )
    if confirmation.research_spec_sha256 != plan.research_spec_sha256:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_CONFIRMATION_MISMATCH,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "confirmation research-spec hash does not match the plan",
        )
    if (
        confirmation.data_ready_manifest_sha256
        != plan.data_ready_manifest_sha256
    ):
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_CONFIRMATION_MISMATCH,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "confirmation manifest hash does not match the plan",
        )


def serialize_analysis_plan_confirmation(
    confirmation: AnalysisPlanConfirmation,
) -> bytes:
    return _canonical_model_bytes(confirmation)


def parse_analysis_plan_confirmation(payload: bytes) -> AnalysisPlanConfirmation:
    return _parse_strict(
        payload, AnalysisPlanConfirmation, "AnalysisPlanConfirmation"
    )


def calculate_analysis_plan_confirmation_sha256(
    confirmation: AnalysisPlanConfirmation,
) -> str:
    return _sha256_hex(serialize_analysis_plan_confirmation(confirmation))


def analysis_plan_confirmation_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "confirmation_schema_version": {"const": "1.0"},
            "confirmed": {"const": True},
        },
        "required": ["confirmation_schema_version", "confirmed"],
    }


def persist_analysis_plan_confirmation(
    confirmation: AnalysisPlanConfirmation,
    output_path: str | Path,
) -> Path:
    payload = serialize_analysis_plan_confirmation(confirmation)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_planning(
                AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_CONFLICT,
                AnalysisPlanningStage.CONFIRMATION_VALIDATION,
                "confirmation output path already exists with different "
                "content",
            )
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "confirmation could not be persisted",
        )
    try:
        restored = parse_analysis_plan_confirmation(persisted.read_bytes())
    except Exception:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "persisted confirmation failed reload validation",
        )
    if restored != confirmation:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.CONFIRMATION_VALIDATION,
            "persisted confirmation does not match the canonical bytes",
        )
    return persisted


# ---------------------------------------------------------------------------
# Authorization API
# ---------------------------------------------------------------------------

def create_analysis_authorization(
    plan: AnalysisPlan,
    confirmation: AnalysisPlanConfirmation,
    *,
    authorized_at: datetime,
) -> AnalysisAuthorization:
    """Create a single-use authorization for one exact confirmed plan."""
    validate_analysis_plan(plan)
    validate_analysis_plan_confirmation_matches(plan, confirmation)
    if plan.status != "ready" or plan.unresolved_requirements:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_NOT_READY,
            AnalysisPlanningStage.AUTHORIZATION_VALIDATION,
            "only a ready confirmed plan can be authorized",
        )
    expected_ids = [test.test_id for test in plan.primary_tests]
    if confirmation.analysis_plan_sha256 != calculate_analysis_plan_sha256(
        plan
    ):
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_AUTHORIZATION_MISMATCH,
            AnalysisPlanningStage.AUTHORIZATION_VALIDATION,
            "authorization must bind the exact confirmed plan",
        )
    authorization = AnalysisAuthorization(
        authorization_schema_version="1.0",
        authorization_id="pending",
        analysis_plan_sha256=calculate_analysis_plan_sha256(plan),
        analysis_plan_confirmation_sha256=(
            calculate_analysis_plan_confirmation_sha256(confirmation)
        ),
        research_spec_sha256=plan.research_spec_sha256,
        data_ready_manifest_sha256=plan.data_ready_manifest_sha256,
        authorized_primary_test_ids=list(expected_ids),
        analysis_execution_authorized=True,
        robustness_execution_authorized=False,
        report_generation_authorized=False,
        network_access_authorized=False,
        provider_access_authorized=False,
        data_mutation_authorized=False,
        automatic_retry_authorized=False,
        fallback_authorized=False,
        single_use=True,
        authorization_statement=ANALYSIS_AUTHORIZATION_STATEMENT,
        authorized_at=authorized_at,
    )
    authorization_id = _derive_authorization_id(authorization)
    return authorization.model_copy(
        update={"authorization_id": authorization_id}
    )


def _derive_authorization_id(authorization: AnalysisAuthorization) -> str:
    pending = authorization.model_copy(update={"authorization_id": "pending"})
    return _sha256_hex(serialize_analysis_authorization(pending))[:20]


def validate_analysis_authorization_matches(
    plan: AnalysisPlan,
    confirmation: AnalysisPlanConfirmation,
    authorization: AnalysisAuthorization,
) -> None:
    """Recompute and compare every bound field."""
    validate_analysis_plan_confirmation_matches(plan, confirmation)
    expected = {
        "authorization_id": _derive_authorization_id(authorization),
        "analysis_plan_sha256": calculate_analysis_plan_sha256(plan),
        "analysis_plan_confirmation_sha256": (
            calculate_analysis_plan_confirmation_sha256(confirmation)
        ),
        "research_spec_sha256": plan.research_spec_sha256,
        "data_ready_manifest_sha256": plan.data_ready_manifest_sha256,
        "authorized_primary_test_ids": [
            test.test_id for test in plan.primary_tests
        ],
    }
    actual = {
        "authorization_id": authorization.authorization_id,
        "analysis_plan_sha256": authorization.analysis_plan_sha256,
        "analysis_plan_confirmation_sha256": (
            authorization.analysis_plan_confirmation_sha256
        ),
        "research_spec_sha256": authorization.research_spec_sha256,
        "data_ready_manifest_sha256": authorization.data_ready_manifest_sha256,
        "authorized_primary_test_ids": list(
            authorization.authorized_primary_test_ids
        ),
    }
    if expected != actual:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_AUTHORIZATION_MISMATCH,
            AnalysisPlanningStage.AUTHORIZATION_VALIDATION,
            "authorization does not match the confirmed plan",
        )


def serialize_analysis_authorization(
    authorization: AnalysisAuthorization,
) -> bytes:
    return _canonical_model_bytes(authorization)


def parse_analysis_authorization(payload: bytes) -> AnalysisAuthorization:
    return _parse_strict(payload, AnalysisAuthorization, "AnalysisAuthorization")


def calculate_analysis_authorization_sha256(
    authorization: AnalysisAuthorization,
) -> str:
    return _sha256_hex(serialize_analysis_authorization(authorization))


def analysis_authorization_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "authorization_schema_version": {"const": "1.0"},
            "analysis_execution_authorized": {"const": True},
            "robustness_execution_authorized": {"const": False},
            "report_generation_authorized": {"const": False},
            "single_use": {"const": True},
        },
        "required": ["authorization_schema_version", "authorization_id"],
    }


def persist_analysis_authorization(
    authorization: AnalysisAuthorization,
    output_path: str | Path,
) -> Path:
    payload = serialize_analysis_authorization(authorization)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_planning(
                AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_CONFLICT,
                AnalysisPlanningStage.AUTHORIZATION_OUTPUT,
                "authorization output path already exists with different "
                "content",
            )
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.AUTHORIZATION_OUTPUT,
            "authorization could not be persisted",
        )
    try:
        restored = parse_analysis_authorization(persisted.read_bytes())
    except Exception:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.AUTHORIZATION_OUTPUT,
            "persisted authorization failed reload validation",
        )
    if restored != authorization:
        fail_planning(
            AnalysisPlanningErrorCode.ANALYSIS_OUTPUT_ERROR,
            AnalysisPlanningStage.AUTHORIZATION_OUTPUT,
            "persisted authorization does not match the canonical bytes",
        )
    return persisted


__all__ = [
    "ANALYSIS_AUTHORIZATION_STATEMENT",
    "ANALYSIS_PLAN_CONFIRMATION_STATEMENT",
    "AUTHORIZATION_SCHEMA_VERSION",
    "AnalysisAuthorization",
    "AnalysisPlanConfirmation",
    "CONFIRMATION_SCHEMA_VERSION",
    "analysis_authorization_schema",
    "analysis_plan_confirmation_schema",
    "calculate_analysis_authorization_sha256",
    "calculate_analysis_plan_confirmation_sha256",
    "confirm_analysis_plan",
    "create_analysis_authorization",
    "parse_analysis_authorization",
    "parse_analysis_plan_confirmation",
    "persist_analysis_authorization",
    "persist_analysis_plan_confirmation",
    "serialize_analysis_authorization",
    "serialize_analysis_plan_confirmation",
    "validate_analysis_authorization_matches",
    "validate_analysis_plan_confirmation_matches",
]
