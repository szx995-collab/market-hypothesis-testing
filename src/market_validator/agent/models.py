"""Research agent models (v0.4.0 Phase 4).

ResearchSession / ArtifactReference / NextAction are strict, canonical,
immutable models. The session stores only references to artifacts; every
reference is re-verified (parser + canonical hash) on load.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, NoReturn

from pydantic import (
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

from market_validator.research.models import StrictResearchModel

SESSION_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]

SESSION_STATUS = Literal["active", "needs_user", "blocked", "completed", "failed"]

SESSION_STAGES = Literal[
    "hypothesis_proposal",
    "hypothesis_clarification",
    "hypothesis_confirmation",
    "research_spec",
    "data_plan",
    "data_plan_confirmation",
    "source_selection",
    "source_selection_confirmation",
    "acquisition_planning",
    "data_access_authorization",
    "data_acquisition",
    "data_readiness",
    "analysis_planning",
    "analysis_confirmation",
    "analysis_authorization",
    "analysis_execution",
    "interpretation",
    "completed",
]

NEXT_ACTION_TYPES = Literal[
    "request_llm_access",
    "answer_hypothesis_questions",
    "complete_research_spec",
    "confirm_hypothesis",
    "confirm_data_plan",
    "choose_data_source",
    "configure_data_interface",
    "provide_local_csv",
    "confirm_source_selection",
    "authorize_data_access",
    "authorize_analysis_plan",
    "execute_analysis",
    "configure_interpretation_llm",
    "review_result",
    "completed",
    "blocked",
]


class ResearchAgentError(ValueError):
    """Safe structured failure; never embeds credentials or raw data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class ResearchAgentErrorCode:
    INVALID_AGENT_INPUT = "invalid_agent_input"
    SESSION_ARTIFACT_MISMATCH = "session_artifact_mismatch"
    SESSION_REVISION_CONFLICT = "session_revision_conflict"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_NOT_RESUMABLE = "session_not_resumable"
    AGENT_NETWORK_NOT_AUTHORIZED = "agent_network_not_authorized"
    AGENT_STATE_INVALID = "agent_state_invalid"
    AGENT_TRANSITION_LIMIT = "agent_transition_limit"
    AGENT_INPUT_REQUIRED = "agent_input_required"
    AGENT_OUTPUT_CONFLICT = "agent_output_conflict"
    AGENT_OUTPUT_ERROR = "agent_output_error"


def fail_agent(code: str, message: str) -> NoReturn:
    raise ResearchAgentError(code, message)


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


def canonical_bytes(payload: object) -> bytes:
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


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_model_bytes(model: StrictResearchModel) -> bytes:
    return canonical_bytes(
        model.model_dump(mode="json", exclude_computed_fields=True)
    )


def parse_strict(payload: bytes, model_type, label: str):
    from pydantic import ValidationError

    try:
        _strict_json_load(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        fail_agent(
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
            f"{label} failed strict JSON validation",
        )
    try:
        return model_type.model_validate_json(payload)
    except ValidationError:
        fail_agent(
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
            f"{label} failed strict domain validation",
        )


def _validate_relative_component(name: str, label: str) -> None:
    if not name or name in (".", ".."):
        fail_agent(
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
            f"{label} is not a valid relative component",
        )
    if "/" in name or "\\" in name or ":" in name:
        fail_agent(
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
            f"{label} is not a valid relative component",
        )
    from pathlib import Path

    if Path(name).is_absolute():
        fail_agent(
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
            f"{label} is not a valid relative component",
        )


# ---------------------------------------------------------------------------
# ArtifactReference
# ---------------------------------------------------------------------------

class ArtifactReference(StrictResearchModel):
    artifact_type: str
    relative_path: str
    sha256: Sha256Hex
    schema_version: str
    created_at: datetime

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        for part in value.split("/"):
            _validate_relative_component(part, "artifact path")
        return value

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# NextAction
# ---------------------------------------------------------------------------

class NextAction(StrictResearchModel):
    action_type: NEXT_ACTION_TYPES
    title: str
    message: str
    why_needed: str
    required_inputs: list[str] = Field(default_factory=list)
    available_choices: list[str] = Field(default_factory=list)
    artifact_summary: dict[str, str] = Field(default_factory=dict)
    safe_command_hint: str = ""
    source_error_code: str | None = None


# ---------------------------------------------------------------------------
# ResearchSession
# ---------------------------------------------------------------------------

class ResearchSession(StrictResearchModel):
    session_schema_version: Literal["1.0"] = "1.0"
    session_id: Identifier
    revision: int = Field(ge=1)
    parent_session_id: Identifier | None = None
    created_at: datetime
    updated_at: datetime
    language: str = "zh-CN"
    status: SESSION_STATUS
    current_stage: SESSION_STAGES
    original_question: str
    artifact_references: list[ArtifactReference] = Field(default_factory=list)
    current_next_action: NextAction | None = None
    last_error: str | None = None
    completed_report: str | None = None
    history_summary: list[str] = Field(default_factory=list)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session timestamps must be timezone-aware")
        return value

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamps(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


def serialize_research_session(session: ResearchSession) -> bytes:
    return canonical_model_bytes(session)


def parse_research_session(payload: bytes) -> ResearchSession:
    return parse_strict(payload, ResearchSession, "ResearchSession")


def calculate_research_session_sha256(session: ResearchSession) -> str:
    return sha256_hex(serialize_research_session(session))


def _derive_session_id(
    original_question: str, created_at: datetime, parent_session_id: str | None
) -> str:
    identity = canonical_bytes(
        {
            "question": original_question,
            "created_at": created_at.isoformat(),
            "parent_session_id": parent_session_id,
        }
    )
    return sha256_hex(identity)[:32]


__all__ = [
    "ArtifactReference",
    "NEXT_ACTION_TYPES",
    "NextAction",
    "ResearchAgentError",
    "ResearchAgentErrorCode",
    "ResearchSession",
    "SESSION_SCHEMA_VERSION",
    "SESSION_STAGES",
    "SESSION_STATUS",
    "calculate_research_session_sha256",
    "canonical_bytes",
    "canonical_model_bytes",
    "fail_agent",
    "parse_research_session",
    "parse_strict",
    "serialize_research_session",
    "sha256_hex",
]
