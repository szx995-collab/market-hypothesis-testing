"""Configuration-only scaffold for the future DeepSeek HTTP API path."""

from __future__ import annotations

import os
from collections.abc import Mapping

from market_validator.backends.base import (
    BackendNotImplementedError,
    BackendStatus,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


class DeepSeekApiBackend:
    """Expose safe configuration diagnostics without contacting DeepSeek."""

    name = "deepseek_api"

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment
        self.base_url = self._environment.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        self.model = self._environment.get("DEEPSEEK_MODEL", DEFAULT_MODEL)

    def status(self) -> BackendStatus:
        """Check only whether required environment configuration is present."""
        configured = bool(self._environment.get("DEEPSEEK_API_KEY"))
        if configured:
            reason = (
                "DEEPSEEK_API_KEY is set; actual API generation is not "
                "implemented yet."
            )
        else:
            reason = (
                "DEEPSEEK_API_KEY is not set; actual API generation is not "
                "implemented yet."
            )
        return BackendStatus(
            name=self.name,
            available=True,
            configured=configured,
            ready=False,
            reason=reason,
            authentication_method="api_key",
        )

    def generate(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        """Refuse model invocation until a later implementation step."""
        raise BackendNotImplementedError(
            "DeepSeek API structured generation is not implemented; "
            "the actual API call will be added in a later step."
        )
