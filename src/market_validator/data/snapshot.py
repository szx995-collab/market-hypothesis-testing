"""Transactional acquisition snapshot commit and verification.

One authorization's full request results are committed as a single immutable
snapshot: staged sibling directory, fsync, atomic directory rename, then
final re-verification. No partial final snapshot is ever observable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import (
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
)

from market_validator.data.acquisition_request import (
    AccessMode,
    Identifier,
    NonEmptyString,
    PublicAcquisitionRequest,
    RequestMethod,
    calculate_acquisition_request_plan_sha256,
    parse_acquisition_request_plan,
    serialize_acquisition_request_plan,
)
from market_validator.data.models import DataBundle, DataRequirement
from market_validator.research.models import StrictResearchModel

SNAPSHOT_SCHEMA_VERSION = "1.0"
EXECUTION_OUTCOME_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionFailure(StrictResearchModel):
    """Safe, deterministic failure record without secrets or raw data."""

    code: NonEmptyString
    stage: NonEmptyString
    provider_id: Identifier | None
    requirement_id: Identifier | None
    safe_message: NonEmptyString
    retryable: Literal[False]


class AcquisitionExecutionOutcome(StrictResearchModel):
    """Immutable outcome of one authorized execution attempt."""

    execution_schema_version: Literal["1.0"] = EXECUTION_OUTCOME_SCHEMA_VERSION
    attempt_id: NonEmptyString
    authorization_id: Identifier
    authorization_sha256: Sha256Hex
    authorization_receipt_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime
    executed_request_ids: list[Identifier]
    snapshot_id: NonEmptyString | None
    failure: ExecutionFailure | None

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("started_at", "completed_at", when_used="json")
    def serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SnapshotArtifact(StrictResearchModel):
    relative_path: NonEmptyString
    sha256: Sha256Hex
    byte_size: int
    media_type: NonEmptyString
    role: NonEmptyString


class SnapshotRequestRecord(StrictResearchModel):
    requirement_id: Identifier
    provider_id: Identifier
    request_steps_sha256: Sha256Hex
    artifacts: list[SnapshotArtifact]


class AcquisitionSnapshotManifest(StrictResearchModel):
    snapshot_schema_version: Literal["1.0"] = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: NonEmptyString
    attempt_id: NonEmptyString
    authorization_id: Identifier
    authorization_sha256: Sha256Hex
    authorization_receipt_sha256: Sha256Hex
    acquisition_request_plan_sha256: Sha256Hex
    research_spec_sha256: Sha256Hex
    data_plan_sha256: Sha256Hex
    data_plan_confirmation_sha256: Sha256Hex
    source_selection_sha256: Sha256Hex
    source_selection_confirmation_sha256: Sha256Hex
    instrument_registry_sha256: Sha256Hex
    calendar_registry_sha256: Sha256Hex
    created_at: datetime
    request_records: list[SnapshotRequestRecord]

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VerifiedAcquisitionSnapshot(StrictResearchModel):
    snapshot_id: NonEmptyString
    snapshot_path: Path
    manifest: AcquisitionSnapshotManifest
    manifest_sha256: Sha256Hex
    outcome: AcquisitionExecutionOutcome


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


def _fail_snapshot(message: str) -> NoReturn:
    raise SnapshotError(message)


class SnapshotError(RuntimeError):
    """Safe transactional failure; messages never embed raw data or secrets."""

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def parse_snapshot_manifest(payload: bytes) -> AcquisitionSnapshotManifest:
    try:
        json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        _fail_snapshot("snapshot manifest failed strict JSON validation")
    try:
        return AcquisitionSnapshotManifest.model_validate_json(payload)
    except ValidationError:
        _fail_snapshot("snapshot manifest failed strict domain validation")


def serialize_snapshot_manifest(manifest: AcquisitionSnapshotManifest) -> bytes:
    try:
        payload = (
            json.dumps(
                manifest.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail_snapshot("snapshot manifest could not be serialized")
    if parse_snapshot_manifest(payload) != manifest:
        _fail_snapshot("snapshot manifest does not round-trip exactly")
    return payload


def parse_execution_outcome(payload: bytes) -> AcquisitionExecutionOutcome:
    try:
        json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        _fail_snapshot("execution outcome failed strict JSON validation")
    try:
        return AcquisitionExecutionOutcome.model_validate_json(payload)
    except ValidationError:
        _fail_snapshot("execution outcome failed strict domain validation")


def serialize_execution_outcome(outcome: AcquisitionExecutionOutcome) -> bytes:
    try:
        payload = (
            json.dumps(
                outcome.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail_snapshot("execution outcome could not be serialized")
    if parse_execution_outcome(payload) != outcome:
        _fail_snapshot("execution outcome does not round-trip exactly")
    return payload


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative_path(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        _fail_snapshot("snapshot artifact path must be relative")
    if ".." in candidate.parts:
        _fail_snapshot("snapshot artifact path must not contain parent segments")
    if relative_path.startswith("/") or "\\" in relative_path:
        _fail_snapshot("snapshot artifact path must use forward slashes")
    return candidate


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsynced(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _derive_snapshot_id(
    attempt_id: str,
    authorization_sha256: str,
    authorization_receipt_sha256: str,
    request_plan_sha256: str,
    content_hashes: list[str],
) -> str:
    identity = json.dumps(
        {
            "attempt_id": attempt_id,
            "authorization_sha256": authorization_sha256,
            "authorization_receipt_sha256": authorization_receipt_sha256,
            "acquisition_request_plan_sha256": request_plan_sha256,
            "content_hashes": sorted(set(content_hashes)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def _staging_name(attempt_id: str) -> str:
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:16]
    return f".staging-{digest}"


def commit_acquisition_snapshot(
    *,
    root: Path,
    attempt_id: str,
    authorization_id: str,
    authorization_sha256: str,
    authorization_receipt_sha256: str,
    request_plan_sha256: str,
    upstream_hashes: dict[str, str],
    request_records: list[dict[str, object]],
    artifact_contents: dict[tuple[str, str], bytes],
    outcome: AcquisitionExecutionOutcome,
    created_at: datetime,
) -> VerifiedAcquisitionSnapshot:
    """Commit one whole-snapshot transaction with atomic directory rename."""
    root = Path(root)
    if not root.is_dir():
        _fail_snapshot("snapshot root must be an existing directory")
    content_hashes: list[str] = []
    for record in request_records:
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, list):
            _fail_snapshot("snapshot request records must list artifacts")
        for artifact in artifacts:
            artifact_sha = artifact.get("sha256")
            if isinstance(artifact_sha, str):
                content_hashes.append(artifact_sha)
    snapshot_id = _derive_snapshot_id(
        attempt_id,
        authorization_sha256,
        authorization_receipt_sha256,
        request_plan_sha256,
        content_hashes,
    )
    final_path = root / snapshot_id
    staging_path = root / _staging_name(attempt_id)
    if final_path.exists() or final_path.is_symlink():
        _fail_snapshot("final snapshot path already exists")
    if staging_path.exists() or staging_path.is_symlink():
        _fail_snapshot("staging path already exists")

    manifest = AcquisitionSnapshotManifest(
        snapshot_id=snapshot_id,
        attempt_id=attempt_id,
        authorization_id=authorization_id,
        authorization_sha256=authorization_sha256,
        authorization_receipt_sha256=authorization_receipt_sha256,
        acquisition_request_plan_sha256=request_plan_sha256,
        research_spec_sha256=upstream_hashes["research_spec"],
        data_plan_sha256=upstream_hashes["data_plan"],
        data_plan_confirmation_sha256=upstream_hashes["data_plan_confirmation"],
        source_selection_sha256=upstream_hashes["source_selection"],
        source_selection_confirmation_sha256=upstream_hashes[
            "source_selection_confirmation"
        ],
        instrument_registry_sha256=upstream_hashes["instrument_registry"],
        calendar_registry_sha256=upstream_hashes["calendar_registry"],
        created_at=created_at,
        request_records=[
            SnapshotRequestRecord.model_validate(record)
            for record in request_records
        ],
    )
    manifest_bytes = serialize_snapshot_manifest(manifest)
    outcome_bytes = serialize_execution_outcome(outcome)

    try:
        staging_path.mkdir(parents=False)
        (staging_path / "requests").mkdir()
        for record in manifest.request_records:
            requirement_dir = staging_path / "requests" / record.requirement_id
            requirement_dir.mkdir()
            for artifact in record.artifacts:
                artifact_path = _safe_relative_path(artifact.relative_path)
                target = requirement_dir / artifact_path
                if requirement_dir.resolve() not in target.resolve().parents:
                    _fail_snapshot(
                        "snapshot artifact escapes its requirement directory"
                    )
                content = _read_staged_artifact(
                    artifact_contents,
                    record,
                    artifact,
                    requirement_dir,
                )
                _write_fsynced(target, content)
                _verify_staged_artifact(target, artifact)
        _write_fsynced(staging_path / "outcome.json", outcome_bytes)
        _write_fsynced(staging_path / "manifest.json", manifest_bytes)
        _fsync_directory(staging_path)
        _verify_staging_tree(staging_path, manifest, outcome_bytes)
    except (OSError, SnapshotError) as error:
        shutil.rmtree(staging_path, ignore_errors=True)
        _fail_snapshot(
            "snapshot staging failed; final snapshot was not created"
        )

    try:
        os.rename(staging_path, final_path)
    except OSError:
        shutil.rmtree(staging_path, ignore_errors=True)
        _fail_snapshot("snapshot atomic rename failed; final snapshot absent")
    _fsync_directory(root)

    try:
        _verify_final_tree(final_path, manifest, outcome_bytes)
    except SnapshotError:
        shutil.rmtree(final_path, ignore_errors=True)
        raise
    return VerifiedAcquisitionSnapshot(
        snapshot_id=snapshot_id,
        snapshot_path=final_path,
        manifest=manifest,
        manifest_sha256=_sha256_hex(manifest_bytes),
        outcome=outcome,
    )


def _read_staged_artifact(
    artifact_contents: dict[tuple[str, str], bytes],
    record: SnapshotRequestRecord,
    artifact: SnapshotArtifact,
    requirement_dir: Path,
) -> bytes:
    """Pull artifact content from the coordinator-provided capture map."""
    content = artifact_contents.get(
        (record.requirement_id, artifact.relative_path)
    )
    if content is None:
        _fail_snapshot("staged artifact content was not provided")
    if len(content) != artifact.byte_size:
        _fail_snapshot("staged artifact byte size does not match the manifest")
    if _sha256_hex(content) != artifact.sha256:
        _fail_snapshot("staged artifact content does not match the manifest hash")
    return content



def _verify_staged_artifact(path: Path, artifact: SnapshotArtifact) -> None:
    if not path.is_file() or path.is_symlink():
        _fail_snapshot("staged artifact is not a regular file")
    content = path.read_bytes()
    if len(content) != artifact.byte_size:
        _fail_snapshot("staged artifact byte size mismatch")
    if _sha256_hex(content) != artifact.sha256:
        _fail_snapshot("staged artifact hash mismatch")


def _verify_staging_tree(
    staging_path: Path,
    manifest: AcquisitionSnapshotManifest,
    outcome_bytes: bytes,
) -> None:
    expected_files: set[str] = {"outcome.json", "manifest.json"}
    for record in manifest.request_records:
        for artifact in record.artifacts:
            expected_files.add(
                f"requests/{record.requirement_id}/{artifact.relative_path}"
            )
    actual_files = {
        str(path.relative_to(staging_path)).replace("\\", "/")
        for path in staging_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != expected_files:
        _fail_snapshot("staged file set does not match the manifest")
    if staging_path.joinpath("outcome.json").read_bytes() != outcome_bytes:
        _fail_snapshot("staged outcome does not match the committed bytes")
    if (
        staging_path.joinpath("manifest.json").read_bytes()
        != serialize_snapshot_manifest(manifest)
    ):
        _fail_snapshot("staged manifest does not match the committed bytes")


def verify_acquisition_snapshot(
    *,
    snapshot_path: Path,
    expected_plan_sha256: str,
    expected_authorization_sha256: str,
    expected_receipt_sha256: str,
    plan: object,
    data_plan: object,
) -> VerifiedAcquisitionSnapshot:
    """Re-read and verify a committed snapshot (Snapshot Verified)."""
    from market_validator.data.models import DataPlan

    snapshot_path = Path(snapshot_path)
    manifest_path = snapshot_path / "manifest.json"
    outcome_path = snapshot_path / "outcome.json"
    if not snapshot_path.is_dir() or snapshot_path.is_symlink():
        _fail_snapshot("snapshot path must be a real directory")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail_snapshot("snapshot manifest is missing or not a regular file")
    if not outcome_path.is_file() or outcome_path.is_symlink():
        _fail_snapshot("snapshot outcome is missing or not a regular file")
    manifest = parse_snapshot_manifest(manifest_path.read_bytes())
    outcome = parse_execution_outcome(outcome_path.read_bytes())
    if manifest.acquisition_request_plan_sha256 != expected_plan_sha256:
        _fail_snapshot("snapshot plan hash does not match the expected plan")
    if manifest.authorization_sha256 != expected_authorization_sha256:
        _fail_snapshot("snapshot authorization hash mismatch")
    if manifest.authorization_receipt_sha256 != expected_receipt_sha256:
        _fail_snapshot("snapshot receipt hash mismatch")
    if outcome.status is not ExecutionStatus.SUCCEEDED:
        _fail_snapshot("snapshot outcome is not succeeded")
    if outcome.snapshot_id != manifest.snapshot_id:
        _fail_snapshot("snapshot outcome id does not match the manifest")
    if not isinstance(plan, object) or not hasattr(plan, "acquisition_request_plan"):
        _fail_snapshot("plan must be a strictly validated generated plan")
    restored_plan = parse_acquisition_request_plan(
        serialize_acquisition_request_plan(
            plan.acquisition_request_plan
        )
    )
    if calculate_acquisition_request_plan_sha256(
        restored_plan
    ) != expected_plan_sha256:
        _fail_snapshot("plan does not match its bound hash")
    if not isinstance(data_plan, DataPlan):
        _fail_snapshot("data plan must be a strictly validated model")

    expected_files: set[str] = {"outcome.json", "manifest.json"}
    actual_files: set[str] = set()
    for record in manifest.request_records:
        requirement = _find_requirement(data_plan, record.requirement_id)
        if requirement is None:
            _fail_snapshot(
                f"manifest requirement {record.requirement_id} is not in "
                "the data plan"
            )
        for artifact in record.artifacts:
            relative = _safe_relative_path(artifact.relative_path)
            expected_files.add(
                f"requests/{record.requirement_id}/{artifact.relative_path}"
            )
            artifact_path = snapshot_path / "requests" / record.requirement_id / relative
            if not artifact_path.is_file() or artifact_path.is_symlink():
                _fail_snapshot("snapshot artifact is missing or not regular")
            content = artifact_path.read_bytes()
            if len(content) != artifact.byte_size:
                _fail_snapshot("snapshot artifact byte size mismatch")
            if _sha256_hex(content) != artifact.sha256:
                _fail_snapshot("snapshot artifact hash mismatch")
            if artifact.role == "bundle":
                bundle = DataBundle.model_validate_json(content)
                if bundle.requirement != requirement:
                    _fail_snapshot(
                        "bundle requirement does not match the data plan"
                    )
                if bundle.source.is_fallback:
                    _fail_snapshot("bundle must not be a fallback")
    for path in snapshot_path.rglob("*"):
        if path.is_file() and not path.is_symlink():
            actual_files.add(str(path.relative_to(snapshot_path)).replace("\\", "/"))
    if actual_files != expected_files:
        _fail_snapshot("snapshot contains extra or missing files")
    return VerifiedAcquisitionSnapshot(
        snapshot_id=manifest.snapshot_id,
        snapshot_path=snapshot_path,
        manifest=manifest,
        manifest_sha256=_sha256_hex(manifest_path.read_bytes()),
        outcome=outcome,
    )


def _find_requirement(data_plan: object, requirement_id: str):
    from market_validator.data.models import DataPlan

    if not isinstance(data_plan, DataPlan):
        return None
    for requirement in data_plan.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    return None


def _verify_final_tree(
    final_path: Path,
    manifest: AcquisitionSnapshotManifest,
    outcome_bytes: bytes,
) -> None:
    if not final_path.is_dir() or final_path.is_symlink():
        _fail_snapshot("final snapshot is not a real directory")
    if final_path.joinpath("outcome.json").read_bytes() != outcome_bytes:
        _fail_snapshot("final outcome does not match the committed bytes")
    if (
        final_path.joinpath("manifest.json").read_bytes()
        != serialize_snapshot_manifest(manifest)
    ):
        _fail_snapshot("final manifest does not match the committed bytes")
    _verify_staging_tree(final_path, manifest, outcome_bytes)


__all__ = [
    "AcquisitionExecutionOutcome",
    "AcquisitionSnapshotManifest",
    "ExecutionFailure",
    "ExecutionStatus",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotArtifact",
    "SnapshotError",
    "SnapshotRequestRecord",
    "VerifiedAcquisitionSnapshot",
    "commit_acquisition_snapshot",
    "parse_execution_outcome",
    "parse_snapshot_manifest",
    "serialize_execution_outcome",
    "serialize_snapshot_manifest",
    "verify_acquisition_snapshot",
]
