"""Provider-neutral, non-persistent credential resolution."""

from __future__ import annotations

from collections.abc import Mapping
import os

from pydantic import SecretStr

from market_validator.credentials.models import (
    CredentialInputCancelledError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialSource,
    CredentialSpec,
    InteractivePromptUnavailableError,
    PromptStatus,
    ResolvedCredential,
)
from market_validator.credentials.prompt import (
    CredentialPrompt,
    TerminalCredentialPrompt,
    TkCredentialPrompt,
)


class CredentialResolver:
    """Resolve environment, memory, GUI, then terminal credentials in order."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        gui_prompt: CredentialPrompt | None = None,
        terminal_prompt: CredentialPrompt | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._memory: dict[str, ResolvedCredential] = {}
        self._gui_prompt = gui_prompt or TkCredentialPrompt()
        self._terminal_prompt = terminal_prompt or TerminalCredentialPrompt()

    @staticmethod
    def _invalid_message(spec: CredentialSpec) -> str:
        return f"{spec.display_name} has an invalid format"

    def remember(self, spec: CredentialSpec, candidate: str) -> ResolvedCredential:
        """Validate and retain a credential only for this resolver process."""
        if not spec.accepts(candidate):
            raise CredentialInvalidError(self._invalid_message(spec))
        resolved = ResolvedCredential(
            credential_id=spec.credential_id,
            provider_id=spec.provider_id,
            source=CredentialSource.MEMORY,
            secret=SecretStr(candidate),
        )
        self._memory[spec.credential_id] = resolved
        return resolved

    def forget(self, credential_id: str) -> None:
        self._memory.pop(credential_id, None)

    def configured(self, spec: CredentialSpec) -> bool:
        candidate = self._environment.get(spec.environment_variable)
        if candidate is not None:
            return spec.accepts(candidate)
        return spec.credential_id in self._memory

    def resolve(
        self, spec: CredentialSpec, *, interactive: bool = False
    ) -> ResolvedCredential:
        environment_value = self._environment.get(spec.environment_variable)
        if environment_value is not None:
            if not spec.accepts(environment_value):
                raise CredentialInvalidError(self._invalid_message(spec))
            return ResolvedCredential(
                credential_id=spec.credential_id,
                provider_id=spec.provider_id,
                source=CredentialSource.ENVIRONMENT,
                secret=SecretStr(environment_value),
            )

        remembered = self._memory.get(spec.credential_id)
        if remembered is not None:
            return remembered

        if not interactive:
            raise CredentialMissingError(
                f"{spec.display_name} is required for live access"
            )

        gui_result = self._gui_prompt.request(spec)
        if gui_result.status is PromptStatus.SUBMITTED:
            resolved = ResolvedCredential(
                credential_id=spec.credential_id,
                provider_id=spec.provider_id,
                source=CredentialSource.INTERACTIVE_GUI,
                secret=gui_result.secret,
            )
            self._memory[spec.credential_id] = resolved
            return resolved
        if gui_result.status is PromptStatus.INVALID:
            raise CredentialInvalidError(self._invalid_message(spec))
        if gui_result.status is PromptStatus.CANCELLED:
            raise CredentialInputCancelledError("credential input was cancelled")

        terminal_result = self._terminal_prompt.request(spec)
        if terminal_result.status is PromptStatus.SUBMITTED:
            resolved = ResolvedCredential(
                credential_id=spec.credential_id,
                provider_id=spec.provider_id,
                source=CredentialSource.INTERACTIVE_TERMINAL,
                secret=terminal_result.secret,
            )
            self._memory[spec.credential_id] = resolved
            return resolved
        if terminal_result.status is PromptStatus.INVALID:
            raise CredentialInvalidError(self._invalid_message(spec))
        if terminal_result.status is PromptStatus.CANCELLED:
            raise CredentialInputCancelledError("credential input was cancelled")
        raise InteractivePromptUnavailableError(
            "no secure interactive credential prompt is available"
        )
