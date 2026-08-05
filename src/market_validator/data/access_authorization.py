"""Offline, deterministic, single-use data-access authorization.

A DataAccessAuthorization binds one Ready AcquisitionRequestPlan to explicit
access limits (network / local-file), forbids paid access, retry, fallback and
credential resolution, and can be consumed exactly once by a later executor.
It never executes requests, resolves credentials, or touches data.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

from market_validator.data.acquisition_request import (
    AccessMode,
    AcquisitionRequestPlan,
    AcquisitionRequestReviewError,
    AcquisitionRequestReviewErrorCode,
    AcquisitionRequestReviewStage,
    GeneratedAcquisitionRequestPlan,
    acquisition_request_readiness_blockers,
    calculate_acquisition_request_plan_sha256,
    parse_acquisition_request_plan,
    serialize_acquisition_request_plan,
)
from collections.abc import Mapping

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import Identifier, NonEmptyString
from market_validator.data.registry import InstrumentRegistry
from market_validator.hypothesis.lifecycle import HypothesisLifecycleError
from market_validator.research.models import StrictResearchModel

DATA_ACCESS_AUTHORIZATION_SCHEMA_VERSION = "1.0"
DATA_ACCESS_AUTHORIZATION_STATEMENT = (
    "I explicitly authorize one execution attempt of these exact "
    "acquisition requests under the stated access limits."
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DataAccessAuthorizationErrorCode(StrEnum):
    INVALID_DATA_ACCESS_AUTHORIZATION = "invalid_data_access_authorization"
    DATA_ACCESS_AUTHORIZATION_MISMATCH = (
        "data_access_authorization_mismatch"
    )
    ACQUISITION_REQUEST_NOT_READY = "acquisition_request_not_ready"
    REQUIRED_ACCESS_NOT_AUTHORIZED = "required_access_not_authorized"
    PAID_ACCESS_FORBIDDEN = "paid_access_forbidden"
    AUTOMATIC_RETRY_FORBIDDEN = "automatic_retry_forbidden"
    FALLBACK_FORBIDDEN = "fallback_forbidden"
    AUTHORIZATION_ALREADY_CONSUMED = "authorization_already_consumed"
    AUTHORIZATION_OUTPUT_CONFLICT = "authorization_output_conflict"
    AUTHORIZATION_OUTPUT_ERROR = "authorization_output_error"


class DataAccessAuthorizationStage(StrEnum):
    AUTHORIZATION_VALIDATION = "authorization_validation"
    AUTHORIZATION_CONSUMPTION = "authorization_consumption"
    AUTHORIZATION_OUTPUT = "authorization_output"


class DataAccessAuthorizationFailure(StrictResearchModel):
    code: DataAccessAuthorizationErrorCode
    stage: DataAccessAuthorizationStage
    message: str


class DataAccessAuthorizationError(ValueError):
    """Structured safe failure that never embeds raw input content."""

    def __init__(self, failure: DataAccessAuthorizationFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_data_access_authorization(
    code: DataAccessAuthorizationErrorCode,
    stage: DataAccessAuthorizationStage,
    message: str,
) -> NoReturn:
    raise DataAccessAuthorizationError(
        DataAccessAuthorizationFailure(code=code, stage=stage, message=message)
    )


class AuthorizationConsumptionStatus(StrEnum):
    UNCONSUMED = "unconsumed"
    CONSUMED = "consumed"


class DataAccessAuthorization(StrictResearchModel):
    """Explicit, single-use authorization bound to exact canonical hashes."""

    authorization_schema_version: Literal["1.0"] = (
        DATA_ACCESS_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: NonEmptyString
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    authorized_request_ids: list[Identifier]
    network_access_authorized: bool
    local_file_read_authorized: bool
    interactive_credential_resolution_authorized: Literal[False]
    paid_access_authorized: Literal[False]
    automatic_retry_authorized: Literal[False]
    fallback_authorized: Literal[False]
    single_use: Literal[True]
    authorization_statement: Literal[
        "I explicitly authorize one execution attempt of these exact "
        "acquisition requests under the stated access limits."
    ] = DATA_ACCESS_AUTHORIZATION_STATEMENT
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def validate_authorized_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorized_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("authorized_at", when_used="json")
    def serialize_authorized_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DataAccessAuthorizationReceipt(StrictResearchModel):
    """Offline record that an authorization was reserved for one attempt."""

    authorization_id: Identifier
    authorization_sha256: Sha256Hex
    request_plan_sha256: Sha256Hex
    status: Literal[AuthorizationConsumptionStatus.CONSUMED]
    consumed_at: datetime
    attempt_id: NonEmptyString

    @field_validator("consumed_at")
    @classmethod
    def validate_consumed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("consumed_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("consumed_at", when_used="json")
    def serialize_consumed_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PersistedDataAccessAuthorization(StrictResearchModel):
    authorization_path: Path
    authorization_byte_size: int
    authorization_sha256: Sha256Hex
    provenance_path: Path
    provenance_sha256: Sha256Hex
    authorization: DataAccessAuthorization


class PersistedAuthorizationReceipt(StrictResearchModel):
    receipt_path: Path
    receipt_byte_size: int
    receipt_sha256: Sha256Hex
    receipt: DataAccessAuthorizationReceipt


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
    code: DataAccessAuthorizationErrorCode,
    stage: DataAccessAuthorizationStage,
    label: str,
) -> ModelT:
    if not isinstance(payload, (bytes, bytearray)):
        fail_data_access_authorization(
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
        fail_data_access_authorization(
            code, stage, f"{label} must be exactly one strict UTF-8 JSON object"
        )
    if not isinstance(decoded, dict):
        fail_data_access_authorization(code, stage, f"{label} must be a JSON object")
    try:
        return model_type.model_validate_json(normalized)
    except ValidationError:
        fail_data_access_authorization(
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
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            f"{label} could not be serialized",
        )
    return payload


def parse_data_access_authorization(
    payload: bytes | bytearray,
) -> DataAccessAuthorization:
    return _parse_strict(
        payload,
        DataAccessAuthorization,
        code=DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
        stage=DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
        label="DataAccessAuthorization",
    )


def serialize_data_access_authorization(
    authorization: DataAccessAuthorization,
) -> bytes:
    payload = _serialize_strict(
        authorization, label="DataAccessAuthorization"
    )
    if parse_data_access_authorization(payload) != authorization:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "DataAccessAuthorization does not round-trip exactly",
        )
    return payload


def calculate_data_access_authorization_sha256(
    authorization: DataAccessAuthorization,
) -> str:
    return hashlib.sha256(
        serialize_data_access_authorization(authorization)
    ).hexdigest()


def parse_data_access_authorization_receipt(
    payload: bytes | bytearray,
) -> DataAccessAuthorizationReceipt:
    return _parse_strict(
        payload,
        DataAccessAuthorizationReceipt,
        code=DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
        stage=DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
        label="DataAccessAuthorization receipt",
    )


def serialize_data_access_authorization_receipt(
    receipt: DataAccessAuthorizationReceipt,
) -> bytes:
    payload = _serialize_strict(receipt, label="DataAccessAuthorization receipt")
    if parse_data_access_authorization_receipt(payload) != receipt:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
            "DataAccessAuthorization receipt does not round-trip exactly",
        )
    return payload


def calculate_data_access_authorization_receipt_sha256(
    receipt: DataAccessAuthorizationReceipt,
) -> str:
    return hashlib.sha256(
        serialize_data_access_authorization_receipt(receipt)
    ).hexdigest()


def _reparse_plan(
    generated: GeneratedAcquisitionRequestPlan,
) -> GeneratedAcquisitionRequestPlan:
    try:
        restored = parse_acquisition_request_plan(
            serialize_acquisition_request_plan(
                generated.acquisition_request_plan
            )
        )
    except AcquisitionRequestReviewError:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "AcquisitionRequestPlan failed strict validation",
        )
    if restored != generated.acquisition_request_plan:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "AcquisitionRequestPlan does not match its canonical bytes",
        )
    if calculate_acquisition_request_plan_sha256(
        restored
    ) != generated.acquisition_request_plan_sha256:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "AcquisitionRequestPlan does not match its bound hash",
        )
    return generated


def _derive_authorization_id(
    generated: GeneratedAcquisitionRequestPlan,
    authorized_request_ids: list[str],
) -> str:
    identity = json.dumps(
        {
            "research_spec_sha256": generated.research_spec_sha256,
            "data_plan_sha256": generated.data_plan_sha256,
            "data_plan_confirmation_sha256": (
                generated.data_plan_confirmation_sha256
            ),
            "source_selection_sha256": generated.source_selection_sha256,
            "source_selection_confirmation_sha256": (
                generated.source_selection_confirmation_sha256
            ),
            "acquisition_request_plan_sha256": (
                generated.acquisition_request_plan_sha256
            ),
            "instrument_registry_sha256": (
                generated.instrument_registry_sha256
            ),
            "calendar_registry_sha256": generated.calendar_registry_sha256,
            "authorized_request_ids": sorted(authorized_request_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def create_data_access_authorization(
    generated_plan: GeneratedAcquisitionRequestPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    capability_snapshots: Mapping[str, object],
    authorized_request_ids: list[str],
    *,
    authorized_at: datetime,
) -> DataAccessAuthorization:
    """Create a single-use authorization only for a fully ready plan."""
    from market_validator.data.acquisition_request import (
        ProviderCapabilitySnapshot,
    )

    generated = _reparse_plan(generated_plan)
    plan = generated.acquisition_request_plan
    blockers = acquisition_request_readiness_blockers(
        generated,
        instrument_registry,
        calendar_registry,
        capability_snapshots,
    )
    if blockers:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.ACQUISITION_REQUEST_NOT_READY,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "AcquisitionRequestPlan is not ready for authorization: "
            + "; ".join(blockers),
        )
    plan_request_ids = [
        request.requirement_id for request in plan.requests
    ]
    if len(authorized_request_ids) != len(set(authorized_request_ids)):
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "authorized request ids must be unique",
        )
    if sorted(authorized_request_ids) != sorted(plan_request_ids):
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "authorized request ids must exactly match the plan requests",
        )
    network_required = any(
        request.access_mode is AccessMode.NETWORK for request in plan.requests
    )
    local_file_required = any(
        request.access_mode is AccessMode.LOCAL_FILE
        for request in plan.requests
    )
    if network_required and local_file_required:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "mixed network and local-file requests are not supported "
            "in one authorization",
        )
    return DataAccessAuthorization(
        authorization_id=_derive_authorization_id(
            generated, authorized_request_ids
        ),
        research_spec_sha256=generated.research_spec_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        data_plan_confirmation_sha256=generated.data_plan_confirmation_sha256,
        source_selection_sha256=generated.source_selection_sha256,
        source_selection_confirmation_sha256=(
            generated.source_selection_confirmation_sha256
        ),
        acquisition_request_plan_sha256=(
            generated.acquisition_request_plan_sha256
        ),
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        authorized_request_ids=sorted(authorized_request_ids),
        network_access_authorized=network_required,
        local_file_read_authorized=local_file_required,
        interactive_credential_resolution_authorized=False,
        paid_access_authorized=False,
        automatic_retry_authorized=False,
        fallback_authorized=False,
        single_use=True,
        authorized_at=authorized_at,
    )


def validate_data_access_authorization_matches(
    generated_plan: GeneratedAcquisitionRequestPlan,
    authorization: DataAccessAuthorization,
) -> None:
    expected = {
        "research_spec": generated_plan.research_spec_sha256,
        "data_plan": generated_plan.data_plan_sha256,
        "data_plan_confirmation": generated_plan.data_plan_confirmation_sha256,
        "source_selection": generated_plan.source_selection_sha256,
        "source_selection_confirmation": (
            generated_plan.source_selection_confirmation_sha256
        ),
        "acquisition_request_plan": (
            generated_plan.acquisition_request_plan_sha256
        ),
        "instrument_registry": generated_plan.instrument_registry_sha256,
        "calendar_registry": generated_plan.calendar_registry_sha256,
    }
    actual = {
        "research_spec": authorization.research_spec_sha256,
        "data_plan": authorization.data_plan_sha256,
        "data_plan_confirmation": authorization.data_plan_confirmation_sha256,
        "source_selection": authorization.source_selection_sha256,
        "source_selection_confirmation": (
            authorization.source_selection_confirmation_sha256
        ),
        "acquisition_request_plan": (
            authorization.acquisition_request_plan_sha256
        ),
        "instrument_registry": authorization.instrument_registry_sha256,
        "calendar_registry": authorization.calendar_registry_sha256,
    }
    mismatches = [name for name in expected if expected[name] != actual[name]]
    if mismatches:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.DATA_ACCESS_AUTHORIZATION_MISMATCH,
            DataAccessAuthorizationStage.AUTHORIZATION_VALIDATION,
            "DataAccessAuthorization does not match: " + ", ".join(mismatches),
        )


def consume_data_access_authorization(
    authorization: DataAccessAuthorization,
    *,
    attempt_id: str,
    consumed_at: datetime,
    existing_receipt: DataAccessAuthorizationReceipt | None = None,
) -> DataAccessAuthorizationReceipt:
    """Reserve the single use of an authorization for one future attempt."""
    if not isinstance(authorization, DataAccessAuthorization):
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
            "authorization must be a strictly validated model",
        )
    if existing_receipt is not None:
        if (
            existing_receipt.authorization_sha256
            != calculate_data_access_authorization_sha256(authorization)
        ):
            fail_data_access_authorization(
                DataAccessAuthorizationErrorCode.DATA_ACCESS_AUTHORIZATION_MISMATCH,
                DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
                "existing receipt does not match this authorization",
            )
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_ALREADY_CONSUMED,
            DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
            "authorization was already consumed",
        )
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_CONSUMPTION,
            "attempt_id must be a non-empty string",
        )
    return DataAccessAuthorizationReceipt(
        authorization_id=authorization.authorization_id,
        authorization_sha256=calculate_data_access_authorization_sha256(
            authorization
        ),
        request_plan_sha256=authorization.acquisition_request_plan_sha256,
        status=AuthorizationConsumptionStatus.CONSUMED,
        consumed_at=consumed_at,
        attempt_id=attempt_id,
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
            fail_data_access_authorization(
                DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_CONFLICT,
                DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
                error.failure.message,
            )
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            error.failure.message,
        )


def persist_data_access_authorization(
    authorization: DataAccessAuthorization,
    output_path: str | Path,
    *,
    authorized_at: datetime | None = None,
) -> PersistedDataAccessAuthorization:
    """Persist canonical authorization bytes plus a provenance sidecar."""
    from market_validator.hypothesis.lifecycle import safe_output_path

    if not isinstance(authorization, DataAccessAuthorization):
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "authorization must be strictly validated before persistence",
        )
    authorization_bytes = serialize_data_access_authorization(authorization)
    provenance = _AuthorizationProvenance(
        research_spec_sha256=authorization.research_spec_sha256,
        data_plan_sha256=authorization.data_plan_sha256,
        data_plan_confirmation_sha256=(
            authorization.data_plan_confirmation_sha256
        ),
        source_selection_sha256=authorization.source_selection_sha256,
        source_selection_confirmation_sha256=(
            authorization.source_selection_confirmation_sha256
        ),
        acquisition_request_plan_sha256=(
            authorization.acquisition_request_plan_sha256
        ),
        instrument_registry_sha256=authorization.instrument_registry_sha256,
        calendar_registry_sha256=authorization.calendar_registry_sha256,
        authorized_at=authorized_at or authorization.authorized_at,
    )
    provenance_bytes = _serialize_strict(provenance, label="authorization provenance")
    try:
        authorization_path = safe_output_path(output_path)
        provenance_path = safe_output_path(
            authorization_path.with_name(
                authorization_path.name + ".provenance.json"
            )
        )
    except HypothesisLifecycleError as error:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            error.failure.message,
        )
    if authorization_path == provenance_path:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "authorization and provenance paths must be distinct",
        )
    authorization_exists = (
        authorization_path.exists() or authorization_path.is_symlink()
    )
    provenance_exists = (
        provenance_path.exists() or provenance_path.is_symlink()
    )
    if authorization_exists != provenance_exists:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_CONFLICT,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "authorization output pair is incomplete and was not modified",
        )
    created_authorization = False
    try:
        persisted_authorization_path = _persist_immutable(
            authorization_bytes, authorization_path
        )
        created_authorization = not authorization_exists
        persisted_provenance_path = _persist_immutable(
            provenance_bytes, provenance_path
        )
    except DataAccessAuthorizationError:
        if created_authorization:
            try:
                authorization_path.unlink()
            except OSError:
                pass
        raise
    try:
        restored = parse_data_access_authorization(
            persisted_authorization_path.read_bytes()
        )
    except OSError:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "persisted authorization could not be read safely",
        )
    if restored != authorization:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "persisted authorization did not match the generated result",
        )
    return PersistedDataAccessAuthorization(
        authorization_path=persisted_authorization_path,
        authorization_byte_size=len(authorization_bytes),
        authorization_sha256=calculate_data_access_authorization_sha256(
            authorization
        ),
        provenance_path=persisted_provenance_path,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        authorization=authorization,
    )


def persist_data_access_authorization_receipt(
    receipt: DataAccessAuthorizationReceipt,
    output_path: str | Path,
) -> PersistedAuthorizationReceipt:
    if not isinstance(receipt, DataAccessAuthorizationReceipt):
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.INVALID_DATA_ACCESS_AUTHORIZATION,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "receipt must be strictly validated before persistence",
        )
    receipt_bytes = serialize_data_access_authorization_receipt(receipt)
    persisted_path = _persist_immutable(receipt_bytes, Path(output_path))
    try:
        restored = parse_data_access_authorization_receipt(
            persisted_path.read_bytes()
        )
    except OSError:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "persisted receipt could not be read safely",
        )
    if restored != receipt:
        fail_data_access_authorization(
            DataAccessAuthorizationErrorCode.AUTHORIZATION_OUTPUT_ERROR,
            DataAccessAuthorizationStage.AUTHORIZATION_OUTPUT,
            "persisted receipt did not match the generated result",
        )
    return PersistedAuthorizationReceipt(
        receipt_path=persisted_path,
        receipt_byte_size=len(receipt_bytes),
        receipt_sha256=calculate_data_access_authorization_receipt_sha256(
            receipt
        ),
        receipt=receipt,
    )


class _AuthorizationProvenance(StrictResearchModel):
    provenance_schema_version: Literal["1.0"] = "1.0"
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def validate_authorized_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorized_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("authorized_at", when_used="json")
    def serialize_authorized_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "AuthorizationConsumptionStatus",
    "DATA_ACCESS_AUTHORIZATION_SCHEMA_VERSION",
    "DATA_ACCESS_AUTHORIZATION_STATEMENT",
    "DataAccessAuthorization",
    "DataAccessAuthorizationError",
    "DataAccessAuthorizationErrorCode",
    "DataAccessAuthorizationFailure",
    "DataAccessAuthorizationReceipt",
    "DataAccessAuthorizationStage",
    "PersistedAuthorizationReceipt",
    "PersistedDataAccessAuthorization",
    "calculate_data_access_authorization_receipt_sha256",
    "calculate_data_access_authorization_sha256",
    "consume_data_access_authorization",
    "create_data_access_authorization",
    "fail_data_access_authorization",
    "parse_data_access_authorization",
    "parse_data_access_authorization_receipt",
    "persist_data_access_authorization",
    "persist_data_access_authorization_receipt",
    "serialize_data_access_authorization",
    "serialize_data_access_authorization_receipt",
    "validate_data_access_authorization_matches",
]
