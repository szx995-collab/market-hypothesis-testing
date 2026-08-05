"""Backend selection from process configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_BACKEND = "codex_plus"
SUPPORTED_BACKENDS = frozenset({"codex_plus", "deepseek_api"})


class BackendConfigurationError(ValueError):
    """Raised when backend selection is not supported."""


def get_selected_backend_name(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return and validate the selected backend name."""
    source = os.environ if environment is None else environment
    selected = source.get("MARKET_VALIDATOR_BACKEND", DEFAULT_BACKEND).strip()
    if selected not in SUPPORTED_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise BackendConfigurationError(
            "Invalid MARKET_VALIDATOR_BACKEND "
            f"{selected!r}; expected one of: {supported}."
        )
    return selected
