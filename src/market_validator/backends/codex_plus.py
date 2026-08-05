"""Configuration-only scaffold for the future local Codex CLI path."""

from __future__ import annotations

import shutil
import subprocess

from market_validator.backends.base import (
    BackendNotImplementedError,
    BackendStatus,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)


class CodexPlusBackend:
    """Diagnose local CLI/login availability without invoking a model."""

    name = "codex_plus"

    def __init__(self, status_timeout_seconds: float = 5.0) -> None:
        self._status_timeout_seconds = status_timeout_seconds

    def status(self) -> BackendStatus:
        """Check CLI presence and bounded ``codex login status`` only."""
        executable = shutil.which("codex")
        if executable is None:
            return BackendStatus(
                name=self.name,
                available=False,
                configured=False,
                ready=False,
                reason="Codex CLI was not found on PATH.",
                authentication_method="unknown",
            )

        try:
            completed = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._status_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return BackendStatus(
                name=self.name,
                available=True,
                configured=False,
                ready=False,
                reason="Codex CLI login status check timed out.",
                authentication_method="unknown",
            )
        except OSError:
            return BackendStatus(
                name=self.name,
                available=True,
                configured=False,
                ready=False,
                reason="Codex CLI login status could not be checked.",
                authentication_method="unknown",
            )

        configured = completed.returncode == 0
        reason = (
            "Codex CLI login is configured; structured generation is not "
            "implemented yet."
            if configured
            else "Codex CLI is installed, but login status was not confirmed."
        )
        return BackendStatus(
            name=self.name,
            available=True,
            configured=configured,
            ready=False,
            reason=reason,
            authentication_method="unknown",
        )

    def generate(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        """Refuse model invocation until a later implementation step."""
        raise BackendNotImplementedError(
            "Codex Plus structured generation is not implemented; the future "
            "implementation will use local codex exec --output-schema."
        )
