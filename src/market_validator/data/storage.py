"""Immutable, atomic snapshot persistence for live provider fetches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal

from pydantic import field_serializer, field_validator

from market_validator.data.bundle_io import serialize_data_bundle
from market_validator.data.models import (
    DataBundle,
    Identifier,
    NonEmptyString,
    StrictDataModel,
)
from market_validator.research.enums import DataRevisionMode


class FredStorageError(RuntimeError):
    """Raised when a complete auditable snapshot cannot be persisted."""


SAFE_SERIES_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SENSITIVE_PARAMETER_FRAGMENTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)


def _validate_public_parameters(parameters: Mapping[str, str | int]) -> None:
    for name in parameters:
        normalized = name.casefold().replace("-", "_")
        if any(fragment in normalized for fragment in SENSITIVE_PARAMETER_FRAGMENTS):
            raise FredStorageError(
                "snapshot public parameters contain a forbidden sensitive field"
            )


class StoredArtifact(StrictDataModel):
    relative_path: NonEmptyString
    sha256: str


class FredSnapshotManifest(StrictDataModel):
    request_id: Identifier
    provider_id: Literal["fred"]
    series_id: NonEmptyString
    observation_start: date
    observation_end: date
    revision_policy: DataRevisionMode
    retrieved_at: datetime
    raw_series: StoredArtifact
    raw_observations: list[StoredArtifact]
    bundle: StoredArtifact
    public_request_parameters: dict[str, str | int]

    @field_validator("retrieved_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("retrieved_at must be UTC")
        return value

    @field_serializer("retrieved_at", when_used="json")
    def serialize_retrieved_at(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class FredSnapshotRecord:
    request_id: str
    manifest_path: Path
    bundle_path: Path
    raw_series_path: Path
    raw_observation_paths: tuple[Path, ...]
    manifest_sha256: str
    bundle_sha256: str

    def public_summary(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "manifest_path": str(self.manifest_path),
            "bundle_path": str(self.bundle_path),
            "raw_series_path": str(self.raw_series_path),
            "raw_observation_paths": [
                str(path) for path in self.raw_observation_paths
            ],
            "manifest_sha256": self.manifest_sha256,
            "bundle_sha256": self.bundle_sha256,
        }


def resolve_data_directory(
    environment: Mapping[str, str] | None = None,
    *,
    current_directory: Path | None = None,
) -> Path:
    source = os.environ if environment is None else environment
    configured = source.get("MARKET_VALIDATOR_DATA_DIR", "").strip()
    base = current_directory or Path.cwd()
    path = Path(configured) if configured else base / ".market_validator" / "data"
    if not path.is_absolute():
        path = base / path
    return path.resolve()


class FredStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _atomic_write_new(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FredStorageError("refusing to overwrite an existing snapshot")
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".market-validator-", suffix=".tmp", dir=path.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                raise FredStorageError("refusing to overwrite an existing snapshot")
            os.replace(temporary_path, path)
            temporary_path = None
        except FredStorageError:
            raise
        except OSError as error:
            raise FredStorageError("atomic snapshot write failed") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def persist(
        self,
        *,
        series_id: str,
        observation_start: date,
        observation_end: date,
        revision_policy: DataRevisionMode,
        retrieved_at: datetime,
        public_request_parameters: Mapping[str, str | int],
        raw_series: bytes,
        raw_observations: Sequence[bytes],
        bundle: DataBundle,
    ) -> FredSnapshotRecord:
        if SAFE_SERIES_ID.fullmatch(series_id) is None:
            raise FredStorageError("series_id is unsafe for snapshot persistence")
        _validate_public_parameters(public_request_parameters)
        if not raw_observations:
            raise FredStorageError("at least one raw observations response is required")
        content_fingerprint = self._sha256(
            raw_series + b"".join(raw_observations)
        )[:12]
        timestamp = retrieved_at.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        request_id = f"fred-{series_id.lower()}-{timestamp}-{content_fingerprint}"

        raw_dir = self.root / "raw" / "fred"
        bundle_dir = self.root / "bundles" / "fred"
        manifest_dir = self.root / "manifests" / "fred"
        raw_series_path = raw_dir / f"{request_id}-series.json"
        raw_observation_paths = tuple(
            raw_dir / f"{request_id}-observations-{index:04d}.json"
            for index in range(len(raw_observations))
        )
        bundle_path = bundle_dir / f"{request_id}.json"
        manifest_path = manifest_dir / f"{request_id}.json"

        bundle_bytes = serialize_data_bundle(bundle)
        raw_series_artifact = StoredArtifact(
            relative_path=raw_series_path.relative_to(self.root).as_posix(),
            sha256=self._sha256(raw_series),
        )
        raw_observation_artifacts = [
            StoredArtifact(
                relative_path=path.relative_to(self.root).as_posix(),
                sha256=self._sha256(content),
            )
            for path, content in zip(
                raw_observation_paths, raw_observations, strict=True
            )
        ]
        bundle_artifact = StoredArtifact(
            relative_path=bundle_path.relative_to(self.root).as_posix(),
            sha256=self._sha256(bundle_bytes),
        )
        manifest = FredSnapshotManifest(
            request_id=request_id,
            provider_id="fred",
            series_id=series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            revision_policy=revision_policy,
            retrieved_at=retrieved_at.astimezone(timezone.utc),
            raw_series=raw_series_artifact,
            raw_observations=raw_observation_artifacts,
            bundle=bundle_artifact,
            public_request_parameters=dict(public_request_parameters),
        )
        manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")

        try:
            self._atomic_write_new(raw_series_path, raw_series)
            for path, content in zip(
                raw_observation_paths, raw_observations, strict=True
            ):
                self._atomic_write_new(path, content)
            self._atomic_write_new(bundle_path, bundle_bytes)
            self._atomic_write_new(manifest_path, manifest_bytes)
        except FredStorageError:
            raise
        except Exception as error:
            raise FredStorageError("complete FRED snapshot persistence failed") from error

        return FredSnapshotRecord(
            request_id=request_id,
            manifest_path=manifest_path,
            bundle_path=bundle_path,
            raw_series_path=raw_series_path,
            raw_observation_paths=raw_observation_paths,
            manifest_sha256=self._sha256(manifest_bytes),
            bundle_sha256=bundle_artifact.sha256,
        )
