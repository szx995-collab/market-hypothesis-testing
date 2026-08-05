"""Deterministic ResearchSpec-to-DataPlan translation."""

from __future__ import annotations

import hashlib
import json

from market_validator.data.calendars import CalendarRegistry, CalendarRegistryError
from market_validator.data.models import (
    DataPlan,
    DataRequirement,
    DataRequirementStatus,
)
from market_validator.data.registry import IdentityStatus, InstrumentRegistry
from market_validator.research.enums import Transformation
from market_validator.research.models import ResearchSpec, VariableSpec


class DataPlanningError(ValueError):
    """Raised when a ResearchSpec conflicts with normalized registry metadata."""


def calculate_required_pre_sample_periods(variable: VariableSpec) -> int:
    """Return raw history needed for transformation plus target-period lag."""
    if variable.transformation is Transformation.LEVEL:
        transformation_periods = 0
    elif variable.transformation in {
        Transformation.SIMPLE_RETURN,
        Transformation.LOG_RETURN,
        Transformation.PCT_CHANGE,
        Transformation.DIFFERENCE,
    }:
        transformation_periods = 1
    elif variable.transformation in {
        Transformation.ROLLING_MEAN,
        Transformation.ZSCORE,
    }:
        if variable.rolling_window_periods is None:
            raise DataPlanningError(
                f"{variable.variable_id} requires rolling_window_periods"
            )
        transformation_periods = variable.rolling_window_periods - 1
    else:  # pragma: no cover - exhaustive enum guard
        raise DataPlanningError(
            f"unsupported transformation {variable.transformation!r}"
        )
    return transformation_periods + variable.lag_periods


def _validate_registry_match(variable: VariableSpec, entry: object) -> None:
    comparisons = {
        "asset_type": (entry.asset_type, variable.instrument.asset_type),
        "market": (entry.market, variable.instrument.market),
        "timezone": (entry.timezone, variable.instrument.timezone),
        "currency": (entry.currency, variable.instrument.currency),
    }
    conflicts = [
        f"{field}: registry={registry_value!r}, spec={spec_value!r}"
        for field, (registry_value, spec_value) in comparisons.items()
        if registry_value != spec_value
    ]
    if conflicts:
        raise DataPlanningError(
            f"registry metadata conflicts for {variable.instrument.instrument_id!r}: "
            + "; ".join(conflicts)
        )


def plan_data_requirements(
    research_spec: ResearchSpec,
    instrument_registry: InstrumentRegistry,
    calendar_registry: CalendarRegistry,
) -> DataPlan:
    """Create a stable provider-neutral plan without mutating the ResearchSpec."""
    if research_spec.alignment is not None:
        try:
            target_calendar = calendar_registry.resolve_reference(
                research_spec.alignment.target_calendar
            )
        except CalendarRegistryError as error:
            raise DataPlanningError(str(error)) from error
    else:
        outcome_entry = instrument_registry.get(
            research_spec.outcome.instrument.instrument_id
        )
        if outcome_entry is None:
            raise DataPlanningError(
                "cannot determine target calendar for an unregistered outcome"
            )
        target_calendar = calendar_registry.require(outcome_entry.calendar_id)

    variables = [
        research_spec.outcome,
        *research_spec.predictors,
        *research_spec.controls,
    ]
    requirements: list[DataRequirement] = []
    unresolved: list[str] = []
    plan_warnings: list[str] = []
    cutoff = (
        research_spec.alignment.information_cutoff
        if research_spec.alignment is not None
        else None
    )

    for variable in variables:
        instrument = variable.instrument
        entry = instrument_registry.get(instrument.instrument_id)
        warnings: list[str] = []
        calendar_id: str | None = None
        status = DataRequirementStatus.UNRESOLVED
        if entry is None:
            warnings.append(
                f"instrument {instrument.instrument_id!r} is not in the registry"
            )
            unresolved.append(instrument.instrument_id)
        else:
            _validate_registry_match(variable, entry)
            calendar_registry.require(entry.calendar_id)
            calendar_id = entry.calendar_id
            if entry.identity_status is not IdentityStatus.VERIFIED:
                warnings.append(
                    f"instrument identity is {entry.identity_status.value}, not verified"
                )
            if not entry.provider_mappings:
                warnings.append("no provider symbol mapping is configured")
                unresolved.append(instrument.instrument_id)
            else:
                status = DataRequirementStatus.READY

        requirement = DataRequirement(
            requirement_id=f"{research_spec.spec_id}.{variable.variable_id}",
            variable_id=variable.variable_id,
            instrument_id=instrument.instrument_id,
            asset_type=instrument.asset_type,
            field=variable.field,
            transformation=variable.transformation,
            rolling_window_periods=variable.rolling_window_periods,
            start_date=research_spec.sample.start_date,
            end_date=research_spec.sample.end_date,
            frequency=research_spec.sample.frequency,
            lag_periods=variable.lag_periods,
            availability_lag_periods=variable.availability_lag_periods,
            required_pre_sample_periods=calculate_required_pre_sample_periods(variable),
            market=instrument.market,
            calendar_id=calendar_id,
            timezone=instrument.timezone,
            currency=instrument.currency,
            unit=instrument.unit,
            price_adjustment=variable.price_adjustment,
            contract_roll_method=variable.contract_roll_method,
            proxy_for=variable.proxy_for,
            continuous_contract=instrument.continuous_contract,
            information_cutoff=cutoff,
            revision_policy=variable.revision_policy,
            status=status,
            warnings=warnings,
        )
        requirements.append(requirement)
        plan_warnings.extend(
            f"{variable.variable_id}: {warning}" for warning in warnings
        )

    fingerprint_payload = {
        "research_spec": research_spec.model_dump(mode="json"),
        "instrument_registry": instrument_registry.canonical_json(),
        "calendar_registry": calendar_registry.canonical_json(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:20]

    return DataPlan(
        schema_version="1.0",
        plan_id=f"data-plan-{fingerprint}",
        research_spec_id=research_spec.spec_id,
        requirements=requirements,
        target_calendar=target_calendar.calendar_id,
        alignment_policy=research_spec.alignment,
        unresolved_instruments=sorted(set(unresolved)),
        warnings=sorted(set(plan_warnings)),
    )
