"""Explicit user confirmation bound to canonical hypothesis proposal bytes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import StringConstraints, field_serializer, field_validator

from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    HypothesisLifecycleStage,
    fail_lifecycle,
    parse_strict_model,
    persist_immutable_bytes,
    serialize_strict_model,
)
from market_validator.hypothesis.models import ResearchHypothesisProposal
from market_validator.hypothesis.serialization import (
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_proposal,
    serialize_research_hypothesis_proposal,
)
from market_validator.research.models import StrictResearchModel


CONFIRMATION_SCHEMA_VERSION = "1.0"
CONFIRMATION_STATEMENT = (
    "I explicitly confirm this exact proposal for ResearchSpec generation."
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ResearchHypothesisProposalConfirmation(StrictResearchModel):
    confirmation_schema_version: Literal["1.0"] = CONFIRMATION_SCHEMA_VERSION
    proposal_sha256: Sha256Hex
    confirmed: Literal[True]
    confirmation_statement: Literal[
        "I explicitly confirm this exact proposal for ResearchSpec generation."
    ] = CONFIRMATION_STATEMENT
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def validate_confirmed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("confirmed_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("confirmed_at", when_used="json")
    def serialize_confirmed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_research_hypothesis_confirmation(
    payload: bytes | bytearray,
) -> ResearchHypothesisProposalConfirmation:
    return parse_strict_model(
        payload,
        ResearchHypothesisProposalConfirmation,
        code=HypothesisLifecycleErrorCode.INVALID_CONFIRMATION,
        stage=HypothesisLifecycleStage.CONFIRMATION_VALIDATION,
        label="proposal confirmation",
    )


def serialize_research_hypothesis_confirmation(
    confirmation: ResearchHypothesisProposalConfirmation,
) -> bytes:
    return serialize_strict_model(
        confirmation,
        ResearchHypothesisProposalConfirmation,
        parser=parse_research_hypothesis_confirmation,
        code=HypothesisLifecycleErrorCode.INVALID_CONFIRMATION,
        stage=HypothesisLifecycleStage.CONFIRMATION_VALIDATION,
        label="proposal confirmation",
    )


def calculate_research_hypothesis_confirmation_sha256(
    confirmation: ResearchHypothesisProposalConfirmation,
) -> str:
    return hashlib.sha256(
        serialize_research_hypothesis_confirmation(confirmation)
    ).hexdigest()


def confirmation_json_schema() -> dict[str, object]:
    return ResearchHypothesisProposalConfirmation.model_json_schema()


def confirm_research_hypothesis_proposal(
    proposal: ResearchHypothesisProposal,
    *,
    confirmed_at: datetime,
) -> ResearchHypothesisProposalConfirmation:
    """Create a record only for a fully reviewed, blocker-free proposal."""

    proposal = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(proposal)
    )
    if (
        proposal.ready_for_spec_review is not True
        or proposal.ambiguities
        or proposal.unsupported_requests
        or proposal.readiness_blockers()
    ):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.PROPOSAL_NOT_READY,
            HypothesisLifecycleStage.CONFIRMATION_VALIDATION,
            "only a fully validated ready proposal without blockers can be confirmed",
        )
    return ResearchHypothesisProposalConfirmation(
        proposal_sha256=calculate_research_hypothesis_proposal_sha256(proposal),
        confirmed=True,
        confirmed_at=confirmed_at,
    )


def validate_confirmation_matches_proposal(
    proposal: ResearchHypothesisProposal,
    confirmation: ResearchHypothesisProposalConfirmation,
) -> None:
    expected = calculate_research_hypothesis_proposal_sha256(proposal)
    if confirmation.proposal_sha256 != expected:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.CONFIRMATION_MISMATCH,
            HypothesisLifecycleStage.CONFIRMATION_VALIDATION,
            "confirmation does not match the canonical proposal SHA-256",
        )


def persist_research_hypothesis_confirmation(
    confirmation: ResearchHypothesisProposalConfirmation,
    output_path: str | Path,
) -> Path:
    return persist_immutable_bytes(
        serialize_research_hypothesis_confirmation(confirmation),
        output_path,
    )


__all__ = [
    "CONFIRMATION_STATEMENT",
    "ResearchHypothesisProposalConfirmation",
    "calculate_research_hypothesis_confirmation_sha256",
    "confirmation_json_schema",
    "confirm_research_hypothesis_proposal",
    "parse_research_hypothesis_confirmation",
    "persist_research_hypothesis_confirmation",
    "serialize_research_hypothesis_confirmation",
    "validate_confirmation_matches_proposal",
]
