"""Deterministic pure-Python statistics (v0.4.0 Phase 2).

Implements Pearson correlation, Spearman correlation (average ranks),
and OLS with classic / HC1 / Newey-West (Bartlett) covariance, Student-t
inference, Fisher-z confidence intervals, and multiple-testing corrections
(none / bonferroni / holm / benjamini_hochberg). No third-party numeric
dependencies. All persisted floats are normalized to 12 significant decimal
digits; NaN / Infinity / overflow are rejected.
"""

from __future__ import annotations

import math
from typing import Sequence

from market_validator.analysis.execution_models import (
    AnalysisExecutionErrorCode,
    fail_execution,
)

SIGNIFICANT_DIGITS = 12


def normalize_float(value: float) -> float:
    """Normalize to 12 significant decimal digits; reject non-finite."""
    if not math.isfinite(value):
        fail_execution(
            AnalysisExecutionErrorCode.NON_FINITE_STATISTIC,
            "statistic is not finite",
        )
    if abs(value) >= 1e308:
        fail_execution(
            AnalysisExecutionErrorCode.NON_FINITE_STATISTIC,
            "statistic overflows the numeric profile",
        )
    return float(f"{value:.{SIGNIFICANT_DIGITS}g}")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _variance(values: Sequence[float], mean: float) -> float:
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _pearson_from_arrays(
    x: Sequence[float], y: Sequence[float]
) -> tuple[float, float, float]:
    """Return (r, mean_x, mean_y); zero-variance raises."""
    n = len(x)
    mean_x = _mean(x)
    mean_y = _mean(y)
    ss_x = sum((value - mean_x) ** 2 for value in x)
    ss_y = sum((value - mean_y) ** 2 for value in y)
    scale_x = max(abs(value) for value in x)
    scale_y = max(abs(value) for value in y)
    if ss_x <= 1e-300 * max(1.0, scale_x * scale_x) or ss_y <= 1e-300 * max(
        1.0, scale_y * scale_y
    ):
        fail_execution(
            AnalysisExecutionErrorCode.ZERO_VARIANCE,
            "correlation requires strictly positive variance in both "
            "variables",
        )
    covariance = sum(
        (x_value - mean_x) * (y_value - mean_y)
        for x_value, y_value in zip(x, y)
    )
    r = covariance / math.sqrt(ss_x * ss_y)
    r = max(-1.0, min(1.0, r))
    return normalize_float(r), mean_x, mean_y


# ---------------------------------------------------------------------------
# Student-t distribution (regularized incomplete beta, standard library)
# ---------------------------------------------------------------------------

