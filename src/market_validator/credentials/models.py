"""Provider-neutral credential descriptions and secret-safe result models."""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class StrictCredentialModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SecretKind(StrEnum):
    API_KEY = "api_key"


class CredentialSource(StrEnum):
    ENVIRONMENT = "environment"
    MEMORY = "memory"
    INTERACTIVE_GUI = "interactive_gui"
    INTERACTIVE_TERMINAL = "interactive_terminal"


class PromptStatus(StrEnum):
    SUBMITTED = "submitted"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class CredentialSpec(StrictCredentialModel):
    """Public metadata for one credential; never contains its value."""

    credential_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    display_name: str = Field(min_length=1)
    environment_variable: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    validation_pattern: str = Field(min_length=1)
    help_text: str = Field(min_length=1)
    secret_kind: SecretKind

    @model_validator(mode="after")
    def validate_pattern(self) -> Self:
        try:
            re.compile(self.validation_pattern)
        except re.error as error:
            raise ValueError("validation_pattern must be a valid regular expression") from error
        return self

    def accepts(self, candidate: str) -> bool:
        """Validate a raw candidate without retaining or describing it."""
        return re.fullmatch(self.validation_pattern, candidate) is not None


class PromptResult(StrictCredentialModel):
    """Secret-safe result from an interactive prompt implementation."""

    status: PromptStatus
    secret: SecretStr | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status is PromptStatus.SUBMITTED and self.secret is None:
            raise ValueError("submitted prompt results require a secret")
        if self.status is not PromptStatus.SUBMITTED and self.secret is not None:
            raise ValueError("only submitted prompt results may contain a secret")
        return self


class ResolvedCredential(StrictCredentialModel):
    """A process-local credential whose normal representations stay redacted."""

    credential_id: str
    provider_id: str
    source: CredentialSource
    secret: SecretStr


class CredentialResolutionError(RuntimeError):
    """Structured, secret-free credential resolution failure."""

    code = "credential_resolution_error"

    def __init__(self, message: str) -> None:
        self.sanitized_message = message
        super().__init__(message)

    def public_error(self) -> dict[str, object]:
        return {"code": self.code, "message": self.sanitized_message}


class CredentialMissingError(CredentialResolutionError):
    code = "credential_missing"


class CredentialInvalidError(CredentialResolutionError):
    code = "credential_invalid"


class CredentialInputCancelledError(CredentialResolutionError):
    code = "credential_input_cancelled"


class InteractivePromptUnavailableError(CredentialResolutionError):
    code = "interactive_prompt_unavailable"
