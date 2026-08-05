"""Strict, serializable ResearchSpec domain model."""

from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from market_validator.research.enums import (
    AssetType,
    ClaimType,
    ContractRollMethod,
    DataRevisionMode,
    Direction,
    Frequency,
    InformationCutoffType,
    JoinPolicy,
    MissingDataPolicy,
    ModelMethod,
    MultipleTestingCorrection,
    PriceAdjustment,
    RobustnessCheckType,
    TargetSession,
    Transformation,
    VariableRole,
)
from market_validator.research.validation import validate_iana_timezone

NonEmptyString = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
IanaTimezone = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(validate_iana_timezone),
]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class StrictResearchModel(BaseModel):
    """Base class that rejects coercion and unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class InstrumentSpec(StrictResearchModel):
    """Provider-neutral identity and market metadata for an instrument."""

    instrument_id: Identifier
    display_name: NonEmptyString
    asset_type: AssetType
    market: NonEmptyString
    exchange_or_venue: NonEmptyString
    timezone: IanaTimezone
    currency: CurrencyCode
    unit: NonEmptyString
    continuous_contract: bool = Field(
        description=(
            "Whether this instrument is a continuous futures series assembled "
            "from expiring contracts. Must be explicit for every instrument."
        )
    )

    @model_validator(mode="after")
    def validate_continuous_contract(self) -> Self:
        if self.continuous_contract and self.asset_type is not AssetType.COMMODITY_FUTURE:
            raise ValueError(
                "continuous_contract may only be true for commodity_future"
            )
        return self


class DataRevisionSpec(StrictResearchModel):
    """Explicit revision/vintage policy for a variable's source data."""

    mode: DataRevisionMode = DataRevisionMode.NOT_APPLICABLE
    as_of_date: date | None = None

    @model_validator(mode="after")
    def validate_as_of_date(self) -> Self:
        if self.mode is DataRevisionMode.AS_OF_DATE:
            if self.as_of_date is None:
                raise ValueError("as_of_date mode requires as_of_date")
        elif self.as_of_date is not None:
            raise ValueError("as_of_date is only valid when mode=as_of_date")
        return self


class VariableSpec(StrictResearchModel):
    """A measurable variable and the transformations known before analysis."""

    variable_id: Identifier
    role: VariableRole
    instrument: InstrumentSpec
    field: NonEmptyString
    transformation: Transformation
    lag_periods: NonNegativeInt = Field(
        description=(
            "N means the variable must be knowable at least N target-market "
            "trading periods before the corresponding outcome session."
        )
    )
    availability_lag_periods: NonNegativeInt = Field(
        description=(
            "Additional non-negative publication or operational delay before "
            "an observation is actually available."
        )
    )
    price_adjustment: PriceAdjustment | None
    contract_roll_method: ContractRollMethod | None
    proxy_for: NonEmptyString | None
    rolling_window_periods: PositiveInt | None = None
    revision_policy: DataRevisionSpec = Field(default_factory=DataRevisionSpec)

    @model_validator(mode="after")
    def validate_asset_and_transformation_details(self) -> Self:
        asset_type = self.instrument.asset_type
        if asset_type in {AssetType.EQUITY, AssetType.ETF}:
            if self.price_adjustment is None:
                raise ValueError("equity and etf variables require price_adjustment")
        elif self.price_adjustment is not None:
            raise ValueError(
                "price_adjustment may only be set for equity or etf variables"
            )

        if asset_type is AssetType.COMMODITY_FUTURE:
            if self.instrument.continuous_contract and self.contract_roll_method is None:
                raise ValueError(
                    "continuous commodity futures require contract_roll_method"
                )
        elif self.contract_roll_method is not None:
            raise ValueError(
                "contract_roll_method may only be set for commodity_future variables"
            )

        rolling_transformations = {
            Transformation.ROLLING_MEAN,
            Transformation.ZSCORE,
        }
        if self.transformation in rolling_transformations:
            if self.rolling_window_periods is None:
                raise ValueError(
                    "rolling_mean and zscore require rolling_window_periods"
                )
        elif self.rolling_window_periods is not None:
            raise ValueError(
                "rolling_window_periods is only valid for rolling_mean or zscore"
            )
        return self


