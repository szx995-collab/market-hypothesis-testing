"""Provider-neutral boundary for generating untrusted plan proposals only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

from market_validator.data.models import NonEmptyString, StrictDataModel


class PlanProposalProviderErrorCode(StrEnum):
    PROVIDER_CONFIGURATION_MISSING = "provider_configuration_missing"
    PROVIDER_REQUEST_FAILED = "provider_request_failed"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_REFUSED = "provider_refused"
    PROVIDER_INVALID_PROPOSAL = "provider_invalid_proposal"
    PROPOSAL_OUTPUT_CONFLICT = "proposal_output_conflict"
    PROPOSAL_OUTPUT_ERROR = "proposal_output_error"


class PlanProposalProviderStage(StrEnum):
    PROVIDER_CONFIGURATION = "provider_configuration"
    PROVIDER_REQUEST = "provider_request"
    PROPOSAL_PARSING = "proposal_parsing"
    PROPOSAL_OUTPUT = "proposal_output"


class PlanProposalProviderFailure(StrictDataModel):
    code: PlanProposalProviderErrorCode
    stage: PlanProposalProviderStage
    message: NonEmptyString


class PlanProposalProviderError(RuntimeError):
    """Safe structured provider failure that never contains raw credentials."""

    def __init__(self, failure: PlanProposalProviderFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")


@dataclass(frozen=True, slots=True)
class PlanProposalGenerationRequest:
    """The complete and deliberately narrow information sent to a provider."""

    original_market_question: str
    system_prompt: str
    output_schema: Mapping[str, Any]
    supported_capabilities: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RawPlanProposalResponse:
    """Untrusted raw model text; consumers must pass it to the strict parser."""

    content: bytes
    provider: str
    model: str


@runtime_checkable
class PlanProposalProvider(Protocol):
    """A provider may generate proposal text, but cannot confirm or execute it."""

    @property
    def name(self) -> str:
        """Return the stable provider adapter name."""

    @property
    def model(self) -> str:
        """Return the explicitly selected model identifier."""

    def generate_proposal(
        self,
        request: PlanProposalGenerationRequest,
    ) -> RawPlanProposalResponse:
        """Return untrusted raw bytes without parsing or executing them."""


__all__ = [
    "PlanProposalGenerationRequest",
    "PlanProposalProvider",
    "PlanProposalProviderError",
    "PlanProposalProviderErrorCode",
    "PlanProposalProviderFailure",
    "PlanProposalProviderStage",
    "RawPlanProposalResponse",
]
