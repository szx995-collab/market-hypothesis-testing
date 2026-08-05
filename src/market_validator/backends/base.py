"""Provider-independent contracts for structured model generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


class BackendNotImplementedError(RuntimeError):
    """Raised when a configured backend has no invocation implementation yet."""


class StructuredGenerationErrorCode(StrEnum):
    """Stable, provider-neutral failure categories for one generation request."""

    CONFIGURATION_MISSING = "provider_configuration_missing"
    REQUEST_FAILED = "provider_request_failed"
    TIMEOUT = "provider_timeout"
    REFUSED = "provider_refused"
    INVALID_RESPONSE = "provider_invalid_proposal"


class StructuredGenerationBackendError(RuntimeError):
    """Sanitized backend failure that never includes credentials or raw bodies."""

    def __init__(self, code: StructuredGenerationErrorCode, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class StructuredGenerationRequest:
    """Input shared by every future structured-generation backend."""

    system_prompt: str
    user_prompt: str
    output_schema: Mapping[str, Any]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class StructuredGenerationResult:
    """Provider-independent structured result returned by a backend."""

    data: Mapping[str, Any]
    backend: str
    model: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """A non-secret-bearing readiness snapshot for one backend."""

    name: str
    available: bool
    configured: bool
    ready: bool
    reason: str
    authentication_method: str = "unknown"


@runtime_checkable
class StructuredGenerationBackend(Protocol):
    """The only model-backend interface that core research code may depend on."""

    def status(self) -> BackendStatus:
        """Return an offline-safe readiness snapshot."""

    def generate(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        """Produce schema-conforming structured data."""
