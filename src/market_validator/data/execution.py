"""Authorized provider execution coordinator and execution adapters.

The coordinator strictly validates the plan/authorization/registries,
consumes the single-use authorization (persisting the receipt first),
executes each authorized request through its explicit adapter, captures raw
bytes and traces, builds DataBundles, then commits the whole result as one
transactional snapshot. It never retries, never falls back, never touches
credentials interactively, and never produces a partial final snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import stat
from typing import NoReturn, Protocol

from market_validator.data.acquisition_request import (
    AccessMode,
    AcquisitionRequestReviewError,
    GeneratedAcquisitionRequestPlan,
    ProviderCapabilitySnapshot,
    PublicAcquisitionRequest,
    RequestMethod,
    calculate_acquisition_request_plan_sha256,
    parse_acquisition_request_plan,
    serialize_acquisition_request_plan,
)
from market_validator.data.access_authorization import (
    DataAccessAuthorization,
    DataAccessAuthorizationError,
    DataAccessAuthorizationReceipt,
    calculate_data_access_authorization_receipt_sha256,
    calculate_data_access_authorization_sha256,
    parse_data_access_authorization,
    serialize_data_access_authorization,
)
from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import (
    DataBundle,
    DataRequirement,
    DataSourceMetadata,
    Identifier,
    NonEmptyString,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.snapshot import (
    AcquisitionExecutionOutcome,
    ExecutionFailure,
    ExecutionStatus,
    SnapshotError,
    VerifiedAcquisitionSnapshot,
    commit_acquisition_snapshot,
    parse_execution_outcome,
    parse_snapshot_manifest,
    serialize_execution_outcome,
)
from market_validator.data.storage import resolve_data_directory
from market_validator.credentials.models import CredentialSpec
from market_validator.credentials.resolver import CredentialResolver
from market_validator.research.models import StrictResearchModel

EXECUTION_SCHEMA_VERSION = "1.0"
MAX_LOCAL_FILE_BYTES = 32 * 1024 * 1024


class ExecutionErrorCode(StrEnum):
    INVALID_EXECUTION_INPUT = "invalid_execution_input"
    AUTHORIZATION_NOT_CONSUMED = "authorization_not_consumed"
    AUTHORIZATION_ALREADY_CONSUMED = "authorization_already_consumed"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    REQUEST_CONTRACT_MISMATCH = "request_contract_mismatch"
    REQUEST_STEP_NOT_AUTHORIZED = "request_step_not_authorized"
    UNEXPECTED_PROVIDER_REQUEST = "unexpected_provider_request"
    CREDENTIAL_NOT_CONFIGURED = "credential_not_configured"
    INTERACTIVE_CREDENTIAL_FORBIDDEN = "interactive_credential_forbidden"
    NETWORK_NOT_AUTHORIZED = "network_not_authorized"
    LOCAL_FILE_READ_NOT_AUTHORIZED = "local_file_read_not_authorized"
    UNSAFE_LOCAL_FILE = "unsafe_local_file"
    LOCAL_FILE_CHANGED_DURING_READ = "local_file_changed_during_read"
    PROVIDER_EXECUTION_FAILED = "provider_execution_failed"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"
    AUTOMATIC_RETRY_FORBIDDEN = "automatic_retry_forbidden"
    FALLBACK_FORBIDDEN = "fallback_forbidden"
    SNAPSHOT_OUTPUT_CONFLICT = "snapshot_output_conflict"
    SNAPSHOT_STAGE_WRITE_FAILED = "snapshot_stage_write_failed"
    SNAPSHOT_STAGE_VERIFICATION_FAILED = (
        "snapshot_stage_verification_failed"
    )
    SNAPSHOT_COMMIT_FAILED = "snapshot_commit_failed"
    SNAPSHOT_FINAL_VERIFICATION_FAILED = (
        "snapshot_final_verification_failed"
    )


class ExecutionStage(StrEnum):
    EXECUTION_VALIDATION = "execution_validation"
    AUTHORIZATION_CONSUMPTION = "authorization_consumption"
    CREDENTIAL_RESOLUTION = "credential_resolution"
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_RESPONSE = "provider_response"
    SNAPSHOT_STAGING = "snapshot_staging"
    SNAPSHOT_VERIFICATION = "snapshot_verification"
    SNAPSHOT_COMMIT = "snapshot_commit"
    EXECUTION_OUTCOME = "execution_outcome"


class ExecutionError(ValueError):
    """Safe structured failure; messages never embed raw data or secrets."""

    def __init__(
        self,
        code: ExecutionErrorCode,
        stage: ExecutionStage,
        message: str,
        *,
        provider_id: str | None = None,
        requirement_id: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.safe_message = message
        self.provider_id = provider_id
        self.requirement_id = requirement_id
        super().__init__(f"{code.value}: {message}")


def fail_execution(
    code: ExecutionErrorCode,
    stage: ExecutionStage,
    message: str,
    *,
    provider_id: str | None = None,
    requirement_id: str | None = None,
) -> NoReturn:
    raise ExecutionError(
        code,
        stage,
        message,
        provider_id=provider_id,
        requirement_id=requirement_id,
    )


@dataclass(frozen=True, slots=True)
class RequestTraceEntry:
    sequence: int
    endpoint: str
    public_parameters: dict[str, str]
    response_sha256: str
    response_byte_size: int
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class RawArtifact:
    logical_name: str
    media_type: str
    content: bytes


@dataclass(slots=True)
class ProviderExecutionCapture:
    """Internal capture; never persisted directly as a public artifact."""

    requirement_id: str
    provider_id: str
    started_at: datetime
    completed_at: datetime
    request_trace: list[RequestTraceEntry] = field(default_factory=list)
    raw_artifacts: list[RawArtifact] = field(default_factory=list)
    bundle: DataBundle | None = None


class ProviderExecutionAdapter(Protocol):
    """Executes one authorized request and returns an in-memory capture."""

    def execute_capture(
        self,
        request: PublicAcquisitionRequest,
        requirement: DataRequirement,
    ) -> ProviderExecutionCapture:
        ...


class _SafePathValidator:
    """Pure-path safety checks for local-file reads (no content reads)."""

    @staticmethod
    def is_safe_absolute_identity(value: str) -> bool:
        if value.startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(value)
            if parsed.scheme != "file" or not parsed.path:
                return False
            return _SafePathValidator.is_safe_absolute_identity(
                unquote(parsed.path)
            )
        path = Path(value)
        if path.is_absolute():
            return ".." not in path.parts
        import re

        if re.match(r"^[A-Za-z]:[\\/]", value):
            return ".." not in path.parts
        return False


def _resolve_file_identity(value: str) -> Path:
    if value.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(value)
        if parsed.scheme != "file" or not parsed.path:
            fail_execution(
                ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                ExecutionStage.PROVIDER_REQUEST,
                "local file URI is not a normalized file URI",
            )
        return Path(unquote(parsed.path))
    return Path(value)


def _check_no_symlink_components(path: Path) -> None:
    current = path
    parts: list[Path] = []
    while current != current.parent:
        parts.append(current)
        current = current.parent
    for component in reversed(parts):
        try:
            if component.is_symlink():
                fail_execution(
                    ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                    ExecutionStage.PROVIDER_REQUEST,
                    "local file path contains a symlink component",
                )
        except OSError:
            fail_execution(
                ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                ExecutionStage.PROVIDER_REQUEST,
                "local file path could not be inspected safely",
            )


def _check_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        fail_execution(
            ExecutionErrorCode.UNSAFE_LOCAL_FILE,
            ExecutionStage.PROVIDER_REQUEST,
            "local file does not exist or cannot be inspected",
        )
    if not stat.S_ISREG(mode):
        fail_execution(
            ExecutionErrorCode.UNSAFE_LOCAL_FILE,
            ExecutionStage.PROVIDER_REQUEST,
            "local file is not a regular file",
        )


class LocalFileExecutionAdapter:
    """Executes one authorized local-file read with strict safety checks."""

    def __init__(self, csv_provider: object) -> None:
        self._csv_provider = csv_provider

    def execute_capture(
        self,
        request: PublicAcquisitionRequest,
        requirement: DataRequirement,
    ) -> ProviderExecutionCapture:
        if request.access_mode is not AccessMode.LOCAL_FILE:
            fail_execution(
                ExecutionErrorCode.REQUEST_CONTRACT_MISMATCH,
                ExecutionStage.PROVIDER_REQUEST,
                "local-file adapter requires a local-file request",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        source_uri = request.public_parameters.get("source_uri")
        if not source_uri or source_uri != request.provider_symbol:
            fail_execution(
                ExecutionErrorCode.REQUEST_CONTRACT_MISMATCH,
                ExecutionStage.PROVIDER_REQUEST,
                "local file source identity does not match the plan",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        if not _SafePathValidator.is_safe_absolute_identity(source_uri):
            fail_execution(
                ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                ExecutionStage.PROVIDER_REQUEST,
                "local file path is not a safe absolute identity",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        if any(
            step.pagination_policy.value != "none"
            for step in request.steps
        ):
            fail_execution(
                ExecutionErrorCode.REQUEST_STEP_NOT_AUTHORIZED,
                ExecutionStage.PROVIDER_REQUEST,
                "local file steps must not declare pagination",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        path = _resolve_file_identity(source_uri)
        _check_no_symlink_components(path)
        _check_regular_file(path)
        try:
            if path.lstat().st_size > MAX_LOCAL_FILE_BYTES:
                fail_execution(
                    ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                    ExecutionStage.PROVIDER_REQUEST,
                    "local file exceeds the safe byte size limit",
                    provider_id=request.provider_id,
                    requirement_id=request.requirement_id,
                )
        except OSError:
            fail_execution(
                ExecutionErrorCode.UNSAFE_LOCAL_FILE,
                ExecutionStage.PROVIDER_REQUEST,
                "local file could not be inspected safely",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        started_at = datetime.now(timezone.utc)
        before = path.lstat()
        try:
            content = path.read_bytes()
        except OSError:
            fail_execution(
                ExecutionErrorCode.PROVIDER_EXECUTION_FAILED,
                ExecutionStage.PROVIDER_REQUEST,
                "local file could not be read",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        after = path.lstat()
        if not stat.S_ISREG(after.st_mode) or not stat.S_ISREG(before.st_mode):
            fail_execution(
                ExecutionErrorCode.LOCAL_FILE_CHANGED_DURING_READ,
                ExecutionStage.PROVIDER_RESPONSE,
                "local file is no longer a regular file while being read",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            fail_execution(
                ExecutionErrorCode.LOCAL_FILE_CHANGED_DURING_READ,
                ExecutionStage.PROVIDER_RESPONSE,
                "local file changed while it was being read",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        content_sha256 = hashlib.sha256(content).hexdigest()
        completed_at = datetime.now(timezone.utc)
        parse_result = self._csv_provider.parse_content(
            content, requirement, path, content_sha256
        )
        bundle = parse_result
        return ProviderExecutionCapture(
            requirement_id=request.requirement_id,
            provider_id=request.provider_id,
            started_at=started_at,
            completed_at=completed_at,
            request_trace=[
                RequestTraceEntry(
                    sequence=1,
                    endpoint=str(path),
                    public_parameters={"source_uri": source_uri},
                    response_sha256=content_sha256,
                    response_byte_size=len(content),
                )
            ],
            raw_artifacts=[
                RawArtifact(
                    logical_name="source.csv",
                    media_type="text/csv",
                    content=content,
                )
            ],
            bundle=bundle,
        )


class FredExecutionAdapter:
    """Executes an authorized FRED request through the existing provider."""

    def __init__(self, provider: object) -> None:
        self._provider = provider

    def execute_capture(
        self,
        request: PublicAcquisitionRequest,
        requirement: DataRequirement,
    ) -> ProviderExecutionCapture:
        if request.access_mode is not AccessMode.NETWORK:
            fail_execution(
                ExecutionErrorCode.REQUEST_CONTRACT_MISMATCH,
                ExecutionStage.PROVIDER_REQUEST,
                "FRED adapter requires a network request",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        provider = self._provider
        expected_steps = provider.render_public_steps(
            requirement, request.provider_symbol
        )
        _compare_steps(request, expected_steps)
        credential = provider.resolve_credential_noninteractive()
        if credential is None:
            fail_execution(
                ExecutionErrorCode.CREDENTIAL_NOT_CONFIGURED,
                ExecutionStage.CREDENTIAL_RESOLUTION,
                "FRED credential is not configured",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )
        return provider.execute_capture(requirement, request)


def _compare_steps(
    request: PublicAcquisitionRequest, expected_steps: list[object]
) -> None:
    if len(request.steps) != len(expected_steps):
        fail_execution(
            ExecutionErrorCode.REQUEST_CONTRACT_MISMATCH,
            ExecutionStage.PROVIDER_REQUEST,
            "authorized request steps do not match the provider contract",
            provider_id=request.provider_id,
            requirement_id=request.requirement_id,
        )
    for authorized, expected in zip(request.steps, expected_steps):
        if isinstance(expected, dict):
            expected_dict = {
                "sequence": expected.get("sequence"),
                "method": expected.get("method"),
                "endpoint": expected.get("endpoint"),
                "public_parameters": expected.get("public_parameters"),
                "pagination_policy": expected.get("pagination_policy"),
            }
        else:
            expected_dict = {
                "sequence": expected.sequence,
                "method": expected.method,
                "endpoint": expected.endpoint,
                "public_parameters": expected.public_parameters,
                "pagination_policy": expected.pagination_policy,
            }
        actual_dict = {
            "sequence": authorized.sequence,
            "method": authorized.method,
            "endpoint": authorized.endpoint,
            "public_parameters": authorized.public_parameters,
            "pagination_policy": authorized.pagination_policy,
        }
        if actual_dict != expected_dict:
            fail_execution(
                ExecutionErrorCode.REQUEST_CONTRACT_MISMATCH,
                ExecutionStage.PROVIDER_REQUEST,
                "authorized request step differs from the provider contract",
                provider_id=request.provider_id,
                requirement_id=request.requirement_id,
            )


def _reparse_plan(
    generated_plan: GeneratedAcquisitionRequestPlan,
) -> GeneratedAcquisitionRequestPlan:
    try:
        restored = parse_acquisition_request_plan(
            serialize_acquisition_request_plan(
                generated_plan.acquisition_request_plan
            )
        )
    except AcquisitionRequestReviewError:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "plan failed strict validation",
        )
    if restored != generated_plan.acquisition_request_plan:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "plan does not match its canonical bytes",
        )
    if calculate_acquisition_request_plan_sha256(
        restored
    ) != generated_plan.acquisition_request_plan_sha256:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "plan does not match its bound hash",
        )
    return generated_plan


def _reparse_authorization(
    authorization: DataAccessAuthorization,
) -> DataAccessAuthorization:
    try:
        restored = parse_data_access_authorization(
            serialize_data_access_authorization(authorization)
        )
    except DataAccessAuthorizationError:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "authorization failed strict validation",
        )
    if restored != authorization:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "authorization does not match its canonical bytes",
        )
    return restored


def _validate_authorization_against_plan(
    generated_plan: GeneratedAcquisitionRequestPlan,
    authorization: DataAccessAuthorization,
) -> None:
    from market_validator.data.access_authorization import (
        validate_data_access_authorization_matches,
    )

    try:
        validate_data_access_authorization_matches(
            generated_plan, authorization
        )
    except DataAccessAuthorizationError:
        fail_execution(
            ExecutionErrorCode.AUTHORIZATION_MISMATCH,
            ExecutionStage.EXECUTION_VALIDATION,
            "authorization does not match the plan",
        )
    plan_request_ids = sorted(
        request.requirement_id
        for request in generated_plan.acquisition_request_plan.requests
    )
    if sorted(authorization.authorized_request_ids) != plan_request_ids:
        fail_execution(
            ExecutionErrorCode.AUTHORIZATION_MISMATCH,
            ExecutionStage.EXECUTION_VALIDATION,
            "authorized request ids do not exactly match the plan",
        )
    if authorization.paid_access_authorized or (
        authorization.automatic_retry_authorized
        or authorization.fallback_authorized
    ):
        fail_execution(
            ExecutionErrorCode.AUTHORIZATION_MISMATCH,
            ExecutionStage.EXECUTION_VALIDATION,
            "authorization must forbid paid access, retry and fallback",
        )
    has_network = any(
        request.access_mode is AccessMode.NETWORK
        for request in generated_plan.acquisition_request_plan.requests
    )
    has_local = any(
        request.access_mode is AccessMode.LOCAL_FILE
        for request in generated_plan.acquisition_request_plan.requests
    )
    if has_network and not authorization.network_access_authorized:
        fail_execution(
            ExecutionErrorCode.NETWORK_NOT_AUTHORIZED,
            ExecutionStage.EXECUTION_VALIDATION,
            "network access is not authorized",
        )
    if has_local and not authorization.local_file_read_authorized:
        fail_execution(
            ExecutionErrorCode.LOCAL_FILE_READ_NOT_AUTHORIZED,
            ExecutionStage.EXECUTION_VALIDATION,
            "local file read access is not authorized",
        )


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def execute_authorized_acquisition(
    *,
    generated_plan: GeneratedAcquisitionRequestPlan,
    authorization: DataAccessAuthorization,
    data_plan: object,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
    capability_snapshots: dict[str, ProviderCapabilitySnapshot],
    attempt_id: str,
    receipt_path: str | Path,
    snapshot_root: str | Path,
    adapters: dict[str, ProviderExecutionAdapter],
    clock: object | None = None,
) -> VerifiedAcquisitionSnapshot:
    """Execute one authorized acquisition as a whole-snapshot transaction."""
    from market_validator.data.acquisition_request import (
        acquisition_request_readiness_blockers,
    )
    from market_validator.data.access_authorization import (
        consume_data_access_authorization,
        persist_data_access_authorization_receipt,
    )
    from market_validator.data.models import DataPlan

    started_at = datetime.now(timezone.utc)
    generated = _reparse_plan(generated_plan)
    restored_authorization = _reparse_authorization(authorization)
    if not isinstance(data_plan, DataPlan):
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "data plan must be a strictly validated model",
        )
    _validate_authorization_against_plan(generated, restored_authorization)
    blockers = acquisition_request_readiness_blockers(
        generated,
        instrument_registry,
        calendar_registry,
        capability_snapshots,
    )
    if blockers:
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.EXECUTION_VALIDATION,
            "plan is not ready: " + "; ".join(blockers),
        )
    authorization_sha256 = calculate_data_access_authorization_sha256(
        restored_authorization
    )
    existing_receipt = _load_existing_receipt(receipt_path)
    try:
        receipt = consume_data_access_authorization(
            restored_authorization,
            attempt_id=attempt_id,
            consumed_at=started_at,
            existing_receipt=existing_receipt,
        )
    except DataAccessAuthorizationError as error:
        code = (
            ExecutionErrorCode.AUTHORIZATION_ALREADY_CONSUMED
            if "already consumed" in error.failure.message
            else ExecutionErrorCode.AUTHORIZATION_MISMATCH
        )
        fail_execution(
            code,
            ExecutionStage.AUTHORIZATION_CONSUMPTION,
            error.failure.message,
        )
    try:
        persist_data_access_authorization_receipt(receipt, receipt_path)
    except DataAccessAuthorizationError:
        fail_execution(
            ExecutionErrorCode.AUTHORIZATION_NOT_CONSUMED,
            ExecutionStage.AUTHORIZATION_CONSUMPTION,
            "authorization receipt could not be persisted; no provider "
            "request was executed",
        )
    receipt_sha256 = calculate_data_access_authorization_receipt_sha256(
        receipt
    )

    captures: dict[str, ProviderExecutionCapture] = {}
    executed_ids: list[str] = []
    request_records: list[dict[str, object]] = []
    artifact_contents: dict[tuple[str, str], bytes] = {}
    try:
        for request in generated.acquisition_request_plan.requests:
            requirement = _find_requirement(data_plan, request.requirement_id)
            if requirement is None:
                fail_execution(
                    ExecutionErrorCode.INVALID_EXECUTION_INPUT,
                    ExecutionStage.EXECUTION_VALIDATION,
                    "plan request has no matching data plan requirement",
                )
            adapter = adapters.get(request.provider_id)
            if adapter is None:
                fail_execution(
                    ExecutionErrorCode.INVALID_EXECUTION_INPUT,
                    ExecutionStage.EXECUTION_VALIDATION,
                    f"no execution adapter for provider {request.provider_id}",
                )
            try:
                capture = adapter.execute_capture(request, requirement)
            except ExecutionError:
                raise
            except Exception:
                fail_execution(
                    ExecutionErrorCode.PROVIDER_EXECUTION_FAILED,
                    ExecutionStage.PROVIDER_REQUEST,
                    "provider execution failed",
                    provider_id=request.provider_id,
                    requirement_id=request.requirement_id,
                )
            if capture.bundle is None:
                fail_execution(
                    ExecutionErrorCode.PROVIDER_EXECUTION_FAILED,
                    ExecutionStage.PROVIDER_RESPONSE,
                    "adapter returned no bundle",
                    provider_id=request.provider_id,
                    requirement_id=request.requirement_id,
                )
            captures[request.requirement_id] = capture
            executed_ids.append(request.requirement_id)
            request_records.append(
                _build_request_record(
                    request, capture, artifact_contents
                )
            )
    except ExecutionError:
        raise

    completed_at = datetime.now(timezone.utc)
    outcome = AcquisitionExecutionOutcome(
        attempt_id=attempt_id,
        authorization_id=restored_authorization.authorization_id,
        authorization_sha256=authorization_sha256,
        authorization_receipt_sha256=receipt_sha256,
        acquisition_request_plan_sha256=(
            generated.acquisition_request_plan_sha256
        ),
        status=ExecutionStatus.SUCCEEDED,
        started_at=started_at,
        completed_at=completed_at,
        executed_request_ids=sorted(executed_ids),
        snapshot_id=None,
        failure=None,
    )
    try:
        verified = commit_acquisition_snapshot(
            root=Path(snapshot_root),
            attempt_id=attempt_id,
            authorization_id=restored_authorization.authorization_id,
            authorization_sha256=authorization_sha256,
            authorization_receipt_sha256=receipt_sha256,
            request_plan_sha256=generated.acquisition_request_plan_sha256,
            upstream_hashes={
                "research_spec": generated.research_spec_sha256,
                "data_plan": generated.data_plan_sha256,
                "data_plan_confirmation": (
                    generated.data_plan_confirmation_sha256
                ),
                "source_selection": generated.source_selection_sha256,
                "source_selection_confirmation": (
                    generated.source_selection_confirmation_sha256
                ),
                "instrument_registry": (
                    generated.instrument_registry_sha256
                ),
                "calendar_registry": generated.calendar_registry_sha256,
            },
            request_records=request_records,
            artifact_contents=artifact_contents,
            outcome=outcome,
            created_at=completed_at,
        )
    except SnapshotError as error:
        raise ExecutionError(
            ExecutionErrorCode.SNAPSHOT_COMMIT_FAILED,
            ExecutionStage.SNAPSHOT_COMMIT,
            error.safe_message,
        ) from error
    succeeded_outcome = AcquisitionExecutionOutcome(
        **{
            **outcome.model_dump(),
            "snapshot_id": verified.snapshot_id,
        }
    )
    final_outcome = parse_execution_outcome(
        serialize_execution_outcome(succeeded_outcome)
    )
    return VerifiedAcquisitionSnapshot(
        snapshot_id=verified.snapshot_id,
        snapshot_path=verified.snapshot_path,
        manifest=verified.manifest,
        manifest_sha256=verified.manifest_sha256,
        outcome=final_outcome,
    )


def _load_existing_receipt(receipt_path: str | Path) -> object | None:
    from market_validator.data.access_authorization import (
        parse_data_access_authorization_receipt,
    )

    path = Path(receipt_path)
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        fail_execution(
            ExecutionErrorCode.AUTHORIZATION_NOT_CONSUMED,
            ExecutionStage.AUTHORIZATION_CONSUMPTION,
            "receipt path is not a regular file; no provider request was "
            "executed",
        )
    try:
        return parse_data_access_authorization_receipt(path.read_bytes())
    except (OSError, DataAccessAuthorizationError):
        fail_execution(
            ExecutionErrorCode.INVALID_EXECUTION_INPUT,
            ExecutionStage.AUTHORIZATION_CONSUMPTION,
            "existing receipt could not be parsed; no provider request "
            "was executed",
        )


def _build_request_record(
    request: PublicAcquisitionRequest,
    capture: ProviderExecutionCapture,
    artifact_contents: dict[tuple[str, str], bytes],
) -> dict[str, object]:
    steps_sha256 = _sha256_hex(
        json.dumps(
            [
                {
                    "sequence": step.sequence,
                    "method": step.method.value,
                    "endpoint": step.endpoint,
                    "public_parameters": step.public_parameters,
                    "pagination_policy": step.pagination_policy.value,
                }
                for step in request.steps
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    artifacts: list[dict[str, object]] = []
    for artifact in capture.raw_artifacts:
        relative_path = f"raw/{artifact.logical_name}"
        artifact_contents[(request.requirement_id, relative_path)] = (
            artifact.content
        )
        artifacts.append(
            {
                "relative_path": relative_path,
                "sha256": _sha256_hex(artifact.content),
                "byte_size": len(artifact.content),
                "media_type": artifact.media_type,
                "role": "raw",
            }
        )
    trace_path = "request-trace.json"
    trace_bytes = json.dumps(
        [
            {
                "sequence": entry.sequence,
                "endpoint": entry.endpoint,
                "public_parameters": entry.public_parameters,
                "response_sha256": entry.response_sha256,
                "response_byte_size": entry.response_byte_size,
                "http_status": entry.http_status,
            }
            for entry in capture.request_trace
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact_contents[(request.requirement_id, trace_path)] = trace_bytes
    artifacts.append(
        {
            "relative_path": trace_path,
            "sha256": _sha256_hex(trace_bytes),
            "byte_size": len(trace_bytes),
            "media_type": "application/json",
            "role": "request-trace",
        }
    )
    bundle_bytes = json.dumps(
        capture.bundle.model_dump(
            mode="json", exclude_none=False, exclude_computed_fields=True
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    bundle_path = "bundle.json"
    artifact_contents[(request.requirement_id, bundle_path)] = bundle_bytes
    artifacts.append(
        {
            "relative_path": bundle_path,
            "sha256": _sha256_hex(bundle_bytes),
            "byte_size": len(bundle_bytes),
            "media_type": "application/json",
            "role": "bundle",
        }
    )
    return {
        "requirement_id": request.requirement_id,
        "provider_id": request.provider_id,
        "request_steps_sha256": steps_sha256,
        "artifacts": artifacts,
    }


def _find_requirement(data_plan: object, requirement_id: str):
    from market_validator.data.models import DataPlan

    if not isinstance(data_plan, DataPlan):
        return None
    for requirement in data_plan.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    return None


__all__ = [
    "EXECUTION_SCHEMA_VERSION",
    "ExecutionError",
    "ExecutionErrorCode",
    "ExecutionStage",
    "FredExecutionAdapter",
    "LocalFileExecutionAdapter",
    "ProviderExecutionAdapter",
    "ProviderExecutionCapture",
    "RawArtifact",
    "RequestTraceEntry",
    "execute_authorized_acquisition",
    "fail_execution",
]
