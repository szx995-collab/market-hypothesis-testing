"""Deterministic data readiness assessment and DataReadyManifest (Phase 4).

Snapshot Verified is not Data Ready. This module reloads a verified snapshot,
validates the full bundle set, checks quality acceptance, sample coverage,
pre-sample completeness, availability and revision consistency, and — only
when every requirement is ready — produces a deterministic, hash-bound
DataReadyManifest. It never touches providers, credentials, network, or
analysis.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, NoReturn, Protocol

from pydantic import (
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
)

from market_validator.data.acquisition_request import (
    GeneratedAcquisitionRequestPlan,
    Identifier,
    NonEmptyString,
    calculate_acquisition_request_plan_sha256,
    parse_acquisition_request_plan,
    serialize_acquisition_request_plan,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import (
    DataBundle,
    DataPlan,
    DataRequirement,
    DataRequirementStatus,
)
from market_validator.data.quality import QualityStatus
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.snapshot import (
    AcquisitionExecutionOutcome,
    AcquisitionSnapshotManifest,
    ExecutionStatus,
    SnapshotError,
    parse_execution_outcome,
    parse_snapshot_manifest,
    verify_acquisition_snapshot,
)
from market_validator.data.storage import resolve_data_directory
from market_validator.research.enums import (
    DataRevisionMode,
    Frequency,
    InformationCutoffType,
)
from market_validator.research.models import StrictResearchModel

READINESS_SCHEMA_VERSION = "1.0"
DATA_READY_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReadinessStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


class ReadinessErrorCode(StrEnum):
    INVALID_DATA_READINESS_INPUT = "invalid_data_readiness_input"
    SNAPSHOT_VERIFICATION_FAILED = "snapshot_verification_failed"
    SNAPSHOT_OUTCOME_NOT_SUCCEEDED = "snapshot_outcome_not_succeeded"
    SNAPSHOT_BINDING_MISMATCH = "snapshot_binding_mismatch"
    DATA_PLAN_BINDING_MISMATCH = "data_plan_binding_mismatch"
    REGISTRY_BINDING_MISMATCH = "registry_binding_mismatch"
    MISSING_REQUIREMENT_BUNDLE = "missing_requirement_bundle"
    DUPLICATE_REQUIREMENT_BUNDLE = "duplicate_requirement_bundle"
    UNEXPECTED_REQUIREMENT_BUNDLE = "unexpected_requirement_bundle"
    BUNDLE_HASH_MISMATCH = "bundle_hash_mismatch"
    BUNDLE_REQUIREMENT_MISMATCH = "bundle_requirement_mismatch"
    BUNDLE_SOURCE_MISMATCH = "bundle_source_mismatch"
    SOURCE_CONTENT_HASH_MISMATCH = "source_content_hash_mismatch"
    UNKNOWN_SOURCE_CONTENT_VERIFIER = "unknown_source_content_verifier"
    QUALITY_FAILED = "quality_failed"
    SAMPLE_COVERAGE_UNVERIFIABLE = "sample_coverage_unverifiable"
    SAMPLE_COVERAGE_INCOMPLETE = "sample_coverage_incomplete"
    SAMPLE_COVERAGE_EXTRA_OBSERVATIONS = (
        "sample_coverage_extra_observations"
    )
    PRE_SAMPLE_METHOD_UNVERIFIABLE = "pre_sample_method_unverifiable"
    PRE_SAMPLE_MISSING_OBSERVATIONS = "pre_sample_missing_observations"
    PRE_SAMPLE_EXTRA_OBSERVATIONS = "pre_sample_extra_observations"
    PRE_SAMPLE_REQUEST_WINDOW_MISMATCH = "pre_sample_request_window_mismatch"
    INFORMATION_CUTOFF_UNVERIFIABLE = "information_cutoff_unverifiable"
    INFORMATION_CUTOFF_VIOLATION = "information_cutoff_violation"
    REVISION_POLICY_MISMATCH = "revision_policy_mismatch"
    UNSUPPORTED_FREQUENCY = "unsupported_frequency"
    DATA_READINESS_NOT_READY = "data_readiness_not_ready"
    DATA_READY_MANIFEST_MISMATCH = "data_ready_manifest_mismatch"
    DATA_READY_OUTPUT_CONFLICT = "data_ready_output_conflict"
    DATA_READY_OUTPUT_ERROR = "data_ready_output_error"


class ReadinessStage(StrEnum):
    INPUT_VALIDATION = "input_validation"
    SNAPSHOT_RELOAD = "snapshot_reload"
    BUNDLE_VALIDATION = "bundle_validation"
    SOURCE_CONTENT_VALIDATION = "source_content_validation"
    QUALITY_VALIDATION = "quality_validation"
    SAMPLE_COVERAGE = "sample_coverage"
    PRE_SAMPLE_COVERAGE = "pre_sample_coverage"
    AVAILABILITY_VALIDATION = "availability_validation"
    REVISION_VALIDATION = "revision_validation"
    MANIFEST_GENERATION = "manifest_generation"
    MANIFEST_OUTPUT = "manifest_output"
    MANIFEST_VERIFICATION = "manifest_verification"


class ReadinessError(ValueError):
    """Safe structured failure; messages never embed raw data or secrets."""

    def __init__(
        self,
        code: ReadinessErrorCode,
        stage: ReadinessStage,
        message: str,
        *,
        requirement_id: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.safe_message = message
        self.requirement_id = requirement_id
        super().__init__(f"{code.value}: {message}")


def fail_readiness(
    code: ReadinessErrorCode,
    stage: ReadinessStage,
    message: str,
    *,
    requirement_id: str | None = None,
) -> NoReturn:
    raise ReadinessError(
        code, stage, message, requirement_id=requirement_id
    )


class ReadinessBlocker(StrictResearchModel):
    code: NonEmptyString
    stage: ReadinessStage
    message: NonEmptyString


class RequirementReadinessAssessment(StrictResearchModel):
    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    provider_id: Identifier
    bundle_relative_path: NonEmptyString
    bundle_sha256: Sha256Hex
    source_content_sha256: Sha256Hex
    quality_status: NonEmptyString
    quality_issue_codes: list[NonEmptyString]
    observation_count: int
    coverage_method: NonEmptyString
    sample_expected_sessions: int
    sample_observed_sessions: int
    sample_first_expected_session: date | None
    sample_last_expected_session: date | None
    sample_first_observed_session: date | None
    sample_last_observed_session: date | None
    pre_sample_required_periods: int
    pre_sample_expected_sessions: int
    pre_sample_observed_sessions: int
    pre_sample_first_expected_session: date | None
    pre_sample_last_expected_session: date | None
    pre_sample_first_observed_session: date | None
    pre_sample_last_observed_session: date | None
    availability_status: NonEmptyString
    revision_status: NonEmptyString
    status: ReadinessStatus
    blockers: list[ReadinessBlocker]
    warnings: list[NonEmptyString]


class DataReadinessAssessment(StrictResearchModel):
    readiness_schema_version: str = "1.0"
    assessment_id: NonEmptyString
    status: ReadinessStatus
    snapshot_id: NonEmptyString
    snapshot_manifest_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    requirements: list[RequirementReadinessAssessment]
    blockers: list[ReadinessBlocker]
    warnings: list[NonEmptyString]

    @field_validator("requirements")
    @classmethod
    def sort_requirements(
        cls, value: list[RequirementReadinessAssessment]
    ) -> list[RequirementReadinessAssessment]:
        return sorted(value, key=lambda item: item.requirement_id)

    @field_validator("blockers")
    @classmethod
    def sort_dedupe_blockers(
        cls, value: list[ReadinessBlocker]
    ) -> list[ReadinessBlocker]:
        seen: set[tuple[str, str, str]] = set()
        result: list[ReadinessBlocker] = []
        for blocker in sorted(value, key=lambda item: (item.code, item.message)):
            key = (blocker.code, blocker.stage.value, blocker.message)
            if key in seen:
                continue
            seen.add(key)
            result.append(blocker)
        return result

    @field_validator("warnings")
    @classmethod
    def sort_dedupe_warnings(
        cls, value: list[NonEmptyString]
    ) -> list[NonEmptyString]:
        return sorted(set(value))


class DataReadyBundleRecord(StrictResearchModel):
    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    provider_id: Identifier
    bundle_relative_path: NonEmptyString
    bundle_sha256: Sha256Hex
    source_content_sha256: Sha256Hex
    quality_status: NonEmptyString
    quality_issue_codes: list[NonEmptyString]
    observation_count: int
    sample_session_count: int
    pre_sample_session_count: int
    warnings: list[NonEmptyString]


class DataReadyManifest(StrictResearchModel):
    data_ready_schema_version: str = "1.0"
    data_ready_id: NonEmptyString
    readiness_assessment_sha256: Sha256Hex
    snapshot_id: NonEmptyString
    snapshot_manifest_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    bundles: list[DataReadyBundleRecord]
    warnings: list[NonEmptyString]

    @field_validator("bundles")
    @classmethod
    def sort_bundles(
        cls, value: list[DataReadyBundleRecord]
    ) -> list[DataReadyBundleRecord]:
        return sorted(value, key=lambda item: item.requirement_id)

    @field_validator("warnings")
    @classmethod
    def sort_dedupe_warnings(
        cls, value: list[NonEmptyString]
    ) -> list[NonEmptyString]:
        return sorted(set(value))


class GeneratedDataReadyManifest(StrictResearchModel):
    data_ready_manifest: DataReadyManifest
    data_ready_manifest_sha256: Sha256Hex
    readiness_assessment_sha256: Sha256Hex
    snapshot_manifest_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex


class DataReadyProvenance(StrictResearchModel):
    generated_at: datetime
    snapshot_path: Path | None = None

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("generated_at", when_used="json")
    def serialize_generated_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VerifiedSessionScheduleAdapter(Protocol):
    """Deterministic verified calendar-session schedule adapter."""

    def sessions_between(self, start_date: date, end_date: date) -> list[date]:
        """All verified sessions in the closed interval, strictly increasing."""
        ...

    def previous_sessions(self, before_date: date, count: int) -> list[date]:
        """Exactly `count` verified sessions strictly before the date."""
        ...


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


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(model: StrictResearchModel) -> bytes:
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
    return payload


def _parse_strict(payload: bytes, model_type, label: str):
    try:
        json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            f"{label} failed strict JSON validation",
        )
    try:
        return model_type.model_validate_json(payload)
    except ValidationError:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            f"{label} failed strict domain validation",
        )


def parse_data_readiness_assessment(
    payload: bytes,
) -> DataReadinessAssessment:
    return _parse_strict(payload, DataReadinessAssessment, "readiness assessment")


def serialize_data_readiness_assessment(
    assessment: DataReadinessAssessment,
) -> bytes:
    payload = _canonical_bytes(assessment)
    if parse_data_readiness_assessment(payload) != assessment:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "readiness assessment does not round-trip exactly",
        )
    return payload


def calculate_data_readiness_assessment_sha256(
    assessment: DataReadinessAssessment,
) -> str:
    return _sha256_hex(serialize_data_readiness_assessment(assessment))


def parse_data_ready_manifest(payload: bytes) -> DataReadyManifest:
    return _parse_strict(payload, DataReadyManifest, "data ready manifest")


def serialize_data_ready_manifest(manifest: DataReadyManifest) -> bytes:
    payload = _canonical_bytes(manifest)
    if parse_data_ready_manifest(payload) != manifest:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "data ready manifest does not round-trip exactly",
        )
    return payload


def calculate_data_ready_manifest_sha256(manifest: DataReadyManifest) -> str:
    return _sha256_hex(serialize_data_ready_manifest(manifest))


def _derive_assessment_id(
    snapshot_manifest_sha256: str,
    plan_sha256: str,
    data_plan_sha256: str,
) -> str:
    identity = json.dumps(
        {
            "snapshot_manifest_sha256": snapshot_manifest_sha256,
            "acquisition_request_plan_sha256": plan_sha256,
            "data_plan_sha256": data_plan_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_hex(identity)[:32]


def _derive_data_ready_id(
    assessment_sha256: str,
    snapshot_manifest_sha256: str,
    plan_sha256: str,
) -> str:
    identity = json.dumps(
        {
            "readiness_assessment_sha256": assessment_sha256,
            "snapshot_manifest_sha256": snapshot_manifest_sha256,
            "acquisition_request_plan_sha256": plan_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_hex(identity)[:32]


def _validate_plan_and_bindings(
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> None:
    from market_validator.data.data_plan_review import (
        calculate_data_plan_sha256,
        calendar_registry_sha256,
        instrument_registry_sha256,
    )

    if not isinstance(data_plan, DataPlan):
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "data plan must be a strictly validated model",
        )
    if data_plan.schema_version != "1.0":
        fail_readiness(
            ReadinessErrorCode.DATA_PLAN_BINDING_MISMATCH,
            ReadinessStage.INPUT_VALIDATION,
            "data plan schema is not supported",
        )
    try:
        restored_plan = parse_acquisition_request_plan(
            serialize_acquisition_request_plan(
                generated_plan.acquisition_request_plan
            )
        )
    except Exception:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "acquisition request plan failed strict validation",
        )
    if (
        generated_plan.acquisition_request_plan.acquisition_request_schema_version
        != "1.2"
    ):
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "acquisition request plan schema must be 1.2",
        )
    if calculate_acquisition_request_plan_sha256(
        restored_plan
    ) != generated_plan.acquisition_request_plan_sha256:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.INPUT_VALIDATION,
            "acquisition request plan does not match its bound hash",
        )
    if generated_plan.data_plan_sha256 != calculate_data_plan_sha256(
        data_plan
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_PLAN_BINDING_MISMATCH,
            ReadinessStage.INPUT_VALIDATION,
            "data plan does not match the bound plan hash",
        )
    if generated_plan.instrument_registry_sha256 != instrument_registry_sha256(
        instrument_registry
    ):
        fail_readiness(
            ReadinessErrorCode.REGISTRY_BINDING_MISMATCH,
            ReadinessStage.INPUT_VALIDATION,
            "instrument registry does not match the bound hash",
        )
    if generated_plan.calendar_registry_sha256 != calendar_registry_sha256(
        calendar_registry
    ):
        fail_readiness(
            ReadinessErrorCode.REGISTRY_BINDING_MISMATCH,
            ReadinessStage.INPUT_VALIDATION,
            "calendar registry does not match the bound hash",
        )


def _load_bundle_artifacts(
    snapshot_path: Path,
    manifest: AcquisitionSnapshotManifest,
    data_plan: DataPlan,
    plan_requests: list,
) -> dict[str, tuple[Path, str, DataBundle]]:
    """Load every bundle artifact with strict hash and identity checks."""
    bundles: dict[str, tuple[Path, str, DataBundle]] = {}
    seen_requirement_ids: set[str] = set()
    for record in manifest.request_records:
        requirement_id = record.requirement_id
        if requirement_id in seen_requirement_ids:
            fail_readiness(
                ReadinessErrorCode.DUPLICATE_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "duplicate request record in snapshot manifest",
                requirement_id=requirement_id,
            )
        seen_requirement_ids.add(requirement_id)
        requirement = _find_requirement(data_plan, requirement_id)
        if requirement is None:
            fail_readiness(
                ReadinessErrorCode.UNEXPECTED_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "snapshot contains a requirement that is not in the data plan",
                requirement_id=requirement_id,
            )
        if not any(
            request.requirement_id == requirement_id
            for request in plan_requests
        ):
            fail_readiness(
                ReadinessErrorCode.UNEXPECTED_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "snapshot contains a requirement that is not in the plan",
                requirement_id=requirement_id,
            )
        bundle_artifacts = [
            artifact
            for artifact in record.artifacts
            if artifact.role == "bundle"
        ]
        if len(bundle_artifacts) != 1:
            fail_readiness(
                ReadinessErrorCode.MISSING_REQUIREMENT_BUNDLE
                if not bundle_artifacts
                else ReadinessErrorCode.DUPLICATE_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "each requirement must have exactly one bundle artifact",
                requirement_id=requirement_id,
            )
        artifact = bundle_artifacts[0]
        relative = _safe_relative_path(artifact.relative_path)
        artifact_path = (
            snapshot_path / "requests" / requirement_id / relative
        )
        if not artifact_path.is_file() or artifact_path.is_symlink():
            fail_readiness(
                ReadinessErrorCode.BUNDLE_HASH_MISMATCH,
                ReadinessStage.BUNDLE_VALIDATION,
                "bundle artifact is missing or not a regular file",
                requirement_id=requirement_id,
            )
        content = artifact_path.read_bytes()
        if _sha256_hex(content) != artifact.sha256:
            fail_readiness(
                ReadinessErrorCode.BUNDLE_HASH_MISMATCH,
                ReadinessStage.BUNDLE_VALIDATION,
                "bundle artifact hash does not match the snapshot manifest",
                requirement_id=requirement_id,
            )
        try:
            bundle = DataBundle.model_validate_json(content)
        except ValidationError:
            fail_readiness(
                ReadinessErrorCode.BUNDLE_HASH_MISMATCH,
                ReadinessStage.BUNDLE_VALIDATION,
                "bundle artifact failed strict validation",
                requirement_id=requirement_id,
            )
        if bundle.requirement != requirement:
            fail_readiness(
                ReadinessErrorCode.BUNDLE_REQUIREMENT_MISMATCH,
                ReadinessStage.BUNDLE_VALIDATION,
                "bundle requirement does not match the data plan",
                requirement_id=requirement_id,
            )
        if bundle.source.is_fallback:
            fail_readiness(
                ReadinessErrorCode.BUNDLE_SOURCE_MISMATCH,
                ReadinessStage.BUNDLE_VALIDATION,
                "bundle must not be a fallback",
                requirement_id=requirement_id,
            )
        if not bundle.observations:
            fail_readiness(
                ReadinessErrorCode.QUALITY_FAILED,
                ReadinessStage.QUALITY_VALIDATION,
                "bundle contains no observations",
                requirement_id=requirement_id,
            )
        bundles[requirement_id] = (artifact_path, artifact.sha256, bundle)
    # No extra bundles for requirements not in the plan:
    for request in plan_requests:
        if request.requirement_id not in bundles:
            fail_readiness(
                ReadinessErrorCode.MISSING_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "plan request has no bundle artifact in the snapshot",
                requirement_id=request.requirement_id,
            )
    return bundles


def _safe_relative_path(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or relative_path.startswith("/"):
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.BUNDLE_VALIDATION,
            "bundle relative path must be relative",
        )
    if "\\" in relative_path:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.BUNDLE_VALIDATION,
            "bundle relative path must use forward slashes",
        )
    if ".." in candidate.parts:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.BUNDLE_VALIDATION,
            "bundle relative path must not contain parent segments",
        )
    return candidate


def _find_requirement(data_plan: DataPlan, requirement_id: str):
    for requirement in data_plan.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    return None


def _find_request(plan, requirement_id: str):
    for request in plan.acquisition_request_plan.requests:
        if request.requirement_id == requirement_id:
            return request
    return None


def _source_content_verifier(
    bundle: DataBundle,
    snapshot_path: Path,
    record,
    requirement_id: str,
) -> str | None:
    """Return the recomputed source content SHA-256 or fail with a blocker."""
    provider_id = bundle.source.provider_id
    raw_artifacts = [
        artifact for artifact in record.artifacts if artifact.role == "raw"
    ]
    if provider_id == "fred":
        pages: list[bytes] = []
        for artifact in raw_artifacts:
            if not artifact.relative_path.startswith("raw/observations-page-"):
                continue
            artifact_path = (
                snapshot_path
                / "requests"
                / requirement_id
                / _safe_relative_path(artifact.relative_path)
            )
            if not artifact_path.is_file() or artifact_path.is_symlink():
                fail_readiness(
                    ReadinessErrorCode.SOURCE_CONTENT_HASH_MISMATCH,
                    ReadinessStage.SOURCE_CONTENT_VALIDATION,
                    "FRED observation page artifact is missing",
                    requirement_id=requirement_id,
                )
            pages.append(artifact_path.read_bytes())
        if not pages:
            fail_readiness(
                ReadinessErrorCode.SOURCE_CONTENT_HASH_MISMATCH,
                ReadinessStage.SOURCE_CONTENT_VALIDATION,
                "FRED observation pages are missing from the snapshot",
                requirement_id=requirement_id,
            )
        digest = hashlib.sha256()
        for page in pages:
            digest.update(len(page).to_bytes(8, "big"))
            digest.update(page)
        return digest.hexdigest()
    if provider_id == "local_csv":
        csv_artifacts = [
            artifact
            for artifact in raw_artifacts
            if artifact.relative_path.startswith("raw/")
        ]
        if len(csv_artifacts) != 1:
            fail_readiness(
                ReadinessErrorCode.SOURCE_CONTENT_HASH_MISMATCH,
                ReadinessStage.SOURCE_CONTENT_VALIDATION,
                "local CSV raw artifact is missing from the snapshot",
                requirement_id=requirement_id,
            )
        artifact_path = (
            snapshot_path
            / "requests"
            / requirement_id
            / _safe_relative_path(csv_artifacts[0].relative_path)
        )
        if not artifact_path.is_file() or artifact_path.is_symlink():
            fail_readiness(
                ReadinessErrorCode.SOURCE_CONTENT_HASH_MISMATCH,
                ReadinessStage.SOURCE_CONTENT_VALIDATION,
                "local CSV raw artifact is not a regular file",
                requirement_id=requirement_id,
            )
        return _sha256_hex(artifact_path.read_bytes())
    fail_readiness(
        ReadinessErrorCode.UNKNOWN_SOURCE_CONTENT_VERIFIER,
        ReadinessStage.SOURCE_CONTENT_VALIDATION,
        "no explicit source content verifier for this provider",
        requirement_id=requirement_id,
    )


def _evaluate_quality(
    bundle: DataBundle,
    requirement_id: str,
    blockers: list[ReadinessBlocker],
    warnings: list[str],
    issue_codes: list[str],
) -> str:
    from market_validator.data.quality import QualitySeverity

    for issue in bundle.quality.issues:
        issue_codes.append(issue.code)
        if issue.severity == QualitySeverity.WARNING:
            warnings.append(issue.code)
    if bundle.quality.status == QualityStatus.FAIL:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.QUALITY_FAILED.value,
                stage=ReadinessStage.QUALITY_VALIDATION,
                message="quality FAIL blocks data readiness",
            )
        )
        return QualityStatus.FAIL.value
    if bundle.quality.status == QualityStatus.WARN:
        return QualityStatus.WARN.value
    return QualityStatus.PASS.value


def _resolve_schedule_adapter(
    requirement: DataRequirement,
    calendar_registry: CalendarRegistry,
    session_adapters: dict[str, VerifiedSessionScheduleAdapter],
    blockers: list[ReadinessBlocker],
) -> VerifiedSessionScheduleAdapter | None:
    calendar_id = requirement.calendar_id
    if calendar_id is None:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.SAMPLE_COVERAGE_UNVERIFIABLE.value,
                stage=ReadinessStage.SAMPLE_COVERAGE,
                message="requirement has no calendar identity",
            )
        )
        return None
    calendar = calendar_registry.get(calendar_id)
    if calendar is None or calendar.schedule_adapter is None:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.SAMPLE_COVERAGE_UNVERIFIABLE.value,
                stage=ReadinessStage.SAMPLE_COVERAGE,
                message="calendar has no verified schedule adapter",
            )
        )
        return None
    adapter = session_adapters.get(calendar.schedule_adapter)
    if adapter is None:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.SAMPLE_COVERAGE_UNVERIFIABLE.value,
                stage=ReadinessStage.SAMPLE_COVERAGE,
                message="no schedule adapter was injected for the calendar",
            )
        )
        return None
    return adapter


def _session_set_error(sessions: list[date], label: str) -> str | None:
    if len(sessions) != len(set(sessions)):
        return f"{label} contains duplicate sessions"
    if any(
        later <= earlier
        for earlier, later in zip(sessions, sessions[1:])
    ):
        return f"{label} is not strictly increasing"
    return None


def _assess_requirement(
    requirement: DataRequirement,
    request,
    bundle: DataBundle,
    bundle_sha256: str,
    snapshot_path: Path,
    record,
    calendar_registry: CalendarRegistry,
    session_adapters: dict[str, VerifiedSessionScheduleAdapter],
) -> RequirementReadinessAssessment:
    blockers: list[ReadinessBlocker] = []
    warnings: list[str] = []
    issue_codes: list[str] = []

    quality_status = _evaluate_quality(
        bundle, requirement.requirement_id, blockers, warnings, issue_codes
    )

    if requirement.frequency is not Frequency.ONE_DAY:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.UNSUPPORTED_FREQUENCY.value,
                stage=ReadinessStage.SAMPLE_COVERAGE,
                message="only daily frequency is supported for coverage",
            )
        )

    coverage_method = "unverified"
    sample_expected = 0
    sample_observed = 0
    sample_first_expected: date | None = None
    sample_last_expected: date | None = None
    sample_first_observed: date | None = None
    sample_last_observed: date | None = None
    adapter = _resolve_schedule_adapter(
        requirement, calendar_registry, session_adapters, blockers
    )
    if adapter is not None:
        try:
            expected_sample = adapter.sessions_between(
                requirement.start_date, requirement.end_date
            )
        except Exception:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.SAMPLE_COVERAGE_UNVERIFIABLE.value,
                    stage=ReadinessStage.SAMPLE_COVERAGE,
                    message="calendar session adapter failed",
                )
            )
        else:
            session_error = _session_set_error(
                expected_sample, "expected sample"
            )
            if session_error is not None:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode
                            .SAMPLE_COVERAGE_UNVERIFIABLE.value
                        ),
                        stage=ReadinessStage.SAMPLE_COVERAGE,
                        message=session_error,
                    )
                )
            observed_sample = sorted(
                {
                    observation.session_date
                    for observation in bundle.observations
                    if requirement.start_date
                    <= observation.session_date
                    <= requirement.end_date
                }
            )
            observed_error = _session_set_error(
                observed_sample, "observed sample"
            )
            if observed_error is not None:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode
                            .SAMPLE_COVERAGE_UNVERIFIABLE.value
                        ),
                        stage=ReadinessStage.SAMPLE_COVERAGE,
                        message=observed_error,
                    )
                )
            missing = [
                session
                for session in expected_sample
                if session not in set(observed_sample)
            ]
            extra = [
                session
                for session in observed_sample
                if session not in set(expected_sample)
            ]
            if missing:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode.SAMPLE_COVERAGE_INCOMPLETE.value
                        ),
                        stage=ReadinessStage.SAMPLE_COVERAGE,
                        message=(
                            "sample coverage is incomplete; expected sessions "
                            "are missing"
                        ),
                    )
                )
            if extra:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode
                            .SAMPLE_COVERAGE_EXTRA_OBSERVATIONS.value
                        ),
                        stage=ReadinessStage.SAMPLE_COVERAGE,
                        message=(
                            "sample contains observations outside the "
                            "expected session set"
                        ),
                    )
                )
            coverage_method = "verified_calendar_sessions"
            sample_expected = len(expected_sample)
            sample_observed = len(observed_sample)
            sample_first_expected = (
                expected_sample[0] if expected_sample else None
            )
            sample_last_expected = (
                expected_sample[-1] if expected_sample else None
            )
            sample_first_observed = (
                observed_sample[0] if observed_sample else None
            )
            sample_last_observed = (
                observed_sample[-1] if observed_sample else None
            )

    pre_sample_required = requirement.required_pre_sample_periods
    pre_sample_expected = 0
    pre_sample_observed = 0
    pre_sample_first_expected: date | None = None
    pre_sample_last_expected: date | None = None
    pre_sample_first_observed: date | None = None
    pre_sample_last_observed: date | None = None
    pre_sample_method = "none_required"
    pre_sample_observations = sorted(
        {
            observation.session_date
            for observation in bundle.observations
            if observation.session_date < requirement.start_date
        }
    )
    if pre_sample_required == 0:
        if pre_sample_observations:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_EXTRA_OBSERVATIONS.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message=(
                        "snapshot contains observations before the sample "
                        "start although no pre-sample was required"
                    ),
                )
            )
        pre_sample_observed = len(pre_sample_observations)
    else:
        if adapter is not None and request is not None:
            try:
                expected_pre_sample = adapter.previous_sessions(
                    requirement.start_date, pre_sample_required
                )
            except Exception:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode.PRE_SAMPLE_METHOD_UNVERIFIABLE
                            .value
                        ),
                        stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                        message="pre-sample session computation failed",
                    )
                )
            else:
                session_error = _session_set_error(
                    expected_pre_sample, "expected pre-sample"
                )
                if session_error is not None:
                    blockers.append(
                        ReadinessBlocker(
                            code=(
                                ReadinessErrorCode
                                .PRE_SAMPLE_METHOD_UNVERIFIABLE.value
                            ),
                            stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                            message=session_error,
                        )
                    )
                if len(expected_pre_sample) != pre_sample_required:
                    blockers.append(
                        ReadinessBlocker(
                            code=(
                                ReadinessErrorCode
                                .PRE_SAMPLE_METHOD_UNVERIFIABLE.value
                            ),
                            stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                            message=(
                                "calendar adapter returned the wrong number "
                                "of pre-sample sessions"
                            ),
                        )
                    )
                missing_pre = [
                    session
                    for session in expected_pre_sample
                    if session not in set(pre_sample_observations)
                ]
                extra_pre = [
                    session
                    for session in pre_sample_observations
                    if session not in set(expected_pre_sample)
                ]
                if missing_pre:
                    blockers.append(
                        ReadinessBlocker(
                            code=(
                                ReadinessErrorCode
                                .PRE_SAMPLE_MISSING_OBSERVATIONS.value
                            ),
                            stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                            message=(
                                "pre-sample observations are missing from "
                                "the bundle"
                            ),
                        )
                    )
                if extra_pre:
                    blockers.append(
                        ReadinessBlocker(
                            code=(
                                ReadinessErrorCode
                                .PRE_SAMPLE_EXTRA_OBSERVATIONS.value
                            ),
                            stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                            message=(
                                "pre-sample observations outside the "
                                "expected session set"
                            ),
                        )
                    )
                expected_first = (
                    expected_pre_sample[0] if expected_pre_sample else None
                )
                if request.acquisition_start != expected_first:
                    blockers.append(
                        ReadinessBlocker(
                            code=(
                                ReadinessErrorCode
                                .PRE_SAMPLE_REQUEST_WINDOW_MISMATCH.value
                            ),
                            stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                            message=(
                                "request acquisition start does not match "
                                "the first expected pre-sample session"
                            ),
                        )
                    )
                pre_sample_method = "verified_calendar_sessions"
                pre_sample_expected = len(expected_pre_sample)
                pre_sample_first_expected = expected_first
                pre_sample_last_expected = (
                    expected_pre_sample[-1] if expected_pre_sample else None
                )
                pre_sample_observed = len(pre_sample_observations)
                pre_sample_first_observed = (
                    pre_sample_observations[0]
                    if pre_sample_observations
                    else None
                )
                pre_sample_last_observed = (
                    pre_sample_observations[-1]
                    if pre_sample_observations
                    else None
                )
        elif request is not None and getattr(
            request, "pre_sample_resolution_method", None
        ) == "provider_native_previous_observations":
            pre_sample_method = "provider_native"
            pre_sample_observed = len(pre_sample_observations)
            if len(pre_sample_observations) != pre_sample_required:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode
                            .PRE_SAMPLE_MISSING_OBSERVATIONS.value
                        ),
                        stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                        message=(
                            "provider-native pre-sample count does not match "
                            "the required periods"
                        ),
                    )
                )
            pre_sample_first_observed = (
                pre_sample_observations[0]
                if pre_sample_observations
                else None
            )
            pre_sample_last_observed = (
                pre_sample_observations[-1]
                if pre_sample_observations
                else None
            )
        else:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_METHOD_UNVERIFIABLE.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message=(
                        "pre-sample completeness cannot be verified without "
                        "a calendar adapter or provider-native contract"
                    ),
                )
            )

    acquisition_start = (
        request.acquisition_start if request is not None else None
    )
    acquisition_end = (
        request.acquisition_end if request is not None else None
    )
    if request is not None:
        if request.sample_start != requirement.start_date:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_REQUEST_WINDOW_MISMATCH.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message="request sample start does not match the data plan",
                )
            )
        if request.sample_end != requirement.end_date:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_REQUEST_WINDOW_MISMATCH.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message="request sample end does not match the data plan",
                )
            )
        if request.acquisition_end != requirement.end_date:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_REQUEST_WINDOW_MISMATCH.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message="request acquisition end does not match the data plan",
                )
            )
        if pre_sample_required == 0 and request.acquisition_start != requirement.start_date:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.PRE_SAMPLE_REQUEST_WINDOW_MISMATCH.value,
                    stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                    message="request acquisition start must equal the sample start",
                )
            )
    window_start = acquisition_start or requirement.start_date
    window_end = acquisition_end or requirement.end_date
    out_of_window = [
        observation.session_date
        for observation in bundle.observations
        if not (window_start <= observation.session_date <= window_end)
    ]
    if out_of_window:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.PRE_SAMPLE_EXTRA_OBSERVATIONS.value,
                stage=ReadinessStage.PRE_SAMPLE_COVERAGE,
                message="bundle contains observations outside the acquisition window",
            )
        )

    availability_status = "verified"
    cutoff = requirement.information_cutoff
    if cutoff is not None:
        if cutoff.type is InformationCutoffType.SPECIFIED_LOCAL_TIME:
            if cutoff.local_time is None or cutoff.timezone is None:
                blockers.append(
                    ReadinessBlocker(
                        code=(
                            ReadinessErrorCode
                            .INFORMATION_CUTOFF_UNVERIFIABLE.value
                        ),
                        stage=ReadinessStage.AVAILABILITY_VALIDATION,
                        message="information cutoff is missing its local time",
                    )
                )
            else:
                for observation in bundle.observations:
                    cutoff_datetime = datetime.combine(
                        observation.session_date,
                        cutoff.local_time,
                        tzinfo=cutoff.timezone,
                    )
                    if observation.observation_time > cutoff_datetime:
                        blockers.append(
                            ReadinessBlocker(
                                code=(
                                    ReadinessErrorCode
                                    .INFORMATION_CUTOFF_VIOLATION.value
                                ),
                                stage=ReadinessStage.AVAILABILITY_VALIDATION,
                                message=(
                                    "observation is later than the "
                                    "information cutoff"
                                ),
                            )
                        )
                        break
                availability_status = "verified"
        else:
            blockers.append(
                ReadinessBlocker(
                    code=(
                        ReadinessErrorCode
                        .INFORMATION_CUTOFF_UNVERIFIABLE.value
                    ),
                    stage=ReadinessStage.AVAILABILITY_VALIDATION,
                    message=(
                        "cutoff semantics cannot be evaluated without "
                        "alignment"
                    ),
                )
            )
            availability_status = "unverifiable"
    for observation in bundle.observations:
        if observation.available_time is None or (
            observation.available_time.tzinfo is None
            or observation.available_time.utcoffset() is None
        ):
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.INFORMATION_CUTOFF_UNVERIFIABLE.value,
                    stage=ReadinessStage.AVAILABILITY_VALIDATION,
                    message="observation availability time is not timezone-aware",
                )
            )
            availability_status = "blocked"
            break

    revision_status = "verified"
    mode = requirement.revision_policy.mode
    for observation in bundle.observations:
        if observation.revision_policy is not mode:
            blockers.append(
                ReadinessBlocker(
                    code=ReadinessErrorCode.REVISION_POLICY_MISMATCH.value,
                    stage=ReadinessStage.REVISION_VALIDATION,
                    message="observation revision policy does not match the requirement",
                )
            )
            revision_status = "blocked"
            break
    if mode is DataRevisionMode.INITIAL_RELEASE:
        for observation in bundle.observations:
            if observation.vintage_date is None:
                blockers.append(
                    ReadinessBlocker(
                        code=ReadinessErrorCode.REVISION_POLICY_MISMATCH.value,
                        stage=ReadinessStage.REVISION_VALIDATION,
                        message="initial-release observation has no vintage date",
                    )
                )
                revision_status = "blocked"
                break
    if mode is DataRevisionMode.NOT_APPLICABLE:
        for observation in bundle.observations:
            if observation.vintage_date is not None:
                blockers.append(
                    ReadinessBlocker(
                        code=ReadinessErrorCode.REVISION_POLICY_MISMATCH.value,
                        stage=ReadinessStage.REVISION_VALIDATION,
                        message="not-applicable revision must not carry a vintage",
                    )
                )
                revision_status = "blocked"
                break
    if mode is DataRevisionMode.AS_OF_DATE:
        blockers.append(
            ReadinessBlocker(
                code=ReadinessErrorCode.REVISION_POLICY_MISMATCH.value,
                stage=ReadinessStage.REVISION_VALIDATION,
                message="as-of-date revision mode is not supported by readiness",
            )
        )
        revision_status = "blocked"

    status = (
        ReadinessStatus.BLOCKED if blockers else ReadinessStatus.READY
    )
    return RequirementReadinessAssessment(
        requirement_id=requirement.requirement_id,
        variable_id=requirement.variable_id,
        instrument_id=requirement.instrument_id,
        provider_id=bundle.source.provider_id,
        bundle_relative_path="bundle.json",
        bundle_sha256=bundle_sha256,
        source_content_sha256=bundle.source.content_sha256,
        quality_status=quality_status,
        quality_issue_codes=issue_codes,
        observation_count=len(bundle.observations),
        coverage_method=coverage_method,
        sample_expected_sessions=sample_expected,
        sample_observed_sessions=sample_observed,
        sample_first_expected_session=sample_first_expected,
        sample_last_expected_session=sample_last_expected,
        sample_first_observed_session=sample_first_observed,
        sample_last_observed_session=sample_last_observed,
        pre_sample_required_periods=pre_sample_required,
        pre_sample_expected_sessions=pre_sample_expected,
        pre_sample_observed_sessions=pre_sample_observed,
        pre_sample_first_expected_session=pre_sample_first_expected,
        pre_sample_last_expected_session=pre_sample_last_expected,
        pre_sample_first_observed_session=pre_sample_first_observed,
        pre_sample_last_observed_session=pre_sample_last_observed,
        availability_status=availability_status,
        revision_status=revision_status,
        status=status,
        blockers=blockers,
        warnings=sorted(set(warnings)),
    )


def assess_data_readiness(
    *,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    snapshot_path: str | Path,
    session_adapters: dict[str, VerifiedSessionScheduleAdapter],
    expected_authorization_sha256: str | None = None,
    expected_receipt_sha256: str | None = None,
) -> DataReadinessAssessment:
    """Run the fixed 15-step readiness pipeline over a verified snapshot.

    When the caller holds the DataAccessAuthorization and its consumption
    receipt, pass their SHA-256 values as external anchors; the snapshot
    verifier then cross-checks them instead of only re-reading the manifest.
    When omitted, the manifest's own hashes are re-read (self-consistency
    only) -- this is an explicit, documented limitation.
    """
    """Run the fixed 15-step readiness pipeline over a verified snapshot."""
    _validate_plan_and_bindings(
        generated_plan, data_plan, instrument_registry, calendar_registry
    )
    manifest_path = Path(snapshot_path) / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_VERIFICATION_FAILED,
            ReadinessStage.SNAPSHOT_RELOAD,
            "snapshot manifest is missing or not a regular file",
        )
    bound_manifest = parse_snapshot_manifest(manifest_path.read_bytes())
    try:
        verified = verify_acquisition_snapshot(
            snapshot_path=Path(snapshot_path),
            expected_plan_sha256=generated_plan.acquisition_request_plan_sha256,
            expected_authorization_sha256=(
                expected_authorization_sha256
                if expected_authorization_sha256 is not None
                else bound_manifest.authorization_sha256
            ),
            expected_receipt_sha256=(
                expected_receipt_sha256
                if expected_receipt_sha256 is not None
                else bound_manifest.authorization_receipt_sha256
            ),
            plan=generated_plan,
            data_plan=data_plan,
        )
    except SnapshotError as error:
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_VERIFICATION_FAILED,
            ReadinessStage.SNAPSHOT_RELOAD,
            error.safe_message,
        )
    if verified.outcome.status is not ExecutionStatus.SUCCEEDED:
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_OUTCOME_NOT_SUCCEEDED,
            ReadinessStage.SNAPSHOT_RELOAD,
            "snapshot outcome is not succeeded",
        )
    if (
        verified.manifest.acquisition_request_plan_sha256
        != generated_plan.acquisition_request_plan_sha256
    ):
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_BINDING_MISMATCH,
            ReadinessStage.SNAPSHOT_RELOAD,
            "snapshot plan hash does not match the generated plan",
        )
    expected_upstream = {
        "research_spec_sha256": generated_plan.research_spec_sha256,
        "data_plan_sha256": generated_plan.data_plan_sha256,
        "data_plan_confirmation_sha256": (
            generated_plan.data_plan_confirmation_sha256
        ),
        "source_selection_sha256": generated_plan.source_selection_sha256,
        "source_selection_confirmation_sha256": (
            generated_plan.source_selection_confirmation_sha256
        ),
        "instrument_registry_sha256": (
            generated_plan.instrument_registry_sha256
        ),
        "calendar_registry_sha256": generated_plan.calendar_registry_sha256,
    }
    manifest_upstream = {
        "research_spec_sha256": verified.manifest.research_spec_sha256,
        "data_plan_sha256": verified.manifest.data_plan_sha256,
        "data_plan_confirmation_sha256": (
            verified.manifest.data_plan_confirmation_sha256
        ),
        "source_selection_sha256": verified.manifest.source_selection_sha256,
        "source_selection_confirmation_sha256": (
            verified.manifest.source_selection_confirmation_sha256
        ),
        "instrument_registry_sha256": (
            verified.manifest.instrument_registry_sha256
        ),
        "calendar_registry_sha256": (
            verified.manifest.calendar_registry_sha256
        ),
    }
    if manifest_upstream != expected_upstream:
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_BINDING_MISMATCH,
            ReadinessStage.SNAPSHOT_RELOAD,
            "snapshot upstream hashes do not match the generated plan",
        )
    plan_request_ids = sorted(
        request.requirement_id
        for request in generated_plan.acquisition_request_plan.requests
    )
    snapshot_request_ids = sorted(
        record.requirement_id
        for record in verified.manifest.request_records
    )
    if plan_request_ids != snapshot_request_ids:
        fail_readiness(
            ReadinessErrorCode.SNAPSHOT_BINDING_MISMATCH,
            ReadinessStage.SNAPSHOT_RELOAD,
            "snapshot request ids do not exactly match the plan",
        )

    bundles = _load_bundle_artifacts(
        verified.snapshot_path,
        verified.manifest,
        data_plan,
        generated_plan.acquisition_request_plan.requests,
    )
    requirement_assessments: list[RequirementReadinessAssessment] = []
    aggregate_blockers: list[ReadinessBlocker] = []
    aggregate_warnings: list[str] = []
    for requirement in data_plan.requirements:
        request = _find_request(generated_plan, requirement.requirement_id)
        bundle_entry = bundles.get(requirement.requirement_id)
        if bundle_entry is None:
            fail_readiness(
                ReadinessErrorCode.MISSING_REQUIREMENT_BUNDLE,
                ReadinessStage.BUNDLE_VALIDATION,
                "data plan requirement has no bundle",
                requirement_id=requirement.requirement_id,
            )
        _, bundle_sha256, bundle = bundle_entry
        record = next(
            record
            for record in verified.manifest.request_records
            if record.requirement_id == requirement.requirement_id
        )
        recomputed = _source_content_verifier(
            bundle,
            verified.snapshot_path,
            record,
            requirement.requirement_id,
        )
        if recomputed != bundle.source.content_sha256:
            fail_readiness(
                ReadinessErrorCode.SOURCE_CONTENT_HASH_MISMATCH,
                ReadinessStage.SOURCE_CONTENT_VALIDATION,
                "recomputed source content hash does not match the bundle",
                requirement_id=requirement.requirement_id,
            )
        assessment = _assess_requirement(
            requirement,
            request,
            bundle,
            bundle_sha256,
            verified.snapshot_path,
            record,
            calendar_registry,
            session_adapters,
        )
        requirement_assessments.append(assessment)
        aggregate_blockers.extend(assessment.blockers)
        aggregate_warnings.extend(assessment.warnings)

    assessment = DataReadinessAssessment(
        assessment_id="pending",
        status=(
            ReadinessStatus.BLOCKED
            if aggregate_blockers
            else ReadinessStatus.READY
        ),
        snapshot_id=verified.manifest.snapshot_id,
        snapshot_manifest_sha256=verified.manifest_sha256,
        acquisition_request_plan_sha256=(
            generated_plan.acquisition_request_plan_sha256
        ),
        research_spec_sha256=generated_plan.research_spec_sha256,
        data_plan_sha256=generated_plan.data_plan_sha256,
        data_plan_confirmation_sha256=(
            generated_plan.data_plan_confirmation_sha256
        ),
        source_selection_sha256=generated_plan.source_selection_sha256,
        source_selection_confirmation_sha256=(
            generated_plan.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=(
            generated_plan.instrument_registry_sha256
        ),
        calendar_registry_sha256=generated_plan.calendar_registry_sha256,
        requirements=requirement_assessments,
        blockers=aggregate_blockers,
        warnings=aggregate_warnings,
    )
    assessment_id = _derive_assessment_id(
        assessment.snapshot_manifest_sha256,
        assessment.acquisition_request_plan_sha256,
        assessment.data_plan_sha256,
    )
    return assessment.model_copy(update={"assessment_id": assessment_id})


def data_readiness_blockers(
    assessment: DataReadinessAssessment,
) -> list[str]:
    """Return sorted, deduplicated blocker codes for a readiness assessment."""
    return sorted(
        {blocker.code for blocker in assessment.blockers}
    )


def create_data_ready_manifest(
    *,
    assessment: DataReadinessAssessment,
    generated_plan: GeneratedAcquisitionRequestPlan,
) -> GeneratedDataReadyManifest:
    """Create a DataReadyManifest only from a ready assessment."""
    restored = parse_data_readiness_assessment(
        serialize_data_readiness_assessment(assessment)
    )
    if restored != assessment:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.MANIFEST_GENERATION,
            "assessment does not match its canonical bytes",
        )
    assessment_sha256 = calculate_data_readiness_assessment_sha256(
        assessment
    )
    if assessment.status is not ReadinessStatus.READY:
        fail_readiness(
            ReadinessErrorCode.DATA_READINESS_NOT_READY,
            ReadinessStage.MANIFEST_GENERATION,
            "blocked assessment cannot produce a data ready manifest",
        )
    for item in assessment.requirements:
        if item.status is not ReadinessStatus.READY or item.blockers:
            fail_readiness(
                ReadinessErrorCode.DATA_READINESS_NOT_READY,
                ReadinessStage.MANIFEST_GENERATION,
                "assessment requirement is not ready",
                requirement_id=item.requirement_id,
            )
    expected_id = _derive_assessment_id(
        assessment.snapshot_manifest_sha256,
        assessment.acquisition_request_plan_sha256,
        assessment.data_plan_sha256,
    )
    if assessment.assessment_id != expected_id:
        fail_readiness(
            ReadinessErrorCode.INVALID_DATA_READINESS_INPUT,
            ReadinessStage.MANIFEST_GENERATION,
            "assessment id is not self-consistent",
        )
    bundle_records = [
        DataReadyBundleRecord(
            requirement_id=item.requirement_id,
            variable_id=item.variable_id,
            instrument_id=item.instrument_id,
            provider_id=item.provider_id,
            bundle_relative_path=item.bundle_relative_path,
            bundle_sha256=item.bundle_sha256,
            source_content_sha256=item.source_content_sha256,
            quality_status=item.quality_status,
            quality_issue_codes=list(item.quality_issue_codes),
            observation_count=item.observation_count,
            sample_session_count=item.sample_observed_sessions,
            pre_sample_session_count=item.pre_sample_observed_sessions,
            warnings=list(item.warnings),
        )
        for item in assessment.requirements
    ]
    manifest = DataReadyManifest(
        data_ready_id="pending",
        readiness_assessment_sha256=assessment_sha256,
        snapshot_id=assessment.snapshot_id,
        snapshot_manifest_sha256=assessment.snapshot_manifest_sha256,
        acquisition_request_plan_sha256=(
            assessment.acquisition_request_plan_sha256
        ),
        research_spec_sha256=assessment.research_spec_sha256,
        data_plan_sha256=assessment.data_plan_sha256,
        data_plan_confirmation_sha256=(
            assessment.data_plan_confirmation_sha256
        ),
        source_selection_sha256=assessment.source_selection_sha256,
        source_selection_confirmation_sha256=(
            assessment.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=assessment.instrument_registry_sha256,
        calendar_registry_sha256=assessment.calendar_registry_sha256,
        bundles=bundle_records,
        warnings=assessment.warnings,
    )
    data_ready_id = _derive_data_ready_id(
        assessment_sha256,
        manifest.snapshot_manifest_sha256,
        manifest.acquisition_request_plan_sha256,
    )
    manifest = manifest.model_copy(update={"data_ready_id": data_ready_id})
    manifest_sha256 = calculate_data_ready_manifest_sha256(manifest)
    return GeneratedDataReadyManifest(
        data_ready_manifest=manifest,
        data_ready_manifest_sha256=manifest_sha256,
        readiness_assessment_sha256=assessment_sha256,
        snapshot_manifest_sha256=manifest.snapshot_manifest_sha256,
        data_plan_sha256=manifest.data_plan_sha256,
        acquisition_request_plan_sha256=(
            manifest.acquisition_request_plan_sha256
        ),
        research_spec_sha256=manifest.research_spec_sha256,
        data_plan_confirmation_sha256=(
            manifest.data_plan_confirmation_sha256
        ),
        source_selection_sha256=manifest.source_selection_sha256,
        source_selection_confirmation_sha256=(
            manifest.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=manifest.instrument_registry_sha256,
        calendar_registry_sha256=manifest.calendar_registry_sha256,
    )


def validate_data_ready_manifest_matches(
    generated_manifest: GeneratedDataReadyManifest,
    assessment: DataReadinessAssessment,
    generated_plan: GeneratedAcquisitionRequestPlan,
    data_plan: DataPlan,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> None:
    """Re-check every binding of a DataReadyManifest before trusting it."""
    manifest = generated_manifest.data_ready_manifest
    if (
        calculate_data_ready_manifest_sha256(manifest)
        != generated_manifest.data_ready_manifest_sha256
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest does not match its bound hash",
        )
    if (
        calculate_data_readiness_assessment_sha256(assessment)
        != manifest.readiness_assessment_sha256
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest does not bind the readiness assessment",
        )
    if (
        manifest.snapshot_manifest_sha256
        != assessment.snapshot_manifest_sha256
        or manifest.snapshot_id != assessment.snapshot_id
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest does not bind the snapshot",
        )
    if (
        manifest.acquisition_request_plan_sha256
        != generated_plan.acquisition_request_plan_sha256
        or manifest.data_plan_sha256
        != generated_plan.data_plan_sha256
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest plan bindings do not match",
        )
    if manifest.data_plan_sha256 != assessment.data_plan_sha256:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest data plan binding does not match the assessment",
        )
    for field_name in (
        "research_spec_sha256",
        "data_plan_confirmation_sha256",
        "source_selection_sha256",
        "source_selection_confirmation_sha256",
        "instrument_registry_sha256",
        "calendar_registry_sha256",
    ):
        if getattr(manifest, field_name) != getattr(
            generated_plan, field_name
        ):
            fail_readiness(
                ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
                ReadinessStage.MANIFEST_VERIFICATION,
                f"manifest {field_name} does not match the plan",
            )
        if getattr(manifest, field_name) != getattr(
            assessment, field_name
        ):
            fail_readiness(
                ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
                ReadinessStage.MANIFEST_VERIFICATION,
                f"manifest {field_name} does not match the assessment",
            )
    if manifest.warnings != assessment.warnings:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest warnings do not match the assessment",
        )
    manifest_ids = [record.requirement_id for record in manifest.bundles]
    assessment_ids = [
        item.requirement_id for item in assessment.requirements
    ]
    if manifest_ids != assessment_ids:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "manifest requirement set does not match the assessment",
        )
    for record in manifest.bundles:
        item = next(
            item
            for item in assessment.requirements
            if item.requirement_id == record.requirement_id
        )
        expected = {
            "variable_id": item.variable_id,
            "instrument_id": item.instrument_id,
            "provider_id": item.provider_id,
            "bundle_relative_path": item.bundle_relative_path,
            "bundle_sha256": item.bundle_sha256,
            "source_content_sha256": item.source_content_sha256,
            "quality_status": item.quality_status,
            "quality_issue_codes": item.quality_issue_codes,
            "observation_count": item.observation_count,
            "sample_session_count": item.sample_observed_sessions,
            "pre_sample_session_count": item.pre_sample_observed_sessions,
            "warnings": item.warnings,
        }
        actual = {
            "variable_id": record.variable_id,
            "instrument_id": record.instrument_id,
            "provider_id": record.provider_id,
            "bundle_relative_path": record.bundle_relative_path,
            "bundle_sha256": record.bundle_sha256,
            "source_content_sha256": record.source_content_sha256,
            "quality_status": record.quality_status,
            "quality_issue_codes": record.quality_issue_codes,
            "observation_count": record.observation_count,
            "sample_session_count": record.sample_session_count,
            "pre_sample_session_count": record.pre_sample_session_count,
            "warnings": record.warnings,
        }
        if actual != expected:
            fail_readiness(
                ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
                ReadinessStage.MANIFEST_VERIFICATION,
                "manifest bundle record does not match the assessment",
                requirement_id=record.requirement_id,
            )


def persist_data_readiness_assessment(
    assessment: DataReadinessAssessment,
    output_path: str | Path,
) -> Path:
    """Persist an assessment create-only with a provenance sidecar."""
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    payload = serialize_data_readiness_assessment(assessment)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_readiness(
                ReadinessErrorCode.DATA_READY_OUTPUT_CONFLICT,
                ReadinessStage.MANIFEST_OUTPUT,
                "assessment output path already exists with different content",
            )
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "assessment could not be persisted",
        )
    try:
        restored = parse_data_readiness_assessment(persisted.read_bytes())
    except Exception:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "persisted assessment failed reload validation",
        )
    if restored != assessment:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "persisted assessment does not match the canonical bytes",
        )
    return persisted


def persist_data_ready_manifest(
    generated: GeneratedDataReadyManifest,
    output_path: str | Path,
    *,
    generated_at: datetime | None = None,
    snapshot_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Persist a DataReadyManifest and its provenance sidecar (create-only)."""
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    payload = serialize_data_ready_manifest(generated.data_ready_manifest)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_readiness(
                ReadinessErrorCode.DATA_READY_OUTPUT_CONFLICT,
                ReadinessStage.MANIFEST_OUTPUT,
                "manifest output path already exists with different content",
            )
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "manifest could not be persisted",
        )
    try:
        restored = parse_data_ready_manifest(persisted.read_bytes())
    except Exception:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "persisted manifest failed reload validation",
        )
    if restored != generated.data_ready_manifest:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "persisted manifest does not match the canonical bytes",
        )
    provenance = DataReadyProvenance(
        generated_at=generated_at or datetime.now(timezone.utc),
        snapshot_path=Path(snapshot_path) if snapshot_path else None,
    )
    provenance_payload = _canonical_bytes(provenance)
    provenance_path = path.with_suffix(path.suffix + ".provenance.json")
    try:
        persist_immutable_bytes(provenance_payload, provenance_path)
    except Exception:
        try:
            persisted.unlink()
        except OSError:
            pass
        fail_readiness(
            ReadinessErrorCode.DATA_READY_OUTPUT_ERROR,
            ReadinessStage.MANIFEST_OUTPUT,
            "manifest provenance sidecar could not be persisted",
        )
    return persisted, provenance_path


