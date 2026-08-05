"""Offline tests for backend status and invocation guards."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import subprocess
import unittest
from unittest.mock import patch

from market_validator.backends.base import (
    BackendNotImplementedError,
    StructuredGenerationRequest,
)
from market_validator.backends.codex_plus import CodexPlusBackend
from market_validator.backends.deepseek_api import DeepSeekApiBackend
from market_validator.cli import main

FAKE_API_KEY = "fake-secret-key-that-must-not-appear"


def _request() -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        system_prompt="system",
        user_prompt="user",
        output_schema={"type": "object"},
        timeout_seconds=1.0,
    )


class DeepSeekStatusTest(unittest.TestCase):
    def test_missing_key_is_not_ready(self) -> None:
        status = DeepSeekApiBackend({}).status()
        self.assertTrue(status.available)
        self.assertFalse(status.configured)
        self.assertFalse(status.ready)
        self.assertIn("DEEPSEEK_API_KEY is not set", status.reason)

    def test_fake_key_is_never_exposed(self) -> None:
        backend = DeepSeekApiBackend({"DEEPSEEK_API_KEY": FAKE_API_KEY})
        serialized = json.dumps(asdict(backend.status()))
        self.assertTrue(backend.status().configured)
        self.assertFalse(backend.status().ready)
        self.assertNotIn(FAKE_API_KEY, serialized)

    def test_generate_is_explicitly_not_implemented(self) -> None:
        with self.assertRaisesRegex(
            BackendNotImplementedError, "actual API call will be added"
        ):
            DeepSeekApiBackend({}).generate(_request())


class CodexStatusTest(unittest.TestCase):
    @patch("market_validator.backends.codex_plus.subprocess.run")
    @patch("market_validator.backends.codex_plus.shutil.which", return_value=None)
    def test_missing_cli_is_not_ready(self, _which, run) -> None:
        status = CodexPlusBackend().status()
        self.assertFalse(status.available)
        self.assertFalse(status.configured)
        self.assertFalse(status.ready)
        self.assertEqual(status.authentication_method, "unknown")
        run.assert_not_called()

    @patch("market_validator.backends.codex_plus.subprocess.run")
    @patch(
        "market_validator.backends.codex_plus.shutil.which",
        return_value="C:\\Tools\\codex.exe",
    )
    def test_login_status_is_bounded_and_auth_method_is_unknown(
        self, _which, run
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["codex", "login", "status"], returncode=0
        )
        status = CodexPlusBackend(status_timeout_seconds=2.0).status()
        self.assertTrue(status.available)
        self.assertTrue(status.configured)
        self.assertFalse(status.ready)
        self.assertEqual(status.authentication_method, "unknown")
        run.assert_called_once_with(
            ["C:\\Tools\\codex.exe", "login", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )

    @patch("market_validator.backends.codex_plus.subprocess.run")
    @patch(
        "market_validator.backends.codex_plus.shutil.which",
        return_value="C:\\Tools\\codex.exe",
    )
    def test_login_status_timeout_is_safe(self, _which, run) -> None:
        run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=1.0)
        status = CodexPlusBackend(status_timeout_seconds=1.0).status()
        self.assertFalse(status.configured)
        self.assertFalse(status.ready)
        self.assertIn("timed out", status.reason)

    def test_generate_is_explicitly_not_implemented(self) -> None:
        with self.assertRaisesRegex(
            BackendNotImplementedError, "codex exec --output-schema"
        ):
            CodexPlusBackend().generate(_request())


class BackendCommandTest(unittest.TestCase):
    @patch("market_validator.backends.codex_plus.shutil.which", return_value=None)
    def test_command_returns_json_without_fake_key(self, _which) -> None:
        environment = {
            "MARKET_VALIDATOR_BACKEND": "deepseek_api",
            "DEEPSEEK_API_KEY": FAKE_API_KEY,
        }
        output = StringIO()
        with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
            exit_code = main(["backends"])

        rendered = output.getvalue()
        payload = json.loads(rendered)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["selected_backend"], "deepseek_api")
        self.assertEqual(set(payload["backends"]), {"codex_plus", "deepseek_api"})
        self.assertNotIn(FAKE_API_KEY, rendered)


if __name__ == "__main__":
    unittest.main()
