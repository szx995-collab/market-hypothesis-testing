"""Pure deterministic compilation from a confirmed proposal to ResearchSpec."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from market_validator.hypothesis.confirmation import (
    CONFIRMATION_STATEMENT,
    ResearchHypothesisProposalConfirmation,
    calculate_research_hypothesis_confirmation_sha256,
    parse_research_hypothesis_confirmation,
    serialize_research_hypothesis_confirmation,
    validate_confirmation_matches_proposal,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleError,
    HypothesisLifecycleErrorCode,
    HypothesisLifecycleStage,
    UnresolvedResearchSpecRequirement,
    fail_lifecycle,
    parse_strict_model,
    persist_immutable_bytes,
    safe_output_path,
    serialize_strict_model,
)
from market_validator.hypothesis.models import (
    DraftMarketRelation,
    DraftTimeRelation,
    HypothesisVariableDraft,
    ResearchHypothesisProposal,
    ResearchSpecVariableInputs,
)
from market_validator.hypothesis.serialization import (
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_proposal,
    serialize_research_hypothesis_proposal,
)
from market_validator.research.models import (
    AlignmentSpec,
    ModelSpec,
    ResearchSpec,
    SampleSpec,
    StrictResearchModel,
    VariableSpec,
)
from market_validator.research.serialization import (
    ResearchSpecSerializationError,
    calculate_research_spec_sha256,
    parse_research_spec,
    serialize_research_spec,
)


PROVENANCE_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ResearchSpecProvenance(StrictResearchModel):
    provenance_schema_version: Literal["1.0"] = PROVENANCE_SCHEMA_VERSION
    proposal_sha256: Sha256Hex
    confirmation_sha256: Sha256Hex
    confirmation_statement: Literal[
        "I explicitly confirm this exact proposal for ResearchSpec generation."
    ] = CONFIRMATION_STATEMENT
    confirmed_at: datetime
    research_spec_sha256: Sha256Hex

    @field_validator("confirmed_at")
    @classmethod
    def validate_confirmed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("confirmed_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("confirmed_at", when_used="json")
    def serialize_confirmed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CompiledResearchSpec(StrictResearchModel):
    proposal_sha256: Sha256Hex
    confirmation_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    research_spec: ResearchSpec
    provenance: ResearchSpecProvenance

    @model_validator(mode="after")
    def validate_internal_identity(self) -> "CompiledResearchSpec":
        if calculate_research_spec_sha256(self.research_spec) != self.research_spec_sha256:
            raise ValueError("research_spec_sha256 must match canonical ResearchSpec bytes")
        if self.provenance.proposal_sha256 != self.proposal_sha256:
            raise ValueError("provenance proposal SHA-256 must match")
        if self.provenance.confirmation_sha256 != self.confirmation_sha256:
            raise ValueError("provenance confirmation SHA-256 must match")
        if self.provenance.research_spec_sha256 != self.research_spec_sha256:
            raise ValueError("provenance ResearchSpec SHA-256 must match")
        return self


class PersistedResearchSpec(StrictResearchModel):
    research_spec_path: Path
    research_spec_byte_size: Annotated[int, Field(gt=0)]
    research_spec_sha256: Sha256Hex
    provenance_path: Path
    provenance_byte_size: Annotated[int, Field(gt=0)]
    provenance_sha256: Sha256Hex
    compiled: CompiledResearchSpec


def parse_research_spec_provenance(
    payload: bytes | bytearray,
) -> ResearchSpecProvenance:
    return parse_strict_model(
        payload,
        ResearchSpecProvenance,
        code=HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
        stage=HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
        label="ResearchSpec provenance",
    )


def serialize_research_spec_provenance(
    provenance: ResearchSpecProvenance,
) -> bytes:
    return serialize_strict_model(
        provenance,
        ResearchSpecProvenance,
        parser=parse_research_spec_provenance,
        code=HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
        stage=HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
        label="ResearchSpec provenance",
    )


def _unresolved(path: str, code: str, message: str) -> UnresolvedResearchSpecRequirement:
    return UnresolvedResearchSpecRequirement(path=path, code=code, message=message)


def _lag_periods(variable: HypothesisVariableDraft) -> int | None:
    relation = variable.time_relation.relation
    if relation in {
        DraftTimeRelation.OUTCOME_PERIOD,
        DraftTimeRelation.CONTEMPORANEOUS,
    }:
        return 0
    if relation is DraftTimeRelation.PRECEDES_OUTCOME:
        return variable.time_relation.lag_periods
    return None


def _collect_unresolved(
    proposal: ResearchHypothesisProposal,
) -> list[UnresolvedResearchSpecRequirement]:
    inputs = proposal.research_spec_inputs
    if inputs is None:
        return [
            _unresolved(
                "research_spec_inputs",
                "missing",
                "explicit ResearchSpec compilation inputs are required",
            ),
            _unresolved("research_spec_inputs.spec_id", "missing", "spec_id is required"),
            _unresolved("research_spec_inputs.title", "missing", "title is required"),
            _unresolved(
                "research_spec_inputs.variables",
                "missing",
                "provider-neutral instrument and field details are required",
            ),
            _unresolved(
                "research_spec_inputs.minimum_observations",
                "missing",
                "minimum_observations is required",
            ),
            _unresolved(
                "research_spec_inputs.multiple_testing_correction",
                "missing",
                "multiple-testing policy is required",
            ),
            _unresolved(
                "research_spec_inputs.robustness_checks",
                "missing",
                "an explicit robustness-check list is required, even if empty",
            ),
            _unresolved(
                "research_spec_inputs.limitations",
                "missing",
                "an explicit limitations list is required, even if empty",
            ),
        ]

    unresolved: list[UnresolvedResearchSpecRequirement] = []
    variables = [proposal.outcome, *proposal.predictors, *proposal.controls]
    declared_ids = {item.variable_id for item in variables}
    input_by_id = {item.variable_id: item for item in inputs.variables}
    for variable in variables:
        path = f"research_spec_inputs.variables.{variable.variable_id}"
        if variable.variable_id not in input_by_id:
            unresolved.append(
                _unresolved(path, "missing", "variable compilation inputs are required")
            )
        if variable.asset_type is None:
            unresolved.append(
                _unresolved(
                    f"{variable.variable_id}.asset_type",
                    "missing",
                    "asset_type must be explicit before ResearchSpec generation",
                )
            )
        if _lag_periods(variable) is None:
            unresolved.append(
                _unresolved(
                    f"{variable.variable_id}.time_relation",
                    "unsupported_mapping",
                    "time relation cannot be represented without future information",
                )
            )
    unknown_ids = sorted(set(input_by_id) - declared_ids)
    if unknown_ids:
        unresolved.append(
            _unresolved(
                "research_spec_inputs.variables",
                "unknown_variable",
                "compilation inputs contain undeclared variable IDs",
            )
        )
    if proposal.alignment.market_relation is DraftMarketRelation.CROSS_MARKET:
        if (
            inputs.join_policy is None
            or inputs.max_staleness_days is None
            or inputs.missing_data_policy is None
        ):
            unresolved.append(
                _unresolved(
                    "research_spec_inputs.join_policy",
                    "missing",
                    "cross-market compilation requires join, staleness, and missing-data policies",
                )
            )
    return unresolved


def _build_variable(
    proposal_variable: HypothesisVariableDraft,
    inputs: ResearchSpecVariableInputs,
) -> VariableSpec:
    if proposal_variable.asset_type is not inputs.instrument.asset_type:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "proposal asset type conflicts with ResearchSpec instrument metadata",
        )
    lag_periods = _lag_periods(proposal_variable)
    if lag_periods is None or proposal_variable.transformation is None:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "proposal variable mapping remains unresolved",
        )
    try:
        return VariableSpec(
            variable_id=proposal_variable.variable_id,
            role=proposal_variable.role,
            instrument=inputs.instrument,
            field=inputs.field,
            transformation=proposal_variable.transformation,
            lag_periods=lag_periods,
            availability_lag_periods=inputs.availability_lag_periods,
            price_adjustment=inputs.price_adjustment,
            contract_roll_method=proposal_variable.contract_roll_method,
            proxy_for=proposal_variable.proxy_for,
            rolling_window_periods=inputs.rolling_window_periods,
            revision_policy=inputs.revision_policy,
        )
    except ValidationError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "variable inputs conflict with the existing ResearchSpec contract",
        )


def compile_confirmed_research_spec(
    proposal: ResearchHypothesisProposal,
    confirmation: ResearchHypothesisProposalConfirmation,
) -> CompiledResearchSpec:
    """Compile without I/O, model calls, data access, or statistical execution."""

    proposal = parse_research_hypothesis_proposal(
        serialize_research_hypothesis_proposal(proposal)
    )
    confirmation = parse_research_hypothesis_confirmation(
        serialize_research_hypothesis_confirmation(confirmation)
    )
    if (
        not proposal.ready_for_spec_review
        or proposal.ambiguities
        or proposal.unsupported_requests
        or proposal.readiness_blockers()
    ):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.PROPOSAL_NOT_READY,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "proposal is not eligible for ResearchSpec generation",
        )
    validate_confirmation_matches_proposal(proposal, confirmation)

    unresolved = _collect_unresolved(proposal)
    if unresolved:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "ResearchSpec generation requires additional explicit inputs",
            unresolved_requirements=unresolved,
        )

    inputs = proposal.research_spec_inputs
    if inputs is None:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "ResearchSpec compilation inputs remain unresolved",
        )
    if (
        proposal.alignment.market_relation is DraftMarketRelation.SAME_MARKET
        and any(
            value is not None
            for value in (
                inputs.join_policy,
                inputs.max_staleness_days,
                inputs.missing_data_policy,
            )
        )
    ):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "same-market proposal must not supply unused cross-market alignment policies",
        )
    input_by_id = {item.variable_id: item for item in inputs.variables}
    outcome = _build_variable(proposal.outcome, input_by_id[proposal.outcome.variable_id])
    predictors = [
        _build_variable(item, input_by_id[item.variable_id])
        for item in proposal.predictors
    ]
    controls = [
        _build_variable(item, input_by_id[item.variable_id]) for item in proposal.controls
    ]

    alignment = None
    if proposal.alignment.market_relation is DraftMarketRelation.CROSS_MARKET:
        alignment_values = (
            proposal.alignment.target_market,
            proposal.alignment.target_timezone,
            proposal.alignment.target_calendar,
            proposal.alignment.target_session,
            proposal.alignment.information_cutoff,
            inputs.join_policy,
            inputs.max_staleness_days,
            inputs.missing_data_policy,
        )
        if any(value is None for value in alignment_values):
            fail_lifecycle(
                HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
                HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
                "cross-market alignment inputs remain unresolved",
            )
        alignment = AlignmentSpec(
            target_market=proposal.alignment.target_market,
            target_timezone=proposal.alignment.target_timezone,
            target_calendar=proposal.alignment.target_calendar,
            target_session=proposal.alignment.target_session,
            information_cutoff=proposal.alignment.information_cutoff,
            join_policy=inputs.join_policy,
            max_staleness_days=inputs.max_staleness_days,
            missing_data_policy=inputs.missing_data_policy,
        )

    statistical = proposal.statistical_hypothesis
    statistical_values = (
        statistical.statistical_method,
        statistical.null_hypothesis,
        statistical.alternative_hypothesis,
        statistical.direction,
        statistical.significance_level,
        statistical.minimum_effect_size,
        proposal.sample.start_date,
        proposal.sample.end_date,
        proposal.sample.frequency,
    )
    if any(value is None for value in statistical_values):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "confirmed proposal contains unresolved required fields",
        )
    variables = [outcome, *predictors, *controls]
    formula = f"{outcome.variable_id} ~ " + " + ".join(
        item.variable_id for item in [*predictors, *controls]
    )
    try:
        spec = ResearchSpec(
            schema_version="1.0",
            spec_id=inputs.spec_id,
            title=inputs.title,
            original_hypothesis=proposal.original_question,
            normalized_hypothesis=proposal.normalized_research_question,
            claim_type=proposal.claim_type,
            outcome=outcome,
            predictors=predictors,
            controls=controls,
            sample=SampleSpec(
                start_date=proposal.sample.start_date,
                end_date=proposal.sample.end_date,
                frequency=proposal.sample.frequency,
                minimum_observations=inputs.minimum_observations,
            ),
            alignment=alignment,
            model=ModelSpec(
                method=statistical.statistical_method,
                formula=formula,
                formula_variable_ids=[item.variable_id for item in variables],
                null_hypothesis=statistical.null_hypothesis,
                alternative_hypothesis=statistical.alternative_hypothesis,
                direction=statistical.direction,
                significance_level=statistical.significance_level,
                minimum_effect_size=statistical.minimum_effect_size,
                multiple_testing_correction=inputs.multiple_testing_correction,
            ),
            robustness_checks=inputs.robustness_checks,
            assumptions=proposal.assumptions,
            limitations=inputs.limitations,
        )
    except ValidationError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "compiled values conflict with the existing ResearchSpec contract",
        )
    try:
        spec = parse_research_spec(serialize_research_spec(spec))
    except ResearchSpecSerializationError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.RESEARCH_SPEC_COMPILATION,
            "compiled ResearchSpec failed canonical strict validation",
        )
    proposal_sha256 = calculate_research_hypothesis_proposal_sha256(proposal)
    confirmation_sha256 = calculate_research_hypothesis_confirmation_sha256(
        confirmation
    )
    spec_sha256 = calculate_research_spec_sha256(spec)
    provenance = ResearchSpecProvenance(
        proposal_sha256=proposal_sha256,
        confirmation_sha256=confirmation_sha256,
        confirmed_at=confirmation.confirmed_at,
        research_spec_sha256=spec_sha256,
    )
    return CompiledResearchSpec(
        proposal_sha256=proposal_sha256,
        confirmation_sha256=confirmation_sha256,
        research_spec_sha256=spec_sha256,
        research_spec=spec,
        provenance=provenance,
    )


def persist_compiled_research_spec(
    compiled: CompiledResearchSpec,
    output_path: str | Path,
) -> PersistedResearchSpec:
    """Persist canonical ResearchSpec bytes plus an auditable confirmation sidecar."""

    if not isinstance(compiled, CompiledResearchSpec):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
            HypothesisLifecycleStage.OUTPUT,
            "compiled result must be strictly validated before persistence",
        )
    spec_bytes = serialize_research_spec(compiled.research_spec)
    provenance_bytes = serialize_research_spec_provenance(compiled.provenance)
    spec_path = safe_output_path(output_path)
    provenance_path = safe_output_path(
        spec_path.with_name(spec_path.name + ".provenance.json")
    )
    if spec_path == provenance_path:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "ResearchSpec and provenance paths must be distinct",
        )

    spec_exists = spec_path.exists() or spec_path.is_symlink()
    provenance_exists = provenance_path.exists() or provenance_path.is_symlink()
    if spec_exists != provenance_exists:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_CONFLICT,
            HypothesisLifecycleStage.OUTPUT,
            "ResearchSpec output pair is incomplete and was not modified",
        )

    created_spec = False
    try:
        persisted_spec_path = persist_immutable_bytes(spec_bytes, spec_path)
        created_spec = not spec_exists
        persisted_provenance_path = persist_immutable_bytes(
            provenance_bytes,
            provenance_path,
        )
    except HypothesisLifecycleError:
        if created_spec:
            try:
                spec_path.unlink()
            except OSError:
                pass
        raise

    try:
        restored_spec = parse_research_spec(persisted_spec_path.read_bytes())
        restored_provenance = parse_research_spec_provenance(
            persisted_provenance_path.read_bytes()
        )
    except OSError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "persisted ResearchSpec output could not be read safely",
        )
    except ResearchSpecSerializationError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "persisted ResearchSpec failed strict reload",
        )
    if (
        restored_spec != compiled.research_spec
        or restored_provenance != compiled.provenance
    ):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "persisted ResearchSpec output did not match the compiled result",
        )
    return PersistedResearchSpec(
        research_spec_path=persisted_spec_path,
        research_spec_byte_size=len(spec_bytes),
        research_spec_sha256=hashlib.sha256(spec_bytes).hexdigest(),
        provenance_path=persisted_provenance_path,
        provenance_byte_size=len(provenance_bytes),
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        compiled=compiled,
    )


__all__ = [
    "CompiledResearchSpec",
    "PersistedResearchSpec",
    "ResearchSpecProvenance",
    "compile_confirmed_research_spec",
    "parse_research_spec_provenance",
    "persist_compiled_research_spec",
    "serialize_research_spec_provenance",
]
