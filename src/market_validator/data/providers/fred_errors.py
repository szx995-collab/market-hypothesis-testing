"""Structured FRED failures and centralized secret-safe diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

MAX_ERROR_MESSAGE_LENGTH = 500
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
SENSITIVE_NAMES = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
PARAMETER_PATTERN = re.compile(
    r"(?i)([\"']?(?:api_key|apikey|token|secret|password|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'&\s,;}]+)"
)
KEY_LIKE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[a-z0-9]{32}(?![A-Za-z0-9])")


def _strip_url_query(match: re.Match[str]) -> str:
    candidate = match.group(0)
    trailing = ""
    while candidate and candidate[-1] in ".,;)]}":
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    parsed = urlsplit(candidate)
    endpoint = parsed.path or "/"
    return endpoint + trailing


def sanitize_fred_message(message: object, *, secret: str | None = None) -> str:
    """Return a short diagnostic without credentials, queries, or raw URLs."""
    text = str(message)
    if secret:
        percent_encoded = "".join(f"%{byte:02X}" for byte in secret.encode("utf-8"))
        variants = {
            secret,
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
            percent_encoded,
            percent_encoded.casefold(),
        }
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                text = text.replace(variant, "[REDACTED]")
    text = URL_PATTERN.sub(_strip_url_query, text)
    text = PARAMETER_PATTERN.sub(r"\1[REDACTED]", text)
    text = KEY_LIKE_PATTERN.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if not text:
        text = "FRED returned an error without a safe diagnostic message"
    return text[:MAX_ERROR_MESSAGE_LENGTH]


def sanitize_public_parameters(
    parameters: Mapping[str, str | int], *, secret: str | None = None
) -> dict[str, str | int]:
    """Copy public request parameters while dropping suspicious names and values."""
    safe: dict[str, str | int] = {}
    for name, value in parameters.items():
        normalized = name.casefold().replace("-", "_")
        if any(fragment in normalized for fragment in SENSITIVE_NAMES):
            continue
        if isinstance(value, str):
            safe[name] = sanitize_fred_message(value, secret=secret)
        else:
            safe[name] = value
    return safe


class FredTransportError(RuntimeError):
    """Base class carrying only sanitized, public diagnostic fields."""

    code = "transport_error"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        provider_error_code: int | str | None = None,
        endpoint: str | None = None,
        public_parameters: Mapping[str, str | int] | None = None,
        retryable: bool = False,
    ) -> None:
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.sanitized_message = sanitize_fred_message(message)
        self.endpoint = endpoint
        self.public_parameters = dict(public_parameters or {})
        self.retryable = retryable
        super().__init__(self.sanitized_message)

    def public_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "message": self.sanitized_message,
            "endpoint": self.endpoint,
            "public_parameters": self.public_parameters,
            "retryable": self.retryable,
        }


class FredInvalidRequestError(FredTransportError):
    code = "invalid_request"


class FredAuthenticationError(FredTransportError):
    code = "authentication_failed"


class FredPermissionDeniedError(FredTransportError):
    code = "permission_denied"


class FredSeriesNotFoundError(FredTransportError):
    code = "series_not_found"


class FredRateLimitError(FredTransportError):
    code = "rate_limited"


class FredServiceUnavailableError(FredTransportError):
    code = "provider_unavailable"


class FredRedirectError(FredTransportError):
    code = "redirect_rejected"


class FredResponseFormatError(FredTransportError):
    code = "response_format_error"


class FredMalformedProviderError(FredTransportError):
    code = "malformed_provider_error"


def _parse_provider_error(raw_body: bytes) -> tuple[int | str | None, str] | None:
    try:
        payload: Any = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    message = payload.get("error_message")
    error_code = payload.get("error_code")
    if not isinstance(message, str) or not message.strip():
        return None
    if not isinstance(error_code, (int, str)) or isinstance(error_code, bool):
        error_code = None
    return error_code, message


def classify_fred_http_error(
    *,
    http_status: int,
    raw_body: bytes,
    endpoint: str,
    public_parameters: Mapping[str, str | int],
    secret: str,
) -> FredTransportError:
    """Parse a FRED error body once, sanitize it, then classify by meaning."""
    safe_parameters = sanitize_public_parameters(public_parameters, secret=secret)
    retryable = http_status in RETRYABLE_HTTP_STATUSES
    if 300 <= http_status < 400:
        return FredRedirectError(
            "FRED redirect was rejected",
            http_status=http_status,
            endpoint=endpoint,
            public_parameters=safe_parameters,
            retryable=False,
        )

    parsed = _parse_provider_error(raw_body)
    if parsed is None:
        return FredMalformedProviderError(
            "FRED returned an unreadable structured error response",
            http_status=http_status,
            endpoint=endpoint,
            public_parameters=safe_parameters,
            retryable=retryable,
        )

    provider_error_code, raw_message = parsed
    safe_message = sanitize_fred_message(raw_message, secret=secret)
    if isinstance(provider_error_code, str):
        provider_error_code = sanitize_fred_message(
            provider_error_code, secret=secret
        )[:64]
    normalized = raw_message.casefold()

    authentication_markers = (
        "not registered",
        "invalid",
        "incorrect",
        "missing",
        "required",
        "32 character",
        "32-character",
        "alpha-numeric lower-case",
    )
    if (
        ("api key" in normalized or "api_key" in normalized)
        and any(marker in normalized for marker in authentication_markers)
    ) or http_status == 401:
        error_type: type[FredTransportError] = FredAuthenticationError
    elif (
        "permission" in normalized
        or "not authorized" in normalized
        or "access denied" in normalized
        or "forbidden" in normalized
        or http_status == 403
    ):
        error_type = FredPermissionDeniedError
    elif (
        "series" in normalized
        and any(
            marker in normalized
            for marker in ("does not exist", "not found", "unknown", "no series")
        )
    ) or http_status == 404:
        error_type = FredSeriesNotFoundError
    elif http_status == 429 or "rate limit" in normalized or "too many requests" in normalized:
        error_type = FredRateLimitError
    elif http_status in {500, 502, 503, 504}:
        error_type = FredServiceUnavailableError
    else:
        error_type = FredInvalidRequestError

    return error_type(
        safe_message,
        http_status=http_status,
        provider_error_code=provider_error_code,
        endpoint=endpoint,
        public_parameters=safe_parameters,
        retryable=retryable,
    )