def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz-style continued fraction for I_x(a, b)."""
    max_iterations = 400
    epsilon = 1e-14
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for iteration in range(1, max_iterations + 1):
        m2 = 2 * iteration
        aa = (
            iteration * (b - iteration) * x
            / ((qam + m2) * (a + m2))
        )
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = (
            -(a + iteration) * (qab + iteration) * x
            / ((a + m2) * (qap + m2))
        )
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def _regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_prefix = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    prefix = math.exp(log_prefix)
    if x < (a + 1.0) / (a + b + 2.0):
        return prefix * _beta_continued_fraction(a, b, x) / a
    return 1.0 - prefix * _beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(t: float, degrees_of_freedom: float) -> float:
    """Student-t CDF value P(T <= t)."""
    if degrees_of_freedom <= 0:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "degrees of freedom must be positive",
        )
    x = degrees_of_freedom / (degrees_of_freedom + t * t)
    tail = 0.5 * _regularized_beta(
        degrees_of_freedom / 2.0, 0.5, x
    )
    if t >= 0.0:
        return 1.0 - tail
    return tail


def student_t_survival_two_sided(t: float, degrees_of_freedom: int) -> float:
    """Two-sided tail probability for |T| >= |t|."""
    if degrees_of_freedom <= 0:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "degrees of freedom must be positive",
        )
    return 2.0 * (1.0 - student_t_cdf(abs(t), degrees_of_freedom))


def student_t_ppf(probability: float, degrees_of_freedom: int) -> float:
    """Inverse two-sided Student-t CDF via deterministic bisection."""
    if not 0.0 < probability < 1.0:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "probability must lie strictly between zero and one",
        )
    low = -1e6
    high = 1e6
    for _ in range(400):
        mid = (low + high) / 2.0
        if student_t_cdf(mid, degrees_of_freedom) < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _normal_ppf(probability: float) -> float:
    """Inverse standard-normal CDF via deterministic bisection on math.erf."""
    low = -10.0
    high = 10.0
    for _ in range(200):
        mid = (low + high) / 2.0
        cdf = 0.5 * (1.0 + math.erf(mid / math.sqrt(2.0)))
        if cdf < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _correlation_t(r: float, degrees_of_freedom: int) -> float:
    """t statistic for a correlation; perfect correlation maps to a finite
    large t so NaN/Infinity never enters persisted artifacts."""
    squared = 1.0 - r * r
    if squared <= 1e-300:
        return normalize_float(
            math.copysign(1e15, r) if r != 0.0 else 0.0
        )
    return normalize_float(r * math.sqrt(degrees_of_freedom / squared))


def _two_sided_p_value(t: float, degrees_of_freedom: int) -> float:
    raw = student_t_survival_two_sided(t, degrees_of_freedom)
    if raw <= 0.0:
        return 0.0
    if raw >= 1.0:
        return 1.0
    return normalize_float(raw)


def _fisher_z_confidence_interval(
    r: float, n: int, significance_level: float
) -> tuple[float, float]:
    if n <= 3:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "Fisher-z confidence interval requires at least four "
            "observations",
        )
    clamped = max(-1.0, min(1.0, r))
    if clamped >= 1.0:
        z = math.inf
    elif clamped <= -1.0:
        z = -math.inf
    else:
        z = math.atanh(clamped)
    se = 1.0 / math.sqrt(n - 3)
    critical = _normal_ppf(1.0 - significance_level / 2.0)
    lower = math.tanh(z - critical * se)
    upper = math.tanh(z + critical * se)
    return normalize_float(lower), normalize_float(upper)


# ---------------------------------------------------------------------------
# Pearson / Spearman
# ---------------------------------------------------------------------------

def compute_pearson_result(
    x: Sequence[float],
    y: Sequence[float],
    *,
    significance_level: float,
) -> dict[str, float]:
    if len(x) != len(y) or len(x) < 4:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "Pearson inference requires at least four paired observations",
        )
    r, _mean_x, _mean_y = _pearson_from_arrays(x, y)
    n = len(x)
    degrees_of_freedom = n - 2
    t = _correlation_t(r, degrees_of_freedom)
    p_value = _two_sided_p_value(t, degrees_of_freedom)
    lower, upper = _fisher_z_confidence_interval(
        r, n, significance_level
    )
    return {
        "coefficient": r,
        "sample_size": n,
        "t_statistic": t,
        "degrees_of_freedom": degrees_of_freedom,
        "p_value": p_value,
        "confidence_interval_lower": lower,
        "confidence_interval_upper": upper,
    }


def _average_ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index
        while end + 1 < len(indexed) and (
            indexed[end + 1][1] == indexed[index][1]
        ):
            end += 1
        average = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[indexed[position][0]] = average
        index = end + 1
    return ranks


def compute_spearman_result(
    x: Sequence[float],
    y: Sequence[float],
    *,
    significance_level: float,
) -> dict[str, float]:
    if len(x) != len(y) or len(x) < 4:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "Spearman inference requires at least four paired observations",
        )
    rank_x = _average_ranks(x)
    rank_y = _average_ranks(y)
    rho, _mean_x, _mean_y = _pearson_from_arrays(rank_x, rank_y)
    n = len(x)
    degrees_of_freedom = n - 2
    t = _correlation_t(rho, degrees_of_freedom)
    p_value = _two_sided_p_value(t, degrees_of_freedom)
    lower, upper = _fisher_z_confidence_interval(
        rho, n, significance_level
    )
    return {
        "coefficient": rho,
        "sample_size": n,
        "t_statistic": t,
        "degrees_of_freedom": degrees_of_freedom,
        "p_value": p_value,
        "confidence_interval_lower": lower,
        "confidence_interval_upper": upper,
    }


# ---------------------------------------------------------------------------
# OLS
# ---------------------------------------------------------------------------

def _matrix_inverse(matrix: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan inversion; raises on singular matrices."""
    n = len(matrix)
    augmented = [
        row[:] + [1.0 if column == row_index else 0.0
                  for column in range(n)]
        for row_index, row in enumerate(matrix)
    ]
    for pivot in range(n):
        pivot_row = pivot
        best = abs(augmented[pivot][pivot])
        for candidate in range(pivot + 1, n):
            value = abs(augmented[candidate][pivot])
            if value > best:
                best = value
                pivot_row = candidate
        scale = max(abs(augmented[candidate][pivot]) for candidate in range(n))
        if best <= 1e-12 * max(1.0, scale):
            fail_execution(
                AnalysisExecutionErrorCode.SINGULAR_DESIGN_MATRIX,
                "the design matrix is singular",
            )
        if pivot_row != pivot:
            augmented[pivot], augmented[pivot_row] = (
                augmented[pivot_row],
                augmented[pivot],
            )
        divisor = augmented[pivot][pivot]
        augmented[pivot] = [value / divisor for value in augmented[pivot]]
        for row_index in range(n):
            if row_index == pivot:
                continue
            factor = augmented[row_index][pivot]
            augmented[row_index] = [
                value - factor * pivot_value
                for value, pivot_value in zip(
                    augmented[row_index], augmented[pivot]
                )
            ]
    return [
        [row[n + column] for column in range(n)]
        for row in augmented
    ]


