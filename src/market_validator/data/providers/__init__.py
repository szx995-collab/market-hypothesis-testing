"""Local and future provider interfaces."""

from market_validator.data.providers.base import (
    DataProvider,
    ProviderCapabilities,
    ProviderStatus,
)
from market_validator.data.providers.csv_provider import CSVProvider, CSVProviderError
from market_validator.data.providers.fred_provider import (
    FRED_CREDENTIAL_SPEC,
    FRED_EARLIEST_REALTIME_DATE,
    FRED_LATEST_REALTIME_DATE,
    FredCapabilityError,
    FredConfigurationError,
    FredDataValidationError,
    FredMappingError,
    FredNetworkNotAuthorizedError,
    FredPersistenceError,
    FredProvider,
    FredProviderError,
    is_valid_fred_api_key,
)
from market_validator.data.providers.fred_errors import (
    FredAuthenticationError,
    FredInvalidRequestError,
    FredMalformedProviderError,
    FredPermissionDeniedError,
    FredRateLimitError,
    FredRedirectError,
    FredResponseFormatError,
    FredSeriesNotFoundError,
    FredServiceUnavailableError,
    FredTransportError,
)

__all__ = [
    "CSVProvider",
    "CSVProviderError",
    "DataProvider",
    "FRED_CREDENTIAL_SPEC",
    "FRED_EARLIEST_REALTIME_DATE",
    "FRED_LATEST_REALTIME_DATE",
    "FredAuthenticationError",
    "FredCapabilityError",
    "FredConfigurationError",
    "FredDataValidationError",
    "FredMappingError",
    "FredInvalidRequestError",
    "FredMalformedProviderError",
    "FredNetworkNotAuthorizedError",
    "FredPermissionDeniedError",
    "FredPersistenceError",
    "FredProvider",
    "FredProviderError",
    "FredRateLimitError",
    "FredRedirectError",
    "FredResponseFormatError",
    "FredSeriesNotFoundError",
    "FredServiceUnavailableError",
    "FredTransportError",
    "ProviderCapabilities",
    "ProviderStatus",
    "is_valid_fred_api_key",
]
