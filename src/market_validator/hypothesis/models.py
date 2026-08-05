"""Strict domain models for an untrusted statistical-hypothesis draft."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
import math
import re
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from market_validator.research.enums import (
    AssetType,
    ClaimType,
    ContractRollMethod,
    Direction,
    Frequency,
    ModelMethod,
    TargetSession,
    Transformation,
    VariableRole,
)
from market_validator.research.models import (
    InformationCutoffSpec,
    StrictResearchModel,
)
from market_validator.research.validation import validate_iana_timezone


HYPOTHESIS_PROPOSAL_SCHEMA_VERSION = "1.0"
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]


class DraftTimeRelation(StrEnum):
    OUTCOME_PERIOD = "outcome_period"
    CONTEMPORANEOUS = "contemporaneous"
    PRECEDES_OUTCOME = "precedes_outcome"
    FOLLOWS_OUTCOME = "follows_outcome"
    UNSPECIFIED = "unspecified"


class DraftMarketRelation(StrEnum):
    SAME_MARKET = "same_market"
    CROSS_MARKET = "cross_market"
    UNSPECIFIED = "unspecified"


class TargetParameterKind(StrEnum):
    CORRELATION = "correlation"
    REGRESSION_COEFFICIENT = "regression_coefficient"


class HypothesisTimeRelationDraft(StrictResearchModel):
    """Conceptual timing relative to the outcome, before calendar alignment."""

    relation: DraftTimeRelation
    lag_periods: Annotated[int, Field(ge=0)] | None
    available_before_outcome: bool | None
    description: NonEmptyText

    @model_validator(mode="after")
    def validate_relation_fields(self) -> Self:
        if self.relation in {
            DraftTimeRelation.OUTCOME_PERIOD,
            DraftTimeRelation.UNSPECIFIED,
        } and (self.lag_periods is not None or self.available_before_outcome is not None):
            raise ValueError(
                "outcome_period and unspecified timing cannot carry lag or availability"
            )
        if self.relation is DraftTimeRelation.CONTEMPORANEOUS:
            if self.lag_periods not in {None, 0}:
                raise ValueError("contemporaneous timing cannot have a positive lag")
        if (
            self.relation is DraftTimeRelation.PRECEDES_OUTCOME
            and self.available_before_outcome is False
        ):
            raise ValueError(
                "precedes_outcome cannot state that the variable is unavailable"
            )
        if (
            self.relation is DraftTimeRelation.FOLLOWS_OUTCOME
            and self.available_before_outcome is True
        ):
            raise ValueError(
                "follows_outcome cannot state that the variable is available before outcome"
            )
        return self


class HypothesisVariableDraft(StrictResearchModel):
    """Provider-neutral conceptual variable recognized from the question."""

    variable_id: Identifier
    concept_name: NonEmptyText
    role: VariableRole
    market_context: NonEmptyText
    asset_type: AssetType | None
    transformation: Transformation | None
    time_relation: HypothesisTimeRelationDraft
    proxy_for: NonEmptyText | None
    contract_roll_method: ContractRollMethod | None

    @model_validator(mode="after")
    def validate_instrument_details(self) -> Self:
        if self.asset_type is not AssetType.COMMODITY_FUTURE:
            if self.contract_roll_method is not None:
                raise ValueError(
                    "contract_roll_method is only valid for commodity_future"
                )
        return self


class HypothesisSampleDraft(StrictResearchModel):
    """Optional dates stay explicit so missing scope cannot become a default."""

    start_date: date | None
    end_date: date | None
    frequency: Frequency | None

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be later than end_date")
        return self


class HypothesisAlignmentDraft(StrictResearchModel):
    """Reviewable target-market timing without performing calendar alignment."""

    market_relation: DraftMarketRelation
    target_market: NonEmptyText | None
    target_timezone: NonEmptyText | None
    target_calendar: NonEmptyText | None
    target_session: TargetSession | None
    information_cutoff: InformationCutoffSpec | None

    @field_validator("target_timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)

    def is_complete_for_cross_market(self) -> bool:
        return all(
            value is not None
            for value in (
                self.target_market,
                self.target_timezone,
                self.target_calendar,
                self.target_session,
                self.information_cutoff,
            )
        )


class TargetParameterSpec(StrictResearchModel):
    """The exact predictor parameter selected for deterministic H0/H1 rendering."""

    kind: TargetParameterKind
    predictor_variable_id: Identifier
    reference_value: Literal[0]


def render_statistical_hypotheses(
    target_kind: TargetParameterKind,
    direction: Direction,
) -> tuple[str, str]:
    """Render the only supported null/alternative forms deterministically."""

    symbol = "rho" if target_kind is TargetParameterKind.CORRELATION else "beta"
    operators = {
        Direction.TWO_SIDED: ("=", "!="),
        Direction.POSITIVE: ("<=", ">"),
        Direction.NEGATIVE: (">=", "<"),
    }
    null_operator, alternative_operator = operators[direction]
    return (
        f"H0: {symbol} {null_operator} 0",
        f"H1: {symbol} {alternative_operator} 0",
    )


class StatisticalHypothesisSpec(StrictResearchModel):
    """Method choice and Python-verified mathematical hypothesis text."""

    statistical_method: ModelMethod | None
    target_parameter: TargetParameterSpec | None
    direction: Direction | None
    null_hypothesis: NonEmptyText | None
    alternative_hypothesis: NonEmptyText | None
    significance_level: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)] | None
    minimum_effect_size: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None

    @model_validator(mode="after")
    def validate_mathematical_text(self) -> Self:
        complete = (
            self.statistical_method is not None
            and self.target_parameter is not None
            and self.direction is not None
        )
        if not complete:
            if self.null_hypothesis is not None or self.alternative_hypothesis is not None:
                raise ValueError(
                    "H0/H1 must be null until method, target, and direction are complete"
                )
            return self
        expected_null, expected_alternative = render_statistical_hypotheses(
            self.target_parameter.kind,
            self.direction,
        )
        if self.null_hypothesis != expected_null:
            raise ValueError("null_hypothesis does not match deterministic rendering")
        if self.alternative_hypothesis != expected_alternative:
            raise ValueError(
                "alternative_hypothesis does not match deterministic rendering"
            )
        return self


_FORBIDDEN_GENERATED_TEXT = re.compile(
    r"(?:```|https?://|[A-Za-z]:[\\/]|(?:^|\s)/(?:home|Users|tmp|var|etc)/|"
    r"\b(?:api[_ -]?key|authorization|bearer|password|secret|access[_ -]?token)\b|"
    r"\b(?:series[_ -]?id|provider[_ -]?symbol|bundle[_ -]?id|request[_ -]?id)\b|"
    r"\b(?:python\s+-m|powershell|cmd\.exe|subprocess|os\.system)\b|"
    r"\b[0-9a-f]{64}\b)",
    flags=re.IGNORECASE | re.MULTILINE,
)

_ORDER_EXECUTION_VERB = (
    r"(?:place|places|placed|placing|submit|submits|submitted|submitting|"
    r"send|sends|sent|sending|execute|executes|executed|executing|"
    r"route|routes|routed|routing|cancel|cancels|canceled|cancelled|"
    r"canceling|cancelling)"
)
_POSITION_EXECUTION_VERB = (
    r"(?:open|opens|opened|opening|close|closes|closed|closing|"
    r"enter|enters|entered|entering|exit|exits|exited|exiting|"
    r"liquidate|liquidates|liquidated|liquidating)"
)
_AUTOMATION_EXECUTION_VERB = (
    r"(?:run|runs|running|start|starts|started|starting|"
    r"enable|enables|enabled|enabling|deploy|deploys|deployed|deploying|"
    r"operate|operates|operated|operating|design|designs|designed|designing|"
    r"build|builds|built|building)"
)
_CONTINUOUS_TRADING_VERB = (
    r"(?:start|starts|started|starting|begin|begins|began|begun|beginning|"
    r"keep|keeps|kept|keeping|stop|stops|stopped|stopping)"
)
_TRADING_INTENT_ENGLISH = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\b(?:trading|trade)\s+strateg(?:y|ies)\b",
        r"\btrading\s+profit(?:s|ability)?\b",
        r"\b(?:i|we)\s+(?:want|need|plan|intend|would\s+like)\s+to\s+(?:buy|sell|trade)\b",
        r"\b(?:help|tell|show|advise|instruct)\s+(?:me|us)\s+(?:(?:when|how|whether)\s+)?(?:to\s+)?(?:buy|sell|trade)\b",
        r"\b(?:please|should|can|could|would|will|may)\s+(?:i\s+|we\s+|you\s+|the\s+(?:system|agent|model)\s+)?(?:buy|sell|trade)\b",
        rf"\b{_ORDER_EXECUTION_VERB}\s+(?:an?\s+|the\s+|this\s+|that\s+|my\s+|our\s+)?(?:buy\s+|sell\s+|market\s+|limit\s+|stop\s+)?(?:orders?|trades?)\b",
        rf"\b{_POSITION_EXECUTION_VERB}\s+(?:an?\s+|the\s+|this\s+|that\s+|my\s+|our\s+)?(?:long\s+|short\s+)?positions?\b",
        rf"\b{_AUTOMATION_EXECUTION_VERB}\s+(?:an?\s+)?(?:live|real[- ]?money|automatic|automated|algorithmic|auto)\s*-?\s*trading\b",
        r"\b(?:automate|automates|automated|automating)\s+(?:my\s+|our\s+|the\s+)?trading\b",
        rf"\b{_CONTINUOUS_TRADING_VERB}\s+(?:buying|selling|trading)\b",
        r"^\s*(?:please\s+)?(?:buy|sell|trade(?!\s+(?:policy|volume|flows?|balance|data|statistics)\b))\b",
    )
)
_TRADING_INTENT_CHINESE = (
    "下单",
    "买入",
    "卖出",
    "开仓",
    "平仓",
    "持仓",
    "实盘",
    "自动交易",
    "交易策略",
)
_BENIGN_CHINESE_TRADING_CONTEXT = (
    "交易日",
    "交易时段",
    "交易所",
    "交易量",
    "交易数据",
    "交易日期",
    "可交易",
)


def _requests_trading_capability(question: str) -> bool:
    """Classify execution intent without mistaking market-calendar terminology."""

    if any(marker in question for marker in _TRADING_INTENT_CHINESE):
        return True
    remaining_chinese = question
    for benign in _BENIGN_CHINESE_TRADING_CONTEXT:
        remaining_chinese = remaining_chinese.replace(benign, "")
    if "交易" in remaining_chinese:
        return True
    return any(pattern.search(question) for pattern in _TRADING_INTENT_ENGLISH)


def _generated_text_values(proposal: "ResearchHypothesisProposal") -> list[str]:
    values = [proposal.normalized_research_question]
    for variable in [proposal.outcome, *proposal.predictors, *proposal.controls]:
        values.extend(
            value
            for value in (
                variable.concept_name,
                variable.market_context,
                variable.time_relation.description,
                variable.proxy_for,
            )
            if value is not None
        )
    values.extend(proposal.assumptions)
    values.extend(proposal.ambiguities)
    values.extend(proposal.unsupported_requests)
    return values


class ResearchHypothesisProposal(StrictResearchModel):
    """Untrusted, reviewable hypothesis draft that grants no execution authority."""

    proposal_schema_version: Literal["1.0"] = HYPOTHESIS_PROPOSAL_SCHEMA_VERSION
    original_question: NonEmptyText
    normalized_research_question: NonEmptyText
    claim_type: ClaimType
    outcome: HypothesisVariableDraft
    predictors: list[HypothesisVariableDraft] = Field(min_length=1)
    controls: list[HypothesisVariableDraft]
    sample: HypothesisSampleDraft
    alignment: HypothesisAlignmentDraft
    statistical_hypothesis: StatisticalHypothesisSpec
    assumptions: list[NonEmptyText]
    ambiguities: list[NonEmptyText]
    unsupported_requests: list[NonEmptyText]
    ready_for_spec_review: bool

    @model_validator(mode="after")
    def validate_proposal_contract(self) -> Self:
        if self.outcome.role is not VariableRole.OUTCOME:
            raise ValueError("outcome must have role=outcome")
        if any(item.role is not VariableRole.PREDICTOR for item in self.predictors):
            raise ValueError("every predictor must have role=predictor")
        if any(item.role is not VariableRole.CONTROL for item in self.controls):
            raise ValueError("every control must have role=control")

        variables = [self.outcome, *self.predictors, *self.controls]
        variable_ids = [item.variable_id for item in variables]
        if len(variable_ids) != len(set(variable_ids)):
            raise ValueError("variable_id values must be unique")

        statistical = self.statistical_hypothesis
        target = statistical.target_parameter
        predictor_ids = {item.variable_id for item in self.predictors}
        if target is not None and target.predictor_variable_id not in predictor_ids:
            raise ValueError("target predictor_variable_id must reference a predictor")
        if statistical.statistical_method is not None and target is None:
            raise ValueError("a statistical method requires target_parameter")
        if statistical.statistical_method is None and target is not None:
            raise ValueError("target_parameter requires statistical_method")
        if statistical.statistical_method in {
            ModelMethod.PEARSON_CORRELATION,
            ModelMethod.SPEARMAN_CORRELATION,
        } and target is not None and target.kind is not TargetParameterKind.CORRELATION:
            raise ValueError("correlation methods require target kind=correlation")
        if statistical.statistical_method in {
            ModelMethod.OLS,
            ModelMethod.LEAD_LAG_REGRESSION,
        } and target is not None and target.kind is not TargetParameterKind.REGRESSION_COEFFICIENT:
            raise ValueError(
                "regression methods require target kind=regression_coefficient"
            )

        incomplete: list[str] = []
        if any(item.transformation is None for item in variables):
            incomplete.append("variable transformation")
        if self.sample.start_date is None or self.sample.end_date is None:
            incomplete.append("sample date range")
        if self.sample.frequency is None:
            incomplete.append("sample frequency")
        if self.alignment.market_relation is DraftMarketRelation.UNSPECIFIED:
            incomplete.append("market relation")
        if (
            self.alignment.market_relation is DraftMarketRelation.CROSS_MARKET
            and not self.alignment.is_complete_for_cross_market()
        ):
            incomplete.append("cross-market alignment and information cutoff")
        if statistical.statistical_method is None:
            incomplete.append("statistical method")
        if statistical.direction is None:
            incomplete.append("test direction")
        if statistical.significance_level is None:
            incomplete.append("significance level")
        if statistical.minimum_effect_size is None:
            incomplete.append("minimum effect size")

        model_inputs = [*self.predictors, *self.controls]
        if self.claim_type is ClaimType.PREDICTIVE:
            for variable in model_inputs:
                timing = variable.time_relation
                if (
                    timing.relation is not DraftTimeRelation.PRECEDES_OUTCOME
                    or timing.available_before_outcome is not True
                ):
                    incomplete.append(
                        f"predictive input {variable.variable_id} availability before outcome"
                    )
        elif self.claim_type is ClaimType.ASSOCIATION:
            for variable in variables:
                if variable.time_relation.relation is DraftTimeRelation.UNSPECIFIED:
                    incomplete.append(
                        f"association variable {variable.variable_id} time relation"
                    )

        original_casefold = self.original_question.casefold()
        unsupported_casefold = " ".join(self.unsupported_requests).casefold()
        requested_categories = {
            "causal": (
                "因果" in self.original_question
                or "causal" in original_casefold
                or re.search(
                    r"\bcause(?:s|d|ing)?\b",
                    self.original_question,
                    flags=re.IGNORECASE,
                )
                is not None
            ),
            "backtest": (
                "回测" in self.original_question or "backtest" in original_casefold
            ),
            "trading": _requests_trading_capability(self.original_question),
        }
        unsupported_category_markers = {
            "causal": ("因果", "causal"),
            "backtest": ("回测", "backtest"),
            "trading": ("交易", "trading", "下单", "order"),
        }
        missing_unsupported = [
            category
            for category, requested in requested_categories.items()
            if requested
            and not any(
                marker in unsupported_casefold
                for marker in unsupported_category_markers[category]
            )
        ]
        if missing_unsupported:
            raise ValueError(
                "requested unsupported capabilities must be named explicitly: "
                + ", ".join(missing_unsupported)
            )
        normalized_casefold = self.normalized_research_question.casefold()
        if (
            "因果" in self.normalized_research_question
            or "causal" in normalized_casefold
            or re.search(
                r"\bcause(?:s|d|ing)?\b",
                self.normalized_research_question,
                flags=re.IGNORECASE,
            )
        ):
            raise ValueError("normalized research question must not claim causation")
        if self.claim_type is ClaimType.ASSOCIATION and (
            "预测" in self.normalized_research_question
            or "predict" in normalized_casefold
        ):
            raise ValueError(
                "association proposal must not be described as predictive"
            )

        if incomplete and not self.ambiguities:
            raise ValueError(
                "incomplete proposal fields require explicit ambiguities: "
                + ", ".join(incomplete)
            )
        if self.ready_for_spec_review and (
            self.ambiguities or self.unsupported_requests or incomplete
        ):
            raise ValueError(
                "ready_for_spec_review cannot be true while review blockers exist"
            )

        for value in _generated_text_values(self):
            if _FORBIDDEN_GENERATED_TEXT.search(value):
                raise ValueError(
                    "generated proposal fields must not contain paths, credentials, "
                    "provider identities, hashes, commands, URLs, or executable code"
                )
        return self


__all__ = [
    "DraftMarketRelation",
    "DraftTimeRelation",
    "HYPOTHESIS_PROPOSAL_SCHEMA_VERSION",
    "HypothesisAlignmentDraft",
    "HypothesisSampleDraft",
    "HypothesisTimeRelationDraft",
    "HypothesisVariableDraft",
    "ResearchHypothesisProposal",
    "StatisticalHypothesisSpec",
    "TargetParameterKind",
    "TargetParameterSpec",
    "render_statistical_hypotheses",
]