def _matvec(matrix: list[list[float]], vector: Sequence[float]) -> list[float]:
    return [
        sum(matrix[row][column] * vector[column]
            for column in range(len(vector)))
        for row in range(len(matrix))
    ]


def _matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    n = len(a)
    m = len(b[0])
    p = len(b)
    return [
        [
            sum(a[row][k] * b[k][column] for k in range(p))
            for column in range(m)
        ]
        for row in range(n)
    ]


def _transpose(matrix: list[list[float]]) -> list[list[float]]:
    return [
        [matrix[row][column] for row in range(len(matrix))]
        for column in range(len(matrix[0]))
    ]


def compute_ols_result(
    x_columns: dict[str, Sequence[float]],
    y: Sequence[float],
    *,
    include_intercept: bool,
    covariance_estimator: str,
    newey_west_max_lags: int | None,
    significance_level: float,
) -> dict[str, object]:
    """Fit OLS; covariance in classic / hc1 / newey_west."""
    n = len(y)
    if not x_columns:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "OLS requires at least one predictor",
        )
    for column in x_columns.values():
        if len(column) != n:
            fail_execution(
                AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
                "all predictors must match the outcome length",
            )
    names = []
    design: list[list[float]] = []
    if include_intercept:
        names.append("intercept")
        design.append([1.0] * n)
    for name, column in x_columns.items():
        names.append(name)
        design.append(list(column))
    k = len(names)
    if n <= k:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "observation count must exceed the parameter count",
        )
    x_matrix = _transpose(design)  # n x k
    xtx = _matmul(_transpose(x_matrix), x_matrix)  # k x k
    inverse = _matrix_inverse(xtx)
    xty = _matvec(_transpose(x_matrix), list(y))
    coefficients = _matvec(inverse, xty)
    for value in coefficients:
        if not math.isfinite(value):
            fail_execution(
                AnalysisExecutionErrorCode.NON_FINITE_STATISTIC,
                "OLS coefficient is not finite",
            )
    residuals = [
        y_value - sum(
            design[column][row] * coefficients[column]
            for column in range(k)
        )
        for row, y_value in enumerate(y)
    ]
    sse = sum(value * value for value in residuals)
    degrees_of_freedom = n - k
    if degrees_of_freedom <= 0:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
            "residual degrees of freedom must be positive",
        )
    if sse <= 0.0:
        fail_execution(
            AnalysisExecutionErrorCode.NON_FINITE_STATISTIC,
            "a perfect fit leaves no residual variance for inference",
        )
    sigma2 = sse / degrees_of_freedom
    if covariance_estimator == "classic":
        covariance = [
            [sigma2 * inverse[row][column] for column in range(k)]
            for row in range(k)
        ]
    elif covariance_estimator == "hc1":
        meat = [[0.0] * k for _ in range(k)]
        for row in range(n):
            weight = residuals[row] * residuals[row]
            for a in range(k):
                for b in range(k):
                    meat[a][b] += (
                        weight * x_matrix[row][a] * x_matrix[row][b]
                    )
        correction = n / degrees_of_freedom
        middle = [
            [meat[row][column] * correction for column in range(k)]
            for row in range(k)
        ]
        covariance = _matmul(_matmul(inverse, middle), inverse)
    elif covariance_estimator == "newey_west":
        if newey_west_max_lags is None or newey_west_max_lags < 0:
            fail_execution(
                AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM,
                "Newey-West requires an explicit non-negative max lag",
            )
        max_lags = int(newey_west_max_lags)
        meat = [[0.0] * k for _ in range(k)]
        for row in range(n):
            weight = residuals[row] * residuals[row]
            for a in range(k):
                for b in range(k):
                    meat[a][b] += (
                        weight * x_matrix[row][a] * x_matrix[row][b]
                    )
        for lag in range(1, max_lags + 1):
            weight = 1.0 - lag / (max_lags + 1.0)
            for row in range(lag, n):
                for a in range(k):
                    for b in range(k):
                        meat[a][b] += (
                            weight
                            * residuals[row]
                            * residuals[row - lag]
                            * (
                                x_matrix[row][a] * x_matrix[row - lag][b]
                                + x_matrix[row - lag][a]
                                * x_matrix[row][b]
                            )
                        )
        covariance = _matmul(_matmul(inverse, meat), inverse)
    else:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            "unknown covariance estimator",
        )
    standard_errors = [
        math.sqrt(covariance[index][index]) for index in range(k)
    ]
    t_statistics = [
        coefficients[index] / standard_errors[index]
        for index in range(k)
    ]
    p_values = [
        _two_sided_p_value(t_statistics[index], degrees_of_freedom)
        for index in range(k)
    ]
    critical = student_t_ppf(
        1.0 - significance_level / 2.0, degrees_of_freedom
    )
    confidence_intervals = [
        {
            "lower": normalize_float(
                coefficients[index] - critical * standard_errors[index]
            ),
            "upper": normalize_float(
                coefficients[index] + critical * standard_errors[index]
            ),
        }
        for index in range(k)
    ]
    mean_y = _mean(y)
    total = sum((value - mean_y) ** 2 for value in y)
    r_squared = (
        1.0 - sse / total
        if total > 0.0
        else fail_execution(
            AnalysisExecutionErrorCode.ZERO_VARIANCE,
            "the outcome has zero variance",
        )
    )
    if total > 0.0:
        adjusted = 1.0 - (1.0 - r_squared) * (n - 1) / degrees_of_freedom
    else:
        adjusted = 0.0
    for value in standard_errors + t_statistics + p_values:
        if not math.isfinite(value):
            fail_execution(
                AnalysisExecutionErrorCode.NON_FINITE_STATISTIC,
                "OLS inference statistic is not finite",
            )
    return {
        "coefficients": {
            name: normalize_float(coefficients[index])
            for index, name in enumerate(names)
        },
        "standard_errors": {
            name: normalize_float(standard_errors[index])
            for index, name in enumerate(names)
        },
        "t_statistics": {
            name: normalize_float(t_statistics[index])
            for index, name in enumerate(names)
        },
        "p_values": {
            name: normalize_float(p_values[index])
            for index, name in enumerate(names)
        },
        "confidence_intervals": {
            name: confidence_intervals[index]
            for index, name in enumerate(names)
        },
        "residuals": [normalize_float(value) for value in residuals],
        "sse": normalize_float(sse),
        "degrees_of_freedom": degrees_of_freedom,
        "r_squared": normalize_float(r_squared),
        "adjusted_r_squared": normalize_float(adjusted),
        "n_observations": n,
        "n_parameters": k,
    }


