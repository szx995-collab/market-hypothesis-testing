"""Reusable validation helpers for provider-neutral research models."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_iana_timezone(value: str) -> str:
    """Return an IANA timezone name or raise a field-level validation error."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"{value!r} is not a valid IANA timezone") from error
    return value
