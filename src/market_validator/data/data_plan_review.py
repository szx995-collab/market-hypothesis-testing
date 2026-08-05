"""Deterministic, offline DataPlan lifecycle: generation, readiness, confirmation.

A DataPlan stays provider-neutral. A confirmation binds the exact canonical
hashes of the ResearchSpec, the DataPlan, and both registries. It never
authorizes network access, downloads, provider requests, paid services,
analysis, backtesting, trading, or order placement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, NoReturn, TypeVar

from pydantic import BaseModel, StringConstraints, ValidationError, field_serializer, field_validator

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import (
    DataPlan,
    DataRequirementStatus,
)
from market_validator.data.planner import DataPlanningError, plan_data_requirements
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.data.serialization import (
    DataPlanSerializationError,
    calculate_data_plan_sha256,
    parse_data_plan,
    serialize_data_plan,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleError,
    UnresolvedResearchSpecRequirement,
)
from market_validator.research.models import ResearchSpec, StrictResearchModel
from market_validator.research.serialization import (
    ResearchSpecSerializationError,
    calculate_research_spec_sha256,
    parse_research_spec,
    serialize_research_spec,
)


DATA_PLAN_CONFIRMATION_SCHEMA_VERSION = "1.0"
DATA_PLAN_CONFIRMATION_STATEMENT = (
    "I explicitly confirm this exact provider-neutral DataPlan "
    "for subsequent source-selection review."
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DataPlanReviewErrorCode(StrEnum):
    INVALID_DATA_PLAN = "invalid_data_plan"
    DATA_PLAN_NOT_READY = "data_plan_not_ready"
    DATA_PLAN_MISMATCH = "data_plan_mismatch"
    REGISTRY_MISMATCH = "data_plan_registry_mismatch"
    INVALID_DATA_PLAN_CONFIRMATION = "invalid_data_plan_confirmation"
    DATA_PLAN_CONFIRMATION_MISMATCH = "data_plan_confirmation_mismatch"
    OUTPUT_CONFLICT = "data_plan_output_conflict"
    OUTPUT_ERROR = "data_plan_output_error"


class DataPlanReviewStage(StrEnum):
    DATA_PLAN_VALIDATION = "data_plan_validation"
    DATA_PLAN_GENERATION = "data_plan_generation"
    DATA_PLAN_CONFIRMATION_VALIDATION = "data_plan_confirmation_validation"
    DATA_PLAN_OUTPUT = "data_plan_output"


class DataPlanReviewFailure(StrictResearchModel):
    code: DataPlanReviewErrorCode
    stage: DataPlanReviewStage
    message: str
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] = []


class DataPlanReviewError(ValueError):
    """Structured safe failure that never embeds raw input content."""

    def __init__(self, failure: DataPlanReviewFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_data_plan_review(
    code: DataPlanReviewErrorCode,
    stage: DataPlanReviewStage,
    message: str,
    *,
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] | None = None,
) -> NoReturn:
    raise DataPlanReviewError(
        DataPlanReviewFailure(
            code=code,
            stage=stage,
            message=message,
            unresolved_requirements=unresolved_requirements or [],
        )
    )


class GeneratedDataPlan(StrictResearchModel):
    """DataPlan bytes identity plus the exact hashes it is bound to."""

    data_plan: DataPlan
    data_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex


class DataPlanProvenance(StrictResearchModel):
    """Auditable, secret-free provenance for one generated DataPlan."""

    provenance_schema_version: Literal["1.0"] = "1.0"
    research_spec_sha256: Sha256Hex
    proposal_sha256: Sha256Hex | None = None
    proposal_confirmation_sha256: Sha256Hex | None = None
    data_plan_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex | None = None
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


class DataPlanConfirmation(StrictResearchModel):
    """Explicit user confirmation bound to exact canonical hashes."""

    confirmation_schema_version: Literal["1.0"] = DATA_PLAN_CONFIRMATION_SCHEMA_VERSION
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    confirmed: Literal[True]
    confirmation_statement: Literal[
        "I explicitly confirm this exact provider-neutral DataPlan "
        "for subsequent source-selection review."
    ] = DATA_PLAN_CONFIRMATION_STATEMENT
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


class PersistedDataPlan(StrictResearchModel):
    data_plan_path: Path
    data_plan_byte_size: int
    data_plan_sha256: Sha256Hex
    provenance_path: Path
    provenance_sha256: Sha256Hex
    generated: GeneratedDataPlan


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
    code: DataPlanReviewErrorCode,
    stage: DataPlanReviewStage,
    label: str,
) -> ModelT:
    if not isinstance(payload, (bytes, bytearray)):
        fail_data_plan_review(code, stage, f"{label} must be supplied as UTF-8 bytes")
    normalized = bytes(payload)
    try:
        decoded = json.loads(
            normalized.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_data_plan_review(
            code, stage, f"{label} must be exactly one strict UTF-8 JSON object"
        )
    if not isinstance(decoded, dict):
        fail_data_plan_review(code, stage, f"{label} must be a JSON object")
    try:
        return model_type.model_validate_json(normalized)
    except ValidationError:
        fail_data_plan_review(code, stage, f"{label} failed strict domain validation")


def _serialize_strict(
    model: ModelT,
    model_type: type[ModelT],
    *,
    parser: object,
    code: DataPlanReviewErrorCode,
    stage: DataPlanReviewStage,
    label: str,
) -> bytes:
    if not isinstance(model, model_type):
        fail_data_plan_review(code, stage, f"{label} must be a validated model")
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
        fail_data_plan_review(code, stage, f"{label} could not be serialized")
    return payload


def _registry_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def instrument_registry_sha256(registry: InstrumentRegistry) -> str:
    return _registry_sha256(registry.canonical_json())


def calendar_registry_sha256(registry: CalendarRegistry) -> str:
    return _registry_sha256(registry.canonical_json())


def generate_data_plan(
    research_spec: ResearchSpec,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> GeneratedDataPlan:
    """Generate one deterministic provider-neutral DataPlan without I/O."""
    try:
        research_spec = parse_research_spec(serialize_research_spec(research_spec))
    except ResearchSpecSerializationError:
        fail_data_plan_review(
            DataPlanReviewErrorCode.INVALID_DATA_PLAN,
            DataPlanReviewStage.DATA_PLAN_VALIDATION,
            "ResearchSpec failed strict validation",
        )
    try:
        data_plan = plan_data_requirements(
            research_spec, instrument_registry, calendar_registry
        )
    except DataPlanningError:
        fail_data_plan_review(
            DataPlanReviewErrorCode.REGISTRY_MISMATCH,
            DataPlanReviewStage.DATA_PLAN_GENERATION,
            "registry metadata conflicts with the ResearchSpec",
        )
    try:
        data_plan = parse_data_plan(serialize_data_plan(data_plan))
    except DataPlanSerializationError:
        fail_data_plan_review(
            DataPlanReviewErrorCode.INVALID_DATA_PLAN,
            DataPlanReviewStage.DATA_PLAN_VALIDATION,
            "generated DataPlan failed canonical strict validation",
        )
    return GeneratedDataPlan(
        data_plan=data_plan,
        data_plan_sha256=calculate_data_plan_sha256(data_plan),
        research_spec_sha256=calculate_research_spec_sha256(research_spec),
        instrument_registry_sha256=instrument_registry_sha256(instrument_registry),
        calendar_registry_sha256=calendar_registry_sha256(calendar_registry),
    )


def data_plan_readiness_blockers(
    generated: GeneratedDataPlan,
    instrument_registry: InstrumentRegistry,
) -> list[str]:
    """Deterministic blockers before a DataPlan may be confirmed."""
    blockers: list[str] = []
    if generated.data_plan.unresolved_instruments:
        blockers.append(
            "unresolved instruments: "
            + ", ".join(sorted(generated.data_plan.unresolved_instruments))
        )
    for requirement in generated.data_plan.requirements:
        if requirement.status is not DataRequirementStatus.READY:
            blockers.append(
                f"{requirement.variable_id}: requirement is "
                f"{requirement.status.value}"
            )
        entry = instrument_registry.get(requirement.instrument_id)
        if entry is None:
            blockers.append(
                f"{requirement.variable_id}: instrument not found in registry"
            )
            continue
        if entry.identity_status is not IdentityStatus.VERIFIED:
            blockers.append(
                f"{requirement.variable_id}: instrument identity is "
                f"{entry.identity_status.value}, not verified"
            )
        verified_mappings = [
            mapping for mapping in entry.provider_mappings if mapping.verified
        ]
        if not verified_mappings:
            blockers.append(
                f"{requirement.variable_id}: no verified provider mapping"
            )
        blockers.extend(
            f"{requirement.variable_id}: {warning}"
            for warning in requirement.warnings
        )
    return blockers


def confirm_data_plan(
    generated: GeneratedDataPlan,
    instrument_registry: InstrumentRegistry,
    *,
    confirmed_at: datetime,
) -> DataPlanConfirmation:
    """Create a confirmation only for a fully ready, registry-verified plan."""
    blockers = data_plan_readiness_blockers(generated, instrument_registry)
    if blockers:
        fail_data_plan_review(
            DataPlanReviewErrorCode.DATA_PLAN_NOT_READY,
            DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
            "DataPlan is not ready for confirmation: " + "; ".join(blockers),
        )
    return DataPlanConfirmation(
        research_spec_sha256=generated.research_spec_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        confirmed=True,
        confirmed_at=confirmed_at,
    )


def parse_data_plan_confirmation(
    payload: bytes | bytearray,
) -> DataPlanConfirmation:
    return _parse_strict(
        payload,
        DataPlanConfirmation,
        code=DataPlanReviewErrorCode.INVALID_DATA_PLAN_CONFIRMATION,
        stage=DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
        label="DataPlan confirmation",
    )


def serialize_data_plan_confirmation(confirmation: DataPlanConfirmation) -> bytes:
    payload = _serialize_strict(
        confirmation,
        DataPlanConfirmation,
        parser=parse_data_plan_confirmation,
        code=DataPlanReviewErrorCode.INVALID_DATA_PLAN_CONFIRMATION,
        stage=DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
        label="DataPlan confirmation",
    )
    if parse_data_plan_confirmation(payload) != confirmation:
        fail_data_plan_review(
            DataPlanReviewErrorCode.INVALID_DATA_PLAN_CONFIRMATION,
            DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
            "DataPlan confirmation does not round-trip exactly",
        )
    return payload


def calculate_data_plan_confirmation_sha256(
    confirmation: DataPlanConfirmation,
) -> str:
    return hashlib.sha256(
        serialize_data_plan_confirmation(confirmation)
    ).hexdigest()


def validate_data_plan_confirmation_matches(
    generated: GeneratedDataPlan,
    confirmation: DataPlanConfirmation,
) -> None:
    expected = {
        "research_spec": generated.research_spec_sha256,
        "data_plan": generated.data_plan_sha256,
        "instrument_registry": generated.instrument_registry_sha256,
        "calendar_registry": generated.calendar_registry_sha256,
    }
    actual = {
        "research_spec": confirmation.research_spec_sha256,
        "data_plan": confirmation.data_plan_sha256,
        "instrument_registry": confirmation.instrument_registry_sha256,
        "calendar_registry": confirmation.calendar_registry_sha256,
    }
    mismatches = [name for name in expected if expected[name] != actual[name]]
    if mismatches:
        fail_data_plan_review(
            DataPlanReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH,
            DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
            "DataPlan confirmation does not match: " + ", ".join(mismatches),
        )


def parse_data_plan_provenance(
    payload: bytes | bytearray,
) -> DataPlanProvenance:
    return _parse_strict(
        payload,
        DataPlanProvenance,
        code=DataPlanReviewErrorCode.INVALID_DATA_PLAN,
        stage=DataPlanReviewStage.DATA_PLAN_OUTPUT,
        label="DataPlan provenance",
    )


def serialize_data_plan_provenance(provenance: DataPlanProvenance) -> bytes:
    payload = _serialize_strict(
        provenance,
        DataPlanProvenance,
        parser=parse_data_plan_provenance,
        code=DataPlanReviewErrorCode.INVALID_DATA_PLAN,
        stage=DataPlanReviewStage.DATA_PLAN_OUTPUT,
        label="DataPlan provenance",
    )
    if parse_data_plan_provenance(payload) != provenance:
        fail_data_plan_review(
            DataPlanReviewErrorCode.INVALID_DATA_PLAN,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "DataPlan provenance does not round-trip exactly",
        )
    return payload


def _persist_immutable(payload: bytes, path: Path) -> Path:
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    try:
        return persist_immutable_bytes(payload, path)
    except HypothesisLifecycleError as error:
        if error.failure.code == HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_data_plan_review(
                DataPlanReviewErrorCode.OUTPUT_CONFLICT,
                DataPlanReviewStage.DATA_PLAN_OUTPUT,
                error.failure.message,
            )
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_ERROR,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            error.failure.message,
        )


def persist_generated_data_plan(
    generated: GeneratedDataPlan,
    output_path: str | Path,
    *,
    proposal_sha256: str | None = None,
    proposal_confirmation_sha256: str | None = None,
    generated_at: datetime | None = None,
) -> PersistedDataPlan:
    """Persist canonical DataPlan bytes plus an auditable provenance sidecar."""
    from market_validator.hypothesis.lifecycle import safe_output_path

    if not isinstance(generated, GeneratedDataPlan):
        fail_data_plan_review(
            DataPlanReviewErrorCode.INVALID_DATA_PLAN,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "generated result must be strictly validated before persistence",
        )
    plan_bytes = serialize_data_plan(generated.data_plan)
    provenance = DataPlanProvenance(
        research_spec_sha256=generated.research_spec_sha256,
        proposal_sha256=proposal_sha256,
        proposal_confirmation_sha256=proposal_confirmation_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        generated_at=generated_at or datetime.now(timezone.utc),
    )
    provenance_bytes = serialize_data_plan_provenance(provenance)
    plan_path = safe_output_path(output_path)
    provenance_path = safe_output_path(
        plan_path.with_name(plan_path.name + ".provenance.json")
    )
    if plan_path == provenance_path:
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_ERROR,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "DataPlan and provenance paths must be distinct",
        )
    plan_exists = plan_path.exists() or plan_path.is_symlink()
    provenance_exists = provenance_path.exists() or provenance_path.is_symlink()
    if plan_exists != provenance_exists:
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_CONFLICT,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "DataPlan output pair is incomplete and was not modified",
        )
    created_plan = False
    try:
        persisted_plan_path = _persist_immutable(plan_bytes, plan_path)
        created_plan = not plan_exists
        persisted_provenance_path = _persist_immutable(
            provenance_bytes, provenance_path
        )
    except DataPlanReviewError:
        if created_plan:
            try:
                plan_path.unlink()
            except OSError:
                pass
        raise
    try:
        restored_plan = parse_data_plan(persisted_plan_path.read_bytes())
        restored_provenance = parse_data_plan_provenance(
            persisted_provenance_path.read_bytes()
        )
    except OSError:
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_ERROR,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "persisted DataPlan output could not be read safely",
        )
    except DataPlanSerializationError:
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_ERROR,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "persisted DataPlan failed strict reload",
        )
    if restored_plan != generated.data_plan or restored_provenance != provenance:
        fail_data_plan_review(
            DataPlanReviewErrorCode.OUTPUT_ERROR,
            DataPlanReviewStage.DATA_PLAN_OUTPUT,
            "persisted DataPlan output did not match the generated result",
        )
    return PersistedDataPlan(
        data_plan_path=persisted_plan_path,
        data_plan_byte_size=len(plan_bytes),
        data_plan_sha256=generated.data_plan_sha256,
        provenance_path=persisted_provenance_path,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        generated=generated,
    )


def persist_data_plan_confirmation(
    confirmation: DataPlanConfirmation,
    output_path: str | Path,
) -> Path:
    return _persist_immutable(
        serialize_data_plan_confirmation(confirmation), Path(output_path)
    )


def data_plan_confirmation_json_schema() -> dict[str, object]:
    return DataPlanConfirmation.model_json_schema()


__all__ = [
    "DATA_PLAN_CONFIRMATION_SCHEMA_VERSION",
    "DATA_PLAN_CONFIRMATION_STATEMENT",
    "DataPlanConfirmation",
    "DataPlanProvenance",
    "DataPlanReviewError",
    "DataPlanReviewErrorCode",
    "DataPlanReviewFailure",
    "DataPlanReviewStage",
    "GeneratedDataPlan",
    "PersistedDataPlan",
    "calculate_data_plan_confirmation_sha256",
    "calendar_registry_sha256",
    "confirm_data_plan",
    "data_plan_confirmation_json_schema",
    "data_plan_readiness_blockers",
    "fail_data_plan_review",
    "generate_data_plan",
    "instrument_registry_sha256",
    "parse_data_plan_confirmation",
    "parse_data_plan_provenance",
    "persist_data_plan_confirmation",
    "persist_generated_data_plan",
    "serialize_data_plan_confirmation",
    "serialize_data_plan_provenance",
    "validate_data_plan_confirmation_matches",
]
