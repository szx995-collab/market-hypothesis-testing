"""HTTPS-only FRED transport with bounded retries and secret-safe errors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import socket
import time
from typing import Any, Protocol
from pydantic import SecretStr
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from market_validator.data.providers.fred_errors import (
    FredAuthenticationError,
    FredInvalidRequestError,
    FredMalformedProviderError,
    FredPermissionDeniedError,
    FredRateLimitError,
    FredRedirectError,
    FredResponseFormatError,
    FredSeriesNotFoundError,
    FredServiceUnavailableError,
    FredTransportError,
    classify_fred_http_error,
    sanitize_public_parameters,
)

FRED_BASE_URL = "https://api.stlouisfed.org"
FRED_HOST = "api.stlouisfed.org"
ALLOWED_PATHS = frozenset({"/fred/series", "/fred/series/observations"})
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 2
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FredTransportResponse:
    raw_body: bytes
    payload: dict[str, Any]
    status_code: int


class FredTransport(Protocol):
    def get_json(
        self, path: str, public_parameters: Mapping[str, str | int]
    ) -> FredTransportResponse:
        """Return exact response bytes and decoded JSON without exposing credentials."""


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_fred_url(url: str) -> None:
    """Fail closed unless a URL is HTTPS on the one official API host."""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise FredInvalidRequestError("FRED transport requires HTTPS")
    if parsed.hostname != FRED_HOST or parsed.port not in (None, 443):
        raise FredInvalidRequestError("FRED transport requires the official API host")


class FredHttpsTransport:
    """Production transport; base URL and redirect policy are not configurable."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        timeout_seconds: float = 15.0,
        opener: object | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, SecretStr):
            raise TypeError("FRED transport requires a protected credential")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("FRED timeout_seconds must be positive and finite")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(_RejectRedirectHandler())
        self._sleep = sleep

    def get_json(
        self, path: str, public_parameters: Mapping[str, str | int]
    ) -> FredTransportResponse:
        if path not in ALLOWED_PATHS:
            raise FredInvalidRequestError("unsupported FRED API path")
        if any(
            fragment in key.casefold().replace("-", "_")
            for key in public_parameters
            for fragment in (
                "api_key",
                "apikey",
                "token",
                "secret",
                "password",
                "authorization",
            )
        ):
            raise FredInvalidRequestError(
                "public parameters must not contain authentication fields"
            )

        safe_public_parameters = sanitize_public_parameters(public_parameters)
        secret_value = self._api_key.get_secret_value()
        parameters = dict(public_parameters)
        parameters["api_key"] = secret_value
        url = f"{FRED_BASE_URL}{path}?{urlencode(parameters)}"
        validate_fred_url(url)
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "market-validator/0.1",
            },
        )

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._opener.open(request, timeout=self._timeout_seconds)
                try:
                    status_code = int(response.getcode())
                    raw_body = response.read(MAX_RESPONSE_BYTES + 1)
                finally:
                    response.close()
                if len(raw_body) > MAX_RESPONSE_BYTES:
                    raise FredResponseFormatError(
                        "FRED response exceeded the safe size limit"
                    )
                if not 200 <= status_code < 300:
                    if status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                        self._sleep(0.25 * (attempt + 1))
                        continue
                    raise classify_fred_http_error(
                        http_status=status_code,
                        raw_body=raw_body,
                        endpoint=path,
                        public_parameters=safe_public_parameters,
                        secret=secret_value,
                    )
            except HTTPError as error:
                status_code = int(error.code)
                if status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    error.close()
                    self._sleep(0.25 * (attempt + 1))
                    continue
                try:
                    raw_error_body = error.read(MAX_RESPONSE_BYTES + 1)
                except Exception:
                    raw_error_body = b""
                finally:
                    error.close()
                if len(raw_error_body) > MAX_RESPONSE_BYTES:
                    raise FredResponseFormatError(
                        "FRED error response exceeded the safe size limit"
                    )
                raise classify_fred_http_error(
                    http_status=status_code,
                    raw_body=raw_error_body,
                    endpoint=path,
                    public_parameters=safe_public_parameters,
                    secret=secret_value,
                ) from None
            except (TimeoutError, socket.timeout, URLError):
                raise FredServiceUnavailableError(
                    "FRED request timed out or could not connect",
                    endpoint=path,
                    public_parameters=safe_public_parameters,
                    retryable=False,
                ) from None

            try:
                payload = json.loads(raw_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise FredResponseFormatError(
                    "FRED returned malformed JSON"
                ) from None
            if not isinstance(payload, dict):
                raise FredResponseFormatError(
                    "FRED JSON response must be an object"
                )
            return FredTransportResponse(
                raw_body=raw_body,
                payload=payload,
                status_code=status_code,
            )

        raise FredServiceUnavailableError("FRED request failed after retries")
