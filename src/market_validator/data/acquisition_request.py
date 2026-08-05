"""Offline, deterministic acquisition-request planning.

An AcquisitionRequestPlan turns a confirmed SourceSelection into exact public
acquisition requests (provider, endpoint, canonical public parameters, sample
window and provable pre-sample resolution). It never executes requests,
resolves credentials, touches files, or authorizes anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
from market_validator.data.serialization import (
    DataPlanSerializationError,
    calculate_data_plan_sha256,
    parse_data_plan,
)
from market_validator.data.providers.base import ProviderCapabilities
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.data.source_selection import (
    GeneratedSourceSelection,
    SourceSelectionConfirmation,
    SourceSelectionReviewError,
    calculate_source_selection_confirmation_sha256,
    instrument_registry_sha256,
    parse_source_selection_confirmation,
    serialize_source_selection_confirmation,
    validate_source_selection_confirmation_matches,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleError,
    UnresolvedResearchSpecRequirement,
)
from market_validator.research.enums import (
    DataRevisionMode,
    Frequency,
    Transformation,
)
from market_validator.research.models import StrictResearchModel
from market_validator.research.serialization import (
    calculate_research_spec_sha256,
)

ACQUISITION_REQUEST_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AcquisitionRequestReviewErrorCode(StrEnum):
    INVALID_ACQUISITION_REQUEST = "invalid_acquisition_request"
    SOURCE_SELECTION_CONFIRMATION_MISMATCH = (
        "source_selection_confirmation_mismatch"
    )
    PROVIDER_CAPABILITY_MISMATCH = "provider_capability_mismatch"
    PRE_SAMPLE_UNRESOLVED = "pre_sample_unresolved"
    ACQUISITION_REQUEST_NOT_READY = "acquisition_request_not_ready"
    ACQUISITION_REQUEST_OUTPUT_CONFLICT = (
        "acquisition_request_output_conflict"
    )
    ACQUISITION_REQUEST_OUTPUT_ERROR = "acquisition_request_output_error"


class AcquisitionRequestReviewStage(StrEnum):
    ACQUISITION_REQUEST_VALIDATION = "acquisition_request_validation"
    CAPABILITY_VALIDATION = "capability_validation"
    PRE_SAMPLE_RESOLUTION = "pre_sample_resolution"
    ACQUISITION_REQUEST_OUTPUT = "acquisition_request_output"


class AcquisitionRequestReviewFailure(StrictResearchModel):
    code: AcquisitionRequestReviewErrorCode
    stage: AcquisitionRequestReviewStage
    message: str
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] = []


class AcquisitionRequestReviewError(ValueError):
    """Structured safe failure that never embeds raw input content."""

    def __init__(self, failure: AcquisitionRequestReviewFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_acquisition_request_review(
    code: AcquisitionRequestReviewErrorCode,
    stage: AcquisitionRequestReviewStage,
    message: str,
    *,
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] | None = None,
) -> NoReturn:
    raise AcquisitionRequestReviewError(
        AcquisitionRequestReviewFailure(
            code=code,
            stage=stage,
            message=message,
            unresolved_requirements=unresolved_requirements or [],
        )
    )


class AccessMode(StrEnum):
    NETWORK = "network"
    LOCAL_FILE = "local_file"


class RequestMethod(StrEnum):
    GET = "get"
    READ = "read"


class PreSampleResolutionMethod(StrEnum):
    NONE_REQUIRED = "none_required"
    VERIFIED_CALENDAR_SESSIONS = "verified_calendar_sessions"
    PROVIDER_NATIVE_PREVIOUS_OBSERVATIONS = (
        "provider_native_previous_observations"
    )
    UNRESOLVED = "unresolved"


class PreSampleStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ProviderCapabilitySnapshot(StrictResearchModel):
    """Deterministic, credential-free view of one provider's capabilities."""

    provider_id: Identifier
    supported_access_modes: list[AccessMode]
    supported_frequencies: list[Frequency]
    supported_transforms: list[Transformation]
    supports_date_range: bool
    supports_revision_policy: bool
    supports_dry_run: bool
    supports_previous_observations: bool
    requires_credential: bool
    paid_access_possible: bool


