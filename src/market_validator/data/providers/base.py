"""Provider-neutral data acquisition interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from market_validator.data.models import (
    DataBundle,
    DataRequirement,
    Identifier,
    NonEmptyString,
    StrictDataModel,
)
from market_validator.research.enums import (
    AssetType,
    DataRevisionMode,
    Frequency,
    PriceAdjustment,
)


class ProviderStatus(StrictDataModel):
    provider_id: Identifier
    available: bool
    configured: bool = True
    ready: bool
    network_tested: bool = False
    reason: NonEmptyString


class ProviderCapabilities(StrictDataModel):
    provider_id: Identifier
    supported_asset_types: list[AssetType]
    supported_markets: list[NonEmptyString]
    supported_frequencies: list[Frequency]
    supported_fields: list[NonEmptyString]
    supported_price_adjustments: list[PriceAdjustment]
    supports_continuous_futures: bool
    requires_authentication: bool
    requires_network: bool
    supports_local_files: bool
    supported_revision_policies: list[DataRevisionMode]


@runtime_checkable
class DataProvider(Protocol):
    provider_id: str

    def status(self) -> ProviderStatus:
        """Return availability without fetching data."""

    def capabilities(self) -> ProviderCapabilities:
        """Describe supported requirement dimensions."""

    def fetch(self, requirement: DataRequirement) -> DataBundle:
        """Fetch one explicit requirement without routing or fallback."""
