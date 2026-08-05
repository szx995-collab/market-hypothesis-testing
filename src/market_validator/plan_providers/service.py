"""Strict orchestration and immutable output for untrusted AI proposals."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import secrets
import stat
from typing import NoReturn

from pydantic import Field, model_validator

from market_validator.data.models import NonEmptyString, StrictDataModel
from market_validator.planning import (
    MarketValidationPlanProposal,
    PlanProposalError,
    SUPPORTED_PLANNING_CAPABILITIES,
    calculate_plan_proposal_sha256,
    market_validation_plan_proposal_json_schema,
    market_validation_plan_proposal_system_prompt,
    parse_market_validation_plan_proposal,
    serialize_market_validation_plan_proposal,
)
from market_validator.plan_providers.base import (
    PlanProposalGenerationRequest,
    PlanProposalProvider,
    PlanProposalProviderError,
    PlanProposalProviderErrorCode,
    PlanProposalProviderFailure,
    PlanProposalProviderStage,
    RawPlanProposalResponse,
)
from market_validator.plan_providers.deepseek_api import (
    DeepSeekApiPlanProposalProvider,
    DeepSeekPlanTransport,
)


SUPPORTED_PLAN_PROPOSAL_PROVIDERS = ("deepseek_api",)
SUPPORTED_CAPABILITY_DESCRIPTIONS = {
    "price_change_volatility": (
        "Fixed retrospective comparison of WTI per-observation signed absolute "
        "price-change volatility using the existing confirmed parameters."
    )
}


class GeneratedPlanProposal(StrictDataModel):
    """A provider output only after strict parsing and canonical serialization."""

    provider: NonEmptyString
    model: NonEmptyString
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_byte_size: int = Field(gt=0)
    proposal: MarketValidationPlanProposal

    @model_validator(mode="after")
    def validate_proposal_identity(self) -> "GeneratedPlanProposal":
        payload = serialize_market_validation_plan_proposal(self.proposal)
        if hashlib.sha256(payload).hexdigest() != self.proposal_sha256:
            raise ValueError("proposal_sha256 must match canonical proposal bytes")
        if len(payload) != self.canonical_byte_size:
            raise ValueError("canonical_byte_size must match proposal bytes")
        return self


class PersistedGeneratedPlanProposal(StrictDataModel):
    """Metadata for one immutable canonical proposal JSON file."""

    output_path: Path
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: NonEmptyString
    model: NonEmptyString
    proposal: MarketValidationPlanProposal

    @model_validator(mode="after")
    def validate_persisted_identity(self) -> "PersistedGeneratedPlanProposal":
        payload = serialize_market_validation_plan_proposal(self.proposal)
        if len(payload) != self.byte_size:
            raise ValueError("byte_size must match canonical proposal bytes")
        if hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ValueError("sha256 must match canonical proposal bytes")
        return self


def _fail(
    code: PlanProposalProviderErrorCode,
    stage: PlanProposalProviderStage,
    message: str,
) -> NoReturn:
    raise PlanProposalProviderError(
        PlanProposalProviderFailure(code=code, stage=stage, message=message)
    )


def create_plan_proposal_provider(
    provider: str,
    model: str,
    *,
    environment: Mapping[str, str] | None = None,
    transport: DeepSeekPlanTransport | None = None,
    allow_network: bool = False,
) -> PlanProposalProvider:
    """Create only an explicitly selected supported proposal provider."""

    if provider != "deepseek_api":
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
            PlanProposalProviderStage.PROVIDER_CONFIGURATION,
            "unsupported plan proposal provider",
        )
    return DeepSeekApiPlanProposalProvider(
        model=model,
        environment=environment,
        transport=transport,
        allow_network=allow_network,
    )


def generate_market_validation_plan_proposal(
    provider: PlanProposalProvider,
    original_market_question: str,
) -> GeneratedPlanProposal:
    """Generate, strictly parse, and normalize one untrusted proposal."""

    if (
        not isinstance(original_market_question, str)
        or not original_market_question.strip()
        or "\x00" in original_market_question
    ):
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
            PlanProposalProviderStage.PROVIDER_REQUEST,
            "original market question must be non-empty text without NUL bytes",
        )
    normalized_question = original_market_question.strip()
    request = PlanProposalGenerationRequest(
        original_market_question=normalized_question,
        system_prompt=market_validation_plan_proposal_system_prompt(),
        output_schema=market_validation_plan_proposal_json_schema(),
        supported_capabilities={
            name: SUPPORTED_CAPABILITY_DESCRIPTIONS[name]
            for name in SUPPORTED_PLANNING_CAPABILITIES
        },
    )
    try:
        raw = provider.generate_proposal(request)
    except PlanProposalProviderError:
        raise
    except TimeoutError:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_TIMEOUT,
            PlanProposalProviderStage.PROVIDER_REQUEST,
            "plan proposal provider timed out",
        )
    except Exception:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
            PlanProposalProviderStage.PROVIDER_REQUEST,
            "plan proposal provider request failed",
        )
    if not isinstance(raw, RawPlanProposalResponse):
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
            PlanProposalProviderStage.PROVIDER_REQUEST,
            "plan proposal provider returned an invalid response envelope",
        )
    if raw.provider != provider.name or raw.model != provider.model:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
            PlanProposalProviderStage.PROVIDER_REQUEST,
            "plan proposal provider response identity did not match the request",
        )
    try:
        proposal = parse_market_validation_plan_proposal(raw.content)
    except PlanProposalError:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_INVALID_PROPOSAL,
            PlanProposalProviderStage.PROPOSAL_PARSING,
            "provider output failed strict MarketValidationPlanProposal parsing",
        )
    if proposal.original_market_question != normalized_question:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_INVALID_PROPOSAL,
            PlanProposalProviderStage.PROPOSAL_PARSING,
            "provider proposal did not preserve the original market question exactly",
        )
    canonical = serialize_market_validation_plan_proposal(proposal)
    return GeneratedPlanProposal(
        provider=raw.provider,
        model=raw.model,
        proposal_sha256=calculate_plan_proposal_sha256(proposal),
        canonical_byte_size=len(canonical),
        proposal=proposal,
    )


def _safe_output_path(output_path: str | Path) -> Path:
    path = Path(output_path)
    if any(part == os.pardir for part in path.parts):
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "proposal output path must not contain path traversal",
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "proposal output path cannot be inspected safely",
            )
        if exists and candidate.is_symlink():
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "proposal output path must not contain symbolic links",
            )
    parent = path.parent.resolve(strict=False)
    if not parent.exists() or not parent.is_dir():
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "proposal output parent directory must already exist",
        )
    return parent / path.name


def _temporary_output(parent: Path) -> Path:
    for _ in range(100):
        candidate = parent / f".plan-proposal-tmp-{secrets.token_hex(8)}"
        try:
            with candidate.open("xb"):
                pass
        except FileExistsError:
            continue
        return candidate
    _fail(
        PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
        PlanProposalProviderStage.PROPOSAL_OUTPUT,
        "could not allocate a temporary proposal output file",
    )


def validate_plan_proposal_output_path(output_path: str | Path) -> Path:
    """Preflight an output location before any potentially billable request."""

    target = _safe_output_path(output_path)
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output must not be a symbolic link",
            )
        try:
            mode = target.lstat().st_mode
        except OSError:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output cannot be inspected safely",
            )
        if not stat.S_ISREG(mode):
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output must be a regular file",
            )
    return target


def _publish_new_output(temporary: Path, target: Path) -> None:
    os.link(temporary, target, follow_symlinks=False)
    temporary.unlink()


def persist_generated_plan_proposal(
    generated: GeneratedPlanProposal,
    output_path: str | Path,
) -> PersistedGeneratedPlanProposal:
    """Atomically create canonical proposal JSON without overwriting content."""

    if not isinstance(generated, GeneratedPlanProposal):
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "generated proposal must be strictly validated before persistence",
        )
    payload = serialize_market_validation_plan_proposal(generated.proposal)
    if hashlib.sha256(payload).hexdigest() != generated.proposal_sha256:
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "generated proposal identity changed before persistence",
        )
    target = _safe_output_path(output_path)
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output must not be a symbolic link",
            )
        try:
            mode = target.lstat().st_mode
            existing = target.read_bytes()
        except OSError:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output cannot be inspected safely",
            )
        if not stat.S_ISREG(mode):
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal output must be a regular file",
            )
        if existing != payload:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_CONFLICT,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "existing proposal differs and was not overwritten",
            )
        return PersistedGeneratedPlanProposal(
            output_path=target,
            byte_size=len(existing),
            sha256=hashlib.sha256(existing).hexdigest(),
            provider=generated.provider,
            model=generated.model,
            proposal=generated.proposal,
        )

    temporary = _temporary_output(target.parent)
    try:
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _publish_new_output(temporary, target)
        except FileExistsError:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_CONFLICT,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "proposal output appeared concurrently and was not overwritten",
            )
        except OSError:
            _fail(
                PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
                PlanProposalProviderStage.PROPOSAL_OUTPUT,
                "proposal could not be published atomically",
            )
    finally:
        if temporary.exists():
            temporary.unlink()

    try:
        written = target.read_bytes()
        restored = parse_market_validation_plan_proposal(written)
    except (OSError, PlanProposalError):
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "published proposal failed strict verification",
        )
    if written != payload or restored != generated.proposal:
        _fail(
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
            PlanProposalProviderStage.PROPOSAL_OUTPUT,
            "published proposal bytes failed deterministic verification",
        )
    return PersistedGeneratedPlanProposal(
        output_path=target,
        byte_size=len(written),
        sha256=hashlib.sha256(written).hexdigest(),
        provider=generated.provider,
        model=generated.model,
        proposal=restored,
    )


__all__ = [
    "GeneratedPlanProposal",
    "PersistedGeneratedPlanProposal",
    "SUPPORTED_CAPABILITY_DESCRIPTIONS",
    "SUPPORTED_PLAN_PROPOSAL_PROVIDERS",
    "create_plan_proposal_provider",
    "generate_market_validation_plan_proposal",
    "persist_generated_plan_proposal",
    "validate_plan_proposal_output_path",
]
