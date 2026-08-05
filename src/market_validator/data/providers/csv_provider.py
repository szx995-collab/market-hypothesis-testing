"""Strict local CSV provider implemented with the Python standard library."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import hashlib
from io import StringIO
import math
from pathlib import Path

from pydantic import ValidationError

from market_validator.data.models import (
    CSVInspectionResult,
    DataBundle,
    DataQualityIssue,
    DataRequirement,
    DataSourceMetadata,
    Observation,
    QualitySeverity,
)
from market_validator.data.providers.base import (
    DataProvider,
    ProviderCapabilities,
    ProviderStatus,
)
from market_validator.data.quality import ObservationRow, build_quality_report
from market_validator.research.enums import (
    AssetType,
    DataRevisionMode,
    Frequency,
    PriceAdjustment,
)

REQUIRED_COLUMNS = (
    "instrument_id",
    "field",
    "value",
    "observation_time",
    "available_time",
    "session_date",
    "timezone",
    "currency",
    "unit",
)


class CSVProviderError(ValueError):
    """Raised for file-level failures that cannot produce trustworthy metadata."""


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CSVProvider:
    """Read only one user-specified local long-table CSV file."""

    provider_id = "local_csv"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def status(self) -> ProviderStatus:
        ready = self.path.is_file()
        reason = (
            "Local CSV file is available."
            if ready
            else "Local CSV file does not exist or is not a regular file."
        )
        return ProviderStatus(
            provider_id=self.provider_id,
            available=True,
            ready=ready,
            reason=reason,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            supported_asset_types=list(AssetType),
            supported_markets=["provider-neutral local files"],
            supported_frequencies=[Frequency.ONE_DAY],
            supported_fields=["any explicitly declared long-table field"],
            supported_price_adjustments=list(PriceAdjustment),
            supports_continuous_futures=True,
            requires_authentication=False,
            requires_network=False,
            supports_local_files=True,
            supported_revision_policies=[DataRevisionMode.NOT_APPLICABLE],
        )

    def _read_source(self) -> tuple[bytes, DataSourceMetadata]:
        try:
            raw_bytes = self.path.read_bytes()
        except OSError as error:
            raise CSVProviderError(f"could not read local CSV file: {error}") from error
        digest = hashlib.sha256(raw_bytes).hexdigest()
        try:
            source_uri = self.path.resolve().as_uri()
        except ValueError as error:
            raise CSVProviderError(f"could not identify local CSV file: {error}") from error
        metadata = DataSourceMetadata(
            provider_id=self.provider_id,
            dataset_id=self.path.name,
            provider_symbol=None,
            source_uri=source_uri,
            retrieved_at=datetime.now(timezone.utc),
            public_request_parameters={"source_kind": "local_file"},
            content_sha256=digest,
            license_note="User-supplied local file; license terms are not asserted.",
            is_fallback=False,
        )
        return raw_bytes, metadata

    def _parse_rows(
        self, raw_bytes: bytes
    ) -> tuple[int, list[ObservationRow], list[DataQualityIssue]]:
        issues: list[DataQualityIssue] = []
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            issues.append(
                DataQualityIssue(
                    code="invalid_encoding",
                    severity=QualitySeverity.ERROR,
                    message=f"CSV must be UTF-8: {error}",
                )
            )
            return 0, [], issues

        try:
            reader = csv.DictReader(StringIO(text), strict=True)
            header = tuple(reader.fieldnames or ())
            missing_columns = sorted(set(REQUIRED_COLUMNS) - set(header))
            unexpected_columns = sorted(set(header) - set(REQUIRED_COLUMNS))
            if missing_columns:
                issues.append(
                    DataQualityIssue(
                        code="missing_columns",
                        severity=QualitySeverity.ERROR,
                        message="missing required columns: " + ", ".join(missing_columns),
                    )
                )
            if unexpected_columns:
                issues.append(
                    DataQualityIssue(
                        code="unexpected_columns",
                        severity=QualitySeverity.ERROR,
                        message="unexpected columns: " + ", ".join(unexpected_columns),
                    )
                )
            if missing_columns or unexpected_columns:
                return 0, [], issues

            parsed_rows: list[ObservationRow] = []
            rows_read = 0
            for row_number, row in enumerate(reader, start=2):
                rows_read += 1
                missing_values = sorted(
                    column
                    for column in REQUIRED_COLUMNS
                    if row.get(column) is None or not row[column].strip()
                )
                if missing_values:
                    issues.append(
                        DataQualityIssue(
                            code="missing_value",
                            severity=QualitySeverity.ERROR,
                            message="empty required values: " + ", ".join(missing_values),
                            row_number=row_number,
                        )
                    )
                    continue

                try:
                    numeric_value = float(row["value"])
                except ValueError:
                    issues.append(
                        DataQualityIssue(
                            code="invalid_numeric_value",
                            severity=QualitySeverity.ERROR,
                            message="value is not a valid number",
                            row_number=row_number,
                        )
                    )
                    continue
                if not math.isfinite(numeric_value):
                    issues.append(
                        DataQualityIssue(
                            code="non_finite_value",
                            severity=QualitySeverity.ERROR,
                            message="value must be finite",
                            row_number=row_number,
                        )
                    )
                    continue

                try:
                    observation_time = _parse_datetime(row["observation_time"])
                    available_time = _parse_datetime(row["available_time"])
                    session_date = date.fromisoformat(row["session_date"])
                except ValueError as error:
                    issues.append(
                        DataQualityIssue(
                            code="invalid_date_or_time",
                            severity=QualitySeverity.ERROR,
                            message=f"invalid ISO 8601 date or time: {error}",
                            row_number=row_number,
                        )
                    )
                    continue
                if observation_time.tzinfo is None or observation_time.utcoffset() is None:
                    issues.append(
                        DataQualityIssue(
                            code="naive_datetime",
                            severity=QualitySeverity.ERROR,
                            message="observation_time must include a timezone offset",
                            row_number=row_number,
                        )
                    )
                    continue
                if available_time.tzinfo is None or available_time.utcoffset() is None:
                    issues.append(
                        DataQualityIssue(
                            code="naive_datetime",
                            severity=QualitySeverity.ERROR,
                            message="available_time must include a timezone offset",
                            row_number=row_number,
                        )
                    )
                    continue
                if available_time < observation_time:
                    issues.append(
                        DataQualityIssue(
                            code="availability_before_observation",
                            severity=QualitySeverity.ERROR,
                            message="available_time is earlier than observation_time",
                            row_number=row_number,
                        )
                    )
                    continue

                try:
                    observation = Observation(
                        instrument_id=row["instrument_id"],
                        field=row["field"],
                        value=numeric_value,
                        observation_time=observation_time,
                        available_time=available_time,
                        session_date=session_date,
                        timezone=row["timezone"],
                        currency=row["currency"],
                        unit=row["unit"],
                    )
                except ValidationError as error:
                    messages = "; ".join(
                        detail["msg"]
                        for detail in error.errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        )
                    )
                    issues.append(
                        DataQualityIssue(
                            code="invalid_observation",
                            severity=QualitySeverity.ERROR,
                            message=messages,
                            row_number=row_number,
                        )
                    )
                    continue
                parsed_rows.append((row_number, observation))
        except csv.Error as error:
            issues.append(
                DataQualityIssue(
                    code="csv_parse_error",
                    severity=QualitySeverity.ERROR,
                    message=f"CSV parsing failed: {error}",
                )
            )
            return 0, [], issues

        return rows_read, parsed_rows, issues

    def inspect(self) -> CSVInspectionResult:
        raw_bytes, source = self._read_source()
        rows_read, parsed_rows, issues = self._parse_rows(raw_bytes)
        observations, quality = build_quality_report(
            rows_read=rows_read,
            observation_rows=parsed_rows,
            initial_issues=issues,
        )
        return CSVInspectionResult(
            observations=observations,
            source=source,
            quality=quality,
        )

    def parse_content(
        self,
        content: bytes,
        requirement: DataRequirement,
        path: Path,
        content_sha256: str,
    ) -> DataBundle:
        """Parse already-captured bytes into a DataBundle without re-reading."""
        rows_read, parsed_rows, issues = self._parse_rows(content)
        observations, quality = build_quality_report(
            rows_read=rows_read,
            observation_rows=parsed_rows,
            initial_issues=issues,
            requirement=requirement,
        )
        try:
            source_uri = path.as_uri()
        except ValueError as error:
            raise CSVProviderError(
                f"could not identify local CSV file: {error}"
            ) from error
        source = DataSourceMetadata(
            provider_id=self.provider_id,
            dataset_id=path.name,
            provider_symbol=path.name,
            source_uri=source_uri,
            retrieved_at=datetime.now(timezone.utc),
            public_request_parameters={"source_kind": "local_file"},
            content_sha256=content_sha256,
            license_note="User-supplied local file; license terms are not asserted.",
            is_fallback=False,
        )
        return DataBundle(
            requirement=requirement,
            observations=observations,
            source=source,
            quality=quality,
        )

    def fetch(self, requirement: DataRequirement) -> DataBundle:
        raw_bytes, source = self._read_source()
        rows_read, parsed_rows, issues = self._parse_rows(raw_bytes)
        observations, quality = build_quality_report(
            rows_read=rows_read,
            observation_rows=parsed_rows,
            initial_issues=issues,
            requirement=requirement,
        )
        return DataBundle(
            requirement=requirement,
            observations=observations,
            source=source,
            quality=quality,
        )


assert isinstance(CSVProvider(Path("unused.csv")), DataProvider)
