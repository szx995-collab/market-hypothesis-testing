"""Shared strict JSON, failure, and immutable-output helpers for hypothesis review."""

from __future__ import annotations

from enum import StrEnum
import json
import os
from pathlib import Path
import secrets
import stat
from collections.abc import Callable
from typing import NoReturn, TypeVar

from pydantic import BaseModel, Field, ValidationError

from market_validator.research.models import StrictResearchModel


class HypothesisLifecycleErrorCode(StrEnum):
    INVALID_CLARIFICATIONS = "invalid_hypothesis_clarifications"
    CLARIFICATION_MISMATCH = "hypothesis_clarification_mismatch"
    CLARIFICATION_CONFLICT = "hypothesis_clarification_conflict"
    PROPOSAL_NOT_READY = "hypothesis_proposal_not_ready"
    INVALID_CONFIRMATION = "invalid_hypothesis_confirmation"
    CONFIRMATION_MISMATCH = "hypothesis_confirmation_mismatch"
    RESEARCH_SPEC_UNRESOLVED = "research_spec_unresolved"
    RESEARCH_SPEC_INVALID = "research_spec_invalid"
    OUTPUT_CONFLICT = "hypothesis_review_output_conflict"
    OUTPUT_ERROR = "hypothesis_review_output_error"


class HypothesisLifecycleStage(StrEnum):
    CLARIFICATION_VALIDATION = "hypothesis_clarification_validation"
    CLARIFICATION_APPLICATION = "hypothesis_clarification_application"
    CONFIRMATION_VALIDATION = "hypothesis_confirmation_validation"
    RESEARCH_SPEC_COMPILATION = "research_spec_compilation"
    OUTPUT = "hypothesis_review_output"


class UnresolvedResearchSpecRequirement(StrictResearchModel):
    path: str
    code: str
    message: str


class HypothesisLifecycleFailure(StrictResearchModel):
    code: HypothesisLifecycleErrorCode
    stage: HypothesisLifecycleStage
    message: str
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] = Field(
        default_factory=list
    )


class HypothesisLifecycleError(ValueError):
    """Structured safe failure that never embeds input content."""

    def __init__(self, failure: HypothesisLifecycleFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


def fail_lifecycle(
    code: HypothesisLifecycleErrorCode,
    stage: HypothesisLifecycleStage,
    message: str,
    *,
    unresolved_requirements: list[UnresolvedResearchSpecRequirement] | None = None,
) -> NoReturn:
    raise HypothesisLifecycleError(
        HypothesisLifecycleFailure(
            code=code,
            stage=stage,
            message=message,
            unresolved_requirements=unresolved_requirements or [],
        )
    )


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


ModelT = TypeVar("ModelT", bound=BaseModel)


def parse_strict_model(
    payload: bytes | bytearray,
    model_type: type[ModelT],
    *,
    code: HypothesisLifecycleErrorCode,
    stage: HypothesisLifecycleStage,
    label: str,
) -> ModelT:
    """Parse exactly one UTF-8 JSON object with duplicate/non-finite rejection."""

    if not isinstance(payload, (bytes, bytearray)):
        fail_lifecycle(code, stage, f"{label} must be supplied as UTF-8 bytes")
    normalized = bytes(payload)
    try:
        decoded = json.loads(
            normalized.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_lifecycle(
            code,
            stage,
            f"{label} must be exactly one strict UTF-8 JSON object",
        )
    if not isinstance(decoded, dict):
        fail_lifecycle(code, stage, f"{label} must be a JSON object")
    try:
        return model_type.model_validate_json(normalized)
    except ValidationError:
        fail_lifecycle(code, stage, f"{label} failed strict domain validation")


def serialize_strict_model(
    model: ModelT,
    model_type: type[ModelT],
    *,
    parser: Callable[[bytes], ModelT],
    code: HypothesisLifecycleErrorCode,
    stage: HypothesisLifecycleStage,
    label: str,
    exclude_unset: bool = False,
) -> bytes:
    if not isinstance(model, model_type):
        fail_lifecycle(code, stage, f"{label} must be a validated model")
    try:
        payload = (
            json.dumps(
                model.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                    exclude_unset=exclude_unset,
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        fail_lifecycle(code, stage, f"{label} could not be serialized")
    if parser(payload) != model:
        fail_lifecycle(code, stage, f"{label} does not round-trip exactly")
    return payload


def safe_output_path(path: str | Path) -> Path:
    target = Path(path)
    if any(part == os.pardir for part in target.parts):
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "output path must not contain path traversal",
        )
    absolute = target.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError:
            fail_lifecycle(
                HypothesisLifecycleErrorCode.OUTPUT_ERROR,
                HypothesisLifecycleStage.OUTPUT,
                "output path cannot be inspected safely",
            )
        if exists and candidate.is_symlink():
            fail_lifecycle(
                HypothesisLifecycleErrorCode.OUTPUT_ERROR,
                HypothesisLifecycleStage.OUTPUT,
                "output path must not contain symbolic links",
            )
    parent = target.parent.resolve(strict=False)
    if not parent.exists() or not parent.is_dir():
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "output parent must already be a directory",
        )
    return parent / target.name


def persist_immutable_bytes(payload: bytes, path: str | Path) -> Path:
    """Atomically create one regular file, accepting only byte-identical repeats."""

    target = safe_output_path(path)
    if target.exists() or target.is_symlink():
        try:
            mode = target.lstat().st_mode
            existing = target.read_bytes()
        except OSError:
            fail_lifecycle(
                HypothesisLifecycleErrorCode.OUTPUT_ERROR,
                HypothesisLifecycleStage.OUTPUT,
                "existing output cannot be inspected safely",
            )
        if not stat.S_ISREG(mode) or existing != payload:
            fail_lifecycle(
                HypothesisLifecycleErrorCode.OUTPUT_CONFLICT,
                HypothesisLifecycleStage.OUTPUT,
                "existing output differs and was not overwritten",
            )
        return target

    temporary = target.parent / f".hypothesis-review-tmp-{secrets.token_hex(8)}"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_CONFLICT,
            HypothesisLifecycleStage.OUTPUT,
            "output appeared concurrently and was not overwritten",
        )
    except OSError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "output could not be published atomically",
        )
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    try:
        published = target.read_bytes()
    except OSError:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "published output could not be verified",
        )
    if published != payload:
        fail_lifecycle(
            HypothesisLifecycleErrorCode.OUTPUT_ERROR,
            HypothesisLifecycleStage.OUTPUT,
            "published output failed byte verification",
        )
    return target


__all__ = [
    "HypothesisLifecycleError",
    "HypothesisLifecycleErrorCode",
    "HypothesisLifecycleFailure",
    "HypothesisLifecycleStage",
    "UnresolvedResearchSpecRequirement",
    "fail_lifecycle",
    "parse_strict_model",
    "persist_immutable_bytes",
    "safe_output_path",
    "serialize_strict_model",
]
