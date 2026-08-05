"""Shared, hardened DeepSeek HTTPS transport without implicit retries."""

from __future__ import annotations

from collections.abc import Mapping
import socket
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 120.0
MAX_DEEPSEEK_RESPONSE_BYTES = 2 * 1024 * 1024


class DeepSeekTransportError(RuntimeError):
    """Sanitized transport error containing no response body or credentials."""


class DeepSeekTransportTimeout(DeepSeekTransportError):
    """The HTTPS request exceeded its configured timeout."""


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Turn every redirect into an HTTP failure before a second request exists."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request:
        raise HTTPError(req.full_url, code, "DeepSeek redirect refused", headers, fp)


def _urlopen_without_redirects(request: Request, *, timeout: float) -> Any:
    """Open exactly one URL with redirect following disabled."""

    return build_opener(_RejectRedirectHandler()).open(request, timeout=timeout)


class DeepSeekTransport(Protocol):
    """One bounded HTTPS POST with no retry behavior."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        """Send one non-streaming JSON request and return bounded bytes."""


class UrllibDeepSeekTransport:
    """Standard-library transport constrained by timeout and response size."""

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        response_limit: int | Callable[[], int] | None = None,
    ) -> None:
        self._opener = opener
        self._response_limit = response_limit

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
            opener = self._opener or _urlopen_without_redirects
            with opener(request, timeout=timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                if isinstance(status, int) and 300 <= status < 400:
                    raise DeepSeekTransportError(
                        f"DeepSeek returned HTTP status {status}"
                    )
                limit = (
                    self._response_limit()
                    if callable(self._response_limit)
                    else self._response_limit
                    if self._response_limit is not None
                    else MAX_DEEPSEEK_RESPONSE_BYTES
                )
                payload = response.read(limit + 1)
                if len(payload) > limit:
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


def validated_deepseek_endpoint(base_url: str) -> str:
    """Return the official chat-completions endpoint or reject configuration."""

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("DeepSeek base URL must be a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "DeepSeek base URL must be the credential-free official HTTPS origin"
        )
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ValueError("DeepSeek base URL path must be empty or /v1")
    return base_url.rstrip("/") + "/chat/completions"


__all__ = [
    "DEFAULT_DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_TIMEOUT_SECONDS",
    "MAX_DEEPSEEK_RESPONSE_BYTES",
    "DeepSeekTransport",
    "DeepSeekTransportError",
    "DeepSeekTransportTimeout",
    "UrllibDeepSeekTransport",
    "validated_deepseek_endpoint",
]
