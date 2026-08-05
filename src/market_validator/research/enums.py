"""Closed vocabularies for ResearchSpec version 1.0."""

from enum import StrEnum


class ClaimType(StrEnum):
    ASSOCIATION = "association"
    PREDICTIVE = "predictive"


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    EQUITY_INDEX = "equity_index"
    SECTOR_INDEX = "sector_index"
    FX = "fx"
    COMMODITY_SPOT = "commodity_spot"
    COMMODITY_FUTURE = "commodity_future"
    MACRO_SERIES = "macro_series"


class VariableRole(StrEnum):
    PREDICTOR = "predictor"
    OUTCOME = "outcome"
    CONTROL = "control"


class Transformation(StrEnum):
    LEVEL = "level"
    SIMPLE_RETURN = "simple_return"
    LOG_RETURN = "log_return"
    PCT_CHANGE = "pct_change"
    DIFFERENCE = "difference"
    ROLLING_MEAN = "rolling_mean"
    ZSCORE = "zscore"


class PriceAdjustment(StrEnum):
    UNADJUSTED = "unadjusted"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"


class ContractRollMethod(StrEnum):
    CALENDAR = "calendar"
    VOLUME = "volume"
    OPEN_INTEREST = "open_interest"


class DataRevisionMode(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    LATEST_AVAILABLE = "latest_available"
    INITIAL_RELEASE = "initial_release"
    AS_OF_DATE = "as_of_date"


class Frequency(StrEnum):
    ONE_DAY = "1d"


class JoinPolicy(StrEnum):
    LAST_COMPLETED_BEFORE_CUTOFF = "last_completed_before_cutoff"
    PREVIOUS_TARGET_TRADING_DAY = "previous_target_trading_day"
    STRICT_MATCH = "strict_match"


class MissingDataPolicy(StrEnum):
    DROP_OBSERVATION = "drop_observation"
    ERROR = "error"
    KEEP_MISSING = "keep_missing"


class InformationCutoffType(StrEnum):
    BEFORE_TARGET_OPEN = "before_target_open"
    BEFORE_TARGET_CLOSE = "before_target_close"
    SPECIFIED_LOCAL_TIME = "specified_local_time"


class TargetSession(StrEnum):
    REGULAR_SESSION = "regular_session"
    OPENING_AUCTION = "opening_auction"
    CLOSING_AUCTION = "closing_auction"


class ModelMethod(StrEnum):
    PEARSON_CORRELATION = "pearson_correlation"
    SPEARMAN_CORRELATION = "spearman_correlation"
    OLS = "ols"
    LEAD_LAG_REGRESSION = "lead_lag_regression"


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    TWO_SIDED = "two_sided"


class MultipleTestingCorrection(StrEnum):
    NONE = "none"
    BONFERRONI = "bonferroni"
    HOLM = "holm"
    BENJAMINI_HOCHBERG = "benjamini_hochberg"


class RobustnessCheckType(StrEnum):
    ALTERNATE_TIME_WINDOW = "alternate_time_window"
    ALTERNATE_PROXY = "alternate_proxy"
    ALTERNATE_RETURN_DEFINITION = "alternate_return_definition"
    CONTROL_VARIABLES = "control_variables"
    LAG_SENSITIVITY = "lag_sensitivity"
    MULTIPLE_TESTING_CORRECTION = "multiple_testing_correction"
    EXCLUDE_PERIOD = "exclude_period"
