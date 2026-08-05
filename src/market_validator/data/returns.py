"""Deterministic, missing-aware transformations of adjacent price pairs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import math
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from market_validator.data.bundle_io import load_data_bundle
from market_validator.data.models import (
    DataBundle,
    Identifier,
    NonEmptyString,
    Observation,
    QualitySeverity,
    QualityStatus,
    StrictDataModel,
)
from market_validator.research.enums import Transformation


BundleSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveSourceRow = Annotated[int, Field(ge=2)]
PositiveIntervalDays = Annotated[int, Field(gt=0)]
ReturnTransformation = Literal[
    Transformation.SIMPLE_RETURN,
    Transformation.LOG_RETURN,
]


class ReturnTransformationError(ValueError):
    """Raised when prices cannot be transformed without unsafe assumptions."""


class AbsolutePriceChangeError(ValueError):
    """Raised when absolute price changes cannot be derived safely."""


class ReturnTransformationParameters(StrictDataModel):
    """Auditable rules applied to every return in a series."""

    formula: Literal[
        "current_price / previous_price - 1",
        "log(current_price / previous_price)",
    ]
    first_price_policy: Literal["omit"] = "omit"
    provider_missing_gap_policy: Literal["exclude"] = "exclude"
    missing_value_fill: Literal["none"] = "none"
    availability_rule: Literal["max_endpoint_available_time"] = (
        "max_endpoint_available_time"
    )


class ReturnSourceTrace(StrictDataModel):
    """Immutable identity and quality summary of the source price bundle."""

    request_id: Identifier
    bundle_sha256: BundleSha256
    provider_id: Identifier
    dataset_id: NonEmptyString
    retrieved_at: datetime
    quality_status: QualityStatus
    rows_read: Annotated[int, Field(ge=0)]
    observations_parsed: Annotated[int, Field(ge=0)]
    quality_issue_codes: list[Identifier]

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value

    @field_serializer("retrieved_at", when_used="json")
    def serialize_retrieved_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _PricePairObservation(StrictDataModel):
    """Shared endpoint and availability contract for price-pair transforms."""

    start_observation_time: datetime
    end_observation_time: datetime
    start_available_time: datetime
    end_available_time: datetime
    available_time: datetime
    start_session_date: date
    end_session_date: date
    interval_days: PositiveIntervalDays
    start_source_row_number: PositiveSourceRow
    end_source_row_number: PositiveSourceRow
    timezone: NonEmptyString

    @field_validator(
        "start_observation_time",
        "end_observation_time",
        "start_available_time",
        "end_available_time",
        "available_time",
    )
    @classmethod
    def validate_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("price-pair timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.end_observation_time <= self.start_observation_time:
            raise ValueError("price-pair observation times must be strictly increasing")
        if self.end_session_date <= self.start_session_date:
            raise ValueError("price-pair session dates must be strictly increasing")
        if self.interval_days != (self.end_session_date - self.start_session_date).days:
            raise ValueError("interval_days must equal the actual session-date gap")
        if self.end_source_row_number <= self.start_source_row_number:
            raise ValueError("price-pair source rows must be strictly increasing")
        if self.available_time != max(
            self.start_available_time, self.end_available_time
        ):
            raise ValueError("available_time must equal the later endpoint availability")
        return self

    @field_serializer(
        "start_observation_time",
        "end_observation_time",
        "start_available_time",
        "end_available_time",
        "available_time",
        when_used="json",
    )
    def serialize_datetime(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ReturnObservation(_PricePairObservation):
    """One return with both source endpoints retained for audit."""

    instrument_id: Identifier
    source_field: NonEmptyString
    transformation: ReturnTransformation
    value: Annotated[float, Field(allow_inf_nan=False)]
    unit: Literal["decimal_return"] = "decimal_return"

    @model_validator(mode="after")
    def validate_return_value(self) -> Self:
        if not math.isfinite(self.value):
            raise ValueError("return value must be finite")
        return self


class _PricePairGapWarning(StrictDataModel):
    """Shared trace for a provider-reported gap between valid endpoints."""

    message: NonEmptyString
    start_session_date: date
    end_session_date: date
    interval_days: PositiveIntervalDays
    start_source_row_number: PositiveSourceRow
    end_source_row_number: PositiveSourceRow
    provider_missing_row_numbers: list[PositiveSourceRow] = Field(min_length=1)
    provider_missing_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_gap(self) -> Self:
        if self.end_session_date <= self.start_session_date:
            raise ValueError("gap session dates must be strictly increasing")
        if self.interval_days != (self.end_session_date - self.start_session_date).days:
            raise ValueError("gap interval_days must equal the session-date gap")
        if self.end_source_row_number <= self.start_source_row_number:
            raise ValueError("gap source rows must be strictly increasing")
        if self.provider_missing_count != len(self.provider_missing_row_numbers):
            raise ValueError("provider_missing_count must match its source rows")
        if self.provider_missing_row_numbers != sorted(
            set(self.provider_missing_row_numbers)
        ):
            raise ValueError("provider missing source rows must be unique and ordered")
        if any(
            row <= self.start_source_row_number or row >= self.end_source_row_number
            for row in self.provider_missing_row_numbers
        ):
            raise ValueError("provider missing rows must lie between return endpoints")
        return self


class ReturnGapWarning(_PricePairGapWarning):
    """A source-reported missing gap excluded from standard returns."""

    code: Literal["provider_missing_value_gap"] = "provider_missing_value_gap"
    excluded_from_standard_returns: Literal[True] = True


class AbsolutePriceChangeParameters(StrictDataModel):
    """Auditable rules applied to absolute price changes."""

    formula: Literal["current_price - previous_price"] = (
        "current_price - previous_price"
    )
    first_price_policy: Literal["omit"] = "omit"
    provider_missing_gap_policy: Literal["exclude"] = "exclude"
    missing_value_fill: Literal["none"] = "none"
    availability_rule: Literal["max_endpoint_available_time"] = (
        "max_endpoint_available_time"
    )


class AbsolutePriceChangeObservation(_PricePairObservation):
    """One signed first difference in the source price unit."""

    source_request_id: Identifier
    source_bundle_sha256: BundleSha256
    instrument_id: Identifier
    source_field: NonEmptyString
    transformation: Literal["absolute_price_change"] = "absolute_price_change"
    start_price: Annotated[float, Field(allow_inf_nan=False)]
    end_price: Annotated[float, Field(allow_inf_nan=False)]
    value: Annotated[float, Field(allow_inf_nan=False)]
    unit: NonEmptyString

    @model_validator(mode="after")
    def validate_price_change(self) -> Self:
        if not all(
            math.isfinite(value)
            for value in (self.start_price, self.end_price, self.value)
        ):
            raise ValueError("price change endpoints and value must be finite")
        if self.value != self.end_price - self.start_price:
            raise ValueError("price change value must equal end_price - start_price")
        return self


class AbsolutePriceChangeGapWarning(_PricePairGapWarning):
    """A source-reported gap excluded from absolute price changes."""

    code: Literal["provider_missing_value_gap"] = "provider_missing_value_gap"
    excluded_from_price_changes: Literal[True] = True


class ReturnSeries(StrictDataModel):
    """Serializable standard returns plus explicit exclusions and provenance."""

    schema_version: Literal["1.0"] = "1.0"
    source: ReturnSourceTrace
    instrument_id: Identifier
    source_field: NonEmptyString
    transformation: ReturnTransformation
    parameters: ReturnTransformationParameters
    input_observation_count: Annotated[int, Field(gt=0)]
    standard_return_count: Annotated[int, Field(ge=0)]
    excluded_gap_count: Annotated[int, Field(ge=0)]
    returns: list[ReturnObservation]
    gap_warnings: list[ReturnGapWarning]

    @model_validator(mode="after")
    def validate_counts_and_identity(self) -> Self:
        if self.standard_return_count != len(self.returns):
            raise ValueError("standard_return_count must match returns")
        if self.excluded_gap_count != len(self.gap_warnings):
            raise ValueError("excluded_gap_count must match gap_warnings")
        if (
            self.standard_return_count + self.excluded_gap_count
            != self.input_observation_count - 1
        ):
            raise ValueError("every adjacent price pair must be returned or excluded")
        for item in self.returns:
            if item.instrument_id != self.instrument_id:
                raise ValueError("return instrument_id must match the series")
            if item.source_field != self.source_field:
                raise ValueError("return source_field must match the series")
            if item.transformation is not self.transformation:
                raise ValueError("return transformation must match the series")
        return self


class AbsolutePriceChangeSeries(StrictDataModel):
    """Serializable signed price changes plus explicit exclusions and provenance."""

    schema_version: Literal["1.0"] = "1.0"
    source: ReturnSourceTrace
    instrument_id: Identifier
    source_field: NonEmptyString
    transformation: Literal["absolute_price_change"] = "absolute_price_change"
    formula: Literal["current_price - previous_price"] = (
        "current_price - previous_price"
    )
    unit: NonEmptyString
    parameters: AbsolutePriceChangeParameters
    input_observation_count: Annotated[int, Field(gt=0)]
    candidate_pair_count: Annotated[int, Field(ge=0)]
    price_change_count: Annotated[int, Field(ge=0)]
    excluded_gap_count: Annotated[int, Field(ge=0)]
    other_exclusion_count: Literal[0] = 0
    price_changes: list[AbsolutePriceChangeObservation]
    gap_warnings: list[AbsolutePriceChangeGapWarning]

    @model_validator(mode="after")
    def validate_counts_and_identity(self) -> Self:
        if self.candidate_pair_count != self.input_observation_count - 1:
            raise ValueError("candidate_pair_count must equal input prices minus one")
        if self.price_change_count != len(self.price_changes):
            raise ValueError("price_change_count must match price_changes")
        if self.excluded_gap_count != len(self.gap_warnings):
            raise ValueError("excluded_gap_count must match gap_warnings")
        if (
            self.price_change_count
            + self.excluded_gap_count
            + self.other_exclusion_count
            != self.candidate_pair_count
        ):
            raise ValueError("every candidate pair must be generated or explained")
        for item in self.price_changes:
            if item.source_request_id != self.source.request_id:
                raise ValueError("price change request ID must match source")
            if item.source_bundle_sha256 != self.source.bundle_sha256:
                raise ValueError("price change Bundle SHA-256 must match source")
            if item.instrument_id != self.instrument_id:
                raise ValueError("price change instrument_id must match the series")
            if item.source_field != self.source_field:
                raise ValueError("price change source_field must match the series")
            if item.unit != self.unit:
                raise ValueError("price change unit must match the source unit")
        return self


@dataclass(frozen=True, slots=True)
class _LoadedPriceBundle:
    path: Path
    content: bytes
    bundle: DataBundle
    source: ReturnSourceTrace


@dataclass(frozen=True, slots=True)
class _AdjacentPricePair:
    previous: Observation
    current: Observation
    previous_row: int
    current_row: int
    interval_days: int


@dataclass(frozen=True, slots=True)
class _ProviderMissingGap:
    previous: Observation
    current: Observation
    previous_row: int
    current_row: int
    interval_days: int
    missing_rows: tuple[int, ...]


PriceErrorType = type[ReturnTransformationError] | type[AbsolutePriceChangeError]


def _return_transformation(value: Transformation | str) -> ReturnTransformation:
    try:
        transformation = Transformation(value)
    except ValueError:
        raise ReturnTransformationError(
            f"unsupported return transformation: {value!r}"
        ) from None
    if transformation not in {
        Transformation.SIMPLE_RETURN,
        Transformation.LOG_RETURN,
    }:
        raise ReturnTransformationError(
            f"unsupported return transformation: {transformation.value!r}"
        )
    return transformation


def _load_price_bundle(
    path: str | Path,
    error_type: PriceErrorType,
) -> _LoadedPriceBundle:
    bundle_path = Path(path)
    before_bytes = bundle_path.read_bytes()
    bundle = load_data_bundle(bundle_path)
    after_bytes = bundle_path.read_bytes()
    if before_bytes != after_bytes:
        raise error_type("source Bundle changed while it was read")
    source = ReturnSourceTrace(
        request_id=bundle_path.stem,
        bundle_sha256=hashlib.sha256(before_bytes).hexdigest(),
        provider_id=bundle.source.provider_id,
        dataset_id=bundle.source.dataset_id,
        retrieved_at=bundle.source.retrieved_at,
        quality_status=bundle.quality.status,
        rows_read=bundle.quality.rows_read,
        observations_parsed=bundle.quality.observations_parsed,
        quality_issue_codes=[issue.code for issue in bundle.quality.issues],
    )
    return _LoadedPriceBundle(
        path=bundle_path,
        content=before_bytes,
        bundle=bundle,
        source=source,
    )


def _validate_prices(
    bundle: DataBundle,
    *,
    require_strictly_positive: bool,
    error_type: PriceErrorType,
) -> None:
    if bundle.requirement.transformation is not Transformation.LEVEL:
        raise error_type("price transformation requires a level DataBundle")
    if any(
        issue.severity is QualitySeverity.ERROR for issue in bundle.quality.issues
    ):
        raise error_type("source DataBundle contains quality errors")
    if not bundle.observations:
        raise error_type("source DataBundle contains no prices")

    seen_keys: set[tuple[str, str, datetime]] = set()
    previous = None
    for observation in bundle.observations:
        if observation.instrument_id != bundle.requirement.instrument_id:
            raise error_type("price instrument does not match requirement")
        if observation.field != bundle.requirement.field:
            raise error_type("price field does not match requirement")
        if not math.isfinite(observation.value):
            raise error_type("prices must be finite")
        if require_strictly_positive and observation.value <= 0:
            raise error_type("prices must be finite and strictly positive")
        key = (
            observation.instrument_id,
            observation.field,
            observation.observation_time,
        )
        if key in seen_keys:
            raise error_type("duplicate price observation key")
        seen_keys.add(key)
        if previous is not None:
            if observation.observation_time < previous.observation_time:
                raise error_type("price observation_time is out of order")
            if observation.session_date <= previous.session_date:
                raise error_type("price session_date must be strictly increasing")
        previous = observation


def _source_rows(
    bundle: DataBundle,
    error_type: PriceErrorType,
) -> tuple[list[int], set[int]]:
    missing_rows: list[int] = []
    for issue in bundle.quality.issues:
        if issue.code != "provider_missing_value":
            continue
        if issue.row_number is None:
            raise error_type("provider_missing_value requires a source row number")
        missing_rows.append(issue.row_number)

    missing_set = set(missing_rows)
    if len(missing_set) != len(missing_rows):
        raise error_type("provider missing source rows are duplicated")
    first_row = 2
    final_row = bundle.quality.rows_read + 1
    if any(row < first_row or row > final_row for row in missing_set):
        raise error_type("provider missing source row is out of range")

    observation_rows = [
        row for row in range(first_row, final_row + 1) if row not in missing_set
    ]
    if len(observation_rows) != len(bundle.observations):
        raise error_type(
            "source rows cannot be mapped uniquely without guessing skipped data"
        )
    return observation_rows, missing_set


def _partition_price_pairs(
    bundle: DataBundle,
    *,
    require_strictly_positive: bool,
    error_type: PriceErrorType,
) -> tuple[list[_AdjacentPricePair], list[_ProviderMissingGap]]:
    _validate_prices(
        bundle,
        require_strictly_positive=require_strictly_positive,
        error_type=error_type,
    )
    source_rows, missing_rows = _source_rows(bundle, error_type)
    pairs: list[_AdjacentPricePair] = []
    gaps: list[_ProviderMissingGap] = []
    for index in range(1, len(bundle.observations)):
        previous = bundle.observations[index - 1]
        current = bundle.observations[index]
        previous_row = source_rows[index - 1]
        current_row = source_rows[index]
        interval_days = (current.session_date - previous.session_date).days
        missing_between = tuple(
            sorted(row for row in missing_rows if previous_row < row < current_row)
        )
        if missing_between:
            gaps.append(
                _ProviderMissingGap(
                    previous=previous,
                    current=current,
                    previous_row=previous_row,
                    current_row=current_row,
                    interval_days=interval_days,
                    missing_rows=missing_between,
                )
            )
        else:
            pairs.append(
                _AdjacentPricePair(
                    previous=previous,
                    current=current,
                    previous_row=previous_row,
                    current_row=current_row,
                    interval_days=interval_days,
                )
            )
    return pairs, gaps


def _gap_fields(gap: _ProviderMissingGap) -> dict[str, object]:
    return {
        "message": (
            "excluded adjacent price pair across "
            f"{len(gap.missing_rows)} provider missing record(s)"
        ),
        "start_session_date": gap.previous.session_date,
        "end_session_date": gap.current.session_date,
        "interval_days": gap.interval_days,
        "start_source_row_number": gap.previous_row,
        "end_source_row_number": gap.current_row,
        "provider_missing_row_numbers": list(gap.missing_rows),
        "provider_missing_count": len(gap.missing_rows),
    }


def transform_price_bundle(
    path: str | Path,
    transformation: Transformation | str,
) -> ReturnSeries:
    """Load one price bundle and return standard returns without filling gaps."""
    loaded = _load_price_bundle(path, ReturnTransformationError)
    bundle = loaded.bundle
    selected_transformation = _return_transformation(transformation)
    pairs, gaps = _partition_price_pairs(
        bundle,
        require_strictly_positive=True,
        error_type=ReturnTransformationError,
    )

    formula = (
        "current_price / previous_price - 1"
        if selected_transformation is Transformation.SIMPLE_RETURN
        else "log(current_price / previous_price)"
    )
    parameters = ReturnTransformationParameters(formula=formula)
    returns: list[ReturnObservation] = []
    gap_warnings = [ReturnGapWarning(**_gap_fields(gap)) for gap in gaps]

    for pair in pairs:
        ratio = pair.current.value / pair.previous.value
        value = (
            ratio - 1.0
            if selected_transformation is Transformation.SIMPLE_RETURN
            else math.log(ratio)
        )
        returns.append(
            ReturnObservation(
                instrument_id=pair.current.instrument_id,
                source_field=pair.current.field,
                transformation=selected_transformation,
                value=value,
                start_observation_time=pair.previous.observation_time,
                end_observation_time=pair.current.observation_time,
                start_available_time=pair.previous.available_time,
                end_available_time=pair.current.available_time,
                available_time=max(
                    pair.previous.available_time, pair.current.available_time
                ),
                start_session_date=pair.previous.session_date,
                end_session_date=pair.current.session_date,
                interval_days=pair.interval_days,
                start_source_row_number=pair.previous_row,
                end_source_row_number=pair.current_row,
                timezone=pair.current.timezone,
            )
        )

    return ReturnSeries(
        source=loaded.source,
        instrument_id=bundle.requirement.instrument_id,
        source_field=bundle.requirement.field,
        transformation=selected_transformation,
        parameters=parameters,
        input_observation_count=len(bundle.observations),
        standard_return_count=len(returns),
        excluded_gap_count=len(gap_warnings),
        returns=returns,
        gap_warnings=gap_warnings,
    )


def transform_absolute_price_change(path: str | Path) -> AbsolutePriceChangeSeries:
    """Load one price bundle and derive signed first differences without filling."""
    loaded = _load_price_bundle(path, AbsolutePriceChangeError)
    bundle = loaded.bundle
    pairs, gaps = _partition_price_pairs(
        bundle,
        require_strictly_positive=False,
        error_type=AbsolutePriceChangeError,
    )
    price_changes = [
        AbsolutePriceChangeObservation(
            source_request_id=loaded.source.request_id,
            source_bundle_sha256=loaded.source.bundle_sha256,
            instrument_id=pair.current.instrument_id,
            source_field=pair.current.field,
            start_price=pair.previous.value,
            end_price=pair.current.value,
            value=pair.current.value - pair.previous.value,
            unit=bundle.requirement.unit,
            start_observation_time=pair.previous.observation_time,
            end_observation_time=pair.current.observation_time,
            start_available_time=pair.previous.available_time,
            end_available_time=pair.current.available_time,
            available_time=max(
                pair.previous.available_time, pair.current.available_time
            ),
            start_session_date=pair.previous.session_date,
            end_session_date=pair.current.session_date,
            interval_days=pair.interval_days,
            start_source_row_number=pair.previous_row,
            end_source_row_number=pair.current_row,
            timezone=pair.current.timezone,
        )
        for pair in pairs
    ]
    gap_warnings = [
        AbsolutePriceChangeGapWarning(**_gap_fields(gap)) for gap in gaps
    ]
    return AbsolutePriceChangeSeries(
        source=loaded.source,
        instrument_id=bundle.requirement.instrument_id,
        source_field=bundle.requirement.field,
        unit=bundle.requirement.unit,
        parameters=AbsolutePriceChangeParameters(),
        input_observation_count=len(bundle.observations),
        candidate_pair_count=max(len(bundle.observations) - 1, 0),
        price_change_count=len(price_changes),
        excluded_gap_count=len(gap_warnings),
        price_changes=price_changes,
        gap_warnings=gap_warnings,
    )
