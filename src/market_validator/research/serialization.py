"""Strict parsing and canonical bytes for the existing ResearchSpec model."""

from __future__ import annotations

import hashlib
import json
from typing import NoReturn

from pydantic import ValidationError

from market_validator.research.models import ResearchSpec


class ResearchSpecSerializationError(ValueError):
    """Safe error without raw research content."""


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


def parse_research_spec(payload: bytes | bytearray) -> ResearchSpec:
    if not isinstance(payload, (bytes, bytearray)):
        raise ResearchSpecSerializationError(
            "ResearchSpec must be supplied as UTF-8 bytes"
        )
    normalized = bytes(payload)
    try:
        decoded = json.loads(
            normalized.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        raise ResearchSpecSerializationError(
            "ResearchSpec must be exactly one strict UTF-8 JSON object"
        ) from None
    if not isinstance(decoded, dict):
        raise ResearchSpecSerializationError("ResearchSpec must be a JSON object")
    try:
        return ResearchSpec.model_validate_json(normalized)
    except ValidationError:
        raise ResearchSpecSerializationError(
            "ResearchSpec failed strict domain validation"
        ) from None


def serialize_research_spec(spec: ResearchSpec) -> bytes:
    if not isinstance(spec, ResearchSpec):
        raise ResearchSpecSerializationError(
            "spec must be a validated ResearchSpec"
        )
    try:
        payload = (
            json.dumps(
                spec.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ResearchSpecSerializationError(
            "ResearchSpec could not be serialized as strict JSON"
        ) from None
    if parse_research_spec(payload) != spec:
        raise ResearchSpecSerializationError("ResearchSpec does not round-trip exactly")
    return payload


def calculate_research_spec_sha256(spec: ResearchSpec) -> str:
    return hashlib.sha256(serialize_research_spec(spec)).hexdigest()


__all__ = [
    "ResearchSpecSerializationError",
    "calculate_research_spec_sha256",
    "parse_research_spec",
    "serialize_research_spec",
]