def snapshot_provider_capabilities(
    capabilities: ProviderCapabilities,
) -> ProviderCapabilitySnapshot:
    """Model a ProviderCapabilities object as a deterministic snapshot.

    Access modes are derived from the code facts in ProviderCapabilities:
    ``requires_network`` implies network access and ``supports_local_files``
    implies local-file access. Transforms are not restricted by either real
    provider's capability validation, so the snapshot lists the full closed
    vocabulary; date ranges are supported by both providers; dry-run support
    follows the presence of a dry-run path (FRED has one, CSV does not).
    Previous-observation ("exactly N observations before the sample start")
    support is not expressible by the current FRED or CSV contracts and is
    therefore modeled as False unless a provider declares it explicitly.
    """
    access_modes: list[AccessMode] = []
    if capabilities.requires_network:
        access_modes.append(AccessMode.NETWORK)
    if capabilities.supports_local_files:
        access_modes.append(AccessMode.LOCAL_FILE)
    if not access_modes:
        raise ValueError(
            "provider capabilities must declare at least one access mode"
        )
    return ProviderCapabilitySnapshot(
        provider_id=capabilities.provider_id,
        supported_access_modes=access_modes,
        supported_frequencies=list(capabilities.supported_frequencies),
        supported_transforms=list(Transformation),
        supports_date_range=True,
        supports_revision_policy=bool(
            capabilities.supported_revision_policies
        ),
        supports_dry_run=False,
        supports_previous_observations=False,
        requires_credential=capabilities.requires_authentication,
        paid_access_possible=False,
    )


class PreSampleResolution(StrictResearchModel):
    """Provable resolution of required pre-sample observations."""

    requirement_id: Identifier
    required_periods: int
    method: PreSampleResolutionMethod
    status: PreSampleStatus
    resolved_acquisition_start: date | None
    calendar_id: Identifier | None
    evidence: list[NonEmptyString]


class PublicAcquisitionRequest(StrictResearchModel):
    """One exact public acquisition request for one requirement."""

    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    provider_id: Identifier
    provider_symbol: NonEmptyString
    dataset_or_endpoint: NonEmptyString
    access_mode: AccessMode
    request_method: RequestMethod
    public_parameters: dict[str, str]
    sample_start: date
    sample_end: date
    acquisition_start: date | None
    acquisition_end: date
    pre_sample_periods_required: int
    pre_sample_resolution_method: PreSampleResolutionMethod
    revision_policy: DataRevisionMode

    @field_validator("sample_end")
    @classmethod
    def validate_sample_end(cls, value: date, info) -> date:
        sample_start = info.data.get("sample_start")
        if sample_start is not None and sample_start > value:
            raise ValueError("sample_start must not be later than sample_end")
        return value

    @field_validator("acquisition_start")
    @classmethod
    def validate_acquisition_start(cls, value: date | None, info) -> date | None:
        if value is None:
            return None
        sample_start = info.data.get("sample_start")
        if sample_start is not None and value > sample_start:
            raise ValueError(
                "acquisition_start must not be later than sample_start"
            )
        return value


class AcquisitionUnresolvedCode(StrEnum):
    SOURCE_SELECTION_UNRESOLVED = "source_selection_unresolved"
    PRE_SAMPLE_CALENDAR_ADAPTER_UNAVAILABLE = (
        "pre_sample_calendar_adapter_unavailable"
    )
    PRE_SAMPLE_PROVIDER_METHOD_UNAVAILABLE = (
        "pre_sample_provider_method_unavailable"
    )
    PRE_SAMPLE_RESOLUTION_AMBIGUOUS = "pre_sample_resolution_ambiguous"
    LOCAL_FILE_CONTENT_IDENTITY_REQUIRED = (
        "local_file_content_identity_required"
    )


class UnresolvedAcquisitionRequirement(StrictResearchModel):
    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    code: AcquisitionUnresolvedCode
    message: NonEmptyString


class AcquisitionRequestPlan(StrictResearchModel):
    """Deterministic, provider-neutral acquisition request artifact."""

    acquisition_request_schema_version: Literal["1.0"] = (
        ACQUISITION_REQUEST_SCHEMA_VERSION
    )
    request_plan_id: NonEmptyString
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    capability_snapshot_sha256s: dict[Identifier, Sha256Hex]
    requests: list[PublicAcquisitionRequest]
    unresolved_requirements: list[UnresolvedAcquisitionRequirement]
    warnings: list[NonEmptyString]


class GeneratedAcquisitionRequestPlan(StrictResearchModel):
    """AcquisitionRequestPlan bytes identity plus the exact bound hashes."""

    acquisition_request_plan: AcquisitionRequestPlan
    acquisition_request_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    capability_snapshot_sha256s: dict[Identifier, Sha256Hex]


class RenderPublicAcquisitionRequest(StrictResearchModel):
    """Exact public rendering an executor must receive later."""

    requirement_id: Identifier
    provider_id: Identifier
    method: RequestMethod
    endpoint: NonEmptyString
    canonical_public_parameters: dict[str, str]
    access_mode: AccessMode


