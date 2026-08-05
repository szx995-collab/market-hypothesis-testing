"""Provider-symbol mappings kept separate from ResearchSpec."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.models import (
    CurrencyCode,
    Identifier,
    NonEmptyString,
    StrictDataModel,
)
from market_validator.research.enums import AssetType
from market_validator.research.validation import validate_iana_timezone


class IdentityStatus(StrEnum):
    EXAMPLE = "example"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class ProviderSymbolMapping(StrictDataModel):
    provider_id: Identifier
    provider_symbol: NonEmptyString
    dataset_or_endpoint: NonEmptyString
    market: NonEmptyString
    verified: bool
    verified_on: date | None
    verification_source_uri: NonEmptyString | None = None
    notes: list[NonEmptyString]

    @model_validator(mode="after")
    def validate_verification_date(self) -> "ProviderSymbolMapping":
        if self.verified and self.verified_on is None:
            raise ValueError("verified provider mappings require verified_on")
        if self.verified and self.verification_source_uri is None:
            raise ValueError(
                "verified provider mappings require verification_source_uri"
            )
        if not self.verified and self.verified_on is not None:
            raise ValueError("unverified provider mappings must not set verified_on")
        if not self.verified and self.verification_source_uri is not None:
            raise ValueError(
                "unverified provider mappings must not set verification_source_uri"
            )
        if (
            self.verification_source_uri is not None
            and not self.verification_source_uri.startswith("https://")
        ):
            raise ValueError("verification_source_uri must use HTTPS")
        return self


class InstrumentRegistryEntry(StrictDataModel):
    instrument_id: Identifier
    display_name: NonEmptyString
    asset_type: AssetType
    market: NonEmptyString
    exchange_or_venue: NonEmptyString
    calendar_id: Identifier
    timezone: NonEmptyString
    currency: CurrencyCode
    unit: NonEmptyString
    aliases: list[NonEmptyString]
    identity_status: IdentityStatus
    provider_mappings: list[ProviderSymbolMapping]
    notes: list[NonEmptyString]

    @model_validator(mode="after")
    def validate_timezone(self) -> "InstrumentRegistryEntry":
        validate_iana_timezone(self.timezone)
        return self


class InstrumentRegistryDocument(StrictDataModel):
    schema_version: Literal["1.0"]
    instruments: list[InstrumentRegistryEntry] = Field(min_length=1)


class InstrumentRegistryError(ValueError):
    """Raised when normalized instrument identity is invalid or ambiguous."""


class InstrumentRegistry:
    def __init__(
        self,
        entries: list[InstrumentRegistryEntry],
        calendar_registry: CalendarRegistry,
    ) -> None:
        instrument_ids = [entry.instrument_id for entry in entries]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise InstrumentRegistryError("instrument_id values must be unique")

        alias_owners: dict[str, str] = {}
        for entry in entries:
            calendar_registry.require(entry.calendar_id)
            names = [entry.instrument_id, *entry.aliases]
            for name in names:
                normalized = name.casefold()
                owner = alias_owners.get(normalized)
                if owner is not None and owner != entry.instrument_id:
                    raise InstrumentRegistryError(
                        f"ambiguous alias {name!r} maps to both {owner!r} and "
                        f"{entry.instrument_id!r}"
                    )
                alias_owners[normalized] = entry.instrument_id

        mapping_owners: dict[tuple[str, str, str, str], str] = {}
        for entry in entries:
            for mapping in entry.provider_mappings:
                key = (
                    mapping.provider_id.casefold(),
                    mapping.dataset_or_endpoint.casefold(),
                    mapping.market.casefold(),
                    mapping.provider_symbol.casefold(),
                )
                owner = mapping_owners.get(key)
                if owner is not None and owner != entry.instrument_id:
                    raise InstrumentRegistryError(
                        "provider mapping is assigned to multiple instruments: "
                        f"{mapping.provider_id}/{mapping.dataset_or_endpoint}/"
                        f"{mapping.market}/{mapping.provider_symbol}"
                    )
                mapping_owners[key] = entry.instrument_id

        self._entries = tuple(entries)
        self._by_id = {entry.instrument_id: entry for entry in entries}
        self._aliases = alias_owners

    @classmethod
    def from_json_file(
        cls, path: Path, calendar_registry: CalendarRegistry
    ) -> "InstrumentRegistry":
        document = InstrumentRegistryDocument.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        return cls(document.instruments, calendar_registry)

    @property
    def entries(self) -> tuple[InstrumentRegistryEntry, ...]:
        return self._entries

    def get(self, instrument_id: str) -> InstrumentRegistryEntry | None:
        return self._by_id.get(instrument_id)

    def resolve(self, reference: str) -> InstrumentRegistryEntry | None:
        owner = self._aliases.get(reference.casefold())
        return None if owner is None else self._by_id[owner]

    def canonical_json(self) -> str:
        payload = [
            entry.model_dump(mode="json")
            for entry in sorted(self._entries, key=lambda item: item.instrument_id)
        ]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