class SampleSpec(StrictResearchModel):
    """The supported daily sample bounds and minimum usable size."""

    start_date: date
    end_date: date
    frequency: Frequency
    minimum_observations: PositiveInt

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self


class InformationCutoffSpec(StrictResearchModel):
    """The latest information permitted for a target trading session."""

    type: InformationCutoffType
    local_time: time | None = None
    timezone: IanaTimezone | None = None

    @model_validator(mode="after")
    def validate_local_time_details(self) -> Self:
        is_explicit_time = (
            self.type is InformationCutoffType.SPECIFIED_LOCAL_TIME
        )
        if is_explicit_time and (self.local_time is None or self.timezone is None):
            raise ValueError(
                "specified_local_time requires both local_time and timezone"
            )
        if not is_explicit_time and (
            self.local_time is not None or self.timezone is not None
        ):
            raise ValueError(
                "local_time and timezone are only valid for specified_local_time"
            )
        return self


class AlignmentSpec(StrictResearchModel):
    """Explicit rules for mapping available observations to target sessions."""

    target_market: NonEmptyString
    target_timezone: IanaTimezone
    target_calendar: NonEmptyString
    target_session: TargetSession
    information_cutoff: InformationCutoffSpec = Field(
        description=(
            "Latest time at which predictor and control information may be "
            "known for the target session."
        )
    )
    join_policy: JoinPolicy
    max_staleness_days: NonNegativeInt = Field(
        description="Finite upper bound on reusing an earlier completed observation."
    )
    missing_data_policy: MissingDataPolicy

    @model_validator(mode="after")
    def validate_join_bounds(self) -> Self:
        if (
            self.join_policy is JoinPolicy.STRICT_MATCH
            and self.max_staleness_days != 0
        ):
            raise ValueError("strict_match requires max_staleness_days=0")
        return self


class ModelSpec(StrictResearchModel):
    """A statistical method description; no computation occurs in this model."""

    method: ModelMethod
    formula: NonEmptyString
    formula_variable_ids: list[Identifier] = Field(
        min_length=2,
        description=(
            "Authoritative structured references for the human-readable formula; "
            "must exactly match all declared variable IDs."
        ),
    )
    null_hypothesis: NonEmptyString
    alternative_hypothesis: NonEmptyString
    direction: Direction
    significance_level: Annotated[float, Field(gt=0, lt=1)]
    minimum_effect_size: Annotated[float, Field(ge=0)]
    multiple_testing_correction: MultipleTestingCorrection


class AlternateTimeWindowCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.ALTERNATE_TIME_WINDOW]
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self


class AlternateProxyCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.ALTERNATE_PROXY]
    variable_id: Identifier
    replacement_instrument: InstrumentSpec
    reason: NonEmptyString


class AlternateReturnDefinitionCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.ALTERNATE_RETURN_DEFINITION]
    variable_id: Identifier
    transformation: Transformation

    @model_validator(mode="after")
    def validate_return_transformation(self) -> Self:
        allowed = {
            Transformation.SIMPLE_RETURN,
            Transformation.LOG_RETURN,
            Transformation.PCT_CHANGE,
        }
        if self.transformation not in allowed:
            raise ValueError(
                "alternate return definition must be simple_return, log_return, "
                "or pct_change"
            )
        return self


class ControlVariablesCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.CONTROL_VARIABLES]
    add_variables: list[VariableSpec] = Field(default_factory=list)
    remove_variable_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_control_changes(self) -> Self:
        if not self.add_variables and not self.remove_variable_ids:
            raise ValueError(
                "control_variables requires add_variables or remove_variable_ids"
            )
        if any(
            variable.role is not VariableRole.CONTROL
            for variable in self.add_variables
        ):
            raise ValueError("all add_variables entries must have role=control")
        return self


class LagSensitivityCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.LAG_SENSITIVITY]
    variable_id: Identifier
    lag_periods: list[NonNegativeInt] = Field(min_length=1)


class MultipleTestingCorrectionCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.MULTIPLE_TESTING_CORRECTION]
    correction: MultipleTestingCorrection


class ExcludePeriodCheck(StrictResearchModel):
    type: Literal[RobustnessCheckType.EXCLUDE_PERIOD]
    start_date: date
    end_date: date
    reason: NonEmptyString

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self


