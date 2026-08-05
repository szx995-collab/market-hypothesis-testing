"""Strict data-planning, observation, provenance, and quality contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
import math
from typing import Annotated, Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    computed_field,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from market_validator.research.enums import (
    AssetType,
    ContractRollMethod,
    DataRevisionMode,
    Frequency,
    PriceAdjustment,
    Transformation,
)
from market_validator.research.models import (
    AlignmentSpec,
    DataRevisionSpec,
    InformationCutoffSpec,
)
from market_validator.research.validation import validate_iana_timezone

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]


class StrictDataModel(BaseModel):
    """Base model for deterministic data contracts."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class DataRequirementStatus(StrEnum):
    READY = "ready"
    UNRESOLVED = "unresolved"


class QualitySeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class QualityStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class TimePrecision(StrEnum):
    TIMESTAMP = "timestamp"
    DATE = "date"


class DataRequirement(StrictDataModel):
    """Provider-neutral raw-data requirement for one ResearchSpec variable."""

    requirement_id: Identifier
    variable_id: Identifier
    instrument_id: Identifier
    asset_type: AssetType
    field: NonEmptyString
    transformation: Transformation
    rolling_window_periods: Annotated[int, Field(gt=0)] | None
    start_date: date
    end_date: date
    frequency: Frequency
    lag_periods: NonNegativeInt
    availability_lag_periods: NonNegativeInt
    required_pre_sample_periods: NonNegativeInt
    market: NonEmptyString
    calendar_id: Identifier | None
    timezone: NonEmptyString
    currency: CurrencyCode
    unit: NonEmptyString
    price_adjustment: PriceAdjustment | None
    contract_roll_method: ContractRollMethod | None
    proxy_for: NonEmptyString | None
    continuous_contract: bool
    information_cutoff: InformationCutoffSpec | None
    revision_policy: DataRevisionSpec = Field(default_factory=DataRevisionSpec)
    status: DataRequirementStatus
    warnings: list[NonEmptyString]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)

    @model_validator(mode="after")
    def validate_requirement_consistency(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        if self.transformation in {
            Transformation.ROLLING_MEAN,
            Transformation.ZSCORE,
        }:
            if self.rolling_window_periods is None:
                raise ValueError(
                    "rolling transformations require rolling_window_periods"
                )
            transformation_periods = self.rolling_window_periods - 1
        else:
            if self.rolling_window_periods is not None:
                raise ValueError(
                    "rolling_window_periods is only valid for rolling transformations"
                )
            transformation_periods = (
                0 if self.transformation is Transformation.LEVEL else 1
            )
        expected = transformation_periods + self.lag_periods
        if self.required_pre_sample_periods != expected:
            raise ValueError(
                "required_pre_sample_periods must equal the transformation "
                "history plus lag_periods"
            )
        return self


class DataPlan(StrictDataModel):
    """Deterministic collection of provider-neutral data requirements."""

    schema_version: Literal["1.0"]
    plan_id: Identifier
    research_spec_id: Identifier
    requirements: list[DataRequirement] = Field(min_length=1)
    target_calendar: Identifier
    alignment_policy: AlignmentSpec | None
    unresolved_instruments: list[Identifier]
    warnings: list[NonEmptyString]


class Observation(StrictDataModel):
    """One normalized long-table observation without derived calculations."""

    instrument_id: Identifier
    field: NonEmptyString
    value: Annotated[float, Field(allow_inf_nan=False)]
    observation_time: datetime
    available_time: datetime
    session_date: date
    timezone: NonEmptyString
    currency: CurrencyCode
    unit: NonEmptyString
    observation_precision: TimePrecision = TimePrecision.TIMESTAMP
    availability_precision: TimePrecision = TimePrecision.TIMESTAMP
    vintage_date: date | None = None
    revision_policy: DataRevisionMode = DataRevisionMode.NOT_APPLICABLE
    availability_assumption: NonEmptyString | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)

    @field_validator("observation_time", "available_time")
    @classmethod
    def validate_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available_time < self.observation_time:
            raise ValueError("available_time must not be earlier than observation_time")
        if not math.isfinite(self.value):
            raise ValueError("value must be finite")
        return self

    @field_serializer("observation_time", "available_time", when_used="json")
    def serialize_datetime_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


