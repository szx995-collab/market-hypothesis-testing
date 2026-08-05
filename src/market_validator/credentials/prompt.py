"""Injectable GUI and terminal prompts that never log or persist secrets."""

from __future__ import annotations

from collections.abc import Callable
import getpass
import re
import sys
from typing import Protocol

from pydantic import SecretStr

from market_validator.credentials.models import (
    CredentialSpec,
    PromptResult,
    PromptStatus,
)


class CredentialPrompt(Protocol):
    source_name: str

    def request(self, spec: CredentialSpec) -> PromptResult:
        """Request one credential without persisting or displaying it."""


class TkCredentialPrompt:
    """A bounded, masked tkinter prompt. Importing this module opens no window."""

    source_name = "interactive_gui"

    def __init__(self, *, timeout_milliseconds: int = 300_000) -> None:
        self._timeout_milliseconds = timeout_milliseconds

    def request(self, spec: CredentialSpec) -> PromptResult:
        root = None
        result = PromptResult(status=PromptStatus.UNAVAILABLE)
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title(f"MarketCheckAgent – 需要 {spec.display_name}")
            root.resizable(False, False)

            frame = tk.Frame(root, padx=18, pady=16)
            frame.pack(fill="both", expand=True)
            tk.Label(
                frame,
                text=f"供应商：{spec.display_name}",
                anchor="w",
                justify="left",
            ).pack(fill="x")
            tk.Label(
                frame,
                text=spec.help_text,
                anchor="w",
                justify="left",
                wraplength=440,
            ).pack(fill="x", pady=(6, 10))

            entry = tk.Entry(frame, show="*", width=52)
            entry.pack(fill="x")
            error_text = tk.StringVar(value="")
            tk.Label(
                frame,
                textvariable=error_text,
                foreground="#b00020",
                anchor="w",
            ).pack(fill="x", pady=(6, 4))

            def finish(new_result: PromptResult) -> None:
                nonlocal result
                entry.delete(0, tk.END)
                result = new_result
                root.quit()

            def confirm() -> None:
                candidate = entry.get()
                if re.fullmatch(spec.validation_pattern, candidate) is None:
                    error_text.set("输入格式无效，请按说明重新输入。")
                    entry.focus_set()
                    return
                finish(
                    PromptResult(
                        status=PromptStatus.SUBMITTED,
                        secret=SecretStr(candidate),
                    )
                )

            def cancel() -> None:
                finish(PromptResult(status=PromptStatus.CANCELLED))

            def timeout() -> None:
                finish(PromptResult(status=PromptStatus.UNAVAILABLE))

            buttons = tk.Frame(frame)
            buttons.pack(fill="x", pady=(8, 0))
            tk.Button(buttons, text="确定", width=10, command=confirm).pack(
                side="right"
            )
            tk.Button(buttons, text="取消", width=10, command=cancel).pack(
                side="right", padx=(0, 8)
            )
            root.protocol("WM_DELETE_WINDOW", cancel)
            root.bind("<Return>", lambda _event: confirm())
            root.bind("<Escape>", lambda _event: cancel())
            root.after(self._timeout_milliseconds, timeout)
            root.lift()
            entry.focus_set()
            root.mainloop()
            return result
        except Exception:
            return PromptResult(status=PromptStatus.UNAVAILABLE)
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass


class TerminalCredentialPrompt:
    """Masked getpass fallback used only for an explicitly interactive TTY."""

    source_name = "interactive_terminal"

    def __init__(
        self,
        *,
        getpass_function: Callable[[str], str] = getpass.getpass,
        is_tty: Callable[[], bool] = lambda: bool(sys.stdin.isatty()),
    ) -> None:
        self._getpass = getpass_function
        self._is_tty = is_tty

    def request(self, spec: CredentialSpec) -> PromptResult:
        if not self._is_tty():
            return PromptResult(status=PromptStatus.UNAVAILABLE)
        try:
            candidate = self._getpass(f"请输入 {spec.display_name}（输入已隐藏）: ")
        except (EOFError, KeyboardInterrupt):
            return PromptResult(status=PromptStatus.CANCELLED)
        if re.fullmatch(spec.validation_pattern, candidate) is None:
            return PromptResult(status=PromptStatus.INVALID)
        return PromptResult(
            status=PromptStatus.SUBMITTED,
            secret=SecretStr(candidate),
        )