RobustnessCheckSpec = Annotated[
    AlternateTimeWindowCheck
    | AlternateProxyCheck
    | AlternateReturnDefinitionCheck
    | ControlVariablesCheck
    | LagSensitivityCheck
    | MultipleTestingCorrectionCheck
    | ExcludePeriodCheck,
    Field(discriminator="type"),
]


class ResearchSpec(StrictResearchModel):
    """Versioned, provider-neutral protocol that must be confirmed before analysis."""

    schema_version: Literal["1.0"]
    spec_id: Identifier
    title: NonEmptyString
    original_hypothesis: NonEmptyString
    normalized_hypothesis: NonEmptyString
    claim_type: ClaimType
    outcome: VariableSpec
    predictors: list[VariableSpec] = Field(min_length=1)
    controls: list[VariableSpec]
    sample: SampleSpec
    alignment: AlignmentSpec | None
    model: ModelSpec
    robustness_checks: list[RobustnessCheckSpec]
    assumptions: list[NonEmptyString]
    limitations: list[NonEmptyString]

    @model_validator(mode="after")
    def validate_research_protocol(self) -> Self:
        if self.outcome.role is not VariableRole.OUTCOME:
            raise ValueError("outcome must have role=outcome")
        if any(
            predictor.role is not VariableRole.PREDICTOR
            for predictor in self.predictors
        ):
            raise ValueError("every predictors entry must have role=predictor")
        if any(control.role is not VariableRole.CONTROL for control in self.controls):
            raise ValueError("every controls entry must have role=control")

        variables = [self.outcome, *self.predictors, *self.controls]
        variable_ids = [variable.variable_id for variable in variables]
        if len(variable_ids) != len(set(variable_ids)):
            raise ValueError("variable_id values must be unique across the spec")

        formula_ids = self.model.formula_variable_ids
        if len(formula_ids) != len(set(formula_ids)):
            raise ValueError("formula_variable_ids must not contain duplicates")
        if set(formula_ids) != set(variable_ids):
            missing = sorted(set(variable_ids) - set(formula_ids))
            unknown = sorted(set(formula_ids) - set(variable_ids))
            details = []
            if missing:
                details.append(f"missing variable IDs: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown variable IDs: {', '.join(unknown)}")
            raise ValueError(
                "formula_variable_ids must reference exactly the declared variables; "
                + "; ".join(details)
            )

        outcome_market = self.outcome.instrument.market
        outcome_timezone = self.outcome.instrument.timezone
        cross_market = any(
            variable.instrument.market != outcome_market
            or variable.instrument.timezone != outcome_timezone
            for variable in [*self.predictors, *self.controls]
        )
        if cross_market and self.alignment is None:
            raise ValueError(
                "cross-market or cross-timezone research requires alignment "
                "with an information_cutoff"
            )
        if self.alignment is not None:
            if self.alignment.target_market != outcome_market:
                raise ValueError(
                    "alignment.target_market must match the outcome instrument market"
                )
            if self.alignment.target_timezone != outcome_timezone:
                raise ValueError(
                    "alignment.target_timezone must match the outcome instrument timezone"
                )

        known_ids = set(variable_ids)
        control_ids = {control.variable_id for control in self.controls}
        for check in self.robustness_checks:
            if isinstance(
                check,
                (AlternateProxyCheck, AlternateReturnDefinitionCheck, LagSensitivityCheck),
            ) and check.variable_id not in known_ids:
                raise ValueError(
                    f"robustness check references unknown variable_id "
                    f"{check.variable_id!r}"
                )
            if isinstance(check, ControlVariablesCheck):
                unknown_removals = set(check.remove_variable_ids) - control_ids
                if unknown_removals:
                    raise ValueError(
                        "remove_variable_ids must reference declared controls: "
                        + ", ".join(sorted(unknown_removals))
                    )
                added_ids = [variable.variable_id for variable in check.add_variables]
                if len(added_ids) != len(set(added_ids)):
                    raise ValueError("added control variable_id values must be unique")
                conflicts = set(added_ids) & known_ids
                if conflicts:
                    raise ValueError(
                        "added control variable_id values already exist: "
                        + ", ".join(sorted(conflicts))
                    )
        return self