SENSITIVE_PARAMETER_FRAGMENTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)


def _is_sensitive_name(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_PARAMETER_FRAGMENTS)


class DataSourceMetadata(StrictDataModel):
    """Auditable, secret-free metadata for the exact raw bytes used."""

    provider_id: Identifier
    dataset_id: NonEmptyString
    provider_symbol: NonEmptyString | None
    source_uri: NonEmptyString
    retrieved_at: datetime
    public_request_parameters: dict[str, str | int | float | bool | None]
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    license_note: NonEmptyString
    is_fallback: bool

    @field_validator("public_request_parameters")
    @classmethod
    def reject_sensitive_parameters(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        sensitive = sorted(key for key in value if _is_sensitive_name(key))
        if sensitive:
            raise ValueError(
                "public_request_parameters contains forbidden sensitive names: "
                + ", ".join(sensitive)
            )
        return value

    @field_validator("source_uri")
    @classmethod
    def reject_credentials_in_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source_uri must not contain user information")
        sensitive = sorted(
            key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            if _is_sensitive_name(key)
        )
        sensitive.extend(
            key for key, _ in parse_qsl(parsed.fragment, keep_blank_values=True)
            if _is_sensitive_name(key)
        )
        if sensitive:
            raise ValueError(
                "source_uri contains forbidden authentication parameters: "
                + ", ".join(sensitive)
            )
        return value

    @field_validator("retrieved_at")
    @classmethod
    def validate_utc_retrieval_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("retrieved_at must be expressed in UTC")
        return value

    @field_serializer("retrieved_at", when_used="json")
    def serialize_retrieved_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DataQualityIssue(StrictDataModel):
    """One explicit problem found without silently repairing source data."""

    code: Identifier
    severity: QualitySeverity
    message: NonEmptyString
    row_number: Annotated[int, Field(ge=2)] | None = None
    observation_key: NonEmptyString | None = None


class DataQualityReport(StrictDataModel):
    """Quality status and coverage summary for one source inspection."""

    status: QualityStatus
    rows_read: NonNegativeInt
    observations_parsed: NonNegativeInt
    coverage_start: datetime | None
    coverage_end: datetime | None
    issues: list[DataQualityIssue]

    @model_validator(mode="after")
    def validate_status_and_coverage(self) -> Self:
        if self.observations_parsed > self.rows_read:
            raise ValueError("observations_parsed must not exceed rows_read")
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("coverage_start and coverage_end must be set together")
        for value in (self.coverage_start, self.coverage_end):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("coverage datetimes must be timezone-aware")
        if (
            self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start > self.coverage_end
        ):
            raise ValueError("coverage_start must not be later than coverage_end")

        has_error = any(
            issue.severity is QualitySeverity.ERROR for issue in self.issues
        )
        has_warning = any(
            issue.severity is QualitySeverity.WARNING for issue in self.issues
        )
        expected = (
            QualityStatus.FAIL
            if has_error
            else QualityStatus.WARN
            if has_warning
            else QualityStatus.PASS
        )
        if self.status is not expected:
            raise ValueError(
                f"status must be {expected.value!r} for the supplied issues"
            )
        return self

    @field_serializer("coverage_start", "coverage_end", when_used="json")
    def serialize_coverage(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DataBundle(StrictDataModel):
    """Normalized observations plus their exact requirement, source, and QA."""

    requirement: DataRequirement
    observations: list[Observation]
    source: DataSourceMetadata
    quality: DataQualityReport

    @computed_field
    @property
    def status(self) -> QualityStatus:
        return self.quality.status


class CSVInspectionResult(StrictDataModel):
    """Provider-independent summary used by the local CSV inspection CLI."""

    observations: list[Observation]
    source: DataSourceMetadata
    quality: DataQualityReport
