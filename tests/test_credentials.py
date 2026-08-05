"""Offline tests for provider-neutral, non-persistent credential resolution."""

from __future__ import annotations

import unittest

from pydantic import SecretStr

from market_validator.credentials import (
    CredentialInputCancelledError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialResolver,
    CredentialSource,
    CredentialSpec,
    InteractivePromptUnavailableError,
    PromptResult,
    PromptStatus,
    SecretKind,
    TerminalCredentialPrompt,
)

SENTINEL_KEY = "m3" * 16


def fred_spec() -> CredentialSpec:
    return CredentialSpec(
        credential_id="fred.api_key",
        provider_id="fred",
        display_name="FRED API Key",
        environment_variable="FRED_API_KEY",
        validation_pattern=r"^[a-z0-9]{32}$",
        help_text="Offline test credential.",
        secret_kind=SecretKind.API_KEY,
    )


class FakeCredentialPrompt:
    source_name = "fake"

    def __init__(self, result: PromptResult) -> None:
        self.result = result
        self.calls = 0

    def request(self, spec: CredentialSpec) -> PromptResult:
        self.calls += 1
        return self.result


def unavailable_prompt() -> FakeCredentialPrompt:
    return FakeCredentialPrompt(PromptResult(status=PromptStatus.UNAVAILABLE))


class CredentialResolverTest(unittest.TestCase):
    def test_environment_has_priority_and_prompt_is_not_called(self) -> None:
        gui = FakeCredentialPrompt(PromptResult(status=PromptStatus.CANCELLED))
        terminal = unavailable_prompt()
        resolver = CredentialResolver(
            environment={"FRED_API_KEY": SENTINEL_KEY},
            gui_prompt=gui,
            terminal_prompt=terminal,
        )
        resolved = resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(resolved.source, CredentialSource.ENVIRONMENT)
        self.assertEqual(gui.calls, 0)
        self.assertEqual(terminal.calls, 0)

    def test_memory_credential_precedes_interactive_prompts(self) -> None:
        gui = FakeCredentialPrompt(PromptResult(status=PromptStatus.CANCELLED))
        resolver = CredentialResolver(
            environment={}, gui_prompt=gui, terminal_prompt=unavailable_prompt()
        )
        resolver.remember(fred_spec(), SENTINEL_KEY)
        resolved = resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(resolved.source, CredentialSource.MEMORY)
        self.assertEqual(gui.calls, 0)

    def test_interactive_missing_key_calls_gui_and_keeps_result_in_memory(self) -> None:
        gui = FakeCredentialPrompt(
            PromptResult(
                status=PromptStatus.SUBMITTED,
                secret=SecretStr(SENTINEL_KEY),
            )
        )
        resolver = CredentialResolver(
            environment={}, gui_prompt=gui, terminal_prompt=unavailable_prompt()
        )
        first = resolver.resolve(fred_spec(), interactive=True)
        second = resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(first.source, CredentialSource.INTERACTIVE_GUI)
        self.assertEqual(second, first)
        self.assertEqual(gui.calls, 1)

    def test_noninteractive_missing_key_never_calls_prompt(self) -> None:
        gui = FakeCredentialPrompt(PromptResult(status=PromptStatus.CANCELLED))
        terminal = unavailable_prompt()
        resolver = CredentialResolver(
            environment={}, gui_prompt=gui, terminal_prompt=terminal
        )
        with self.assertRaises(CredentialMissingError):
            resolver.resolve(fred_spec(), interactive=False)
        self.assertEqual(gui.calls, 0)
        self.assertEqual(terminal.calls, 0)

    def test_cancel_and_invalid_results_are_structured(self) -> None:
        cases = (
            (PromptStatus.CANCELLED, CredentialInputCancelledError),
            (PromptStatus.INVALID, CredentialInvalidError),
        )
        for status, error_type in cases:
            with self.subTest(status=status):
                resolver = CredentialResolver(
                    environment={},
                    gui_prompt=FakeCredentialPrompt(PromptResult(status=status)),
                    terminal_prompt=unavailable_prompt(),
                )
                with self.assertRaises(error_type) as caught:
                    resolver.resolve(fred_spec(), interactive=True)
                self.assertEqual(caught.exception.code, error_type.code)
                self.assertNotIn(SENTINEL_KEY, str(caught.exception))

    def test_invalid_environment_does_not_fall_through_to_prompt(self) -> None:
        gui = FakeCredentialPrompt(
            PromptResult(
                status=PromptStatus.SUBMITTED,
                secret=SecretStr(SENTINEL_KEY),
            )
        )
        resolver = CredentialResolver(
            environment={"FRED_API_KEY": "invalid"},
            gui_prompt=gui,
            terminal_prompt=unavailable_prompt(),
        )
        with self.assertRaises(CredentialInvalidError):
            resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(gui.calls, 0)

    def test_gui_unavailable_falls_back_to_terminal(self) -> None:
        terminal = FakeCredentialPrompt(
            PromptResult(
                status=PromptStatus.SUBMITTED,
                secret=SecretStr(SENTINEL_KEY),
            )
        )
        resolver = CredentialResolver(
            environment={},
            gui_prompt=unavailable_prompt(),
            terminal_prompt=terminal,
        )
        resolved = resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(resolved.source, CredentialSource.INTERACTIVE_TERMINAL)
        self.assertEqual(terminal.calls, 1)

    def test_both_prompts_unavailable_is_structured(self) -> None:
        resolver = CredentialResolver(
            environment={},
            gui_prompt=unavailable_prompt(),
            terminal_prompt=unavailable_prompt(),
        )
        with self.assertRaises(InteractivePromptUnavailableError) as caught:
            resolver.resolve(fred_spec(), interactive=True)
        self.assertEqual(caught.exception.code, "interactive_prompt_unavailable")

    def test_terminal_prompt_uses_injected_getpass_only_for_tty(self) -> None:
        calls: list[str] = []

        def fake_getpass(prompt: str) -> str:
            calls.append(prompt)
            return SENTINEL_KEY

        available = TerminalCredentialPrompt(
            getpass_function=fake_getpass, is_tty=lambda: True
        ).request(fred_spec())
        self.assertEqual(available.status, PromptStatus.SUBMITTED)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(SENTINEL_KEY, calls[0])

        calls.clear()
        unavailable = TerminalCredentialPrompt(
            getpass_function=fake_getpass, is_tty=lambda: False
        ).request(fred_spec())
        self.assertEqual(unavailable.status, PromptStatus.UNAVAILABLE)
        self.assertEqual(calls, [])

    def test_secret_representations_and_json_are_redacted(self) -> None:
        resolver = CredentialResolver(
            environment={"FRED_API_KEY": SENTINEL_KEY},
            gui_prompt=unavailable_prompt(),
            terminal_prompt=unavailable_prompt(),
        )
        resolved = resolver.resolve(fred_spec())
        representations = (
            repr(resolved.secret),
            str(resolved.secret),
            repr(resolved),
            str(resolved),
            resolved.model_dump_json(),
        )
        for rendered in representations:
            self.assertNotIn(SENTINEL_KEY, rendered)
            self.assertNotIn(SENTINEL_KEY[:4], rendered)
            self.assertNotIn(SENTINEL_KEY[-4:], rendered)


if __name__ == "__main__":
    unittest.main()
