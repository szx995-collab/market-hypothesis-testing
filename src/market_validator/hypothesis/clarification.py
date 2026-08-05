"""Deterministic, field-whitelisted clarification of hypothesis proposals."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    HypothesisLifecycleStage,
    fail_lifecycle,
    parse_strict_model,
    persist_immutable_bytes,
    serialize_strict_model,
)
from market_validator.hypothesis.models import (
    HypothesisTimeRelationDraft,
    HypothesisVariableDraft,
    Identifier,
    ResearchHypothesisProposal,
    ResearchSpecCompilationInputs,
    TargetParameterSpec,
    render_statistical_hypotheses,
)
from market_validator.hypothesis.serialization import (
    HypothesisProposalError,
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_proposal,
    serialize_research_hypothesis_proposal,
)
from market_validator.research.enums import (
    AssetType,
    ContractRollMethod,
    Direction,
    Frequency,
    ModelMethod,
    TargetSession,
    Transformation,
)
from market_validator.research.models import (
    InformationCutoffSpec,
    StrictResearchModel,
)


CLARIFICATION_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
AmbiguityId = Annotated[
    str,
    StringConstraints(pattern=r"^ambiguity-[0-9a-f]{16}$"),
]


class ProposalAmbiguityReference(StrictResearchModel):
    ambiguity_id: AmbiguityId
    position: Annotated[int, Field(ge=0)]
    text: Annotated[str, StringConstraints(min_length=1)]


class VariableClarificationUpdate(StrictResearchModel):
    variable_id: Identifier
    asset_type: AssetType | None = None
    transformation: Transformation | None = None
    time_relation: HypothesisTimeRelationDraft | None = None
    proxy_for: str | None = None
    contract_roll_method: ContractRollMethod | None = None

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if not (self.model_fields_set - {"variable_id"}):
            raise ValueError("variable clarification must set at least one field")
        return self


class SampleClarificationUpdate(StrictResearchModel):
    start_date: date | None = None
    end_date: date | None = None
    frequency: Frequency | None = None

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("sample clarification must set at least one field")
        return self


class AlignmentClarificationUpdate(StrictResearchModel):
    target_market: str | None = None
    target_timezone: str | None = None
    target_calendar: str | None = None
    target_session: TargetSession | None = None
    information_cutoff: InformationCutoffSpec | None = None

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("alignment clarification must set at least one field")
        return self


class StatisticalClarificationUpdate(StrictResearchModel):
    statistical_method: ModelMethod | None = None
    target_parameter: TargetParameterSpec | None = None
    direction: Direction | None = None
    significance_level: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)] | None = None
    minimum_effect_size: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("statistical clarification must set at least one field")
        return self


class ClarificationUpdate(StrictResearchModel):
    variables: list[VariableClarificationUpdate] = Field(default_factory=list)
    sample: SampleClarificationUpdate | None = None
    alignment: AlignmentClarificationUpdate | None = None
    statistical_hypothesis: StatisticalClarificationUpdate | None = None
    controls: list[HypothesisVariableDraft] | None = None
    research_spec_inputs: ResearchSpecCompilationInputs | None = None

    @model_validator(mode="after")
    def validate_updates(self) -> Self:
        effective_fields = self.model_fields_set - (
            {"variables"} if not self.variables else set()
        )
        if not effective_fields and not self.variables:
            raise ValueError("clarification answer must contain a whitelisted update")
        variable_ids = [item.variable_id for item in self.variables]
        if len(variable_ids) != len(set(variable_ids)):
            raise ValueError("variable clarification IDs must be unique per answer")
        return self


class ClarificationAnswer(StrictResearchModel):
    ambiguity_id: AmbiguityId
    update: ClarificationUpdate


class ClarificationAnswers(StrictResearchModel):
    clarification_schema_version: Literal["1.0"] = CLARIFICATION_SCHEMA_VERSION
    proposal_sha256: Sha256Hex
    answers: list[ClarificationAnswer] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_answers(self) -> Self:
        identifiers = [item.ambiguity_id for item in self.answers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("ambiguity_id answers must be unique")
        return self


class AppliedClarifications(StrictResearchModel):
    source_proposal_sha256: Sha256Hex
    clarified_proposal_sha256: Sha256Hex
    answered_ambiguity_ids: list[AmbiguityId]
    remaining_ambiguities: list[str]
    proposal: ResearchHypothesisProposal


def proposal_ambiguity_references(
    proposal: ResearchHypothesisProposal,
) -> list[ProposalAmbiguityReference]:
    references: list[ProposalAmbiguityReference] = []
    for position, ambiguity in enumerate(proposal.ambiguities):
        digest = hashlib.sha256(
            f"{position}\x00{ambiguity}".encode("utf-8")
        ).hexdigest()[:16]
        references.append(
            ProposalAmbiguityReference(
                ambiguity_id=f"ambiguity-{digest}",
                position=position,
                text=ambiguity,
            )
        )
    return references


def parse_clarification_answers(payload: bytes | bytearray) -> ClarificationAnswers:
    return parse_strict_model(
        payload,
        ClarificationAnswers,
        code=HypothesisLifecycleErrorCode.INVALID_CLARIFICATIONS,
        stage=HypothesisLifecycleStage.CLARIFICATION_VALIDATION,
        label="clarification answers",
    )


def serialize_clarification_answers(answers: ClarificationAnswers) -> bytes:
    return serialize_strict_model(
        answers,
        ClarificationAnswers,
        parser=parse_clarification_answers,
        code=HypothesisLifecycleErrorCode.INVALID_CLARIFICATIONS,
        stage=HypothesisLifecycleStage.CLARIFICATION_VALIDATION,
        label="clarification answers",
        exclude_unset=True,
    )


def clarification_answers_json_schema() -> dict[str, object]:
    return ClarificationAnswers.model_json_schema()


def _claimed_paths(answer: ClarificationAnswer) -> set[str]:
    update = answer.update
    paths: set[str] = set()
    for variable in update.variables:
        for field_name in variable.model_fields_set - {"variable_id"}:
            paths.add(f"variables.{variable.variable_id}.{field_name}")
    for group_name in ("sample", "alignment", "statistical_hypothesis"):
        group = getattr(update, group_name)
        if group is not None:
            paths.update(f"{group_name}.{name}" for name in group.model_fields_set)
    if "controls" in update.model_fields_set:
        paths.add("controls")
    if "research_spec_inputs" in update.model_fields_set:
        paths.add("research_spec_inputs")
    return paths


def _set_nested_updates(target: dict[str, object], update: object) -> None:
    serialized = update.model_dump(
        mode="json",
        include=update.model_fields_set,
    )
    for field_name in update.model_fields_set:
        if field_name == "variable_id":
            continue
        target[field_name] = serialized[field_name]


def apply_clarification_answers(
    proposal: ResearchHypothesisProposal,
    answers: ClarificationAnswers,
) -> AppliedClarifications:
    """Apply only explicit fields and rerun the complete proposal contract."""

    proposal = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(proposal)
    )
    answers = parse_clarification_answers(serialize_clarification_answers(answers))
    proposal_sha256 = calculate_research_hypothesis_proposal_sha256(proposal)
    if answers.proposal_sha256 != proposal_sha256:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.CLARIFICATION_MISMATCH,
            HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
            "clarification answers do not match the canonical proposal SHA-256",
        )

    reference_by_id = {
        item.ambiguity_id: item for item in proposal_ambiguity_references(proposal)
    }
    unknown = sorted(
        {item.ambiguity_id for item in answers.answers} - set(reference_by_id)
    )
    if unknown:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.INVALID_CLARIFICATIONS,
            HypothesisLifecycleStage.CLARIFICATION_VALIDATION,
            "clarification answers reference an unknown ambiguity ID",
        )

    claimed: set[str] = set()
    for answer in answers.answers:
        overlap = claimed & _claimed_paths(answer)
        if overlap:
            fail_lifecycle(
                HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
                HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
                "multiple clarification answers modify the same proposal field",
            )
        claimed.update(_claimed_paths(answer))
    replacement_control_ids = {
        item.variable_id
        for answer in answers.answers
        if "controls" in answer.update.model_fields_set
        for item in (answer.update.controls or [])
    }
    control_ids = {item.variable_id for item in proposal.controls} | replacement_control_ids
    if "controls" in claimed and any(
        path.startswith(f"variables.{variable_id}.")
        for path in claimed
        for variable_id in control_ids
    ):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
            HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
            "control replacement cannot be combined with variable field updates",
        )

    proposal_data = proposal.model_dump(
        mode="json",
        exclude_computed_fields=True,
        exclude_unset=True,
    )
    variable_locations: dict[str, tuple[str, int | None]] = {
        proposal.outcome.variable_id: ("outcome", None)
    }
    variable_locations.update(
        (item.variable_id, ("predictors", index))
        for index, item in enumerate(proposal.predictors)
    )
    variable_locations.update(
        (item.variable_id, ("controls", index))
        for index, item in enumerate(proposal.controls)
    )

    answered_ids: list[str] = []
    for answer in sorted(answers.answers, key=lambda item: item.ambiguity_id):
        answered_ids.append(answer.ambiguity_id)
        update = answer.update
        if "controls" in update.model_fields_set:
            proposal_data["controls"] = [
                item.model_dump(mode="json") for item in (update.controls or [])
            ]
            variable_locations = {
                key: value
                for key, value in variable_locations.items()
                if value[0] != "controls"
            }
            variable_locations.update(
                (item.variable_id, ("controls", index))
                for index, item in enumerate(update.controls or [])
            )

        for variable_update in update.variables:
            location = variable_locations.get(variable_update.variable_id)
            if location is None:
                fail_lifecycle(
                    HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
                    HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
                    "clarification references an unknown proposal variable",
                )
            group_name, index = location
            variable_data = (
                proposal_data[group_name]
                if index is None
                else proposal_data[group_name][index]
            )
            _set_nested_updates(variable_data, variable_update)

        for group_name in ("sample", "alignment", "statistical_hypothesis"):
            group_update = getattr(update, group_name)
            if group_update is not None:
                _set_nested_updates(proposal_data[group_name], group_update)

        if "research_spec_inputs" in update.model_fields_set:
            proposal_data["research_spec_inputs"] = (
                None
                if update.research_spec_inputs is None
                else update.research_spec_inputs.model_dump(mode="json")
            )

    statistical = proposal_data["statistical_hypothesis"]
    if all(
        statistical.get(name) is not None
        for name in ("target_parameter", "direction", "statistical_method")
    ):
        target = TargetParameterSpec.model_validate_json(
            json.dumps(statistical["target_parameter"])
        )
        direction = Direction(statistical["direction"])
        null_hypothesis, alternative_hypothesis = render_statistical_hypotheses(
            target.kind,
            direction,
        )
        statistical["null_hypothesis"] = null_hypothesis
        statistical["alternative_hypothesis"] = alternative_hypothesis
    else:
        statistical["null_hypothesis"] = None
        statistical["alternative_hypothesis"] = None

    answered_set = set(answered_ids)
    remaining = [
        reference.text
        for reference in proposal_ambiguity_references(proposal)
        if reference.ambiguity_id not in answered_set
    ]
    proposal_data["ambiguities"] = remaining or [
        "Clarification answers are undergoing deterministic readiness validation."
    ]
    proposal_data["ready_for_spec_review"] = False
    try:
        intermediate = parse_research_hypothesis_proposal(
            json.dumps(
                proposal_data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except HypothesisProposalError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
            HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
            "clarification updates conflict with the proposal domain contract",
        )

    blockers = intermediate.readiness_blockers()
    if not remaining and blockers:
        remaining = [f"Unresolved after clarification: {item}." for item in blockers]
    proposal_data["ambiguities"] = remaining
    proposal_data["ready_for_spec_review"] = not (
        remaining or proposal.unsupported_requests
    )
    try:
        clarified = parse_research_hypothesis_proposal(
            json.dumps(
                proposal_data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except HypothesisProposalError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
            HypothesisLifecycleStage.CLARIFICATION_APPLICATION,
            "clarified proposal failed complete domain revalidation",
        )
    clarified = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(clarified)
    )
    return AppliedClarifications(
        source_proposal_sha256=proposal_sha256,
        clarified_proposal_sha256=(
            calculate_research_hypothesis_proposal_sha256(clarified)
        ),
        answered_ambiguity_ids=answered_ids,
        remaining_ambiguities=clarified.ambiguities,
        proposal=clarified,
    )


def persist_clarified_proposal(
    applied: AppliedClarifications,
    output_path: str | Path,
) -> Path:
    payload = serialize_research_hypothesis_proposal(applied.proposal)
    return persist_immutable_bytes(payload, output_path)


__all__ = [
    "AppliedClarifications",
    "ClarificationAnswer",
    "ClarificationAnswers",
    "ClarificationUpdate",
    "ProposalAmbiguityReference",
    "apply_clarification_answers",
    "clarification_answers_json_schema",
    "parse_clarification_answers",
    "persist_clarified_proposal",
    "proposal_ambiguity_references",
    "serialize_clarification_answers",
]
