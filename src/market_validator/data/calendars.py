"""Identity-only calendar registry; no schedule calculations are implemented."""

from __future__ import annotations

from enum import StrEnum
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from market_validator.data.models import Identifier, NonEmptyString, StrictDataModel
from market_validator.research.validation import validate_iana_timezone


class CalendarType(StrEnum):
    EQUITY_EXCHANGE = "equity_exchange"
    FX_24_5 = "fx_24_5"
    ENERGY_FUTURES = "energy_futures"
    MACRO_RELEASE = "macro_release"


class CalendarDefinition(StrictDataModel):
    calendar_id: Identifier
    display_name: NonEmptyString
    timezone: NonEmptyString
    calendar_type: CalendarType
    schedule_adapter: NonEmptyString | None
    supports_special_sessions: bool
    notes: list[NonEmptyString]

    @model_validator(mode="after")
    def validate_timezone(self) -> "CalendarDefinition":
        validate_iana_timezone(self.timezone)
        return self


class CalendarRegistryDocument(StrictDataModel):
    schema_version: Literal["1.0"]
    calendars: list[CalendarDefinition] = Field(min_length=1)


class CalendarRegistryError(ValueError):
    """Raised when calendar metadata is invalid or ambiguous."""


class CalendarOperationNotImplementedError(RuntimeError):
    """Raised when callers request schedule calculations not implemented yet."""


class CalendarRegistry:
    def __init__(self, definitions: list[CalendarDefinition]) -> None:
        ids = [definition.calendar_id for definition in definitions]
        if len(ids) != len(set(ids)):
            raise CalendarRegistryError("calendar_id values must be unique")
        display_names = [definition.display_name.casefold() for definition in definitions]
        if len(display_names) != len(set(display_names)):
            raise CalendarRegistryError("calendar display_name values must be unique")
        self._definitions = tuple(definitions)
        self._by_id = {definition.calendar_id: definition for definition in definitions}
        self._by_display_name = {
            definition.display_name.casefold(): definition for definition in definitions
        }

    @classmethod
    def from_json_file(cls, path: Path) -> "CalendarRegistry":
        document = CalendarRegistryDocument.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        return cls(document.calendars)

    @property
    def definitions(self) -> tuple[CalendarDefinition, ...]:
        return self._definitions

    def get(self, calendar_id: str) -> CalendarDefinition | None:
        return self._by_id.get(calendar_id)

    def require(self, calendar_id: str) -> CalendarDefinition:
        definition = self.get(calendar_id)
        if definition is None:
            raise CalendarRegistryError(f"unknown calendar_id {calendar_id!r}")
        return definition

    def resolve_reference(self, reference: str) -> CalendarDefinition:
        definition = self.get(reference)
        if definition is None:
            definition = self._by_display_name.get(reference.casefold())
        if definition is None:
            raise CalendarRegistryError(f"unknown calendar reference {reference!r}")
        return definition

    def canonical_json(self) -> str:
        payload = [
            definition.model_dump(mode="json")
            for definition in sorted(
                self._definitions, key=lambda item: item.calendar_id
            )
        ]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sessions_between(self, *_args: object, **_kwargs: object) -> None:
        raise CalendarOperationNotImplementedError(
            "Trading-session calculation is not implemented; a dedicated "
            "calendar adapter must handle holidays, special sessions, and DST."
        )
