"""Explicit session schedule snapshots with exact, verifiable session dates.

A session schedule snapshot is a strictly validated list of exact verified
session dates for one calendar over a declared coverage interval. It never
derives sessions from calendar names, markets, weekdays, or code imports.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from pydantic import (
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from typing import Self

from market_validator.data.calendars import CalendarDefinition
from market_validator.research.models import StrictResearchModel

SESSION_SCHEDULE_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

VERIFICATION_STATEMENT = (
    "I confirm that these exact dates are the verified sessions for this "
    "calendar over the declared coverage interval."
)


class SessionScheduleError(ValueError):
    """Safe structured failure for session schedule handling."""

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def fail_session_schedule(message: str) -> NoReturn:
    raise SessionScheduleError(message)


class ExplicitSessionScheduleSnapshot(StrictResearchModel):
    schedule_schema_version: Literal["1.0"] = SESSION_SCHEDULE_SCHEMA_VERSION
    schedule_id: str
    calendar_id: str
    schedule_adapter_id: str
    coverage_start: date
    coverage_end: date
    sessions: list[date]
    verification_source_uri: str
    verified_as_of: date
    verified: Literal[True] = True
    verification_statement: str = VERIFICATION_STATEMENT

    @field_validator("sessions")
    @classmethod
    def validate_sessions(cls, value: list[date]) -> list[date]:
        if not value:
            raise ValueError("sessions must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("sessions must not contain duplicates")
        if any(
            later <= earlier for earlier, later in zip(value, value[1:])
        ):
            raise ValueError("sessions must be strictly increasing")
        return value

    @field_validator("verification_source_uri")
    @classmethod
    def validate_verification_source_uri(cls, value: str) -> str:
        # Reject any userinfo (credentials) in the URI.
        from urllib.parse import urlsplit

        if urlsplit(value).username is not None:
            raise ValueError(
                "verification source URI must not contain credentials"
            )
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.coverage_start > self.coverage_end:
            raise ValueError(
                "coverage_start must not be later than coverage_end"
            )
        for session in self.sessions:
            if not (self.coverage_start <= session <= self.coverage_end):
                raise ValueError(
                    "session must lie within the coverage interval"
                )
        return self

    @field_validator("verification_statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        if value != VERIFICATION_STATEMENT:
            raise ValueError("verification statement is fixed")
        return value


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> NoReturn:
    raise ValueError(value)


def _canonical_bytes(model: StrictResearchModel) -> bytes:
    payload = (
        json.dumps(
            model.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return payload


def parse_explicit_session_schedule_snapshot(
    payload: bytes,
) -> ExplicitSessionScheduleSnapshot:
    try:
        json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        fail_session_schedule(
            "session schedule snapshot failed strict JSON validation"
        )
    try:
        return ExplicitSessionScheduleSnapshot.model_validate_json(payload)
    except ValidationError:
        fail_session_schedule(
            "session schedule snapshot failed strict domain validation"
        )


def serialize_explicit_session_schedule_snapshot(
    snapshot: ExplicitSessionScheduleSnapshot,
) -> bytes:
    payload = _canonical_bytes(snapshot)
    if (
        parse_explicit_session_schedule_snapshot(payload) != snapshot
    ):
        fail_session_schedule(
            "session schedule snapshot does not round-trip exactly"
        )
    return payload


def calculate_explicit_session_schedule_snapshot_sha256(
    snapshot: ExplicitSessionScheduleSnapshot,
) -> str:
    return hashlib.sha256(
        serialize_explicit_session_schedule_snapshot(snapshot)
    ).hexdigest()


def canonical_session_set_sha256(sessions: list[date]) -> str:
    """Canonical SHA-256 of a session date set (strictly increasing)."""
    ordered = sorted(set(sessions))
    payload = json.dumps(
        [session.isoformat() for session in ordered],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ExplicitSessionScheduleAdapter:
    """Deterministic adapter over an explicit session schedule snapshot."""

    def __init__(self, snapshot: ExplicitSessionScheduleSnapshot) -> None:
        restored = parse_explicit_session_schedule_snapshot(
            serialize_explicit_session_schedule_snapshot(snapshot)
        )
        if restored != snapshot:
            fail_session_schedule(
                "session schedule snapshot is not canonical"
            )
        self.snapshot = snapshot

    def sessions_between(self, start_date: date, end_date: date) -> list[date]:
        if start_date > end_date:
            fail_session_schedule(
                "session query start must not be later than end"
            )
        if not (
            self.snapshot.coverage_start
            <= start_date
            <= self.snapshot.coverage_end
        ) or not (
            self.snapshot.coverage_start
            <= end_date
            <= self.snapshot.coverage_end
        ):
            fail_session_schedule(
                "session query interval is outside the schedule coverage"
            )
        return [
            session
            for session in self.snapshot.sessions
            if start_date <= session <= end_date
        ]

    def previous_sessions(self, before_date: date, count: int) -> list[date]:
        if count < 0:
            fail_session_schedule("session count must not be negative")
        if not (
            self.snapshot.coverage_start
            <= before_date
            <= self.snapshot.coverage_end
        ):
            fail_session_schedule(
                "previous-session query is outside the schedule coverage"
            )
        result = [
            session
            for session in self.snapshot.sessions
            if session < before_date
        ]
        if len(result) < count:
            fail_session_schedule(
                "schedule does not contain enough previous sessions"
            )
        return result[-count:] if count else []


def persist_explicit_session_schedule_snapshot(
    snapshot: ExplicitSessionScheduleSnapshot,
    output_path: str | Path,
) -> Path:
    """Create-only persistence with provenance sidecar and reload check."""
    from market_validator.hypothesis.lifecycle import (
        HypothesisLifecycleErrorCode,
        persist_immutable_bytes,
    )

    payload = serialize_explicit_session_schedule_snapshot(snapshot)
    path = Path(output_path)
    try:
        persisted = persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_session_schedule(
                "session schedule output path already exists with "
                "different content"
            )
        fail_session_schedule(
            "session schedule could not be persisted"
        )
    try:
        restored = parse_explicit_session_schedule_snapshot(
            persisted.read_bytes()
        )
    except SessionScheduleError:
        fail_session_schedule(
            "persisted session schedule failed reload validation"
        )
    if restored != snapshot:
        fail_session_schedule(
            "persisted session schedule does not match the canonical bytes"
        )
    return persisted


__all__ = [
    "ExplicitSessionScheduleAdapter",
    "ExplicitSessionScheduleSnapshot",
    "SESSION_SCHEDULE_SCHEMA_VERSION",
    "SessionScheduleError",
    "VERIFICATION_STATEMENT",
    "calculate_explicit_session_schedule_snapshot_sha256",
    "canonical_session_set_sha256",
    "fail_session_schedule",
    "parse_explicit_session_schedule_snapshot",
    "persist_explicit_session_schedule_snapshot",
    "serialize_explicit_session_schedule_snapshot",
]
