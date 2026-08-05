"""Pre-specified WTI per-observation price-change volatility comparison."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
import math
from pathlib import Path
import random
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from market_validator.data.models import Identifier, NonEmptyString, StrictDataModel
from market_validator.data.returns import (
    AbsolutePriceChangeError,
    AbsolutePriceChangeObservation,
    ReturnSourceTrace,
    transform_absolute_price_change,
)


HYPOTHESIS_TEXT = (
    "2020-03-01 至 2020-05-31 的 WTI 相邻有效报价价格变化波动，"
    "是否高于 2021-01-01 至 2024-12-31？"
)
NULL_HYPOTHESIS = (
    "shock-window per-observation price-change standard deviation is less than "
    "or equal to the reference-window standard deviation"
)
ALTERNATIVE_HYPOTHESIS = (
    "shock-window per-observation price-change standard deviation is greater "
    "than the reference-window standard deviation"
)
FIXED_ANALYSIS_AS_OF = datetime(2025, 1, 3, tzinfo=timezone.utc)
NEGATIVE_WTI_DATE = date(2020, 4, 20)


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeCount = Annotated[int, Field(ge=0)]
PositiveCount = Annotated[int, Field(gt=0)]


class PriceChangeVolatilityAnalysisError(ValueError):
    """Raised when the fixed analysis cannot be completed safely."""


class VolatilityConclusion(StrEnum):
    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class PriceChangeWindow(StrictDataModel):
    name: Identifier
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("window start_date must not be later than end_date")
        return self

    def contains(self, value: date) -> bool:
        return self.start_date <= value <= self.end_date


def _shock_window() -> PriceChangeWindow:
    return PriceChangeWindow(
        name="shock",
        start_date=date(2020, 3, 1),
        end_date=date(2020, 5, 31),
    )


def _reference_window() -> PriceChangeWindow:
    return PriceChangeWindow(
        name="reference",
        start_date=date(2021, 1, 1),
        end_date=date(2024, 12, 31),
    )


class PriceChangeVolatilityParameters(StrictDataModel):
    hypothesis: Literal[HYPOTHESIS_TEXT] = HYPOTHESIS_TEXT
    null_hypothesis: Literal[NULL_HYPOTHESIS] = NULL_HYPOTHESIS
    alternative_hypothesis: Literal[ALTERNATIVE_HYPOTHESIS] = ALTERNATIVE_HYPOTHESIS
    shock_window: PriceChangeWindow = Field(default_factory=_shock_window)
    reference_window: PriceChangeWindow = Field(default_factory=_reference_window)
    analysis_as_of: datetime = FIXED_ANALYSIS_AS_OF
    grouping_date: Literal["end_session_date"] = "end_session_date"
    statistic: Literal["sample_stddev_ratio"] = "sample_stddev_ratio"
    stddev_ddof: Literal[1] = 1
    mad_definition: Literal["median_absolute_deviation_from_median"] = (
        "median_absolute_deviation_from_median"
    )
    quantile_method: Literal["linear_type_7"] = "linear_type_7"
    bootstrap_method: Literal["moving_block"] = "moving_block"
    group_resampling: Literal["independent_within_group"] = (
        "independent_within_group"
    )
    block_length: Literal[5] = 5
    repetitions: Literal[10000] = 10000
    random_seed: Literal[20260804] = 20260804
    confidence_level: Literal[0.95] = 0.95
    conclusion_rule: Literal[
        "lower_gt_1_supported__upper_lt_1_not_supported__otherwise_insufficient"
    ] = "lower_gt_1_supported__upper_lt_1_not_supported__otherwise_insufficient"
    exclusion_precedence: Literal[
        "transform_gap_then_as_of_then_window_membership"
    ] = "transform_gap_then_as_of_then_window_membership"
    main_interval_policy: Literal[
        "all_valid_adjacent_quotes_including_non_one_day"
    ] = "all_valid_adjacent_quotes_including_non_one_day"

    @field_validator("analysis_as_of")
    @classmethod
    def validate_analysis_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analysis_as_of must be timezone-aware")
        normalized = value.astimezone(timezone.utc)
        if normalized != FIXED_ANALYSIS_AS_OF:
            raise ValueError("analysis_as_of is fixed at 2025-01-03T00:00:00Z")
        return normalized

    @model_validator(mode="after")
    def validate_fixed_disjoint_windows(self) -> Self:
        disjoint = (
            self.shock_window.end_date < self.reference_window.start_date
            or self.reference_window.end_date < self.shock_window.start_date
        )
        if not disjoint:
            raise ValueError("shock and reference windows must not overlap")
        if self.shock_window != _shock_window():
            raise ValueError("shock window is fixed at 2020-03-01 through 2020-05-31")
        if self.reference_window != _reference_window():
            raise ValueError(
                "reference window is fixed at 2021-01-01 through 2024-12-31"
            )
        return self

    @field_serializer("analysis_as_of", when_used="json")
    def serialize_analysis_as_of(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SampleSummary(StrictDataModel):
    count: Annotated[int, Field(ge=2)]
    mean: FiniteFloat
    median: FiniteFloat
    sample_stddev: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    mad: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    minimum: FiniteFloat
    maximum: FiniteFloat
    quantile_05: FiniteFloat
    quantile_25: FiniteFloat
    quantile_75: FiniteFloat
    quantile_95: FiniteFloat

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        ordered = (
            self.minimum
            <= self.quantile_05
            <= self.quantile_25
            <= self.median
            <= self.quantile_75
            <= self.quantile_95
            <= self.maximum
        )
        if not ordered:
            raise ValueError("sample quantiles must be ordered")
        if not self.minimum <= self.mean <= self.maximum:
            raise ValueError("sample mean must lie within the observed range")
        return self


class BootstrapConfidenceInterval(StrictDataModel):
    lower: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    upper: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    confidence_level: Literal[0.95] = 0.95
    method: Literal["moving_block_percentile"] = "moving_block_percentile"
    block_length: Literal[5] = 5
    repetitions: Literal[10000] = 10000
    random_seed: Literal[20260804] = 20260804

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.lower > self.upper:
            raise ValueError("confidence interval lower must not exceed upper")
        return self


class VolatilityEstimate(StrictDataModel):
    shock: SampleSummary
    reference: SampleSummary
    stddev_ratio: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    confidence_interval: BootstrapConfidenceInterval
    conclusion: VolatilityConclusion

    @model_validator(mode="after")
    def validate_conclusion(self) -> Self:
        expected = _conclusion(
            self.confidence_interval.lower, self.confidence_interval.upper
        )
        if self.conclusion is not expected:
            raise ValueError("conclusion does not match the pre-specified CI rule")
        return self


class RobustnessCheckResult(StrictDataModel):
    check_id: Literal[
        "interval_days_equal_1",
        "exclude_2020_04_20_endpoints",
    ]
    description: NonEmptyString
    shock_excluded_count: NonNegativeCount
    reference_excluded_count: NonNegativeCount
    estimate: VolatilityEstimate
    ratio_direction_gt_one: bool

    @model_validator(mode="after")
    def validate_direction(self) -> Self:
        if self.ratio_direction_gt_one != (self.estimate.stddev_ratio > 1.0):
            raise ValueError("ratio direction flag must match stddev_ratio")
        return self


class VolatilityComparisonWarning(StrictDataModel):
    code: Identifier
    message: NonEmptyString
    count: NonNegativeCount


class VolatilityComparisonError(StrictDataModel):
    code: Identifier
    message: NonEmptyString
    count: PositiveCount = 1


class VolatilityExclusionCounts(StrictDataModel):
    source_candidate_pair_count: NonNegativeCount
    transform_generated_count: NonNegativeCount
    transform_gap_excluded_count: NonNegativeCount
    transform_other_excluded_count: NonNegativeCount
    analysis_as_of_excluded_count: NonNegativeCount
    outside_windows_excluded_count: NonNegativeCount
    shock_included_count: NonNegativeCount
    reference_included_count: NonNegativeCount
    analysis_error_excluded_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_reconciliation(self) -> Self:
        if self.source_candidate_pair_count != (
            self.transform_generated_count
            + self.transform_gap_excluded_count
            + self.transform_other_excluded_count
        ):
            raise ValueError("source candidate-pair reconciliation failed")
        if self.transform_generated_count != (
            self.analysis_as_of_excluded_count
            + self.outside_windows_excluded_count
            + self.shock_included_count
            + self.reference_included_count
            + self.analysis_error_excluded_count
        ):
            raise ValueError("analysis inclusion/exclusion reconciliation failed")
        return self


class PriceChangeVolatilityResult(StrictDataModel):
    schema_version: Literal["1.0"] = "1.0"
    hypothesis: Literal[HYPOTHESIS_TEXT] = HYPOTHESIS_TEXT
    null_hypothesis: Literal[NULL_HYPOTHESIS] = NULL_HYPOTHESIS
    alternative_hypothesis: Literal[ALTERNATIVE_HYPOTHESIS] = ALTERNATIVE_HYPOTHESIS
    parameters: PriceChangeVolatilityParameters
    source: ReturnSourceTrace
    transformation: Literal["absolute_price_change"] = "absolute_price_change"
    transformation_formula: Literal["current_price - previous_price"] = (
        "current_price - previous_price"
    )
    unit: NonEmptyString
    volatility_label: Literal["per-observation price-change volatility"] = (
        "per-observation price-change volatility"
    )
    main: VolatilityEstimate
    robustness_checks: list[RobustnessCheckResult] = Field(min_length=2, max_length=2)
    final_conclusion: VolatilityConclusion
    final_conclusion_reason: NonEmptyString
    exclusions: VolatilityExclusionCounts
    transform_warning_count: NonNegativeCount
    transform_error_count: Literal[0] = 0
    warnings: list[VolatilityComparisonWarning]
    errors: list[VolatilityComparisonError]
    limitations: list[NonEmptyString] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.transform_warning_count != self.exclusions.transform_gap_excluded_count:
            raise ValueError("transform warning count must match excluded gaps")
        if self.errors:
            raise ValueError("a successful volatility result cannot contain errors")
        expected_final = self.main.conclusion
        if self.main.conclusion is VolatilityConclusion.SUPPORTED and any(
            not check.ratio_direction_gt_one for check in self.robustness_checks
        ):
            expected_final = VolatilityConclusion.INSUFFICIENT_EVIDENCE
        if self.final_conclusion is not expected_final:
            raise ValueError("final conclusion does not match robustness downgrade rule")
        return self


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _sample_stddev(values: list[float]) -> float:
    if len(values) < 2:
        raise PriceChangeVolatilityAnalysisError(
            "each analysis sample requires at least two observations"
        )
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / (
        len(values) - 1
    )
    result = math.sqrt(variance)
    if not math.isfinite(result):
        raise PriceChangeVolatilityAnalysisError("sample standard deviation is not finite")
    return result


def _summarize(values: list[float]) -> SampleSummary:
    if len(values) < 2:
        raise PriceChangeVolatilityAnalysisError(
            "each analysis sample requires at least two observations"
        )
    if not all(math.isfinite(value) for value in values):
        raise PriceChangeVolatilityAnalysisError("analysis sample contains non-finite data")
    ordered = sorted(values)
    mean = math.fsum(ordered) / len(ordered)
    median = _quantile(ordered, 0.5)
    absolute_deviations = [abs(value - median) for value in ordered]
    return SampleSummary(
        count=len(ordered),
        mean=mean,
        median=median,
        sample_stddev=_sample_stddev(ordered),
        mad=_quantile(absolute_deviations, 0.5),
        minimum=ordered[0],
        maximum=ordered[-1],
        quantile_05=_quantile(ordered, 0.05),
        quantile_25=_quantile(ordered, 0.25),
        quantile_75=_quantile(ordered, 0.75),
        quantile_95=_quantile(ordered, 0.95),
    )


def _moving_block_sample(
    values: list[float], block_length: int, generator: random.Random
) -> list[float]:
    if len(values) < block_length:
        raise PriceChangeVolatilityAnalysisError(
            "moving-block bootstrap sample is shorter than block length"
        )
    maximum_start = len(values) - block_length
    sampled: list[float] = []
    while len(sampled) < len(values):
        start = generator.randrange(maximum_start + 1)
        sampled.extend(values[start : start + block_length])
    return sampled[: len(values)]


def _bootstrap_interval(
    shock_values: list[float],
    reference_values: list[float],
    parameters: PriceChangeVolatilityParameters,
) -> BootstrapConfidenceInterval:
    if len(shock_values) < parameters.block_length or len(reference_values) < parameters.block_length:
        raise PriceChangeVolatilityAnalysisError(
            "each bootstrap sample must contain at least block_length observations"
        )
    generator = random.Random(parameters.random_seed)
    ratios: list[float] = []
    for _ in range(parameters.repetitions):
        shock_sample = _moving_block_sample(
            shock_values, parameters.block_length, generator
        )
        reference_sample = _moving_block_sample(
            reference_values, parameters.block_length, generator
        )
        denominator = _sample_stddev(reference_sample)
        numerator = _sample_stddev(shock_sample)
        if denominator <= 0:
            raise PriceChangeVolatilityAnalysisError(
                "bootstrap reference standard deviation must be positive"
            )
        ratio = numerator / denominator
        if not math.isfinite(ratio) or ratio <= 0:
            raise PriceChangeVolatilityAnalysisError(
                "bootstrap standard-deviation ratio must be finite and positive"
            )
        ratios.append(ratio)
    return BootstrapConfidenceInterval(
        lower=_quantile(ratios, 0.025),
        upper=_quantile(ratios, 0.975),
    )


def _conclusion(lower: float, upper: float) -> VolatilityConclusion:
    if lower > 1.0:
        return VolatilityConclusion.SUPPORTED
    if upper < 1.0:
        return VolatilityConclusion.NOT_SUPPORTED
    return VolatilityConclusion.INSUFFICIENT_EVIDENCE


def _estimate(
    shock: list[AbsolutePriceChangeObservation],
    reference: list[AbsolutePriceChangeObservation],
    parameters: PriceChangeVolatilityParameters,
) -> VolatilityEstimate:
    shock_values = [item.value for item in shock]
    reference_values = [item.value for item in reference]
    shock_summary = _summarize(shock_values)
    reference_summary = _summarize(reference_values)
    if shock_summary.sample_stddev <= 0 or reference_summary.sample_stddev <= 0:
        raise PriceChangeVolatilityAnalysisError(
            "both sample standard deviations must be positive"
        )
    ratio = shock_summary.sample_stddev / reference_summary.sample_stddev
    if not math.isfinite(ratio) or ratio <= 0:
        raise PriceChangeVolatilityAnalysisError(
            "standard-deviation ratio must be finite and positive"
        )
    interval = _bootstrap_interval(shock_values, reference_values, parameters)
    return VolatilityEstimate(
        shock=shock_summary,
        reference=reference_summary,
        stddev_ratio=ratio,
        confidence_interval=interval,
        conclusion=_conclusion(interval.lower, interval.upper),
    )


def _robustness(
    check_id: Literal[
        "interval_days_equal_1", "exclude_2020_04_20_endpoints"
    ],
    description: str,
    shock_main: list[AbsolutePriceChangeObservation],
    reference_main: list[AbsolutePriceChangeObservation],
    shock_filtered: list[AbsolutePriceChangeObservation],
    reference_filtered: list[AbsolutePriceChangeObservation],
    parameters: PriceChangeVolatilityParameters,
) -> RobustnessCheckResult:
    estimate = _estimate(shock_filtered, reference_filtered, parameters)
    return RobustnessCheckResult(
        check_id=check_id,
        description=description,
        shock_excluded_count=len(shock_main) - len(shock_filtered),
        reference_excluded_count=len(reference_main) - len(reference_filtered),
        estimate=estimate,
        ratio_direction_gt_one=estimate.stddev_ratio > 1.0,
    )


def compare_price_change_volatility(
    path: str | Path,
    parameters: PriceChangeVolatilityParameters,
) -> PriceChangeVolatilityResult:
    """Execute only the fixed retrospective WTI volatility comparison."""
    try:
        transformed = transform_absolute_price_change(path)
    except (AbsolutePriceChangeError, ValidationError, OSError, ValueError) as error:
        raise PriceChangeVolatilityAnalysisError(
            "absolute_price_change transformation failed"
        ) from error

    shock: list[AbsolutePriceChangeObservation] = []
    reference: list[AbsolutePriceChangeObservation] = []
    as_of_excluded = 0
    outside_excluded = 0
    for item in transformed.price_changes:
        if item.available_time > parameters.analysis_as_of:
            as_of_excluded += 1
        elif parameters.shock_window.contains(item.end_session_date):
            shock.append(item)
        elif parameters.reference_window.contains(item.end_session_date):
            reference.append(item)
        else:
            outside_excluded += 1

    main = _estimate(shock, reference, parameters)
    one_day_shock = [item for item in shock if item.interval_days == 1]
    one_day_reference = [item for item in reference if item.interval_days == 1]
    without_negative_date_shock = [
        item
        for item in shock
        if item.start_session_date != NEGATIVE_WTI_DATE
        and item.end_session_date != NEGATIVE_WTI_DATE
    ]
    without_negative_date_reference = [
        item
        for item in reference
        if item.start_session_date != NEGATIVE_WTI_DATE
        and item.end_session_date != NEGATIVE_WTI_DATE
    ]
    robustness_checks = [
        _robustness(
            "interval_days_equal_1",
            "Keep only adjacent quotes whose session-date interval is exactly one day.",
            shock,
            reference,
            one_day_shock,
            one_day_reference,
            parameters,
        ),
        _robustness(
            "exclude_2020_04_20_endpoints",
            "Exclude changes whose start or end session date is 2020-04-20.",
            shock,
            reference,
            without_negative_date_shock,
            without_negative_date_reference,
            parameters,
        ),
    ]

    final_conclusion = main.conclusion
    reason = "Final conclusion equals the pre-specified main confidence-interval rule."
    if main.conclusion is VolatilityConclusion.SUPPORTED and any(
        not check.ratio_direction_gt_one for check in robustness_checks
    ):
        final_conclusion = VolatilityConclusion.INSUFFICIENT_EVIDENCE
        reason = (
            "Main analysis was supported, but at least one robustness ratio was not "
            "greater than one."
        )

    exclusions = VolatilityExclusionCounts(
        source_candidate_pair_count=transformed.candidate_pair_count,
        transform_generated_count=transformed.price_change_count,
        transform_gap_excluded_count=transformed.excluded_gap_count,
        transform_other_excluded_count=transformed.other_exclusion_count,
        analysis_as_of_excluded_count=as_of_excluded,
        outside_windows_excluded_count=outside_excluded,
        shock_included_count=len(shock),
        reference_included_count=len(reference),
    )
    warnings = []
    if transformed.excluded_gap_count:
        warnings.append(
            VolatilityComparisonWarning(
                code="provider_missing_value_gap",
                message=(
                    "Source-reported missing gaps were excluded before window "
                    "classification."
                ),
                count=transformed.excluded_gap_count,
            )
        )
    non_one_day_count = sum(item.interval_days != 1 for item in shock + reference)
    if non_one_day_count:
        warnings.append(
            VolatilityComparisonWarning(
                code="non_one_day_intervals_in_main",
                message=(
                    "Main analysis includes valid adjacent quotes with intervals other "
                    "than one calendar day."
                ),
                count=non_one_day_count,
            )
        )

    return PriceChangeVolatilityResult(
        parameters=parameters,
        source=transformed.source,
        unit=transformed.unit,
        main=main,
        robustness_checks=robustness_checks,
        final_conclusion=final_conclusion,
        final_conclusion_reason=reason,
        exclusions=exclusions,
        transform_warning_count=transformed.excluded_gap_count,
        warnings=warnings,
        errors=[],
        limitations=[
            "This is a retrospective volatility comparison, not a causal claim.",
            "The result is not evidence of predictive ability or trading profitability.",
            "Volatility is per observed adjacent quote change and is not annualized.",
            "Calendar-day intervals vary because valid Friday-to-Monday pairs are kept.",
            "The percentile moving-block bootstrap is conditional on the fixed windows, "
            "block length, and observed source data.",
        ],
    )