class PersistedAcquisitionRequestPlan(StrictResearchModel):
    acquisition_request_plan_path: Path
    acquisition_request_plan_byte_size: int
    acquisition_request_plan_sha256: Sha256Hex
    provenance_path: Path
    provenance_sha256: Sha256Hex
    generated: GeneratedAcquisitionRequestPlan


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
    code: AcquisitionRequestReviewErrorCode,
    stage: AcquisitionRequestReviewStage,
    label: str,
) -> ModelT:
    if not isinstance(payload, (bytes, bytearray)):
        fail_acquisition_request_review(
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
        fail_acquisition_request_review(
            code, stage, f"{label} must be exactly one strict UTF-8 JSON object"
        )
    if not isinstance(decoded, dict):
        fail_acquisition_request_review(
            code, stage, f"{label} must be a JSON object"
        )
    try:
        return model_type.model_validate_json(normalized)
    except ValidationError:
        fail_acquisition_request_review(
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
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            f"{label} could not be serialized",
        )
    return payload


def parse_provider_capability_snapshot(
    payload: bytes | bytearray,
) -> ProviderCapabilitySnapshot:
    return _parse_strict(
        payload,
        ProviderCapabilitySnapshot,
        code=AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
        stage=AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
        label="Provider capability snapshot",
    )


def serialize_provider_capability_snapshot(
    snapshot: ProviderCapabilitySnapshot,
) -> bytes:
    payload = _serialize_strict(snapshot, label="Provider capability snapshot")
    if parse_provider_capability_snapshot(payload) != snapshot:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            "Provider capability snapshot does not round-trip exactly",
        )
    return payload


def calculate_provider_capability_snapshot_sha256(
    snapshot: ProviderCapabilitySnapshot,
) -> str:
    return hashlib.sha256(
        serialize_provider_capability_snapshot(snapshot)
    ).hexdigest()


def parse_acquisition_request_plan(payload: bytes | bytearray) -> AcquisitionRequestPlan:
    return _parse_strict(
        payload,
        AcquisitionRequestPlan,
        code=AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
        stage=AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
        label="AcquisitionRequestPlan",
    )


def serialize_acquisition_request_plan(plan: AcquisitionRequestPlan) -> bytes:
    payload = _serialize_strict(plan, label="AcquisitionRequestPlan")
    if parse_acquisition_request_plan(payload) != plan:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "AcquisitionRequestPlan does not round-trip exactly",
        )
    return payload


def calculate_acquisition_request_plan_sha256(
    plan: AcquisitionRequestPlan,
) -> str:
    return hashlib.sha256(serialize_acquisition_request_plan(plan)).hexdigest()


def _reparse_source_selection(
    generated: GeneratedSourceSelection,
    confirmation: SourceSelectionConfirmation,
) -> GeneratedSourceSelection:
    from market_validator.data.source_selection import (
        parse_source_selection,
        serialize_source_selection,
    )

    try:
        restored = parse_source_selection(
            serialize_source_selection(generated.source_selection)
        )
        restored_confirmation = parse_source_selection_confirmation(
            serialize_source_selection_confirmation(confirmation)
        )
    except SourceSelectionReviewError:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "SourceSelection inputs failed strict validation",
        )
    if restored != generated.source_selection:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "SourceSelection does not match its canonical bytes",
        )
    try:
        validate_source_selection_confirmation_matches(
            generated, restored_confirmation
        )
    except SourceSelectionReviewError:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.SOURCE_SELECTION_CONFIRMATION_MISMATCH,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "SourceSelection confirmation no longer matches",
        )
    return generated


def _validate_registry_hashes(
    generated: GeneratedSourceSelection,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> None:
    from market_validator.data.source_selection import (
        calendar_registry_sha256,
    )

    mismatches = []
    if instrument_registry_sha256(instrument_registry) != generated.instrument_registry_sha256:
        mismatches.append("instrument registry")
    if calendar_registry_sha256(calendar_registry) != generated.calendar_registry_sha256:
        mismatches.append("calendar registry")
    if mismatches:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.SOURCE_SELECTION_CONFIRMATION_MISMATCH,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "registry snapshots no longer match the bound hashes: "
            + ", ".join(mismatches),
        )


def _is_absolute_path_identity(value: str) -> bool:
    if value.startswith("file://"):
        return True
    return Path(value).is_absolute() and ".." not in Path(value).parts


