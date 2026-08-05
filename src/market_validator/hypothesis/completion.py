"""Explicit ResearchSpec completion answers that produce a new Proposal version.

Completion is a Proposal-layer operation: it fills the missing
``research_spec_inputs`` on a *new* Proposal version and forces the old
confirmation to become invalid because the canonical Proposal hash changes.
It never bypasses the Proposal or directly creates a ResearchSpec.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import StringConstraints, field_serializer, field_validator

from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    HypothesisLifecycleStage,
    fail_lifecycle,
    parse_strict_model,
    persist_immutable_bytes,
    serialize_strict_model,
)
from market_validator.hypothesis.models import (
    ResearchHypothesisProposal,
    ResearchSpecCompilationInputs,
)
from market_validator.hypothesis.serialization import (
    HypothesisProposalError,
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_proposal,
    serialize_research_hypothesis_proposal,
)
from market_validator.research.models import StrictResearchModel


COMPLETION_SCHEMA_VERSION = "1.0"
COMPLETION_STATEMENT = (
    "I explicitly provide the missing ResearchSpec compilation inputs "
    "for this exact Proposal."
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ResearchSpecCompletionAnswers(StrictResearchModel):
    """Auditable, Proposal-bound explicit values for research_spec_inputs."""

    completion_schema_version: Literal["1.0"] = COMPLETION_SCHEMA_VERSION
    source_proposal_sha256: Sha256Hex
    source_confirmation_sha256: Sha256Hex | None = None
    research_spec_inputs: ResearchSpecCompilationInputs
    completion_statement: Literal[
        "I explicitly provide the missing ResearchSpec compilation inputs "
        "for this exact Proposal."
    ] = COMPLETION_STATEMENT
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("completed_at", when_used="json")
    def serialize_completed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AppliedResearchSpecCompletion(StrictResearchModel):
    """The new Proposal version and the identity of its source."""

    source_proposal_sha256: Sha256Hex
    source_confirmation_sha256: Sha256Hex | None
    completed_proposal_sha256: Sha256Hex
    proposal: ResearchHypothesisProposal


def parse_research_spec_completion_answers(
    payload: bytes | bytearray,
) -> ResearchSpecCompletionAnswers:
    return parse_strict_model(
        payload,
        ResearchSpecCompletionAnswers,
        code=HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        stage=HypothesisLifecycleStage.COMPLETION_VALIDATION,
        label="ResearchSpec completion answers",
    )


def serialize_research_spec_completion_answers(
    answers: ResearchSpecCompletionAnswers,
) -> bytes:
    return serialize_strict_model(
        answers,
        ResearchSpecCompletionAnswers,
        parser=parse_research_spec_completion_answers,
        code=HypothesisLifecycleErrorCode.INVALID_COMPLETION,
        stage=HypothesisLifecycleStage.COMPLETION_VALIDATION,
        label="ResearchSpec completion answers",
    )


def research_spec_completion_json_schema() -> dict[str, object]:
    return ResearchSpecCompletionAnswers.model_json_schema()


def apply_research_spec_completion_answers(
    proposal: ResearchHypothesisProposal,
    answers: ResearchSpecCompletionAnswers,
) -> AppliedResearchSpecCompletion:
    """Apply only research_spec_inputs and rerun the complete Proposal contract."""

    proposal = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(proposal)
    )
    answers = parse_research_spec_completion_answers(
        serialize_research_spec_completion_answers(answers)
    )
    source_sha256 = calculate_research_hypothesis_proposal_sha256(proposal)
    if answers.source_proposal_sha256 != source_sha256:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.COMPLETION_MISMATCH,
            HypothesisLifecycleStage.COMPLETION_APPLICATION,
            "completion answers do not match the canonical source Proposal SHA-256",
        )
    if proposal.research_spec_inputs is not None:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
            HypothesisLifecycleStage.COMPLETION_APPLICATION,
            "source Proposal already contains research_spec_inputs; "
            "a second completion source is refused",
        )
    declared_ids = {
        proposal.outcome.variable_id,
        *(variable.variable_id for variable in proposal.predictors),
        *(variable.variable_id for variable in proposal.controls),
    }
    input_ids = {
        item.variable_id for item in answers.research_spec_inputs.variables
    }
    if input_ids != declared_ids:
        missing = sorted(declared_ids - input_ids)
        unknown = sorted(input_ids - declared_ids)
        details = []
        if missing:
            details.append("missing mappings: " + ", ".join(missing))
        if unknown:
            details.append("unknown variable ids: " + ", ".join(unknown))
        fail_lifecycle(
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
            HypothesisLifecycleStage.COMPLETION_APPLICATION,
            "completion inputs must exactly cover the declared Proposal "
            "variables; " + "; ".join(details),
        )
    draft_by_id = {
        variable.variable_id: variable
        for variable in (
            proposal.outcome,
            *proposal.predictors,
            *proposal.controls,
        )
    }
    for item in answers.research_spec_inputs.variables:
        draft = draft_by_id[item.variable_id]
        if (
            draft.asset_type is not None
            and item.instrument.asset_type is not draft.asset_type
        ):
            fail_lifecycle(
                HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
                HypothesisLifecycleStage.COMPLETION_APPLICATION,
                f"completion instrument asset type conflicts for "
                f"{item.variable_id}",
            )

    proposal_data = proposal.model_dump(
        mode="json",
        exclude_computed_fields=True,
        exclude_unset=True,
    )
    proposal_data["research_spec_inputs"] = answers.research_spec_inputs.model_dump(
        mode="json"
    )
    try:
        completed = parse_research_hypothesis_proposal(
            json.dumps(
                proposal_data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except HypothesisProposalError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.COMPLETION_CONFLICT,
            HypothesisLifecycleStage.COMPLETION_APPLICATION,
            "completion inputs conflict with the Proposal domain contract",
        )
    completed = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(completed)
    )
    return AppliedResearchSpecCompletion(
        source_proposal_sha256=source_sha256,
        source_confirmation_sha256=answers.source_confirmation_sha256,
        completed_proposal_sha256=calculate_research_hypothesis_proposal_sha256(
            completed
        ),
        proposal=completed,
    )


def persist_completed_research_hypothesis_proposal(
    applied: AppliedResearchSpecCompletion,
    output_path: str | Path,
) -> Path:
    payload = serialize_research_hypothesis_proposal(applied.proposal)
    return persist_immutable_bytes(payload, output_path)


__all__ = [
    "AppliedResearchSpecCompletion",
    "COMPLETION_SCHEMA_VERSION",
    "COMPLETION_STATEMENT",
    "ResearchSpecCompletionAnswers",
    "apply_research_spec_completion_answers",
    "parse_research_spec_completion_answers",
    "persist_completed_research_hypothesis_proposal",
    "research_spec_completion_json_schema",
    "serialize_research_spec_completion_answers",
]
