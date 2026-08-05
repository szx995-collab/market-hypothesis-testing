"""Offline fixtures for diagnostic FRED error parsing and sanitization."""

from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

from pydantic import SecretStr

from market_validator.data.providers.fred_errors import (
    FredAuthenticationError,
    FredInvalidRequestError,
    FredMalformedProviderError,
    FredPermissionDeniedError,
    FredRateLimitError,
    FredSeriesNotFoundError,
    FredServiceUnavailableError,
    classify_fred_http_error,
    sanitize_fred_message,
)
from market_validator.data.providers.fred_transport import FredHttpsTransport

SENTINEL_KEY = "q8" * 16
ENDPOINT = "/fred/series"
PUBLIC_PARAMETERS = {"series_id": "DCOILWTICO", "file_type": "json"}


def error_body(message: str, code: int = 400) -> bytes:
    return json.dumps(
        {"error_code": code, "error_message": message},
        separators=(",", ":"),
    ).encode("utf-8")


class CountingBody(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.read_count += 1
        if self.read_count > 1:
            raise AssertionError("HTTPError body was read more than once")
        return super().read(*args, **kwargs)


class ScriptedOpener:
    def __init__(self, errors: list[HTTPError]) -> None:
        self.errors = list(errors)
        self.calls = 0

    def open(self, _request, timeout: float):
        self.calls += 1
        raise self.errors.pop(0)


def make_http_error(status: int, body: CountingBody) -> HTTPError:
    return HTTPError(
        f"https://api.stlouisfed.org{ENDPOINT}?api_key={SENTINEL_KEY}",
        status,
        "unsafe provider reason",
        {},
        body,
    )


class FredErrorClassificationTest(unittest.TestCase):
    def classify(self, status: int, message: str, code: int | None = None):
        return classify_fred_http_error(
            http_status=status,
            raw_body=error_body(message, status if code is None else code),
            endpoint=ENDPOINT,
            public_parameters=PUBLIC_PARAMETERS,
            secret=SENTINEL_KEY,
        )

    def test_diagnostic_fixtures_are_classified_by_meaning(self) -> None:
        fixtures = (
            (
                400,
                "The value for variable api_key is not a 32 character alpha-numeric lower-case string.",
                FredAuthenticationError,
                "32 character",
            ),
            (
                400,
                "The supplied FRED API key is not registered.",
                FredAuthenticationError,
                "not registered",
            ),
            (
                400,
                "The required variable series_id is missing.",
                FredInvalidRequestError,
                "series_id",
            ),
            (
                400,
                "The requested series does not exist.",
                FredSeriesNotFoundError,
                "does not exist",
            ),
            (
                400,
                "The output_type value is not supported.",
                FredInvalidRequestError,
                "output_type",
            ),
            (
                400,
                "The observation_start date is invalid.",
                FredInvalidRequestError,
                "observation_start",
            ),
            (403, "Permission denied for this series.", FredPermissionDeniedError, "Permission"),
            (429, "Rate limit exceeded.", FredRateLimitError, "Rate limit"),
            (503, "Service temporarily unavailable.", FredServiceUnavailableError, "unavailable"),
        )
        for status, message, error_type, diagnostic in fixtures:
            with self.subTest(message=message):
                error = self.classify(status, message)
                self.assertIsInstance(error, error_type)
                self.assertIn(diagnostic.casefold(), str(error).casefold())
                self.assertEqual(error.http_status, status)
                self.assertEqual(error.provider_error_code, status)
                self.assertEqual(error.endpoint, ENDPOINT)
                self.assertEqual(error.public_parameters, PUBLIC_PARAMETERS)
                self.assertEqual(error.retryable, status in {429, 503})
                self.assertNotIn(SENTINEL_KEY, repr(error.public_error()))

    def test_non_json_or_missing_error_message_is_malformed(self) -> None:
        bodies = (b"not-json", b"{}", b"[]", b'{"error_code":400}')
        for body in bodies:
            with self.subTest(body=body):
                error = classify_fred_http_error(
                    http_status=400,
                    raw_body=body,
                    endpoint=ENDPOINT,
                    public_parameters=PUBLIC_PARAMETERS,
                    secret=SENTINEL_KEY,
                )
                self.assertIsInstance(error, FredMalformedProviderError)
                self.assertNotIn(body.decode(errors="ignore"), str(error))

    def test_full_url_secret_encoded_secret_and_key_like_strings_are_removed(self) -> None:
        encoded = "".join(f"%{byte:02X}" for byte in SENTINEL_KEY.encode())
        other_key_like = "a1" * 16
        message = (
            f"Request https://api.stlouisfed.org/fred/series?api_key={SENTINEL_KEY} "
            f"failed; encoded={encoded}; token={other_key_like}; "
            f"authorization={SENTINEL_KEY}."
        )
        sanitized = sanitize_fred_message(message, secret=SENTINEL_KEY)
        self.assertIn("/fred/series", sanitized)
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("?", sanitized)
        self.assertNotIn(SENTINEL_KEY, sanitized)
        self.assertNotIn(encoded, sanitized)
        self.assertNotIn(other_key_like, sanitized)
        self.assertLessEqual(len(sanitized), 500)

    def test_very_long_provider_message_is_bounded(self) -> None:
        sanitized = sanitize_fred_message("diagnostic " + "x" * 2000)
        self.assertEqual(len(sanitized), 500)

    def test_provider_code_and_public_parameters_are_also_sanitized(self) -> None:
        body = json.dumps(
            {
                "error_code": SENTINEL_KEY,
                "error_message": "The observation_start parameter is invalid.",
            }
        ).encode()
        error = classify_fred_http_error(
            http_status=400,
            raw_body=body,
            endpoint=ENDPOINT,
            public_parameters={
                "series_id": "DCOILWTICO",
                "api_key": SENTINEL_KEY,
                "note": SENTINEL_KEY,
            },
            secret=SENTINEL_KEY,
        )
        rendered = repr(error.public_error())
        self.assertNotIn(SENTINEL_KEY, rendered)
        self.assertNotIn("api_key", error.public_parameters)
        self.assertEqual(error.public_parameters["note"], "[REDACTED]")


class FredHttpErrorIntegrationTest(unittest.TestCase):
    def test_http_error_body_is_read_once_and_not_retried_for_400(self) -> None:
        body = CountingBody(
            error_body("The supplied api_key is not registered.")
        )
        opener = ScriptedOpener([make_http_error(400, body)])
        sleeps: list[float] = []
        transport = FredHttpsTransport(
            SecretStr(SENTINEL_KEY), opener=opener, sleep=sleeps.append
        )
        with self.assertRaises(FredAuthenticationError) as caught:
            transport.get_json(ENDPOINT, PUBLIC_PARAMETERS)
        self.assertEqual(body.read_count, 1)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(sleeps, [])
        rendered = repr(caught.exception.public_error())
        self.assertNotIn(SENTINEL_KEY, rendered)
        self.assertNotIn("https://", rendered)

    def test_rate_limit_retries_twice_and_reads_only_final_body(self) -> None:
        bodies = [CountingBody(error_body("Rate limit exceeded.", 429)) for _ in range(3)]
        opener = ScriptedOpener(
            [make_http_error(429, body) for body in bodies]
        )
        sleeps: list[float] = []
        transport = FredHttpsTransport(
            SecretStr(SENTINEL_KEY), opener=opener, sleep=sleeps.append
        )
        with self.assertRaises(FredRateLimitError):
            transport.get_json(ENDPOINT, PUBLIC_PARAMETERS)
        self.assertEqual([body.read_count for body in bodies], [0, 0, 1])
        self.assertEqual(opener.calls, 3)
        self.assertEqual(sleeps, [0.25, 0.5])


if __name__ == "__main__":
    unittest.main()