def resolve_pre_sample_requirement(
    requirement: DataRequirement,
    calendar_registry: CalendarRegistry,
    snapshot: ProviderCapabilitySnapshot,
    session_adapters: Mapping[str, Callable[[date, int], date]] | None = None,
) -> PreSampleResolution:
    """Deterministically prove the pre-sample acquisition start, or fail closed.

    N pre-sample periods are never interpreted as N calendar days unless a
    verified calendar-session adapter produced the date. Without a provable
    method the result stays unresolved and blocks readiness/authorization.
    """
    adapters = session_adapters or {}
    periods = requirement.required_pre_sample_periods
    if periods == 0:
        return PreSampleResolution(
            requirement_id=requirement.requirement_id,
            required_periods=0,
            method=PreSampleResolutionMethod.NONE_REQUIRED,
            status=PreSampleStatus.RESOLVED,
            resolved_acquisition_start=requirement.start_date,
            calendar_id=None,
            evidence=["no pre-sample observations are required"],
        )
    calendar_definition = None
    if requirement.calendar_id is not None:
        calendar_definition = calendar_registry.get(requirement.calendar_id)
    if snapshot.supports_previous_observations:
        return PreSampleResolution(
            requirement_id=requirement.requirement_id,
            required_periods=periods,
            method=PreSampleResolutionMethod.PROVIDER_NATIVE_PREVIOUS_OBSERVATIONS,
            status=PreSampleStatus.RESOLVED,
            resolved_acquisition_start=None,
            calendar_id=requirement.calendar_id,
            evidence=[
                "provider natively supports exactly N previous observations "
                "before the sample start"
            ],
        )
    if calendar_definition is not None and calendar_definition.schedule_adapter:
        adapter_id = calendar_definition.schedule_adapter
        adapter = adapters.get(adapter_id)
        if adapter is None:
            return PreSampleResolution(
                requirement_id=requirement.requirement_id,
                required_periods=periods,
                method=PreSampleResolutionMethod.UNRESOLVED,
                status=PreSampleStatus.UNRESOLVED,
                resolved_acquisition_start=None,
                calendar_id=requirement.calendar_id,
                evidence=[
                    "calendar declares an adapter but no executable "
                    "verified adapter is available"
                ],
            )
        try:
            resolved_start = adapter(requirement.start_date, periods)
        except (TypeError, ValueError):
            return PreSampleResolution(
                requirement_id=requirement.requirement_id,
                required_periods=periods,
                method=PreSampleResolutionMethod.UNRESOLVED,
                status=PreSampleStatus.UNRESOLVED,
                resolved_acquisition_start=None,
                calendar_id=requirement.calendar_id,
                evidence=[
                    "declared calendar session adapter failed to compute "
                    "the acquisition start"
                ],
            )
        return PreSampleResolution(
            requirement_id=requirement.requirement_id,
            required_periods=periods,
            method=PreSampleResolutionMethod.VERIFIED_CALENDAR_SESSIONS,
            status=PreSampleStatus.RESOLVED,
            resolved_acquisition_start=resolved_start,
            calendar_id=requirement.calendar_id,
            evidence=[
                f"verified calendar session adapter {adapter_id!r} "
                "computed the acquisition start"
            ],
        )
    return PreSampleResolution(
        requirement_id=requirement.requirement_id,
        required_periods=periods,
        method=PreSampleResolutionMethod.UNRESOLVED,
        status=PreSampleStatus.UNRESOLVED,
        resolved_acquisition_start=None,
        calendar_id=requirement.calendar_id,
        evidence=[
            "no verified calendar-session adapter and no provider-native "
            "previous-observation method is available"
        ],
    )


def _pre_sample_unresolved_code(
    resolution: PreSampleResolution,
    snapshot: ProviderCapabilitySnapshot,
    calendar_registry: CalendarRegistry,
) -> AcquisitionUnresolvedCode:
    calendar_definition = (
        calendar_registry.get(resolution.calendar_id)
        if resolution.calendar_id is not None
        else None
    )
    if calendar_definition is not None and calendar_definition.schedule_adapter:
        return AcquisitionUnresolvedCode.PRE_SAMPLE_CALENDAR_ADAPTER_UNAVAILABLE
    if snapshot.supports_previous_observations:
        return AcquisitionUnresolvedCode.PRE_SAMPLE_RESOLUTION_AMBIGUOUS
    return AcquisitionUnresolvedCode.PRE_SAMPLE_PROVIDER_METHOD_UNAVAILABLE


def _fred_public_parameters(
    requirement: DataRequirement, series_id: str
) -> dict[str, str]:
    mode = requirement.revision_policy.mode
    output_type = "4" if mode is DataRevisionMode.INITIAL_RELEASE else "1"
    parameters: dict[str, str] = {
        "series_id": series_id,
        "file_type": "json",
        "units": "lin",
        "sort_order": "asc",
        "observation_start": requirement.start_date.isoformat(),
        "observation_end": requirement.end_date.isoformat(),
        "output_type": output_type,
        "revision_policy": mode.value,
    }
    if mode is DataRevisionMode.INITIAL_RELEASE:
        parameters.update(
            {
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
            }
        )
    return parameters


