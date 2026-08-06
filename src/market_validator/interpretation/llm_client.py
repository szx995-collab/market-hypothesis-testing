"""LLM clients (v0.4.0 Phase 3).

A protocol plus two adapters: an offline FixtureLLMClient for tests/CI and
a minimal ChatCompletionsHTTPClient using only the standard library.
Network is forbidden by default; callers must pass allow_network=True.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlparse

from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    InterpretationErrorCode,
    fail_interpretation,
)

MAX_RESPONSE_BYTES_DEFAULT = 1 << 20  # 1 MiB


class InterpretationLLMClient(Protocol):
    """Minimal protocol: one JSON-in, JSON-out interpretation call."""

    def generate_interpretation_json(
        self,
        evidence_package: AnalysisEvidencePackage,
        *,
        language: str,
        style: str = "concise",
        prompt: str,
    ) -> dict[str, object]:
        """Return the parsed LLM JSON object; raise InterpretationError."""
        ...


class FixtureLLMClient:
    """Deterministic offline client; never touches the network."""

    def __init__(self, *, model: str = "fixture-v1") -> None:
        self.model = model

    def generate_interpretation_json(
        self,
        evidence_package: AnalysisEvidencePackage,
        *,
        language: str,
        style: str = "concise",
        prompt: str,
    ) -> dict[str, object]:
        conclusion = evidence_package.overall_conclusion
        headline = {
            "supported": "Evidence supports the hypothesis",
            "not_supported": "Evidence does not support the hypothesis",
            "inconclusive": "Evidence is inconclusive",
            "mixed": "Evidence is mixed across tests",
        }[conclusion]
        if language == "zh-CN":
            headline = {
                "supported": "证据支持该假设",
                "not_supported": "证据不支持该假设",
                "inconclusive": "证据尚不充分",
                "mixed": "各检验结果不一致",
            }[conclusion]
        test_interpretations = [
            {
                "test_id": evidence.test_id,
                "explanation": (
                    "The deterministic statement describes this test "
                    "result."
                ),
            }
            for evidence in evidence_package.primary_test_evidence
        ]
        return {
            "acknowledged_overall_conclusion": conclusion,
            "language": language,
            "headline": headline,
            "plain_language_summary": (
                "The verified analysis result is interpreted here without "
                "recomputation."
            ),
            "test_interpretations": test_interpretations,
            "limitations_explanation": (
                "The listed limitations apply to this analysis."
            ),
            "cannot_conclude": [
                "This result does not prove causation.",
                "Robustness was not executed.",
            ],
            "suggested_followups": ["执行 robustness"],
            "model_metadata": {"model": self.model},
            "warnings": [],
        }


class ChatCompletionsHTTPClient:
    """Minimal standard-library OpenAI-style chat completions adapter.

    Network is disabled unless allow_network=True; only https (or loopback
    http) endpoints are accepted; the API key is used only in the
    Authorization header and never persisted; no retries, no fallback, no
    cross-host redirects, bounded response size, safe timeout.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES_DEFAULT,
        allow_network: bool = False,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.allow_network = allow_network

    def _validate_endpoint(self) -> None:
        parsed = urlparse(self.endpoint_url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname or ""
        if scheme == "https":
            return
        if scheme == "http":
            try:
                address = socket.getaddrinfo(hostname, parsed.port or 80)
            except (OSError, ValueError):
                fail_interpretation(
                    InterpretationErrorCode.LLM_REQUEST_FAILED,
                    "the LLM endpoint could not be resolved",
                )
            if not address:
                fail_interpretation(
                    InterpretationErrorCode.LLM_REQUEST_FAILED,
                    "the LLM endpoint could not be resolved",
                )
            for entry in address:
                ip = entry[4][0]
                if not (ip.startswith("127.") or ip == "::1"):
                    fail_interpretation(
                        InterpretationErrorCode.LLM_REQUEST_FAILED,
                        "non-loopback http endpoints are rejected",
                    )
            return
        fail_interpretation(
            InterpretationErrorCode.LLM_REQUEST_FAILED,
            "only http or https endpoints are accepted",
        )

    def generate_interpretation_json(
        self,
        evidence_package: AnalysisEvidencePackage,
        *,
        language: str,
        style: str = "concise",
        prompt: str,
    ) -> dict[str, object]:
        if not self.allow_network:
            fail_interpretation(
                InterpretationErrorCode.LLM_NETWORK_NOT_AUTHORIZED,
                "network access is not authorized for the LLM client",
            )
        self._validate_endpoint()
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            _HttpsContextHandler(),
        )
        try:
            with opener.open(
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            fail_interpretation(
                InterpretationErrorCode.LLM_REQUEST_FAILED,
                "the LLM endpoint returned an error status",
            )
        except (urllib.error.URLError, OSError, ssl.SSLError):
            fail_interpretation(
                InterpretationErrorCode.LLM_REQUEST_FAILED,
                "the LLM request failed or timed out",
            )
        if len(body) > self.max_response_bytes:
            fail_interpretation(
                InterpretationErrorCode.LLM_RESPONSE_TOO_LARGE,
                "the LLM response exceeded the size limit",
            )
        def _reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(key)
                result[key] = value
            return result

        try:
            data = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(value)
                ),
            )
            content = data["choices"][0]["message"]["content"]
            return json.loads(
                content,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(value)
                ),
            )
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            fail_interpretation(
                InterpretationErrorCode.LLM_RESPONSE_INVALID,
                "the LLM response is not valid JSON",
            )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never follow redirects (cross-host or otherwise)


class _HttpsContextHandler(urllib.request.HTTPSHandler):
    def __init__(self) -> None:
        context = ssl.create_default_context()
        super().__init__(context=context)


__all__ = [
    "ChatCompletionsHTTPClient",
    "FixtureLLMClient",
    "InterpretationLLMClient",
    "MAX_RESPONSE_BYTES_DEFAULT",
]
