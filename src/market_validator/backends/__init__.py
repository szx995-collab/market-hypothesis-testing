"""Provider-independent structured-generation backend interfaces."""

from market_validator.backends.base import (
    BackendNotImplementedError,
    BackendStatus,
    StructuredGenerationBackend,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from market_validator.backends.codex_plus import CodexPlusBackend
from market_validator.backends.deepseek_api import DeepSeekApiBackend

__all__ = [
    "BackendNotImplementedError",
    "BackendStatus",
    "CodexPlusBackend",
    "DeepSeekApiBackend",
    "StructuredGenerationBackend",
    "StructuredGenerationRequest",
    "StructuredGenerationResult",
]