def _default_fred_template(
    requirement: DataRequirement, series_id: str
) -> dict[str, str]:
    return _fred_public_parameters(requirement, series_id)


def _build_request(
    requirement: DataRequirement,
    selection,
    snapshot: ProviderCapabilitySnapshot,
    resolution: PreSampleResolution,
    parameter_templates: Mapping[
        str, Callable[[DataRequirement, str], dict[str, str]]
    ],
) -> PublicAcquisitionRequest:
    if AccessMode.NETWORK in snapshot.supported_access_modes:
        access_mode = AccessMode.NETWORK
        request_method = RequestMethod.GET
        template = parameter_templates.get(selection.provider_id)
        if template is None:
            fail_acquisition_request_review(
                AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
                AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
                f"no public parameter template for network provider "
                f"{selection.provider_id}",
            )
        public_parameters = template(requirement, selection.provider_symbol)
        endpoint = selection.dataset_or_endpoint
    else:
        access_mode = AccessMode.LOCAL_FILE
        request_method = RequestMethod.READ
        public_parameters = {"source_uri": selection.provider_symbol}
        endpoint = selection.provider_symbol
    return PublicAcquisitionRequest(
        requirement_id=requirement.requirement_id,
        variable_id=requirement.variable_id,
        instrument_id=requirement.instrument_id,
        provider_id=selection.provider_id,
        provider_symbol=selection.provider_symbol,
        dataset_or_endpoint=endpoint,
        access_mode=access_mode,
        request_method=request_method,
        public_parameters=public_parameters,
        sample_start=requirement.start_date,
        sample_end=requirement.end_date,
        acquisition_start=resolution.resolved_acquisition_start,
        acquisition_end=requirement.end_date,
        pre_sample_periods_required=requirement.required_pre_sample_periods,
        pre_sample_resolution_method=resolution.method,
        revision_policy=requirement.revision_policy.mode,
    )


