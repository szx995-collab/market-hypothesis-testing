"""DeepSeek structured-generation backend with explicit network authorization."""

from __future__ import annotations

import os
from collections.abc import Mapping
import json
import math

from market_validator.backends.base import (
    BackendNotImplementedError,
    BackendStatus,
    StructuredGenerationBackendError,
    StructuredGenerationErrorCode,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from market_validator.deepseek_transport import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    DeepSeekTransport,
    DeepSeekTransportError,
    DeepSeekTransportTimeout,
    UrllibDeepSeekTransport,
    validated_deepseek_endpoint,
)

DEFAULT_BASE_URL = DEFAULT_DEEPSEEK_BASE_URL
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = 8192


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(value)


class DeepSeekApiBackend:
    """Expose safe configuration diagnostics without contacting DeepSeek."""

    name = "deepseek_api"

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        model: str | None = None,
        transport: DeepSeekTransport | None = None,
        timeout_seconds: float = DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
        allow_network: bool = False,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self.base_url = self._environment.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or self._environment.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self._transport = transport or UrllibDeepSeekTransport()
        self._timeout_seconds = timeout_seconds
        self._allow_network = allow_network

    def status(self) -> BackendStatus:
        """Check only whether required environment configuration is present."""
        configured = bool(self._environment.get("DEEPSEEK_API_KEY"))
        if configured and self._allow_network:
            try:
                validated_deepseek_endpoint(self.base_url)
            except ValueError:
                return BackendStatus(
                    name=self.name,
                    available=True,
                    configured=True,
                    ready=False,
                    reason="DeepSeek HTTPS endpoint configuration is invalid.",
                    authentication_method="api_key",
                )
            reason = "DeepSeek is configured for one explicitly authorized request."
            ready = True
        elif configured:
            reason = (
                "DEEPSEEK_API_KEY is set; network generation is disabled by default."
            )
            ready = False
        else:
            reason = (
                "DEEPSEEK_API_KEY is not set; network generation is not ready."
            )
            ready = False
        return BackendStatus(
            name=self.name,
            available=True,
            configured=configured,
            ready=ready,
            reason=reason,
            authentication_method="api_key",
        )

    def _configuration(self) -> tuple[str, str]:
        api_key = self._environment.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.CONFIGURATION_MISSING,
                "DEEPSEEK_API_KEY is not configured",
            )
        try:
            endpoint = validated_deepseek_endpoint(self.base_url)
        except ValueError:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.CONFIGURATION_MISSING,
                "DeepSeek endpoint must be the credential-free official HTTPS origin",
            ) from None
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or any(character.isspace() or ord(character) < 32 for character in self.model)
        ):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.CONFIGURATION_MISSING,
                "an explicit non-empty DeepSeek model identifier is required",
            )
        return api_key, endpoint

    @staticmethod
    def _request_body(
        request: StructuredGenerationRequest,
        model: str,
    ) -> bytes:
        schema_json = json.dumps(
            request.output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload = {
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        request.system_prompt
                        + "\n\nRequired JSON Schema:\n"
                        + schema_json
                    ),
                },
                {"role": "user", "content": request.user_prompt},
            ],
            "model": model,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _extract_data(response_bytes: bytes) -> tuple[dict[str, object], str | None]:
        try:
            envelope = json.loads(response_bytes.decode("utf-8", errors="strict"))
            choices = envelope["choices"]
            choice = choices[0]
            message = choice["message"]
        except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek returned an invalid response envelope",
            ) from None
        if not isinstance(envelope, dict) or not isinstance(choices, list) or len(choices) != 1:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek returned an invalid response envelope",
            )
        if not isinstance(choice, dict) or not isinstance(message, dict):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek returned an invalid response envelope",
            )
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter" or message.get("refusal"):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REFUSED,
                "DeepSeek declined to generate the proposal",
            )
        if finish_reason == "length":
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek proposal output was truncated",
            )
        if finish_reason not in {None, "stop"}:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek did not complete a normal structured response",
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek returned empty structured content",
            )
        try:
            decoded = json.loads(
                content,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonstandard_number,
            )
        except (json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.INVALID_RESPONSE,
                "DeepSeek returned a non-strict JSON proposal",
            ) from None
        if not isinstance(decoded, dict):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.INVALID_RESPONSE,
                "DeepSeek proposal must be a JSON object",
            )
        return decoded, finish_reason

    def generate(
        self, request: StructuredGenerationRequest
    ) -> StructuredGenerationResult:
        """Perform at most one explicitly authorized structured request."""
        if self._allow_network is not True:
            raise BackendNotImplementedError(
                "DeepSeek API structured generation is disabled by default; the "
                "actual API call will be added only when allow_network=True."
            )
        if (
            not isinstance(request, StructuredGenerationRequest)
            or not request.system_prompt.strip()
            or not request.user_prompt.strip()
            or not isinstance(request.output_schema, Mapping)
            or not math.isfinite(request.timeout_seconds)
            or request.timeout_seconds <= 0
            or not math.isfinite(self._timeout_seconds)
            or self._timeout_seconds <= 0
        ):
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.CONFIGURATION_MISSING,
                "structured generation request or timeout is invalid",
            )
        api_key, endpoint = self._configuration()
        body = self._request_body(request, self.model)
        try:
            response = self._transport.post_json(
                url=endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout_seconds=min(request.timeout_seconds, self._timeout_seconds),
            )
        except DeepSeekTransportTimeout:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.TIMEOUT,
                "DeepSeek structured generation timed out",
            ) from None
        except DeepSeekTransportError:
            raise StructuredGenerationBackendError(
                StructuredGenerationErrorCode.REQUEST_FAILED,
                "DeepSeek structured generation request failed",
            ) from None
        data, finish_reason = self._extract_data(response)
        return StructuredGenerationResult(
            data=data,
            backend=self.name,
            model=self.model,
            metadata={"finish_reason": finish_reason or "unknown"},
        )