# ---------------------------------------------------------------------------
# Multiple testing corrections
# ---------------------------------------------------------------------------

def apply_multiple_testing_correction(
    p_values: Sequence[float],
    correction: str,
) -> list[float]:
    """Return adjusted p values for none / bonferroni / holm / BH."""
    raw = [float(value) for value in p_values]
    m = len(raw)
    if m == 0:
        return []
    if correction == "none":
        adjusted = list(raw)
    elif correction == "bonferroni":
        adjusted = [min(1.0, value * m) for value in raw]
    elif correction == "holm":
        adjusted = [0.0] * m
        order = sorted(range(m), key=lambda index: raw[index])
        running = 0.0
        for position, index in enumerate(order):
            candidate = raw[index] * (m - position)
            running = min(1.0, max(running, candidate))
            adjusted[index] = running
    elif correction == "benjamini_hochberg":
        adjusted = [0.0] * m
        order = sorted(range(m), key=lambda index: raw[index])
        running = 1.0
        for position in range(m - 1, -1, -1):
            index = order[position]
            candidate = raw[index] * m / (position + 1)
            running = min(running, candidate)
            adjusted[index] = running
    else:
        fail_execution(
            AnalysisExecutionErrorCode.INVALID_ANALYSIS_EXECUTION_INPUT,
            "unknown multiple-testing correction",
        )
    return [normalize_float(min(1.0, max(0.0, value))) for value in adjusted]


__all__ = [
    "SIGNIFICANT_DIGITS",
    "apply_multiple_testing_correction",
    "compute_ols_result",
    "compute_pearson_result",
    "compute_spearman_result",
    "normalize_float",
    "student_t_cdf",
    "student_t_ppf",
    "student_t_survival_two_sided",
]