def _derive_request_plan_id(plan_core: dict[str, object]) -> str:
    identity = json.dumps(
        plan_core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def generate_acquisition_request_plan(
    generated_source_selection: GeneratedSourceSelection,
    source_selection_confirmation: SourceSelectionConfirmation,
    data_plan: object,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    capability_snapshots: Mapping[str, ProviderCapabilitySnapshot],
    *,
    session_adapters: Mapping[str, Callable[[date, int], date]] | None = None,
    public_parameter_templates: Mapping[
        str, Callable[[DataRequirement, str], dict[str, str]]
    ]
    | None = None,
) -> GeneratedAcquisitionRequestPlan:
    """Deterministically plan exact public requests from a confirmed selection.

    Capability mismatches are hard failures; unprovable pre-sample needs are
    structured unresolved requirements that block readiness and authorization.
    """
    from market_validator.data.data_plan_review import (
        parse_data_plan,
        serialize_data_plan,
    )
    from market_validator.data.serialization import DataPlanSerializationError
    from market_validator.data.models import DataPlan

    if not isinstance(data_plan, DataPlan):
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "data plan must be a strictly validated model",
        )
    if not isinstance(capability_snapshots, Mapping):
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            "capability snapshots must be a provider-keyed mapping",
        )
    generated = _reparse_source_selection(
        generated_source_selection, source_selection_confirmation
    )
    _validate_registry_hashes(generated, instrument_registry, calendar_registry)
    try:
        restored_plan = parse_data_plan(
            _serialize_strict(data_plan, label="DataPlan")
        )
    except DataPlanSerializationError:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "DataPlan failed strict validation",
        )
    if calculate_data_plan_sha256(restored_plan) != generated.data_plan_sha256:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
            "DataPlan does not match its bound hash",
        )
    capability_by_provider = dict(capability_snapshots)
    capability_sha256s = {
        provider_id: calculate_provider_capability_snapshot_sha256(snapshot)
        for provider_id, snapshot in sorted(capability_snapshots.items())
    }
    templates: Mapping[
        str, Callable[[DataRequirement, str], dict[str, str]]
    ] = {
        **public_parameter_templates,
        "fred": _default_fred_template,
    } if public_parameter_templates else {"fred": _default_fred_template}

    requests: list[PublicAcquisitionRequest] = []
    unresolved: list[UnresolvedAcquisitionRequirement] = []
    warnings: list[str] = []

    for unresolved_item in generated.source_selection.unresolved_requirements:
        unresolved.append(
            UnresolvedAcquisitionRequirement(
                requirement_id=unresolved_item.requirement_id,
                variable_id=unresolved_item.variable_id,
                instrument_id=unresolved_item.instrument_id,
                code=AcquisitionUnresolvedCode.SOURCE_SELECTION_UNRESOLVED,
                message="source selection is unresolved for this requirement",
            )
        )

    for selection in generated.source_selection.selections:
        snapshot = capability_by_provider.get(selection.provider_id)
        if snapshot is None:
            fail_acquisition_request_review(
                AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
                AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
                f"no capability snapshot for provider "
                f"{selection.provider_id}",
            )
        requirement = _find_requirement(
            restored_plan, selection.requirement_id
        )
        if requirement is None:
            fail_acquisition_request_review(
                AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
                AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
                f"selection references an unknown requirement "
                f"{selection.requirement_id}",
            )
        _validate_capability(requirement, selection, snapshot)
        resolution = resolve_pre_sample_requirement(
            requirement,
            calendar_registry,
            snapshot,
            session_adapters,
        )
        if resolution.status is PreSampleStatus.UNRESOLVED:
            unresolved.append(
                UnresolvedAcquisitionRequirement(
                    requirement_id=requirement.requirement_id,
                    variable_id=requirement.variable_id,
                    instrument_id=requirement.instrument_id,
                    code=_pre_sample_unresolved_code(
                        resolution, snapshot, calendar_registry
                    ),
                    message="; ".join(resolution.evidence),
                )
            )
        request = _build_request(
            requirement, selection, snapshot, resolution, templates
        )
        if (
            request.access_mode is AccessMode.LOCAL_FILE
            and not _is_absolute_path_identity(selection.provider_symbol)
        ):
            unresolved.append(
                UnresolvedAcquisitionRequirement(
                    requirement_id=requirement.requirement_id,
                    variable_id=requirement.variable_id,
                    instrument_id=requirement.instrument_id,
                    code=AcquisitionUnresolvedCode.LOCAL_FILE_CONTENT_IDENTITY_REQUIRED,
                    message="local-file source identity cannot be proven "
                    "without a normalized absolute path or file URI",
                )
            )
        requests.append(request)

    requests.sort(key=lambda item: item.requirement_id)
    unresolved.sort(key=lambda item: (item.requirement_id, item.code.value))
    warnings = sorted(set(warnings))

    confirmation_sha256 = calculate_source_selection_confirmation_sha256(
        source_selection_confirmation
    )
    core = {
        "research_spec_sha256": generated.research_spec_sha256,
        "data_plan_sha256": generated.data_plan_sha256,
        "data_plan_confirmation_sha256": (
            generated.data_plan_confirmation_sha256
        ),
        "source_selection_sha256": generated.source_selection_sha256,
        "source_selection_confirmation_sha256": confirmation_sha256,
        "instrument_registry_sha256": generated.instrument_registry_sha256,
        "calendar_registry_sha256": generated.calendar_registry_sha256,
        "capability_snapshot_sha256s": capability_sha256s,
        "requests": [
            request.model_dump(mode="json") for request in requests
        ],
        "unresolved_requirements": [
            item.model_dump(mode="json") for item in unresolved
        ],
    }
    plan = AcquisitionRequestPlan(
        request_plan_id=_derive_request_plan_id(core),
        research_spec_sha256=generated.research_spec_sha256,
        data_plan_sha256=generated.data_plan_sha256,
        data_plan_confirmation_sha256=generated.data_plan_confirmation_sha256,
        source_selection_sha256=generated.source_selection_sha256,
        source_selection_confirmation_sha256=confirmation_sha256,
        instrument_registry_sha256=generated.instrument_registry_sha256,
        calendar_registry_sha256=generated.calendar_registry_sha256,
        capability_snapshot_sha256s=capability_sha256s,
        requests=requests,
        unresolved_requirements=unresolved,
        warnings=warnings,
    )
    return GeneratedAcquisitionRequestPlan(
        acquisition_request_plan=plan,
        acquisition_request_plan_sha256=(
            calculate_acquisition_request_plan_sha256(plan)
        ),
        research_spec_sha256=plan.research_spec_sha256,
        data_plan_sha256=plan.data_plan_sha256,
        data_plan_confirmation_sha256=plan.data_plan_confirmation_sha256,
        source_selection_sha256=plan.source_selection_sha256,
        source_selection_confirmation_sha256=(
            plan.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=plan.instrument_registry_sha256,
        calendar_registry_sha256=plan.calendar_registry_sha256,
        capability_snapshot_sha256s=plan.capability_snapshot_sha256s,
    )


def _find_requirement(
    plan: object, requirement_id: str
) -> DataRequirement | None:
    from market_validator.data.models import DataPlan

    if not isinstance(plan, DataPlan):
        return None
    for requirement in plan.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    return None


def _validate_capability(
    requirement: DataRequirement,
    selection,
    snapshot: ProviderCapabilitySnapshot,
) -> None:
    if not snapshot.supported_access_modes:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            f"provider {selection.provider_id} declares no access mode",
        )
    if requirement.frequency not in snapshot.supported_frequencies:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            f"provider {selection.provider_id} does not support frequency "
            f"{requirement.frequency.value}",
        )
    if requirement.transformation not in snapshot.supported_transforms:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            f"provider {selection.provider_id} does not support transform "
            f"{requirement.transformation.value}",
        )
    if not snapshot.supports_date_range:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            f"provider {selection.provider_id} does not support date ranges",
        )
    if (
        requirement.revision_policy.mode is not DataRevisionMode.NOT_APPLICABLE
        and not snapshot.supports_revision_policy
    ):
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.PROVIDER_CAPABILITY_MISMATCH,
            AcquisitionRequestReviewStage.CAPABILITY_VALIDATION,
            f"provider {selection.provider_id} does not support revision "
            f"policy {requirement.revision_policy.mode.value}",
        )


