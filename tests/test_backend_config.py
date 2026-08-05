"""Tests for backend selection and environment-derived configuration."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from market_validator.backends.config import (
    BackendConfigurationError,
    get_selected_backend_name,
)
from market_validator.backends.deepseek_api import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekApiBackend,
)


class BackendSelectionTest(unittest.TestCase):
    def test_default_backend_is_codex_plus(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_selected_backend_name(), "codex_plus")

    def test_both_supported_backends_can_be_selected(self) -> None:
        for backend_name in ("codex_plus", "deepseek_api"):
            with self.subTest(backend_name=backend_name):
                environment = {"MARKET_VALIDATOR_BACKEND": backend_name}
                self.assertEqual(
                    get_selected_backend_name(environment), backend_name
                )

    def test_invalid_backend_has_clear_error(self) -> None:
        with self.assertRaisesRegex(
            BackendConfigurationError, "Invalid MARKET_VALIDATOR_BACKEND"
        ):
            get_selected_backend_name({"MARKET_VALIDATOR_BACKEND": "unsupported"})

    def test_deepseek_uses_safe_defaults(self) -> None:
        backend = DeepSeekApiBackend({})
        self.assertEqual(backend.base_url, DEFAULT_BASE_URL)
        self.assertEqual(backend.model, DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
