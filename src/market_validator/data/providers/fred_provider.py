"""FRED provider with explicit network consent, vintages, QA, and persistence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
import hashlib
import math
import os
import re
from typing import Any

from pydantic import SecretStr, ValidationError

from market_validator.credentials import (
    CredentialResolver,
    CredentialSpec,
    SecretKind,
)

from market_validator.data.models import (
    DataBundle,
    DataQualityIssue,
    DataQualityReport,
    DataRequirement,
    DataRequirementStatus,
    DataSourceMetadata,
    Observation,
    QualitySeverity,
    TimePrecision,
)
from market_validator.data.providers.base import (
    ProviderCapabilities,
    ProviderStatus,
)
from market_validator.data.providers.fred_transport import (
    FredHttpsTransport,
    FredResponseFormatError,
    FredTransport,
    FredTransportResponse,
)
from market_validator.data.providers.fred_errors import (
    sanitize_fred_message,
    sanitize_public_parameters,
)
from market_validator.data.quality import ObservationRow, build_quality_report
from market_validator.data.registry import (
    InstrumentRegistry,
    InstrumentRegistryEntry,
    ProviderSymbolMapping,
)
from market_validator.data.storage import (
    FredSnapshotRecord,
    FredStorage,
    resolve_data_directory,
)
from market_validator.research.enums import (
    AssetType,
    DataRevisionMode,
    Frequency,
)

FRED_SERIES_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
FRED_SERIES_PATH = "/fred/series"
FRED_OBSERVATIONS_PATH = "/fred/series/observations"
FRED_OBSERVATIONS_SOURCE_URI = (
    "https://api.stlouisfed.org/fred/series/observations"
)
FRED_PAGE_LIMIT = 100_000
FRED_EARLIEST_REALTIME_DATE = "1776-07-04"
FRED_LATEST_REALTIME_DATE = "9999-12-31"
FRED_CREDENTIAL_SPEC = CredentialSpec(
    credential_id="fred.api_key",
    provider_id="fred",
    display_name="FRED API Key",
    environment_variable="FRED_API_KEY",
    validation_pattern=r"^[a-z0-9]{32}$",
    help_text=(
        "请输入用于本次 FRED HTTPS 请求的32位小写字母数字 API Key。"
        "该值只保留在当前进程内存中，不会保存。"
    ),
    secret_kind=SecretKind.API_KEY,
)


class FredProviderError(RuntimeError):
    """Base class for safe provider-domain failures."""

    code = "fred_provider_error"

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        public_parameters: Mapping[str, str | int] | None = None,
        retryable: bool = False,
    ) -> None:
        self.http_status = None
        self.provider_error_code = None
        self.sanitized_message = sanitize_fred_message(message)
        self.endpoint = endpoint
        self.public_parameters = sanitize_public_parameters(
            public_parameters or {}
        )
        self.retryable = retryable
        super().__init__(self.sanitized_message)

    def public_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "message": self.sanitized_message,
            "endpoint": self.endpoint,
            "public_parameters": self.public_parameters,
            "retryable": self.retryable,
        }


class FredConfigurationError(FredProviderError):
    code = "fred_configuration_error"


class FredNetworkNotAuthorizedError(FredProviderError):
    code = "network_not_authorized"


class FredMappingError(FredProviderError):
    code = "fred_mapping_error"


class FredCapabilityError(FredProviderError):
    code = "fred_capability_error"


class FredDataValidationError(FredProviderError):
    code = "fred_data_validation_error"


class FredPersistenceError(FredProviderError):
    code = "fred_persistence_error"


def is_valid_fred_api_key(value: str | None) -> bool:
    return value is not None and FRED_CREDENTIAL_SPEC.accepts(value)


def _require_dict(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FredResponseFormatError(f"FRED {description} must be a JSON object")
    return value


class FredProvider:
    provider_id = "fred"

    def __init__(
        self,
        instrument_registry: InstrumentRegistry,
        *,
        allow_network: bool = False,
        interactive: bool = False,
        environment: Mapping[str, str] | None = None,
        credential_resolver: CredentialResolver | None = None,
        transport: FredTransport | None = None,
        transport_factory: Callable[[SecretStr], FredTransport] | None = None,
        storage: FredStorage | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._instrument_registry = instrument_registry
        self._allow_network = allow_network
        self._interactive = interactive
        self._environment = os.environ if environment is None else environment
        self._credential_resolver = credential_resolver or CredentialResolver(
            environment=self._environment
        )
        self._transport = transport
        self._transport_factory = transport_factory
        self._storage = storage
        self._clock = clock
        self._network_tested = False
        self.last_snapshot: FredSnapshotRecord | None = None

    def status(self) -> ProviderStatus:
        configured = self._credential_resolver.configured(FRED_CREDENTIAL_SPEC)
        if not configured:
            reason = "FRED credential is missing or invalid; network was not tested."
        elif self._network_tested:
            reason = "FRED configuration and the most recent network fetch succeeded."
        else:
            reason = (
                "FRED credential is configured in the environment or current "
                "process memory; network was not tested."
            )
        return ProviderStatus(
            provider_id=self.provider_id,
            available=True,
            configured=configured,
            ready=configured and self._network_tested,
            network_tested=self._network_tested,
            reason=reason,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            supported_asset_types=[
                AssetType.COMMODITY_SPOT,
                AssetType.FX,
                AssetType.MACRO_SERIES,
            ],
            supported_markets=["verified FRED-mapped macro and reference series"],
            supported_frequencies=[Frequency.ONE_DAY],
            supported_fields=["value"],
            supported_price_adjustments=[],
            supports_continuous_futures=False,
            requires_authentication=True,
            requires_network=True,
            supports_local_files=False,
            supported_revision_policies=[
                DataRevisionMode.LATEST_AVAILABLE,
                DataRevisionMode.INITIAL_RELEASE,
            ],
        )

    def _resolve_mapping(
        self, requirement: DataRequirement
    ) -> tuple[InstrumentRegistryEntry, ProviderSymbolMapping]:
        entry = self._instrument_registry.get(requirement.instrument_id)
        if entry is None:
            raise FredMappingError("requirement instrument is not registered")
        comparisons = {
            "asset_type": (entry.asset_type, requirement.asset_type),
            "market": (entry.market, requirement.market),
            "calendar_id": (entry.calendar_id, requirement.calendar_id),
            "timezone": (entry.timezone, requirement.timezone),
            "currency": (entry.currency, requirement.currency),
            "unit": (entry.unit, requirement.unit),
        }
        if any(left != right for left, right in comparisons.values()):
            raise FredMappingError(
                "requirement metadata conflicts with the instrument registry"
            )
        mappings = [
            mapping
            for mapping in entry.provider_mappings
            if mapping.provider_id == self.provider_id
        ]
        if len(mappings) != 1:
            raise FredMappingError(
                "instrument must have exactly one explicit FRED mapping"
            )
        mapping = mappings[0]
        if not mapping.verified or mapping.verification_source_uri is None:
            raise FredMappingError("FRED mapping is not verified")
        if FRED_SERIES_ID_PATTERN.fullmatch(mapping.provider_symbol) is None:
            raise FredMappingError("FRED mapping contains an invalid series ID")
        if mapping.dataset_or_endpoint != FRED_OBSERVATIONS_PATH:
            raise FredMappingError("FRED mapping uses an unexpected endpoint")
        expected_verification_uri = (
            f"https://fred.stlouisfed.org/series/{mapping.provider_symbol}"
        )
        if mapping.verification_source_uri != expected_verification_uri:
            raise FredMappingError(
                "FRED mapping verification source is not the official series page"
            )
        if mapping.market != entry.market:
            raise FredMappingError("FRED mapping market conflicts with the registry")
        return entry, mapping

    def _validate_capabilities(self, requirement: DataRequirement) -> None:
        capabilities = self.capabilities()
        if requirement.status is not DataRequirementStatus.READY:
            raise FredCapabilityError("FRED requires a ready DataRequirement")
        if requirement.asset_type not in capabilities.supported_asset_types:
            raise FredCapabilityError("FRED does not support this asset type")
        if requirement.frequency is not Frequency.ONE_DAY:
            raise FredCapabilityError("FRED provider currently supports only 1d")
        if requirement.field != "value":
            raise FredCapabilityError("FRED requirements must use field='value'")
        if requirement.price_adjustment is not None:
            raise FredCapabilityError("FRED does not apply stock price adjustments")
        if requirement.continuous_contract or requirement.contract_roll_method is not None:
            raise FredCapabilityError("FRED does not provide continuous futures rolls")
        mode = requirement.revision_policy.mode
        if mode is DataRevisionMode.AS_OF_DATE:
            raise FredCapabilityError("FRED as_of_date mode is not implemented")
        if mode not in capabilities.supported_revision_policies:
            raise FredCapabilityError(
                "FRED requires an explicit latest_available or initial_release policy"
            )

    @staticmethod
    def _output_type(mode: DataRevisionMode) -> int:
        return 4 if mode is DataRevisionMode.INITIAL_RELEASE else 1

    def _public_observation_parameters(
        self, requirement: DataRequirement, series_id: str
    ) -> dict[str, str | int]:
        parameters: dict[str, str | int] = {
            "series_id": series_id,
            "file_type": "json",
            "units": "lin",
            "sort_order": "asc",
            "observation_start": requirement.start_date.isoformat(),
            "observation_end": requirement.end_date.isoformat(),
            "output_type": self._output_type(requirement.revision_policy.mode),
            "limit": FRED_PAGE_LIMIT,
        }
        if requirement.revision_policy.mode is DataRevisionMode.INITIAL_RELEASE:
            parameters.update(
                {
                    "realtime_start": FRED_EARLIEST_REALTIME_DATE,
                    "realtime_end": FRED_LATEST_REALTIME_DATE,
                }
            )
        return parameters

    def dry_run(self, requirement: DataRequirement) -> dict[str, object]:
        _, mapping = self._resolve_mapping(requirement)
        self._validate_capabilities(requirement)
        parameters = self._public_observation_parameters(
            requirement, mapping.provider_symbol
        )
        return {
            "provider_id": self.provider_id,
            "dry_run": True,
            "network_requested": False,
            "instrument_id": requirement.instrument_id,
            "series_id": mapping.provider_symbol,
            "revision_policy": requirement.revision_policy.mode.value,
            "public_parameters": parameters,
        }

    def _get_transport(self, api_key: SecretStr) -> FredTransport:
        if self._transport is not None:
            return self._transport
        if self._transport_factory is not None:
            return self._transport_factory(api_key)
        return FredHttpsTransport(api_key)

    def _validate_series_metadata(
        self,
        response: FredTransportResponse,
        requirement: DataRequirement,
        series_id: str,
    ) -> dict[str, Any]:
        series_items = response.payload.get("seriess")
        if not isinstance(series_items, list) or len(series_items) != 1:
            raise FredResponseFormatError(
                "FRED series response must contain exactly one seriess item"
            )
        metadata = _require_dict(series_items[0], "series metadata")
        required_fields = {
            "id",
            "title",
            "frequency",
            "units",
            "seasonal_adjustment",
            "last_updated",
            "notes",
        }
        missing = sorted(required_fields - set(metadata))
        if missing:
            raise FredResponseFormatError(
                "FRED series metadata is missing required fields: "
                + ", ".join(missing)
            )
        if any(not isinstance(metadata[field], str) for field in required_fields):
            raise FredResponseFormatError(
                "FRED series metadata fields must be strings"
            )
        if metadata["id"] != series_id:
            raise FredDataValidationError("FRED returned an unexpected series ID")
        if metadata["frequency"] != "Daily":
            raise FredDataValidationError(
                "daily requirement cannot use a non-Daily FRED series"
            )
        if metadata["units"] != requirement.unit:
            raise FredDataValidationError(
                "FRED series units conflict with the normalized registry"
            )
        return metadata

    def _fetch_observation_pages(
        self,
        transport: FredTransport,
        base_parameters: dict[str, str | int],
    ) -> tuple[list[bytes], list[dict[str, Any]]]:
        raw_pages: list[bytes] = []
        observations: list[dict[str, Any]] = []
        requested_offset = 0
        expected_count: int | None = None

        while True:
            parameters = dict(base_parameters)
            parameters["offset"] = requested_offset
            response = transport.get_json(FRED_OBSERVATIONS_PATH, parameters)
            payload = response.payload
            for field in ("count", "offset", "limit", "observations"):
                if field not in payload:
                    raise FredResponseFormatError(
                        f"FRED observations response is missing {field!r}"
                    )
            count = payload["count"]
            offset = payload["offset"]
            limit = payload["limit"]
            page_items = payload["observations"]
            if (
                type(count) is not int
                or type(offset) is not int
                or type(limit) is not int
                or count < 0
                or offset < 0
                or limit <= 0
                or not isinstance(page_items, list)
            ):
                raise FredResponseFormatError(
                    "FRED pagination fields have invalid types or values"
                )
            if offset != requested_offset:
                raise FredResponseFormatError("FRED returned an unexpected offset")
            if expected_count is None:
                expected_count = count
            elif count != expected_count:
                raise FredResponseFormatError("FRED count changed during pagination")

            raw_pages.append(response.raw_body)
            observations.extend(
                _require_dict(item, "observation") for item in page_items
            )
            if len(observations) >= count:
                break
            if not page_items:
                raise FredResponseFormatError(
                    "FRED returned an empty page before count was satisfied"
                )
            requested_offset += len(page_items)

        if expected_count is None or len(observations) != expected_count:
            raise FredResponseFormatError(
                "FRED observation count does not match returned records"
            )
        return raw_pages, observations

    def _normalize_observations(
        self,
        records: list[dict[str, Any]],
        requirement: DataRequirement,
        retrieved_at: datetime,
    ) -> tuple[list[Observation], DataQualityReport]:
        issues: list[DataQualityIssue] = []
        rows: list[ObservationRow] = []
        mode = requirement.revision_policy.mode
        if mode is DataRevisionMode.LATEST_AVAILABLE:
            issues.append(
                DataQualityIssue(
                    code="latest_revision_hindsight_risk",
                    severity=QualitySeverity.WARNING,
                    message=(
                        "latest_available may contain historical revisions and "
                        "must not be treated as point-in-time data"
                    ),
                )
            )
        else:
            issues.append(
                DataQualityIssue(
                    code="conservative_date_availability",
                    severity=QualitySeverity.WARNING,
                    message=(
                        "initial release availability uses 23:59:59.999999 UTC "
                        "because FRED supplies date precision"
                    ),
                )
            )

        for index, record in enumerate(records, start=2):
            if "date" not in record or "value" not in record:
                raise FredResponseFormatError(
                    "FRED observation is missing date or value"
                )
            try:
                observation_date = date.fromisoformat(str(record["date"]))
            except ValueError:
                raise FredResponseFormatError(
                    "FRED observation date is not ISO 8601"
                ) from None
            raw_value = record["value"]
            if raw_value == ".":
                issues.append(
                    DataQualityIssue(
                        code="provider_missing_value",
                        severity=QualitySeverity.WARNING,
                        message="FRED represented this observation as missing '.'",
                        row_number=index,
                    )
                )
                continue
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                raise FredResponseFormatError(
                    "FRED observation value is not numeric or '.'"
                ) from None
            if not math.isfinite(numeric_value):
                raise FredResponseFormatError(
                    "FRED observation value must be finite"
                )

            observation_time = datetime.combine(
                observation_date, time.min, tzinfo=timezone.utc
            )
            if mode is DataRevisionMode.INITIAL_RELEASE:
                if "realtime_start" not in record:
                    raise FredResponseFormatError(
                        "initial_release observation is missing realtime_start"
                    )
                try:
                    vintage_date = date.fromisoformat(str(record["realtime_start"]))
                except ValueError:
                    raise FredResponseFormatError(
                        "FRED realtime_start is not an ISO date"
                    ) from None
                available_time = datetime.combine(
                    vintage_date, time.max, tzinfo=timezone.utc
                )
                availability_precision = TimePrecision.DATE
                availability_assumption = (
                    "Conservative end-of-day UTC assumption for FRED "
                    "date-only realtime_start."
                )
            else:
                vintage_date = retrieved_at.date()
                available_time = retrieved_at
                availability_precision = TimePrecision.TIMESTAMP
                availability_assumption = (
                    "Actual fetch time for the latest available revision; "
                    "historical first-publication time is not asserted."
                )

            try:
                observation = Observation(
                    instrument_id=requirement.instrument_id,
                    field="value",
                    value=numeric_value,
                    observation_time=observation_time,
                    available_time=available_time,
                    session_date=observation_date,
                    timezone="UTC",
                    currency=requirement.currency,
                    unit=requirement.unit,
                    observation_precision=TimePrecision.DATE,
                    availability_precision=availability_precision,
                    vintage_date=vintage_date,
                    revision_policy=mode,
                    availability_assumption=availability_assumption,
                )
            except ValidationError as error:
                raise FredDataValidationError(
                    "normalized FRED observation failed validation"
                ) from error
            rows.append((index, observation))

        return build_quality_report(
            rows_read=len(records),
            observation_rows=rows,
            initial_issues=issues,
            requirement=requirement,
        )

    @staticmethod
    def _combined_sha256(raw_pages: list[bytes]) -> str:
        digest = hashlib.sha256()
        for page in raw_pages:
            digest.update(len(page).to_bytes(8, "big"))
            digest.update(page)
        return digest.hexdigest()

    def fetch(self, requirement: DataRequirement) -> DataBundle:
        if not self._allow_network:
            raise FredNetworkNotAuthorizedError(
                "FRED network access requires explicit allow_network=True"
            )
        credential = self._credential_resolver.resolve(
            FRED_CREDENTIAL_SPEC,
            interactive=self._interactive,
        )
        _, mapping = self._resolve_mapping(requirement)
        self._validate_capabilities(requirement)
        transport = self._get_transport(credential.secret)

        series_parameters: dict[str, str | int] = {
            "series_id": mapping.provider_symbol,
            "file_type": "json",
        }
        series_response = transport.get_json(FRED_SERIES_PATH, series_parameters)
        self._validate_series_metadata(
            series_response, requirement, mapping.provider_symbol
        )
        observation_parameters = self._public_observation_parameters(
            requirement, mapping.provider_symbol
        )
        raw_observation_pages, records = self._fetch_observation_pages(
            transport, observation_parameters
        )
        clock_value = self._clock()
        if clock_value.tzinfo is None or clock_value.utcoffset() is None:
            raise FredDataValidationError(
                "FRED retrieval clock must be timezone-aware"
            )
        retrieved_at = clock_value.astimezone(timezone.utc)
        observations, quality = self._normalize_observations(
            records, requirement, retrieved_at
        )
        source = DataSourceMetadata(
            provider_id=self.provider_id,
            dataset_id=mapping.provider_symbol,
            provider_symbol=mapping.provider_symbol,
            source_uri=FRED_OBSERVATIONS_SOURCE_URI,
            retrieved_at=retrieved_at,
            public_request_parameters={
                **observation_parameters,
                "observation_page_count": len(raw_observation_pages),
            },
            content_sha256=self._combined_sha256(raw_observation_pages),
            license_note=(
                "FRED official series; review FRED and series-specific notes "
                "for terms and attribution."
            ),
            is_fallback=False,
        )
        bundle = DataBundle(
            requirement=requirement,
            observations=observations,
            source=source,
            quality=quality,
        )

        storage = self._storage or FredStorage(
            resolve_data_directory(self._environment)
        )
        try:
            snapshot = storage.persist(
                series_id=mapping.provider_symbol,
                observation_start=requirement.start_date,
                observation_end=requirement.end_date,
                revision_policy=requirement.revision_policy.mode,
                retrieved_at=retrieved_at,
                public_request_parameters={
                    **series_parameters,
                    **observation_parameters,
                    "observation_page_count": len(raw_observation_pages),
                },
                raw_series=series_response.raw_body,
                raw_observations=raw_observation_pages,
                bundle=bundle,
            )
        except Exception as error:
            raise FredPersistenceError(
                "FRED fetch could not persist a complete auditable snapshot"
            ) from error
        self.last_snapshot = snapshot
        self._network_tested = True
        return bundle