def acquisition_request_readiness_blockers(
    generated: GeneratedAcquisitionRequestPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    capability_snapshots: Mapping[str, ProviderCapabilitySnapshot],
) -> list[str]:
    """Deterministic blockers before the plan may be authorized."""
    blockers: list[str] = []
    if generated.acquisition_request_plan.unresolved_requirements:
        blockers.append(
            "unresolved acquisition requirements: "
            + ", ".join(
                sorted(
                    item.requirement_id
                    for item in (
                        generated.acquisition_request_plan.unresolved_requirements
                    )
                )
            )
        )
    if instrument_registry_sha256(
        instrument_registry
    ) != generated.instrument_registry_sha256:
        blockers.append("instrument registry no longer matches the bound hash")
    from market_validator.data.source_selection import (
        calendar_registry_sha256,
    )

    if calendar_registry_sha256(
        calendar_registry
    ) != generated.calendar_registry_sha256:
        blockers.append("calendar registry no longer matches the bound hash")
    bound_snapshot_sha256s = (
        generated.acquisition_request_plan.capability_snapshot_sha256s
    )
    for provider_id, snapshot in capability_snapshots.items():
        if provider_id not in bound_snapshot_sha256s:
            blockers.append(
                f"capability snapshot for {provider_id} is not bound to "
                "the plan"
            )
            continue
        if calculate_provider_capability_snapshot_sha256(
            snapshot
        ) != bound_snapshot_sha256s[provider_id]:
            blockers.append(
                f"capability snapshot for {provider_id} no longer matches "
                "the bound hash"
            )
    request_ids = [
        item.requirement_id for item in generated.acquisition_request_plan.requests
    ]
    if len(request_ids) != len(set(request_ids)):
        blockers.append("duplicate acquisition requests")
    if generated.acquisition_request_plan.warnings:
        blockers.extend(
            f"warning: {warning}"
            for warning in generated.acquisition_request_plan.warnings
        )
    for request in generated.acquisition_request_plan.requests:
        snapshot = capability_snapshots.get(request.provider_id)
        if snapshot is None:
            blockers.append(
                f"{request.requirement_id}: capability snapshot missing for "
                f"{request.provider_id}"
            )
            continue
        if request.access_mode not in snapshot.supported_access_modes:
            blockers.append(
                f"{request.requirement_id}: access mode no longer supported "
                f"by {request.provider_id}"
            )
        if (
            request.revision_policy is not DataRevisionMode.NOT_APPLICABLE
            and not snapshot.supports_revision_policy
        ):
            blockers.append(
                f"{request.requirement_id}: revision policy no longer "
                f"supported by {request.provider_id}"
            )
        if request.pre_sample_resolution_method is (
            PreSampleResolutionMethod.UNRESOLVED
        ):
            blockers.append(
                f"{request.requirement_id}: pre-sample resolution is unresolved"
            )
    return blockers


def render_public_acquisition_request(
    generated: GeneratedAcquisitionRequestPlan,
    requirement_id: str,
) -> RenderPublicAcquisitionRequest:
    """Render the exact public request an executor must receive later."""
    for request in generated.acquisition_request_plan.requests:
        if request.requirement_id == requirement_id:
            endpoint = (
                request.dataset_or_endpoint
                if request.access_mode is AccessMode.NETWORK
                else request.provider_symbol
            )
            return RenderPublicAcquisitionRequest(
                requirement_id=request.requirement_id,
                provider_id=request.provider_id,
                method=request.request_method,
                endpoint=endpoint,
                canonical_public_parameters=dict(
                    sorted(request.public_parameters.items())
                ),
                access_mode=request.access_mode,
            )
    fail_acquisition_request_review(
        AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
        AcquisitionRequestReviewStage.ACQUISITION_REQUEST_VALIDATION,
        f"no acquisition request exists for {requirement_id}",
    )


