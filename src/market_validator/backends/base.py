"""Provider-independent contracts for structured model generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class BackendNotImplementedError(RuntimeError):
    """Raised when a configured backend has no invocation implementation yet."""


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
