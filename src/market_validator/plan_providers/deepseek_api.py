"""Optional DeepSeek HTTP adapter limited to untrusted proposal generation."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import os
import socket
from typing import NoReturn, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from market_validator.backends.deepseek_api import DEFAULT_BASE_URL
from market_validator.plan_providers.base import (
    PlanProposalGenerationRequest,
    PlanProposalProviderError,
    PlanProposalProviderErrorCode,
    PlanProposalProviderFailure,
    PlanProposalProviderStage,
    RawPlanProposalResponse,
)


DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_TOKENS = 8192
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class DeepSeekTransportError(RuntimeError):
    """Sanitized transport error containing no response body or credentials."""


class DeepSeekTransportTimeout(DeepSeekTransportError):
    """The HTTPS request exceeded its configured timeout."""


class DeepSeekPlanTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        """Send one non-streaming JSON request and return response bytes."""


class UrllibDeepSeekPlanTransport:
    """Small standard-library HTTPS transport with sanitized exceptions."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise DeepSeekTransportError(
                        "DeepSeek response exceeded the safe size limit"
                    )
                return payload
        except HTTPError as exc:
            raise DeepSeekTransportError(
                f"DeepSeek returned HTTP status {exc.code}"
            ) from None
        except (TimeoutError, socket.timeout) as exc:
            raise DeepSeekTransportTimeout("DeepSeek request timed out") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise DeepSeekTransportTimeout("DeepSeek request timed out") from exc
            raise DeepSeekTransportError("DeepSeek HTTPS request failed") from None
        except OSError:
            raise DeepSeekTransportError("DeepSeek HTTPS request failed") from None


def _fail(
    code: PlanProposalProviderErrorCode,
    stage: PlanProposalProviderStage,
    message: str,
) -> NoReturn:
    raise PlanProposalProviderError(
        PlanProposalProviderFailure(code=code, stage=stage, message=message)
    )


def _validated_endpoint(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
            PlanProposalProviderStage.PROVIDER_CONFIGURATION,
            "DEEPSEEK_BASE_URL must be a valid HTTPS origin",
        )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
            PlanProposalProviderStage.PROVIDER_CONFIGURATION,
            "DEEPSEEK_BASE_URL must be the credential-free official DeepSeek HTTPS origin",
        )
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        _fail(
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
            PlanProposalProviderStage.PROVIDER_CONFIGURATION,
            "DEEPSEEK_BASE_URL path must be empty or /v1",
        )
    return base_url.rstrip("/") + "/chat/completions"


class DeepSeekApiPlanProposalProvider:
    """Generate raw proposal JSON without tools or execution capabilities."""

    name = "deepseek_api"

    def __init__(
        self,
        *,
        model: str,
        environment: Mapping[str, str] | None = None,
        transport: DeepSeekPlanTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allow_network: bool = False,
    ) -> None:
        if not isinstance(model, str) or not model.strip() or any(
            character.isspace() or ord(character) < 32 for character in model
        ):
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
                PlanProposalProviderStage.PROVIDER_CONFIGURATION,
                "an explicit non-empty DeepSeek model identifier is required",
            )
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
                PlanProposalProviderStage.PROVIDER_CONFIGURATION,
                "provider timeout must be positive",
            )
        self._model = model
        self._environment = os.environ if environment is None else environment
        self._transport = transport or UrllibDeepSeekPlanTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._allow_network = allow_network

    @property
    def model(self) -> str:
        return self._model

    def _configuration(self) -> tuple[str, str]:
        if self._allow_network is not True:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
                PlanProposalProviderStage.PROVIDER_CONFIGURATION,
                "proposal provider network access was not explicitly allowed",
            )
        api_key = self._environment.get("DEEPSEEK_API_KEY")
        if not api_key:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
                PlanProposalProviderStage.PROVIDER_CONFIGURATION,
                "DEEPSEEK_API_KEY is not configured",
            )
        base_url = self._environment.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        return api_key, _validated_endpoint(base_url)

    @staticmethod
    def _request_body(
        request: PlanProposalGenerationRequest,
        model: str,
    ) -> bytes:
        capability_json = json.dumps(
            request.supported_capabilities,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_json = json.dumps(
            request.output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        system_content = (
            request.system_prompt
            + "\n\nSupported capabilities (JSON):\n"
            + capability_json
            + "\n\nRequired proposal JSON Schema:\n"
            + schema_json
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": request.original_market_question},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _extract_content(response_bytes: bytes) -> bytes:
        try:
            decoded = json.loads(response_bytes.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek returned an invalid response envelope",
            )
        if not isinstance(decoded, dict):
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek returned an invalid response envelope",
            )
        choices = decoded.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek returned an invalid response envelope",
            )
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek returned an invalid response envelope",
            )
        message = choice["message"]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter" or message.get("refusal"):
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REFUSED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek declined to generate the proposal",
            )
        if finish_reason == "length":
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek proposal output was truncated",
            )
        if finish_reason not in {None, "stop"}:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek did not complete a normal proposal response",
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek returned empty proposal content",
            )
        return content.encode("utf-8")

    def generate_proposal(
        self,
        request: PlanProposalGenerationRequest,
    ) -> RawPlanProposalResponse:
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
                timeout_seconds=self._timeout_seconds,
            )
        except DeepSeekTransportTimeout:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_TIMEOUT,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek proposal request timed out",
            )
        except DeepSeekTransportError:
            _fail(
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
                PlanProposalProviderStage.PROVIDER_REQUEST,
                "DeepSeek proposal request failed",
            )
        return RawPlanProposalResponse(
            content=self._extract_content(response),
            provider=self.name,
            model=self.model,
        )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "DeepSeekApiPlanProposalProvider",
    "DeepSeekPlanTransport",
    "DeepSeekTransportError",
    "DeepSeekTransportTimeout",
    "UrllibDeepSeekPlanTransport",
]
