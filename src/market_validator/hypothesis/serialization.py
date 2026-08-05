"""Strict parsing and deterministic serialization for hypothesis proposals."""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from typing import NoReturn

from pydantic import ValidationError

from market_validator.hypothesis.models import ResearchHypothesisProposal
from market_validator.research.models import StrictResearchModel


class HypothesisProposalErrorCode(StrEnum):
    INVALID_PROPOSAL = "invalid_hypothesis_proposal"
    BACKEND_UNAVAILABLE = "hypothesis_backend_unavailable"
    BACKEND_IDENTITY_MISMATCH = "hypothesis_backend_identity_mismatch"
    OUTPUT_CONFLICT = "hypothesis_output_conflict"
    OUTPUT_ERROR = "hypothesis_output_error"


class HypothesisProposalStage(StrEnum):
    PROPOSAL_VALIDATION = "hypothesis_proposal_validation"
    BACKEND_GENERATION = "hypothesis_backend_generation"
    PROPOSAL_OUTPUT = "hypothesis_proposal_output"


class HypothesisProposalFailure(StrictResearchModel):
    code: HypothesisProposalErrorCode
    stage: HypothesisProposalStage
    message: str


class HypothesisProposalError(ValueError):
    """Safe failure without raw model output, credentials, or stack details."""

    def __init__(self, failure: HypothesisProposalFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_hypothesis_proposal(
    code: HypothesisProposalErrorCode,
    stage: HypothesisProposalStage,
    message: str,
) -> NoReturn:
    raise HypothesisProposalError(
        HypothesisProposalFailure(code=code, stage=stage, message=message)
    )


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> NoReturn:
    raise ValueError(value)


def parse_research_hypothesis_proposal(
    response_bytes: bytes | bytearray,
) -> ResearchHypothesisProposal:
    """Accept exactly one strict UTF-8 JSON object and nothing else."""

    if not isinstance(response_bytes, (bytes, bytearray)):
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "hypothesis proposal must be supplied as UTF-8 bytes",
        )
    normalized = bytes(response_bytes)
    try:
        text = normalized.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "hypothesis proposal must be exactly one strict UTF-8 JSON object",
        )
    if not isinstance(decoded, dict):
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "hypothesis proposal must be a JSON object",
        )
    try:
        return ResearchHypothesisProposal.model_validate_json(normalized)
    except ValidationError:
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "hypothesis proposal failed strict domain validation",
        )


def serialize_research_hypothesis_proposal(
    proposal: ResearchHypothesisProposal,
) -> bytes:
    """Return canonical UTF-8 bytes that strictly round-trip."""

    if not isinstance(proposal, ResearchHypothesisProposal):
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "proposal must be a validated ResearchHypothesisProposal",
        )
    try:
        payload = (
            json.dumps(
                proposal.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "proposal could not be serialized as strict JSON",
        )
    if parse_research_hypothesis_proposal(payload) != proposal:
        fail_hypothesis_proposal(
            HypothesisProposalErrorCode.INVALID_PROPOSAL,
            HypothesisProposalStage.PROPOSAL_VALIDATION,
            "proposal does not round-trip exactly",
        )
    return payload


def calculate_research_hypothesis_proposal_sha256(
    proposal: ResearchHypothesisProposal,
) -> str:
    return hashlib.sha256(serialize_research_hypothesis_proposal(proposal)).hexdigest()


def research_hypothesis_proposal_json_schema() -> dict[str, object]:
    return ResearchHypothesisProposal.model_json_schema()


__all__ = [
    "HypothesisProposalError",
    "HypothesisProposalErrorCode",
    "HypothesisProposalFailure",
    "HypothesisProposalStage",
    "calculate_research_hypothesis_proposal_sha256",
    "fail_hypothesis_proposal",
    "parse_research_hypothesis_proposal",
    "research_hypothesis_proposal_json_schema",
    "serialize_research_hypothesis_proposal",
]
