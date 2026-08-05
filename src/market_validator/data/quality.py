"""Deterministic quality checks that never repair or reorder observations."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from market_validator.data.models import (
    DataQualityIssue,
    DataQualityReport,
    DataRequirement,
    Observation,
    QualitySeverity,
    QualityStatus,
)

ObservationRow = tuple[int, Observation]


def build_quality_report(
    rows_read: int,
    observation_rows: Iterable[ObservationRow],
    initial_issues: Iterable[DataQualityIssue] = (),
    requirement: DataRequirement | None = None,
) -> tuple[list[Observation], DataQualityReport]:
    """Check parsed rows in source order and return them without silent repair."""
    issues = list(initial_issues)
    selected_rows: list[ObservationRow] = []

    for row_number, observation in observation_rows:
        if requirement is not None:
            unexpected_identity = False
            if observation.instrument_id != requirement.instrument_id:
                issues.append(
                    DataQualityIssue(
                        code="unexpected_instrument",
                        severity=QualitySeverity.ERROR,
                        message=(
                            f"expected instrument {requirement.instrument_id!r}, got "
                            f"{observation.instrument_id!r}"
                        ),
                        row_number=row_number,
                    )
                )
                unexpected_identity = True
            if observation.field != requirement.field:
                issues.append(
                    DataQualityIssue(
                        code="unexpected_field",
                        severity=QualitySeverity.ERROR,
                        message=(
                            f"expected field {requirement.field!r}, got "
                            f"{observation.field!r}"
                        ),
                        row_number=row_number,
                    )
                )
                unexpected_identity = True
            if unexpected_identity:
                continue
            if not (
                requirement.start_date
                <= observation.session_date
                <= requirement.end_date
            ):
                continue
            if observation.currency != requirement.currency:
                issues.append(
                    DataQualityIssue(
                        code="currency_mismatch",
                        severity=QualitySeverity.ERROR,
                        message=(
                            f"expected currency {requirement.currency!r}, got "
                            f"{observation.currency!r}"
                        ),
                        row_number=row_number,
                    )
                )
            if observation.unit != requirement.unit:
                issues.append(
                    DataQualityIssue(
                        code="unit_mismatch",
                        severity=QualitySeverity.ERROR,
                        message=(
                            f"expected unit {requirement.unit!r}, got "
                            f"{observation.unit!r}"
                        ),
                        row_number=row_number,
                    )
                )
            if observation.timezone != requirement.timezone:
                issues.append(
                    DataQualityIssue(
                        code="timezone_mismatch",
                        severity=QualitySeverity.ERROR,
                        message=(
                            f"expected timezone {requirement.timezone!r}, got "
                            f"{observation.timezone!r}"
                        ),
                        row_number=row_number,
                    )
                )
        selected_rows.append((row_number, observation))

    seen_keys: dict[tuple[str, str, datetime], int] = {}
    last_times: dict[tuple[str, str], datetime] = {}
    timezones: dict[tuple[str, str], set[str]] = defaultdict(set)
    currencies: dict[tuple[str, str], set[str]] = defaultdict(set)
    units: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row_number, observation in selected_rows:
        series_key = (observation.instrument_id, observation.field)
        primary_key = (*series_key, observation.observation_time)
        previous_row = seen_keys.get(primary_key)
        if previous_row is not None:
            issues.append(
                DataQualityIssue(
                    code="duplicate_observation",
                    severity=QualitySeverity.ERROR,
                    message=f"duplicate primary key; first seen on row {previous_row}",
                    row_number=row_number,
                    observation_key=(
                        f"{observation.instrument_id}|{observation.field}|"
                        f"{observation.observation_time.isoformat()}"
                    ),
                )
            )
        else:
            seen_keys[primary_key] = row_number

        previous_time = last_times.get(series_key)
        if previous_time is not None and observation.observation_time < previous_time:
            issues.append(
                DataQualityIssue(
                    code="time_order_error",
                    severity=QualitySeverity.ERROR,
                    message="observation_time is earlier than the preceding source row",
                    row_number=row_number,
                )
            )
        last_times[series_key] = observation.observation_time
        timezones[series_key].add(observation.timezone)
        currencies[series_key].add(observation.currency)
        units[series_key].add(observation.unit)

    for series_key, values in timezones.items():
        if len(values) > 1:
            issues.append(
                DataQualityIssue(
                    code="inconsistent_timezone",
                    severity=QualitySeverity.ERROR,
                    message=f"series {series_key!r} contains multiple timezone values",
                )
            )
    for series_key, values in currencies.items():
        if len(values) > 1:
            issues.append(
                DataQualityIssue(
                    code="inconsistent_currency",
                    severity=QualitySeverity.ERROR,
                    message=f"series {series_key!r} contains multiple currencies",
                )
            )
    for series_key, values in units.items():
        if len(values) > 1:
            issues.append(
                DataQualityIssue(
                    code="inconsistent_unit",
                    severity=QualitySeverity.ERROR,
                    message=f"series {series_key!r} contains multiple units",
                )
            )

    observations = [observation for _, observation in selected_rows]
    if not observations:
        issues.append(
            DataQualityIssue(
                code="no_observations",
                severity=QualitySeverity.ERROR,
                message="no valid observations remain for this inspection",
            )
        )

    has_errors = any(issue.severity is QualitySeverity.ERROR for issue in issues)
    has_warnings = any(issue.severity is QualitySeverity.WARNING for issue in issues)
    status = (
        QualityStatus.FAIL
        if has_errors
        else QualityStatus.WARN
        if has_warnings
        else QualityStatus.PASS
    )
    times = [observation.observation_time for observation in observations]
    coverage_start = min(times).astimezone(timezone.utc) if times else None
    coverage_end = max(times).astimezone(timezone.utc) if times else None
    report = DataQualityReport(
        status=status,
        rows_read=rows_read,
        observations_parsed=len(observations),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        issues=issues,
    )
    return observations, report
