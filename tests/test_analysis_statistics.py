"""Statistics contract tests (v0.4.0 Phase 2, reference values)."""

from __future__ import annotations

import math
import unittest

from market_validator.analysis.execution_models import (
    AnalysisExecutionError,
    AnalysisExecutionErrorCode,
)
from market_validator.analysis.statistics import (
    apply_multiple_testing_correction,
    compute_ols_result,
    compute_pearson_result,
    compute_spearman_result,
    normalize_float,
    student_t_cdf,
    student_t_ppf,
)


class PearsonTest(unittest.TestCase):
    def test_reference_values(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        result = compute_pearson_result(x, y, significance_level=0.05)
        self.assertAlmostEqual(result["coefficient"], 0.7745966692, places=8)
        self.assertEqual(result["sample_size"], 5)
        self.assertEqual(result["degrees_of_freedom"], 3)
        self.assertAlmostEqual(result["t_statistic"], 2.1213203436, places=8)
        self.assertAlmostEqual(result["p_value"], 0.124, places=3)

    def test_perfect_negative(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [5.0, 4.0, 3.0, 2.0, 1.0]
        result = compute_pearson_result(x, y, significance_level=0.05)
        self.assertAlmostEqual(result["coefficient"], -1.0, places=10)

    def test_zero_variance_safe_failure(self):
        with self.assertRaises(AnalysisExecutionError) as caught:
            compute_pearson_result(
                [1.0, 1.0, 1.0, 1.0],
                [1.0, 2.0, 3.0, 4.0],
                significance_level=0.05,
            )
        self.assertEqual(
            caught.exception.code, AnalysisExecutionErrorCode.ZERO_VARIANCE
        )

    def test_insufficient_pairs(self):
        with self.assertRaises(AnalysisExecutionError):
            compute_pearson_result(
                [1.0, 2.0], [1.0, 2.0], significance_level=0.05
            )


class SpearmanTest(unittest.TestCase):
    def test_reference_ties_average_rank(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        result = compute_spearman_result(x, y, significance_level=0.05)
        # ranks of y: 1, 2.5, 4.5, 2.5, 4.5 -> rho with perfect x ranks
        self.assertAlmostEqual(result["coefficient"], 0.7379, places=3)

    def test_monotonic_perfect(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        y = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        result = compute_spearman_result(x, y, significance_level=0.05)
        self.assertAlmostEqual(result["coefficient"], 1.0, places=10)

    def test_tie_method_recorded_by_executor(self):
        # executor-level fields are checked in test_analysis_execution;
        # here we just verify ranks are average (ties produce finite rho)
        result = compute_spearman_result(
            [1.0, 2.0, 2.0, 3.0, 4.0],
            [4.0, 5.0, 5.0, 6.0, 7.0],
            significance_level=0.05,
        )
        self.assertTrue(-1.0 <= result["coefficient"] <= 1.0)


class OLSTest(unittest.TestCase):
    def test_classic_reference(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        result = compute_ols_result(
            {"x": x},
            y,
            include_intercept=True,
            covariance_estimator="classic",
            newey_west_max_lags=None,
            significance_level=0.05,
        )
        self.assertAlmostEqual(result["coefficients"]["x"], 0.6, places=10)
        self.assertAlmostEqual(result["coefficients"]["intercept"], 2.2, places=10)
        self.assertAlmostEqual(result["standard_errors"]["x"], 0.2828427124, places=8)
        self.assertAlmostEqual(result["t_statistics"]["x"], 2.1213203436, places=8)
        self.assertAlmostEqual(result["r_squared"], 0.6, places=10)
        self.assertEqual(result["degrees_of_freedom"], 3)
        self.assertEqual(result["n_parameters"], 2)

    def test_hc1_different_from_classic(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [2.0, 5.0, 3.0, 8.0, 7.0, 11.0, 9.0, 14.0, 12.0, 16.0]
        classic = compute_ols_result(
            {"x": x}, y, include_intercept=True,
            covariance_estimator="classic",
            newey_west_max_lags=None, significance_level=0.05,
        )
        hc1 = compute_ols_result(
            {"x": x}, y, include_intercept=True,
            covariance_estimator="hc1",
            newey_west_max_lags=None, significance_level=0.05,
        )
        self.assertAlmostEqual(
            classic["coefficients"]["x"], hc1["coefficients"]["x"], places=10
        )
        self.assertNotAlmostEqual(
            classic["standard_errors"]["x"], hc1["standard_errors"]["x"], places=6
        )

    def test_newey_west_explicit_lag(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [2.0, 5.0, 3.0, 8.0, 7.0, 11.0, 9.0, 14.0, 12.0, 16.0]
        result = compute_ols_result(
            {"x": x}, y, include_intercept=True,
            covariance_estimator="newey_west",
            newey_west_max_lags=2, significance_level=0.05,
        )
        classic = compute_ols_result(
            {"x": x}, y, include_intercept=True,
            covariance_estimator="classic",
            newey_west_max_lags=None, significance_level=0.05,
        )
        self.assertAlmostEqual(
            result["coefficients"]["x"], classic["coefficients"]["x"],
            places=10,
        )
        self.assertTrue(math.isfinite(result["standard_errors"]["x"]))

    def test_missing_lag_rejected(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        with self.assertRaises(AnalysisExecutionError) as caught:
            compute_ols_result(
                {"x": x}, y, include_intercept=True,
                covariance_estimator="newey_west",
                newey_west_max_lags=None, significance_level=0.05,
            )
        self.assertEqual(
            caught.exception.code, AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM
        )

    def test_singular_design(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        with self.assertRaises(AnalysisExecutionError) as caught:
            compute_ols_result(
                {"a": x, "b": [v * 2 for v in x]}, y,
                include_intercept=True, covariance_estimator="classic",
                newey_west_max_lags=None, significance_level=0.05,
            )
        self.assertEqual(
            caught.exception.code, AnalysisExecutionErrorCode.SINGULAR_DESIGN_MATRIX
        )

    def test_insufficient_observations(self):
        x = [1.0, 2.0]
        y = [2.0, 4.0]
        with self.assertRaises(AnalysisExecutionError) as caught:
            compute_ols_result(
                {"x": x}, y, include_intercept=True,
                covariance_estimator="classic",
                newey_west_max_lags=None, significance_level=0.05,
            )
        self.assertEqual(
            caught.exception.code, AnalysisExecutionErrorCode.INVALID_DEGREES_OF_FREEDOM
        )

    def test_zero_variance_outcome(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [3.0, 3.0, 3.0, 3.0, 3.0]
        with self.assertRaises(AnalysisExecutionError):
            compute_ols_result(
                {"x": x}, y, include_intercept=True,
                covariance_estimator="classic",
                newey_west_max_lags=None, significance_level=0.05,
            )


class StudentTTest(unittest.TestCase):
    def test_reference_cdf(self):
        self.assertAlmostEqual(student_t_cdf(2.353, 3), 0.9500, places=3)
        self.assertAlmostEqual(student_t_cdf(0.0, 10), 0.5, places=10)

    def test_reference_ppf(self):
        self.assertAlmostEqual(student_t_ppf(0.975, 10), 2.228, places=2)
        self.assertAlmostEqual(student_t_ppf(0.95, 30), 1.697, places=2)

    def test_symmetry(self):
        self.assertAlmostEqual(
            student_t_cdf(-1.0, 8) + student_t_cdf(1.0, 8), 1.0, places=10
        )


class MultipleTestingTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(
            apply_multiple_testing_correction([0.01, 0.04], "none"),
            [0.01, 0.04],
        )

    def test_bonferroni(self):
        self.assertEqual(
            apply_multiple_testing_correction([0.01, 0.04], "bonferroni"),
            [0.02, 0.08],
        )
        self.assertEqual(
            apply_multiple_testing_correction([0.7, 0.8], "bonferroni"),
            [1.0, 1.0],
        )

    def test_holm(self):
        self.assertEqual(
            apply_multiple_testing_correction([0.01, 0.04], "holm"),
            [0.02, 0.04],
        )

    def test_benjamini_hochberg(self):
        self.assertEqual(
            apply_multiple_testing_correction(
                [0.01, 0.04], "benjamini_hochberg"
            ),
            [0.02, 0.04],
        )

    def test_unknown_correction_rejected(self):
        with self.assertRaises(AnalysisExecutionError):
            apply_multiple_testing_correction([0.01], "bogus")


class NumericProfileTest(unittest.TestCase):
    def test_normalize_12_digits(self):
        self.assertEqual(normalize_float(0.123456789012345), 0.123456789012)
        self.assertEqual(normalize_float(1.0 / 3.0), 0.333333333333)
        self.assertEqual(normalize_float(2.0), 2.0)

    def test_non_finite_rejected(self):
        import math

        with self.assertRaises(AnalysisExecutionError) as caught:
            normalize_float(float("nan"))
        self.assertEqual(
            caught.exception.code, AnalysisExecutionErrorCode.NON_FINITE_STATISTIC
        )
        with self.assertRaises(AnalysisExecutionError):
            normalize_float(float("inf"))


if __name__ == "__main__":
    unittest.main()
