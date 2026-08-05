"""Strict parsing and canonical bytes for the existing DataPlan model."""

from __future__ import annotations

import hashlib
import json
from typing import NoReturn

from pydantic import ValidationError

from market_validator.data.models import DataPlan


class DataPlanSerializationError(ValueError):
    """Safe error without raw planning content."""


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


def parse_data_plan(payload: bytes | bytearray) -> DataPlan:
    if not isinstance(payload, (bytes, bytearray)):
        raise DataPlanSerializationError("DataPlan must be supplied as UTF-8 bytes")
    normalized = bytes(payload)
    try:
        decoded = json.loads(
            normalized.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKeyError, ValueError):
        raise DataPlanSerializationError(
            "DataPlan must be exactly one strict UTF-8 JSON object"
        ) from None
    if not isinstance(decoded, dict):
        raise DataPlanSerializationError("DataPlan must be a JSON object")
    try:
        return DataPlan.model_validate_json(normalized)
    except ValidationError:
        raise DataPlanSerializationError(
            "DataPlan failed strict domain validation"
        ) from None


def serialize_data_plan(plan: DataPlan) -> bytes:
    if not isinstance(plan, DataPlan):
        raise DataPlanSerializationError("plan must be a validated DataPlan")
    try:
        payload = (
            json.dumps(
                plan.model_dump(mode="json", exclude_computed_fields=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise DataPlanSerializationError(
            "DataPlan could not be serialized as strict JSON"
        ) from None
    if parse_data_plan(payload) != plan:
        raise DataPlanSerializationError("DataPlan does not round-trip exactly")
    return payload


def calculate_data_plan_sha256(plan: DataPlan) -> str:
    return hashlib.sha256(serialize_data_plan(plan)).hexdigest()


__all__ = [
    "DataPlanSerializationError",
    "calculate_data_plan_sha256",
    "parse_data_plan",
    "serialize_data_plan",
]