def persist_generated_acquisition_request_plan(
    generated: GeneratedAcquisitionRequestPlan,
    output_path: str | Path,
    *,
    generated_at: datetime | None = None,
) -> PersistedAcquisitionRequestPlan:
    """Persist canonical plan bytes plus a provenance sidecar."""
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
        safe_output_path,
    )

    if not isinstance(generated, GeneratedAcquisitionRequestPlan):
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "generated result must be strictly validated before persistence",
        )
    if calculate_acquisition_request_plan_sha256(
        generated.acquisition_request_plan
    ) != generated.acquisition_request_plan_sha256:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.INVALID_ACQUISITION_REQUEST,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "AcquisitionRequestPlan content does not match its bound hash",
        )
    plan_bytes = serialize_acquisition_request_plan(
        generated.acquisition_request_plan
    )
    provenance = _AcquisitionRequestProvenance(
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
        generated_at=generated_at or datetime.now(timezone.utc),
    )
    provenance_bytes = _serialize_strict(provenance, label="plan provenance")
    try:
        plan_path = safe_output_path(output_path)
        provenance_path = safe_output_path(
            plan_path.with_name(plan_path.name + ".provenance.json")
        )
    except HypothesisLifecycleError as error:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_ERROR,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            error.failure.message,
        )
    if plan_path == provenance_path:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_ERROR,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "plan and provenance paths must be distinct",
        )
    plan_exists = plan_path.exists() or plan_path.is_symlink()
    provenance_exists = (
        provenance_path.exists() or provenance_path.is_symlink()
    )
    if plan_exists != provenance_exists:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_CONFLICT,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "plan output pair is incomplete and was not modified",
        )
    created_plan = False
    try:
        persisted_plan_path = _persist_immutable(plan_bytes, plan_path)
        created_plan = not plan_exists
        persisted_provenance_path = _persist_immutable(
            provenance_bytes, provenance_path
        )
    except AcquisitionRequestReviewError:
        if created_plan:
            try:
                plan_path.unlink()
            except OSError:
                pass
        raise
    try:
        restored_plan = parse_acquisition_request_plan(
            persisted_plan_path.read_bytes()
        )
    except OSError:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_ERROR,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "persisted plan output could not be read safely",
        )
    if restored_plan != generated.acquisition_request_plan:
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_ERROR,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            "persisted plan did not match the generated result",
        )
    return PersistedAcquisitionRequestPlan(
        acquisition_request_plan_path=persisted_plan_path,
        acquisition_request_plan_byte_size=len(plan_bytes),
        acquisition_request_plan_sha256=(
            generated.acquisition_request_plan_sha256
        ),
        provenance_path=persisted_provenance_path,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        generated=generated,
    )


class _AcquisitionRequestProvenance(StrictResearchModel):
    provenance_schema_version: Literal["1.0"] = "1.0"
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
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


def _persist_immutable(payload: bytes, path: Path) -> Path:
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    try:
        return persist_immutable_bytes(payload, path)
    except HypothesisLifecycleError as error:
        if error.failure.code == HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_acquisition_request_review(
                AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_CONFLICT,
                AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
                error.failure.message,
            )
        fail_acquisition_request_review(
            AcquisitionRequestReviewErrorCode.ACQUISITION_REQUEST_OUTPUT_ERROR,
            AcquisitionRequestReviewStage.ACQUISITION_REQUEST_OUTPUT,
            error.failure.message,
        )


__all__ = [
    "ACQUISITION_REQUEST_SCHEMA_VERSION",
    "AccessMode",
    "AcquisitionRequestPlan",
    "AcquisitionRequestReviewError",
    "AcquisitionRequestReviewErrorCode",
    "AcquisitionRequestReviewFailure",
    "AcquisitionRequestReviewStage",
    "AcquisitionUnresolvedCode",
    "GeneratedAcquisitionRequestPlan",
    "PersistedAcquisitionRequestPlan",
    "PreSampleResolution",
    "PreSampleResolutionMethod",
    "PreSampleStatus",
    "ProviderCapabilitySnapshot",
    "PublicAcquisitionRequest",
    "RenderPublicAcquisitionRequest",
    "RequestMethod",
    "UnresolvedAcquisitionRequirement",
    "acquisition_request_readiness_blockers",
    "calculate_acquisition_request_plan_sha256",
    "calculate_provider_capability_snapshot_sha256",
    "fail_acquisition_request_review",
    "generate_acquisition_request_plan",
    "parse_acquisition_request_plan",
    "parse_provider_capability_snapshot",
    "persist_generated_acquisition_request_plan",
    "render_public_acquisition_request",
    "resolve_pre_sample_requirement",
    "serialize_acquisition_request_plan",
    "serialize_provider_capability_snapshot",
    "snapshot_provider_capabilities",
]
