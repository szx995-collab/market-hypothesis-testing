"""Minimal deterministic workflow for the fixed volatility comparison.

The workflow coordinates existing, separately tested boundaries. It does not
parse Bundles, calculate statistics, change an analysis result, or add a new
workflow artifact format.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Annotated, Literal, Mapping, NoReturn

from pydantic import Field, StringConstraints, ValidationError, model_validator

from market_validator.analysis.artifacts import (
    ArtifactConflictError,
    ArtifactFileMetadata,
    ArtifactIntegrityError,
    ArtifactPathError,
    MANIFEST_FILENAME,
    PriceChangeVolatilityArtifactError,
    load_price_change_volatility_artifact,
    persist_price_change_volatility_artifact,
)
from market_validator.analysis.price_change_volatility import (
    PriceChangeVolatilityAnalysisError,
    PriceChangeVolatilityParameters,
    PriceChangeVolatilityResult,
    VolatilityConclusion,
    compare_price_change_volatility,
)
from market_validator.data.models import Identifier, NonEmptyString, StrictDataModel


WORKFLOW_SCHEMA_VERSION = "1.0"
SUPPORTED_ANALYSIS_TYPE = "price_change_volatility"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class WorkflowStage(StrEnum):
    """Observable deterministic stages in their required execution order."""

    PLAN_VALIDATION = "plan_validation"
    PATH_VALIDATION = "path_validation"
    ANALYSIS = "analysis"
    SOURCE_VERIFICATION = "source_verification"
    ARTIFACT_PERSISTENCE = "artifact_persistence"
    ARTIFACT_VERIFICATION = "artifact_verification"
    COMPLETED = "completed"


COMPLETED_STAGE_SEQUENCE = [
    WorkflowStage.PLAN_VALIDATION,
    WorkflowStage.PATH_VALIDATION,
    WorkflowStage.ANALYSIS,
    WorkflowStage.SOURCE_VERIFICATION,
    WorkflowStage.ARTIFACT_PERSISTENCE,
    WorkflowStage.ARTIFACT_VERIFICATION,
    WorkflowStage.COMPLETED,
]


class WorkflowErrorCode(StrEnum):
    INVALID_PLAN = "invalid_plan"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    ANALYSIS_CONTRACT_MISMATCH = "analysis_contract_mismatch"
    ANALYSIS_FAILED = "analysis_failed"
    ARTIFACT_CONFLICT = "artifact_conflict"
    ARTIFACT_VERIFICATION_FAILED = "artifact_verification_failed"
    WORKFLOW_PATH_ERROR = "workflow_path_error"


class MarketValidationWorkflowPlan(StrictDataModel):
    """AI-to-deterministic-executor boundary for the one supported workflow."""

    workflow_schema_version: Literal["1.0"] = WORKFLOW_SCHEMA_VERSION
    analysis_type: Literal["price_change_volatility"] = SUPPORTED_ANALYSIS_TYPE
    expected_source_request_id: Identifier
    expected_source_bundle_sha256: Sha256Hex
    parameters: PriceChangeVolatilityParameters
    expected_artifact_manifest_sha256: Sha256Hex | None = None


class WorkflowFailure(StrictDataModel):
    """Structured, safe failure details carried by WorkflowExecutionError."""

    workflow_status: Literal["failed"] = "failed"
    code: WorkflowErrorCode
    stage: WorkflowStage
    message: NonEmptyString


class WorkflowExecutionError(RuntimeError):
    """Workflow failure that remains distinct from statistical conclusions."""

    def __init__(self, failure: WorkflowFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")

    @property
    def code(self) -> WorkflowErrorCode:
        return self.failure.code


class WorkflowArtifactFiles(StrictDataModel):
    """Verified metadata for all three files in the analysis artifact."""

    result: ArtifactFileMetadata
    report: ArtifactFileMetadata
    manifest: ArtifactFileMetadata

    @model_validator(mode="after")
    def validate_filenames(self) -> "WorkflowArtifactFiles":
        expected = {
            "result": "result.json",
            "report": "report.md",
            "manifest": MANIFEST_FILENAME,
        }
        for field_name, filename in expected.items():
            if getattr(self, field_name).filename != filename:
                raise ValueError(f"{field_name}.filename must be {filename!r}")
        return self


class CompletedWorkflowRun(StrictDataModel):
    """Successful workflow state based only on the strictly reloaded result."""

    workflow_status: Literal["completed"] = "completed"
    analysis_type: Literal["price_change_volatility"] = SUPPORTED_ANALYSIS_TYPE
    source_request_id: Identifier
    source_bundle_sha256: Sha256Hex
    artifact_id: Identifier
    artifact_path: Path
    files: WorkflowArtifactFiles
    manifest_sha256: Sha256Hex
    final_conclusion: VolatilityConclusion
    result: PriceChangeVolatilityResult
    completed_stages: list[WorkflowStage] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def validate_completed_run(self) -> "CompletedWorkflowRun":
        if self.completed_stages != COMPLETED_STAGE_SEQUENCE:
            raise ValueError("completed_stages must match the fixed workflow order")
        if self.source_request_id != self.result.source.request_id:
            raise ValueError("workflow source request ID must match the loaded result")
        if self.source_bundle_sha256 != self.result.source.bundle_sha256:
            raise ValueError("workflow Bundle SHA-256 must match the loaded result")
        if self.final_conclusion is not self.result.final_conclusion:
            raise ValueError("workflow conclusion must match the loaded result")
        return self


def _fail(
    code: WorkflowErrorCode,
    stage: WorkflowStage,
    message: str,
) -> NoReturn:
    raise WorkflowExecutionError(
        WorkflowFailure(code=code, stage=stage, message=message)
    )


def _validate_plan(
    plan: MarketValidationWorkflowPlan | Mapping[str, object] | str | bytes,
) -> MarketValidationWorkflowPlan:
    try:
        if isinstance(plan, MarketValidationWorkflowPlan):
            return MarketValidationWorkflowPlan.model_validate_json(
                plan.model_dump_json(exclude_computed_fields=True)
            )
        if isinstance(plan, (str, bytes)):
            return MarketValidationWorkflowPlan.model_validate_json(plan)
        if isinstance(plan, Mapping):
            encoded = json.dumps(plan, ensure_ascii=False).encode("utf-8")
            return MarketValidationWorkflowPlan.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError, OverflowError):
        _fail(
            WorkflowErrorCode.INVALID_PLAN,
            WorkflowStage.PLAN_VALIDATION,
            "workflow plan failed strict validation",
        )
    _fail(
        WorkflowErrorCode.INVALID_PLAN,
        WorkflowStage.PLAN_VALIDATION,
        "workflow plan must be a plan model, JSON object, or JSON bytes",
    )


def _path_is_symlink(path: Path) -> bool:
    return path.is_symlink()


def _reject_traversal(path: Path, label: str) -> None:
    if any(part == os.pardir for part in path.parts):
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            f"{label} must not contain path traversal",
        )


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        if candidate.exists() and _path_is_symlink(candidate):
            _fail(
                WorkflowErrorCode.WORKFLOW_PATH_ERROR,
                WorkflowStage.PATH_VALIDATION,
                f"{label} must not contain symbolic links",
            )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_paths(
    bundle_path: str | Path,
    artifact_root: str | Path,
) -> tuple[Path, Path]:
    source = Path(bundle_path)
    root = Path(artifact_root)
    _reject_traversal(source, "bundle_path")
    _reject_traversal(root, "artifact_root")
    _reject_symlink_components(source, "bundle_path")
    _reject_symlink_components(root, "artifact_root")

    if not source.exists():
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "bundle_path does not exist",
        )
    if _path_is_symlink(source):
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "bundle_path must not be a symbolic link",
        )
    try:
        source_mode = source.lstat().st_mode
    except OSError:
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "bundle_path cannot be inspected safely",
        )
    if not stat.S_ISREG(source_mode):
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "bundle_path must be a regular file",
        )
    if root.exists() and not root.is_dir():
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "artifact_root must be a directory or a not-yet-created directory",
        )

    source_resolved = source.resolve(strict=True)
    root_resolved = root.resolve(strict=False)
    if _is_within(source_resolved, root_resolved):
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.PATH_VALIDATION,
            "source Bundle must not be inside artifact_root",
        )
    return source_resolved, root_resolved


def _manifest_file_metadata(
    manifest_path: Path,
    expected_sha256: str,
) -> ArtifactFileMetadata:
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "verified manifest could not be read for workflow metadata",
        )
    actual_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "manifest changed after strict artifact verification",
        )
    return ArtifactFileMetadata(
        filename=MANIFEST_FILENAME,
        byte_size=len(manifest_bytes),
        sha256=actual_sha256,
    )


def run_market_validation_workflow(
    plan: MarketValidationWorkflowPlan | Mapping[str, object] | str | bytes,
    bundle_path: str | Path,
    artifact_root: str | Path,
) -> CompletedWorkflowRun:
    """Run the sole supported deterministic market-validation workflow."""

    validated_plan = _validate_plan(plan)
    source_path, safe_artifact_root = _validate_paths(bundle_path, artifact_root)

    try:
        analysis_result = compare_price_change_volatility(
            source_path,
            validated_plan.parameters,
        )
    except (PriceChangeVolatilityAnalysisError, ValidationError) as exc:
        _fail(
            WorkflowErrorCode.ANALYSIS_FAILED,
            WorkflowStage.ANALYSIS,
            "price-change volatility analysis failed",
        )
    if not isinstance(analysis_result, PriceChangeVolatilityResult):
        _fail(
            WorkflowErrorCode.ANALYSIS_FAILED,
            WorkflowStage.ANALYSIS,
            "analysis returned an invalid result type",
        )

    if analysis_result.source.request_id != validated_plan.expected_source_request_id:
        _fail(
            WorkflowErrorCode.SOURCE_IDENTITY_MISMATCH,
            WorkflowStage.SOURCE_VERIFICATION,
            "analysis source request ID does not match the workflow plan",
        )
    if (
        analysis_result.source.bundle_sha256
        != validated_plan.expected_source_bundle_sha256
    ):
        _fail(
            WorkflowErrorCode.SOURCE_IDENTITY_MISMATCH,
            WorkflowStage.SOURCE_VERIFICATION,
            "analysis source Bundle SHA-256 does not match the workflow plan",
        )
    if analysis_result.parameters != validated_plan.parameters:
        _fail(
            WorkflowErrorCode.ANALYSIS_CONTRACT_MISMATCH,
            WorkflowStage.SOURCE_VERIFICATION,
            "analysis parameters do not exactly match the workflow plan",
        )
    if analysis_result.errors or analysis_result.transform_error_count:
        _fail(
            WorkflowErrorCode.ANALYSIS_FAILED,
            WorkflowStage.ANALYSIS,
            "analysis result contains errors and cannot be published",
        )

    try:
        persisted = persist_price_change_volatility_artifact(
            analysis_result,
            safe_artifact_root,
        )
    except ArtifactConflictError:
        _fail(
            WorkflowErrorCode.ARTIFACT_CONFLICT,
            WorkflowStage.ARTIFACT_PERSISTENCE,
            "immutable analysis artifact conflicts with existing content",
        )
    except ArtifactPathError:
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.ARTIFACT_PERSISTENCE,
            "analysis artifact path failed safety validation",
        )
    except PriceChangeVolatilityArtifactError:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_PERSISTENCE,
            "analysis artifact could not be safely persisted and verified",
        )
    except OSError:
        _fail(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR,
            WorkflowStage.ARTIFACT_PERSISTENCE,
            "analysis artifact could not be written safely",
        )

    trust_anchor = (
        validated_plan.expected_artifact_manifest_sha256
        or persisted.manifest_sha256
    )
    try:
        loaded = load_price_change_volatility_artifact(
            persisted.artifact_path,
            expected_manifest_sha256=trust_anchor,
        )
    except (ArtifactIntegrityError, ArtifactPathError, OSError):
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "strict artifact reload failed",
        )
    if loaded.result != analysis_result:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "strictly reloaded result differs from the analyzed result",
        )
    if loaded.result.parameters != validated_plan.parameters:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "strictly reloaded parameters differ from the workflow plan",
        )
    if (
        loaded.result.source.request_id
        != validated_plan.expected_source_request_id
        or loaded.result.source.bundle_sha256
        != validated_plan.expected_source_bundle_sha256
    ):
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "strictly reloaded source identity differs from the workflow plan",
        )
    if loaded.result.errors or loaded.result.transform_error_count:
        _fail(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED,
            WorkflowStage.ARTIFACT_VERIFICATION,
            "strictly reloaded result contains analysis errors",
        )

    manifest_metadata = _manifest_file_metadata(
        persisted.manifest_path,
        loaded.manifest_sha256,
    )
    return CompletedWorkflowRun(
        source_request_id=loaded.result.source.request_id,
        source_bundle_sha256=loaded.result.source.bundle_sha256,
        artifact_id=loaded.manifest.artifact_id,
        artifact_path=loaded.artifact_path,
        files=WorkflowArtifactFiles(
            result=loaded.manifest.result_file,
            report=loaded.manifest.report_file,
            manifest=manifest_metadata,
        ),
        manifest_sha256=loaded.manifest_sha256,
        final_conclusion=loaded.result.final_conclusion,
        result=loaded.result,
        completed_stages=list(COMPLETED_STAGE_SEQUENCE),
    )


__all__ = [
    "COMPLETED_STAGE_SEQUENCE",
    "CompletedWorkflowRun",
    "MarketValidationWorkflowPlan",
    "WorkflowArtifactFiles",
    "WorkflowErrorCode",
    "WorkflowExecutionError",
    "WorkflowFailure",
    "WorkflowStage",
    "run_market_validation_workflow",
]
