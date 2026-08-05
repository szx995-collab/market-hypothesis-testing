"""Canonical JSON wire format for persisted :class:`DataBundle` values."""

from __future__ import annotations

import json
from pathlib import Path

from market_validator.data.models import DataBundle


class DataBundleWireFormatError(ValueError):
    """Raised when a legacy bundle violates its narrow compatibility contract."""


def serialize_data_bundle(bundle: DataBundle) -> bytes:
    """Serialize a bundle for persistence without derived computed fields."""
    return bundle.model_dump_json(
        indent=2,
        exclude_computed_fields=True,
    ).encode("utf-8")


def deserialize_data_bundle(content: str | bytes | bytearray) -> DataBundle:
    """Strictly validate current bundles and one precisely defined legacy shape."""
    payload = json.loads(content)
    if not isinstance(payload, dict):
        return DataBundle.model_validate_json(content)

    extra_fields = set(payload).difference(DataBundle.model_fields)
    if extra_fields != {"status"}:
        return DataBundle.model_validate_json(content)

    quality = payload.get("quality")
    quality_status = quality.get("status") if isinstance(quality, dict) else None
    if payload["status"] != quality_status:
        raise DataBundleWireFormatError(
            "legacy top-level status must exactly match quality.status"
        )

    compatible_payload = dict(payload)
    del compatible_payload["status"]
    return DataBundle.model_validate_json(
        json.dumps(compatible_payload, ensure_ascii=False, separators=(",", ":"))
    )


def load_data_bundle(path: Path) -> DataBundle:
    """Read a persisted bundle without modifying its source file."""
    return deserialize_data_bundle(path.read_bytes())