def verify_persisted_data_ready_manifest(
    output_path: str | Path,
    expected_manifest: GeneratedDataReadyManifest,
) -> Path:
    """Re-read a persisted manifest and verify every binding."""
    path = Path(output_path)
    if not path.is_file() or path.is_symlink():
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "persisted manifest is missing or not a regular file",
        )
    restored = parse_data_ready_manifest(path.read_bytes())
    if restored != expected_manifest.data_ready_manifest:
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "persisted manifest does not match the generated manifest",
        )
    if (
        calculate_data_ready_manifest_sha256(restored)
        != expected_manifest.data_ready_manifest_sha256
    ):
        fail_readiness(
            ReadinessErrorCode.DATA_READY_MANIFEST_MISMATCH,
            ReadinessStage.MANIFEST_VERIFICATION,
            "persisted manifest hash does not match",
        )
    return path


__all__ = [
    "DATA_READY_SCHEMA_VERSION",
    "DataReadyBundleRecord",
    "DataReadyManifest",
    "DataReadyProvenance",
    "DataReadinessAssessment",
    "GeneratedDataReadyManifest",
    "READINESS_SCHEMA_VERSION",
    "ReadinessBlocker",
    "ReadinessError",
    "ReadinessErrorCode",
    "ReadinessStage",
    "ReadinessStatus",
    "RequirementReadinessAssessment",
    "VerifiedSessionScheduleAdapter",
    "assess_data_readiness",
    "calculate_data_ready_manifest_sha256",
    "calculate_data_readiness_assessment_sha256",
    "create_data_ready_manifest",
    "data_readiness_blockers",
    "fail_readiness",
    "parse_data_ready_manifest",
    "parse_data_readiness_assessment",
    "persist_data_ready_manifest",
    "persist_data_readiness_assessment",
    "serialize_data_ready_manifest",
    "serialize_data_readiness_assessment",
    "validate_data_ready_manifest_matches",
    "verify_persisted_data_ready_manifest",
]
