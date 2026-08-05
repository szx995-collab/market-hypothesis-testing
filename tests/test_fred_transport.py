"""Offline tests for the HTTPS-only FRED transport."""

from __future__ import annotations

from io import BytesIO
import json
import socket
import unittest
from unittest import mock
from urllib.error import HTTPError

from pydantic import SecretStr

from market_validator.data.providers.fred_transport import (
    FredAuthenticationError,
    FredHttpsTransport,
    FredInvalidRequestError,
    FredPermissionDeniedError,
    FredRateLimitError,
    FredRedirectError,
    FredResponseFormatError,
    FredSeriesNotFoundError,
    FredServiceUnavailableError,
    validate_fred_url,
)

SENTINEL_KEY = "k7" * 16


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]

    def close(self) -> None:
        self.closed = True


class ScriptedOpener:
    def __init__(self, events: list[object]) -> None:
        self.events = list(events)
        self.calls: list[tuple[str, float]] = []

    def open(self, request, timeout: float):
        self.calls.append((request.full_url, timeout))
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


def error_message_for(status: int) -> str:
    return {
        400: "The observation_start parameter is invalid.",
        401: "The supplied api_key is invalid.",
        403: "Permission denied for this request.",
        404: "The requested series does not exist.",
        429: "Rate limit exceeded.",
        500: "Internal provider error.",
        502: "Bad gateway.",
        503: "Service temporarily unavailable.",
        504: "Gateway timeout.",
    }.get(status, "Provider error.")


def http_error(status: int, message: str | None = None) -> HTTPError:
    body = json.dumps(
        {"error_code": status, "error_message": message or error_message_for(status)}
    ).encode()
    return HTTPError(
        f"https://api.stlouisfed.org/redacted-{SENTINEL_KEY}",
        status,
        "provider message",
        {},
        BytesIO(body),
    )


class FredTransportTest(unittest.TestCase):
    def make_transport(self, events: list[object]):
        opener = ScriptedOpener(events)
        sleeps: list[float] = []
        transport = FredHttpsTransport(
            SecretStr(SENTINEL_KEY),
            timeout_seconds=3.5,
            opener=opener,
            sleep=sleeps.append,
        )
        return transport, opener, sleeps

    def test_normal_json_response_preserves_raw_bytes(self) -> None:
        raw = json.dumps({"seriess": []}).encode()
        transport, opener, sleeps = self.make_transport([FakeResponse(200, raw)])
        response = transport.get_json(
            "/fred/series", {"series_id": "DCOILWTICO", "file_type": "json"}
        )
        self.assertEqual(response.raw_body, raw)
        self.assertEqual(response.payload, {"seriess": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][1], 3.5)
        self.assertEqual(sleeps, [])

    def test_timeout_and_response_size_are_bounded(self) -> None:
        for timeout in (0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FredHttpsTransport(SecretStr(SENTINEL_KEY), timeout_seconds=timeout)

        transport, opener, sleeps = self.make_transport(
            [FakeResponse(200, b"x" * 11)]
        )
        with (
            mock.patch(
                "market_validator.data.providers.fred_transport.MAX_RESPONSE_BYTES",
                10,
            ),
            self.assertRaises(FredResponseFormatError),
        ):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(sleeps, [])

    def test_non_https_and_non_official_hosts_are_rejected(self) -> None:
        for url in (
            "http://api.stlouisfed.org/fred/series",
            "https://example.com/fred/series",
            "https://api.stlouisfed.org:444/fred/series",
        ):
            with self.subTest(url=url), self.assertRaises(FredInvalidRequestError):
                validate_fred_url(url)

    def test_unknown_path_and_sensitive_public_parameter_are_rejected(self) -> None:
        transport, opener, _ = self.make_transport([])
        with self.assertRaises(FredInvalidRequestError):
            transport.get_json("/fred/search", {})
        with self.assertRaises(FredInvalidRequestError):
            transport.get_json("/fred/series", {"api_key": "not-public"})
        self.assertEqual(opener.calls, [])

    def test_timeout_is_classified_without_leaking_key(self) -> None:
        transport, opener, sleeps = self.make_transport([socket.timeout("timeout")])
        with self.assertRaises(FredServiceUnavailableError) as caught:
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertNotIn(SENTINEL_KEY, str(caught.exception))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(sleeps, [])

    def test_non_retryable_http_errors_are_classified_once(self) -> None:
        expectations = {
            400: FredInvalidRequestError,
            401: FredAuthenticationError,
            403: FredPermissionDeniedError,
            404: FredSeriesNotFoundError,
        }
        for status, error_type in expectations.items():
            with self.subTest(status=status):
                transport, opener, sleeps = self.make_transport([http_error(status)])
                with self.assertRaises(error_type) as caught:
                    transport.get_json("/fred/series", {"series_id": "A"})
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(sleeps, [])
                self.assertNotIn(SENTINEL_KEY, str(caught.exception))

    def test_rate_limit_retries_at_most_twice_without_real_sleep(self) -> None:
        transport, opener, sleeps = self.make_transport(
            [http_error(429), http_error(429), http_error(429)]
        )
        with self.assertRaises(FredRateLimitError):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_retryable_service_errors_can_recover(self) -> None:
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                raw = b'{"ok": true}'
                transport, opener, sleeps = self.make_transport(
                    [http_error(status), FakeResponse(200, raw)]
                )
                response = transport.get_json(
                    "/fred/series", {"series_id": "A"}
                )
                self.assertEqual(response.payload, {"ok": True})
                self.assertEqual(len(opener.calls), 2)
                self.assertEqual(sleeps, [0.25])

    def test_retryable_service_error_is_bounded(self) -> None:
        transport, opener, sleeps = self.make_transport(
            [http_error(503), http_error(503), http_error(503)]
        )
        with self.assertRaises(FredServiceUnavailableError):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_redirect_is_rejected_and_not_retried(self) -> None:
        transport, opener, sleeps = self.make_transport([http_error(302)])
        with self.assertRaises(FredRedirectError):
            transport.get_json("/fred/series", {"series_id": "A"})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(sleeps, [])

    def test_malformed_or_non_object_json_is_rejected(self) -> None:
        for body in (b"not-json", b"[]"):
            with self.subTest(body=body):
                transport, _, sleeps = self.make_transport([FakeResponse(200, body)])
                with self.assertRaises(FredResponseFormatError):
                    transport.get_json("/fred/series", {"series_id": "A"})
                self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
