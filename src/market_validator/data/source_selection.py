"""Offline, deterministic, auditable source-selection lifecycle.

A SourceSelection records the user's explicit choice of one registry-backed,
verified Provider mapping per DataPlan requirement. It never auto-selects,
never guesses, never touches the network, and never authorizes acquisition.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, NoReturn, TypeVar

from pydantic import (
    BaseModel,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
)

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    GeneratedDataPlan,
    calculate_data_plan_confirmation_sha256,
    validate_data_plan_confirmation_matches,
)
from market_validator.data.models import (
    DataRequirement,
    Identifier,
    NonEmptyString,
    StrictDataModel,
)
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.data.serialization import (
    DataPlanSerializationError,
    calculate_data_plan_sha256,
    parse_data_plan,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleError,
    UnresolvedResearchSpecRequirement,
)
from market_validator.research.models import StrictResearchModel


SOURCE_SELECTION_SCHEMA_VERSION = "1.0"
SOURCE_SELECTION_CONFIRMATION_SCHEMA_VERSION = "1.0"
SOURCE_SELECTION_PROVENANCE_SCHEMA_VERSION = "1.0"
SOURCE_SELECTION_CONFIRMATION_STATEMENT = (
    "I explicitly confirm this exact source selection "
    "for subsequent acquisition-request planning."
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SourceSelectionReviewErrorCode(StrEnum):
    INVALID_SOURCE_SELECTION = "invalid_source_selection"
    DATA_PLAN_CONFIRMATION_MISMATCH = "data_plan_confirmation_mismatch"
    SOURCE_SELECTION_MAPPING_MISMATCH = "source_selection_mapping_mismatch"
    SOURCE_SELECTION_NOT_READY = "source_selection_not_ready"
    INVALID_SOURCE_SELECTION_CONFIRMATION = (
        "invalid_source_selection_confirmation"
    )
    SOURCE_SELECTION_CONFIRMATION_MISMATCH = (
        "source_selection_confirmation_mismatch"
    )
    SOURCE_SELECTION_OUTPUT_CONFLICT = "source_selection_output_conflict"
    SOURCE_SELECTION_OUTPUT_ERROR = "source_selection_output_error"


class SourceSelectionReviewStage(StrEnum):
    SOURCE_SELECTION_VALIDATION = "source_selection_validation"
    SOURCE_SELECTION_GENERATION = "source_selection_generation"
    SOURCE_SELECTION_CONFIRMATION_VALIDATION = (
        "source_selection_confirmation_validation"
    )
    SOURCE_SELECTION_OUTPUT = "source_selection_output"


class SourceSelectionReviewFailure(StrictResearchModel):
    code: SourceSelectionReviewErrorCode
    stage: SourceSelectionReviewStage
    message: str
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] = []


class SourceSelectionReviewError(ValueError):
    """Structured safe failure that never embeds raw input content."""

    def __init__(self, failure: SourceSelectionReviewFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_source_selection_review(
    code: SourceSelectionReviewErrorCode,
    stage: SourceSelectionReviewStage,
    message: str,
    *,
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] | None = None,
) -> NoReturn:
    raise SourceSelectionReviewError(
        SourceSelectionReviewFailure(
            code=code,
            stage=stage,
            message=message,
            unresolved_requirements=unresolved_requirements or [],
        )
    )


class SourceSelectionDecision(StrictResearchModel):
    """One explicit user choice identifying a registry-backed mapping.

    The decision only identifies the mapping; every identity and verification
    field of the final selection is copied from the registry.
    """

    requirement_id: Identifier
    provider_id: Identifier
    provider_symbol: NonEmptyString
    dataset_or_endpoint: NonEmptyString


class SelectedProviderSource(StrictResearchModel):
    """Registry-verified source copied exactly from InstrumentRegistry."""

    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    provider_id: Identifier
    provider_symbol: NonEmptyString
    dataset_or_endpoint: NonEmptyString
    market: NonEmptyString
    mapping_verified_on: date
    mapping_verification_source_uri: NonEmptyString


class SourceSelectionUnresolvedCode(StrEnum):
    SOURCE_NOT_SELECTED = "source_not_selected"
    INSTRUMENT_NOT_REGISTERED = "instrument_not_registered"
    INSTRUMENT_IDENTITY_NOT_VERIFIED = "instrument_identity_not_verified"
    NO_VERIFIED_PROVIDER_MAPPING = "no_verified_provider_mapping"


class UnresolvedSourceSelectionRequirement(StrictResearchModel):
    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    code: SourceSelectionUnresolvedCode
    message: NonEmptyString


class SourceSelection(StrictResearchModel):
    """Deterministic provider-neutral artifact of explicit selections."""

    source_selection_schema_version: Literal["1.0"] = (
        SOURCE_SELECTION_SCHEMA_VERSION
    )
    selection_id: NonEmptyString
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    selections: list[SelectedProviderSource]
    unresolved_requirements: list[UnresolvedSourceSelectionRequirement]
    warnings: list[NonEmptyString]


class GeneratedSourceSelection(StrictResearchModel):
    """SourceSelection bytes identity plus the exact hashes it is bound to."""

    source_selection: SourceSelection
    source_selection_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex


class SourceSelectionConfirmation(StrictResearchModel):
    """Explicit user confirmation bound to exact canonical hashes."""

    confirmation_schema_version: Literal["1.0"] = (
        SOURCE_SELECTION_CONFIRMATION_SCHEMA_VERSION
    )
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    confirmed: Literal[True]
    confirmation_statement: Literal[
        "I explicitly confirm this exact source selection "
        "for subsequent acquisition-request planning."
    ] = SOURCE_SELECTION_CONFIRMATION_STATEMENT
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


class SourceSelectionProvenance(StrictResearchModel):
    """Auditable, secret-free provenance for one generated SourceSelection."""

    provenance_schema_version: Literal["1.0"] = (
        SOURCE_SELECTION_PROVENANCE_SCHEMA_VERSION
    )
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("generated_at", when_used="json")
    def serialize_generated_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PersistedSourceSelection(StrictResearchModel):
    source_selection_path: Path
    source_selection_byte_size: int
    source_selection_sha256: Sha256Hex
    provenance_path: Path
    provenance_sha256: Sha256Hex
    generated: GeneratedSourceSelection


ModelT = TypeVar("ModelT", bound=BaseModel)


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


def _parse_strict(
    payload: bytes | bytearray,
    model_type: type[ModelT],
    *,
    code: SourceSelectionReviewErrorCode,
    stage: SourceSelectionReviewStage,
    label: str,
) -> ModelT:
    if not isinstance(payload, (bytes, bytearray)):
        fail_source_selection_review(
            code, stage, f"{label} must be supplied as UTF-8 bytes"
        )
    normalized = bytes(payload)
    try:
        decoded = json.loads(
            normalized.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_source_selection_review(
            code, stage, f"{label} must be exactly one strict UTF-8 JSON object"
        )
    if not isinstance(decoded, dict):
        fail_source_selection_review(code, stage, f"{label} must be a JSON object")
    try:
        return model_type.model_validate_json(normalized)
    except ValidationError:
        fail_source_selection_review(
            code, stage, f"{label} failed strict domain validation"
        )


def _serialize_strict(model: ModelT, *, label: str) -> bytes:
    try:
        payload = (
            json.dumps(
                model.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
            f"{label} could not be serialized",
        )
    return payload


def _registry_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def instrument_registry_sha256(registry: InstrumentRegistry) -> str:
    return _registry_sha256(registry.canonical_json())


def calendar_registry_sha256(registry: CalendarRegistry) -> str:
    return _registry_sha256(registry.canonical_json())


def parse_source_selection(payload: bytes | bytearray) -> SourceSelection:
    return _parse_strict(
        payload,
        SourceSelection,
        code=SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
        stage=SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
        label="SourceSelection",
    )


def serialize_source_selection(selection: SourceSelection) -> bytes:
    payload = _serialize_strict(selection, label="SourceSelection")
    if parse_source_selection(payload) != selection:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
            "SourceSelection does not round-trip exactly",
        )
    return payload


def calculate_source_selection_sha256(selection: SourceSelection) -> str:
    return hashlib.sha256(serialize_source_selection(selection)).hexdigest()


def parse_source_selection_confirmation(
    payload: bytes | bytearray,
) -> SourceSelectionConfirmation:
    return _parse_strict(
        payload,
        SourceSelectionConfirmation,
        code=SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION_CONFIRMATION,
        stage=SourceSelectionReviewStage.SOURCE_SELECTION_CONFIRMATION_VALIDATION,
        label="SourceSelection confirmation",
    )


def serialize_source_selection_confirmation(
    confirmation: SourceSelectionConfirmation,
) -> bytes:
    payload = _serialize_strict(confirmation, label="SourceSelection confirmation")
    if parse_source_selection_confirmation(payload) != confirmation:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION_CONFIRMATION,
            SourceSelectionReviewStage.SOURCE_SELECTION_CONFIRMATION_VALIDATION,
            "SourceSelection confirmation does not round-trip exactly",
        )
    return payload


def calculate_source_selection_confirmation_sha256(
    confirmation: SourceSelectionConfirmation,
) -> str:
    return hashlib.sha256(
        serialize_source_selection_confirmation(confirmation)
    ).hexdigest()


def parse_source_selection_provenance(
    payload: bytes | bytearray,
) -> SourceSelectionProvenance:
    return _parse_strict(
        payload,
        SourceSelectionProvenance,
        code=SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
        stage=SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
        label="SourceSelection provenance",
    )


def serialize_source_selection_provenance(
    provenance: SourceSelectionProvenance,
) -> bytes:
    payload = _serialize_strict(provenance, label="SourceSelection provenance")
    if parse_source_selection_provenance(payload) != provenance:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "SourceSelection provenance does not round-trip exactly",
        )
    return payload


def _requirement_market_matches(
    requirement: DataRequirement, market: str
) -> bool:
    return requirement.market == market


def generate_source_selection(
    generated_data_plan: GeneratedDataPlan,
    data_plan_confirmation: object,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    decisions: list[SourceSelectionDecision],
) -> GeneratedSourceSelection:
    """Deterministically produce one SourceSelection from explicit decisions."""
    from market_validator.data.data_plan_review import (
        DataPlanConfirmation,
        parse_data_plan_confirmation,
        serialize_data_plan_confirmation,
    )

    try:
        generated_data_plan = _reparse_generated_data_plan(generated_data_plan)
        confirmation = parse_data_plan_confirmation(
            serialize_data_plan_confirmation(
                _coerce_data_plan_confirmation(data_plan_confirmation)
            )
        )
    except (DataPlanSerializationError, ValueError):
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
            "inputs failed strict validation",
        )
    validate_data_plan_confirmation_matches(generated_data_plan, confirmation)
    _validate_registry_hashes(
        generated_data_plan, instrument_registry, calendar_registry
    )
    decisions = [
        parse_source_selection_decision(_serialize_decision(decision))
        for decision in decisions
    ]
    _reject_duplicate_decisions(decisions)
    known_requirement_ids = {
        requirement.requirement_id
        for requirement in generated_data_plan.data_plan.requirements
    }
    for decision in decisions:
        if decision.requirement_id not in known_requirement_ids:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                f"decision references an unknown requirement "
                f"{decision.requirement_id}",
            )

    selections: list[SelectedProviderSource] = []
    unresolved: list[UnresolvedSourceSelectionRequirement] = []
    warnings: list[str] = []
    decision_by_requirement = {
        decision.requirement_id: decision for decision in decisions
    }

    for requirement in generated_data_plan.data_plan.requirements:
        decision = decision_by_requirement.get(requirement.requirement_id)
        entry = instrument_registry.get(requirement.instrument_id)
        if entry is None:
            if decision is not None:
                fail_source_selection_review(
                    SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                    SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                    f"selected mapping does not exist for {requirement.requirement_id}",
                )
            unresolved.append(
                UnresolvedSourceSelectionRequirement(
                    requirement_id=requirement.requirement_id,
                    variable_id=requirement.variable_id,
                    instrument_id=requirement.instrument_id,
                    code=SourceSelectionUnresolvedCode.INSTRUMENT_NOT_REGISTERED,
                    message="instrument is not present in the InstrumentRegistry",
                )
            )
            continue
        if entry.identity_status is not IdentityStatus.VERIFIED:
            if decision is not None:
                fail_source_selection_review(
                    SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                    SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                    f"selected instrument is not verified for "
                    f"{requirement.requirement_id}",
                )
            unresolved.append(
                UnresolvedSourceSelectionRequirement(
                    requirement_id=requirement.requirement_id,
                    variable_id=requirement.variable_id,
                    instrument_id=requirement.instrument_id,
                    code=SourceSelectionUnresolvedCode.INSTRUMENT_IDENTITY_NOT_VERIFIED,
                    message="instrument identity is not verified",
                )
            )
            continue
        verified_mappings = [
            mapping for mapping in entry.provider_mappings if mapping.verified
        ]
        if decision is None:
            if not verified_mappings:
                unresolved.append(
                    UnresolvedSourceSelectionRequirement(
                        requirement_id=requirement.requirement_id,
                        variable_id=requirement.variable_id,
                        instrument_id=requirement.instrument_id,
                        code=SourceSelectionUnresolvedCode.NO_VERIFIED_PROVIDER_MAPPING,
                        message="no verified provider mapping exists",
                    )
                )
                continue
            unresolved.append(
                UnresolvedSourceSelectionRequirement(
                    requirement_id=requirement.requirement_id,
                    variable_id=requirement.variable_id,
                    instrument_id=requirement.instrument_id,
                    code=SourceSelectionUnresolvedCode.SOURCE_NOT_SELECTED,
                    message="no explicit source selection decision was supplied",
                )
            )
            continue
        mapping = _exact_mapping_lookup(entry.provider_mappings, decision)
        if mapping is None:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                f"selected mapping does not exist for "
                f"{requirement.requirement_id}",
            )
        if not mapping.verified:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                f"selected mapping is not verified for "
                f"{requirement.requirement_id}",
            )
        if mapping.market != entry.market:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                f"selected mapping market conflicts with the registry for "
                f"{requirement.requirement_id}",
            )
        if not _requirement_market_matches(requirement, entry.market):
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_MAPPING_MISMATCH,
                SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
                f"requirement market conflicts with the registry for "
                f"{requirement.requirement_id}",
            )
        selections.append(
            SelectedProviderSource(
                requirement_id=requirement.requirement_id,
                variable_id=requirement.variable_id,
                instrument_id=requirement.instrument_id,
                provider_id=mapping.provider_id,
                provider_symbol=mapping.provider_symbol,
                dataset_or_endpoint=mapping.dataset_or_endpoint,
                market=mapping.market,
                mapping_verified_on=mapping.verified_on,
                mapping_verification_source_uri=mapping.verification_source_uri,
            )
        )

    selections.sort(key=lambda item: item.requirement_id)
    unresolved.sort(
        key=lambda item: (item.requirement_id, item.code.value)
    )
    warnings = sorted(set(warnings))

    selection = SourceSelection(
        selection_id=_derive_selection_id(
            generated_data_plan,
            confirmation,
            instrument_registry,
            calendar_registry,
            selections,
        ),
        research_spec_sha256=generated_data_plan.research_spec_sha256,
        data_plan_sha256=generated_data_plan.data_plan_sha256,
        data_plan_confirmation_sha256=calculate_data_plan_confirmation_sha256(
            confirmation
        ),
        instrument_registry_sha256=generated_data_plan.instrument_registry_sha256,
        calendar_registry_sha256=generated_data_plan.calendar_registry_sha256,
        selections=selections,
        unresolved_requirements=unresolved,
        warnings=warnings,
    )
    return GeneratedSourceSelection(
        source_selection=selection,
        source_selection_sha256=calculate_source_selection_sha256(selection),
        research_spec_sha256=selection.research_spec_sha256,
        data_plan_sha256=selection.data_plan_sha256,
        data_plan_confirmation_sha256=selection.data_plan_confirmation_sha256,
        instrument_registry_sha256=selection.instrument_registry_sha256,
        calendar_registry_sha256=selection.calendar_registry_sha256,
    )


def parse_source_selection_decision(
    payload: bytes | bytearray,
) -> SourceSelectionDecision:
    return _parse_strict(
        payload,
        SourceSelectionDecision,
        code=SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
        stage=SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
        label="SourceSelection decision",
    )


def _serialize_decision(decision: SourceSelectionDecision) -> bytes:
    return _serialize_strict(decision, label="SourceSelection decision")


def _coerce_data_plan_confirmation(confirmation: object) -> object:
    if isinstance(confirmation, BaseModel):
        return confirmation
    fail_source_selection_review(
        SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
        SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
        "DataPlan confirmation must be a validated model",
    )


def _reparse_generated_data_plan(
    generated: GeneratedDataPlan,
) -> GeneratedDataPlan:
    try:
        plan = parse_data_plan(
            _serialize_strict(generated.data_plan, label="DataPlan")
        )
    except DataPlanSerializationError:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
            "DataPlan failed strict validation",
        )
    if calculate_data_plan_sha256(plan) != generated.data_plan_sha256:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
            "DataPlan identity does not match its bound hash",
        )
    return GeneratedDataPlan(
        data_plan=plan,
        data_plan_sha256=generated.data_plan_sha256,
        research_spec_sha256=generated.research_spec_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
    )


def _validate_registry_hashes(
    generated: GeneratedDataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> None:
    mismatches = []
    if instrument_registry_sha256(instrument_registry) != generated.instrument_registry_sha256:
        mismatches.append("instrument registry")
    if calendar_registry_sha256(calendar_registry) != generated.calendar_registry_sha256:
        mismatches.append("calendar registry")
    if mismatches:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
            SourceSelectionReviewStage.SOURCE_SELECTION_GENERATION,
            "registry snapshots no longer match the bound hashes: "
            + ", ".join(mismatches),
        )


def _reject_duplicate_decisions(
    decisions: list[SourceSelectionDecision],
) -> None:
    seen: set[str] = set()
    for decision in decisions:
        if decision.requirement_id in seen:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
                SourceSelectionReviewStage.SOURCE_SELECTION_VALIDATION,
                f"duplicate source selection decision for "
                f"{decision.requirement_id}",
            )
        seen.add(decision.requirement_id)


def _exact_mapping_lookup(
    mappings: list[object], decision: SourceSelectionDecision
) -> object | None:
    from market_validator.data.registry import ProviderSymbolMapping

    for mapping in mappings:
        if not isinstance(mapping, ProviderSymbolMapping):
            continue
        if (
            mapping.provider_id == decision.provider_id
            and mapping.provider_symbol == decision.provider_symbol
            and mapping.dataset_or_endpoint == decision.dataset_or_endpoint
        ):
            return mapping
    return None


def _derive_selection_id(
    generated: GeneratedDataPlan,
    confirmation: object,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    selections: list[SelectedProviderSource],
) -> str:
    identity = json.dumps(
        {
            "research_spec_sha256": generated.research_spec_sha256,
            "data_plan_sha256": generated.data_plan_sha256,
            "data_plan_confirmation_sha256": (
                calculate_data_plan_confirmation_sha256(confirmation)
            ),
            "instrument_registry_sha256": generated.instrument_registry_sha256,
            "calendar_registry_sha256": generated.calendar_registry_sha256,
            "selections": [
                item.model_dump(mode="json") for item in selections
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def source_selection_readiness_blockers(
    generated: GeneratedSourceSelection,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> list[str]:
    """Deterministic blockers before a SourceSelection may be confirmed."""
    blockers: list[str] = []
    if generated.source_selection.unresolved_requirements:
        blockers.append(
            "unresolved source selection requirements: "
            + ", ".join(
                sorted(
                    item.requirement_id
                    for item in generated.source_selection.unresolved_requirements
                )
            )
        )
    if instrument_registry_sha256(
        instrument_registry
    ) != generated.instrument_registry_sha256:
        blockers.append("instrument registry no longer matches the bound hash")
    if calendar_registry_sha256(
        calendar_registry
    ) != generated.calendar_registry_sha256:
        blockers.append("calendar registry no longer matches the bound hash")
    requirement_ids = {
        item.requirement_id
        for item in generated.source_selection.selections
    }
    if len(requirement_ids) != len(generated.source_selection.selections):
        blockers.append("duplicate requirement selections")
    if generated.source_selection.warnings:
        blockers.extend(
            f"warning: {warning}" for warning in generated.source_selection.warnings
        )
    for selection in generated.source_selection.selections:
        entry = instrument_registry.get(selection.instrument_id)
        if entry is None:
            blockers.append(
                f"{selection.requirement_id}: instrument no longer registered"
            )
            continue
        if entry.identity_status is not IdentityStatus.VERIFIED:
            blockers.append(
                f"{selection.requirement_id}: instrument identity is "
                f"{entry.identity_status.value}, not verified"
            )
        mapping = _exact_mapping_lookup(entry.provider_mappings, _decision_from_selection(selection))
        if mapping is None:
            blockers.append(
                f"{selection.requirement_id}: selected mapping no longer exists"
            )
            continue
        if not mapping.verified:
            blockers.append(
                f"{selection.requirement_id}: selected mapping is no longer verified"
            )
        if mapping.market != entry.market or mapping.market != selection.market:
            blockers.append(
                f"{selection.requirement_id}: selected mapping metadata changed"
            )
    return blockers


def _decision_from_selection(
    selection: SelectedProviderSource,
) -> SourceSelectionDecision:
    return SourceSelectionDecision(
        requirement_id=selection.requirement_id,
        provider_id=selection.provider_id,
        provider_symbol=selection.provider_symbol,
        dataset_or_endpoint=selection.dataset_or_endpoint,
    )


def confirm_source_selection(
    generated: GeneratedSourceSelection,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    *,
    confirmed_at: datetime,
) -> SourceSelectionConfirmation:
    """Create a confirmation only for a fully ready source selection."""
    blockers = source_selection_readiness_blockers(
        generated, instrument_registry, calendar_registry
    )
    if blockers:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_NOT_READY,
            SourceSelectionReviewStage.SOURCE_SELECTION_CONFIRMATION_VALIDATION,
            "SourceSelection is not ready for confirmation: "
            + "; ".join(blockers),
        )
    return SourceSelectionConfirmation(
        research_spec_sha256=generated.research_spec_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        data_plan_confirmation_sha256=generated.data_plan_confirmation_sha256,
        source_selection_sha256=generated.source_selection_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        confirmed=True,
        confirmed_at=confirmed_at,
    )


def validate_source_selection_confirmation_matches(
    generated: GeneratedSourceSelection,
    confirmation: SourceSelectionConfirmation,
) -> None:
    expected = {
        "research_spec": generated.research_spec_sha256,
        "data_plan": generated.data_plan_sha256,
        "data_plan_confirmation": generated.data_plan_confirmation_sha256,
        "source_selection": generated.source_selection_sha256,
        "instrument_registry": generated.instrument_registry_sha256,
        "calendar_registry": generated.calendar_registry_sha256,
    }
    actual = {
        "research_spec": confirmation.research_spec_sha256,
        "data_plan": confirmation.data_plan_sha256,
        "data_plan_confirmation": confirmation.data_plan_confirmation_sha256,
        "source_selection": confirmation.source_selection_sha256,
        "instrument_registry": confirmation.instrument_registry_sha256,
        "calendar_registry": confirmation.calendar_registry_sha256,
    }
    mismatches = [name for name in expected if expected[name] != actual[name]]
    if mismatches:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_CONFIRMATION_MISMATCH,
            SourceSelectionReviewStage.SOURCE_SELECTION_CONFIRMATION_VALIDATION,
            "SourceSelection confirmation does not match: "
            + ", ".join(mismatches),
        )


def _persist_immutable(payload: bytes, path: Path) -> Path:
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    try:
        return persist_immutable_bytes(payload, path)
    except HypothesisLifecycleError as error:
        if error.failure.code == HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_source_selection_review(
                SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_CONFLICT,
                SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
                error.failure.message,
            )
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            error.failure.message,
        )


def persist_generated_source_selection(
    generated: GeneratedSourceSelection,
    output_path: str | Path,
    *,
    generated_at: datetime | None = None,
) -> PersistedSourceSelection:
    """Persist canonical SourceSelection bytes plus a provenance sidecar."""
    from market_validator.hypothesis.lifecycle import safe_output_path

    if not isinstance(generated, GeneratedSourceSelection):
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "generated result must be strictly validated before persistence",
        )
    if calculate_source_selection_sha256(
        generated.source_selection
    ) != generated.source_selection_sha256:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.INVALID_SOURCE_SELECTION,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "SourceSelection content does not match its bound hash",
        )
    selection_bytes = serialize_source_selection(generated.source_selection)
    provenance = SourceSelectionProvenance(
        research_spec_sha256=generated.research_spec_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        data_plan_confirmation_sha256=generated.data_plan_confirmation_sha256,
        source_selection_sha256=generated.source_selection_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        generated_at=generated_at or datetime.now(timezone.utc),
    )
    provenance_bytes = serialize_source_selection_provenance(provenance)
    try:
        selection_path = safe_output_path(output_path)
        provenance_path = safe_output_path(
            selection_path.with_name(selection_path.name + ".provenance.json")
        )
    except HypothesisLifecycleError as error:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            error.failure.message,
        )
    if selection_path == provenance_path:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "SourceSelection and provenance paths must be distinct",
        )
    selection_exists = selection_path.exists() or selection_path.is_symlink()
    provenance_exists = provenance_path.exists() or provenance_path.is_symlink()
    if selection_exists != provenance_exists:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_CONFLICT,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "SourceSelection output pair is incomplete and was not modified",
        )
    created_selection = False
    try:
        persisted_selection_path = _persist_immutable(
            selection_bytes, selection_path
        )
        created_selection = not selection_exists
        persisted_provenance_path = _persist_immutable(
            provenance_bytes, provenance_path
        )
    except SourceSelectionReviewError:
        if created_selection:
            try:
                selection_path.unlink()
            except OSError:
                pass
        raise
    try:
        restored_selection = parse_source_selection(
            persisted_selection_path.read_bytes()
        )
        restored_provenance = parse_source_selection_provenance(
            persisted_provenance_path.read_bytes()
        )
    except OSError:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "persisted SourceSelection output could not be read safely",
        )
    if restored_selection != generated.source_selection:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "persisted SourceSelection did not match the generated result",
        )
    if restored_provenance != provenance:
        fail_source_selection_review(
            SourceSelectionReviewErrorCode.SOURCE_SELECTION_OUTPUT_ERROR,
            SourceSelectionReviewStage.SOURCE_SELECTION_OUTPUT,
            "persisted provenance did not match the generated result",
        )
    return PersistedSourceSelection(
        source_selection_path=persisted_selection_path,
        source_selection_byte_size=len(selection_bytes),
        source_selection_sha256=generated.source_selection_sha256,
        provenance_path=persisted_provenance_path,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        generated=generated,
    )


def persist_source_selection_confirmation(
    confirmation: SourceSelectionConfirmation,
    output_path: str | Path,
) -> Path:
    return _persist_immutable(
        serialize_source_selection_confirmation(confirmation), Path(output_path)
    )


__all__ = [
    "SOURCE_SELECTION_CONFIRMATION_SCHEMA_VERSION",
    "SOURCE_SELECTION_CONFIRMATION_STATEMENT",
    "SOURCE_SELECTION_SCHEMA_VERSION",
    "GeneratedSourceSelection",
    "PersistedSourceSelection",
    "SelectedProviderSource",
    "SourceSelection",
    "SourceSelectionConfirmation",
    "SourceSelectionDecision",
    "SourceSelectionProvenance",
    "SourceSelectionReviewError",
    "SourceSelectionReviewErrorCode",
    "SourceSelectionReviewFailure",
    "SourceSelectionReviewStage",
    "SourceSelectionUnresolvedCode",
    "UnresolvedSourceSelectionRequirement",
    "calculate_source_selection_confirmation_sha256",
    "calculate_source_selection_sha256",
    "calendar_registry_sha256",
    "confirm_source_selection",
    "fail_source_selection_review",
    "generate_source_selection",
    "instrument_registry_sha256",
    "parse_source_selection",
    "parse_source_selection_confirmation",
    "parse_source_selection_decision",
    "parse_source_selection_provenance",
    "persist_generated_source_selection",
    "persist_source_selection_confirmation",
    "serialize_source_selection",
    "serialize_source_selection_confirmation",
    "serialize_source_selection_provenance",
    "source_selection_readiness_blockers",
    "validate_source_selection_confirmation_matches",
]
