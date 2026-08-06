"""Interpretation artifacts (v0.4.0 Phase 3).

Deterministic EvidencePackage and the strict AnalysisInterpretation model.
Python computes; the LLM only explains. No statistics are recomputed here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Literal, NoReturn

from pydantic import (
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from market_validator.research.models import StrictResearchModel

EVIDENCE_SCHEMA_VERSION = "1.0"
INTERPRETATION_SCHEMA_VERSION = "1.0"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Language = Literal["zh-CN", "en"]
InterpretationStyle = Literal["concise", "detailed", "academic"]

SUPPORTED_LANGUAGES = ("zh-CN", "en")
SUPPORTED_STYLES = ("concise", "detailed", "academic")

# Fixed per-method limitations (deterministic, never LLM-generated)
METHOD_LIMITATIONS: dict[str, list[str]] = {
    "pearson_correlation": [
        "线性相关不代表因果",
        "可能受异常值影响",
        "无法捕捉一般非线性关系",
    ],
    "spearman_correlation": [
        "单调关系不代表因果",
        "p 值和置信区间使用近似推断",
        "rank 关系不等于原始单位效应",
    ],
    "ols": [
        "回归关系不代表因果",
        "遗漏变量可能影响系数",
        "结果依赖模型设定",
        "时间序列可能存在结构变化",
    ],
}

GENERAL_LIMITATIONS = [
    "未执行 robustness",
    "未自动控制所有潜在混杂因素",
    "结果只适用于声明的样本窗口和数据口径",
    "统计不显著不证明不存在关系",
    "统计显著不证明具有实际或经济意义",
    "本结果不构成投资或交易建议",
]

# Fixed conclusion wording (Python-rendered, deterministic)
CONCLUSION_WORDING: dict[str, str] = {
    "supported": "当前样本与声明方法下，证据支持该假设。",
    "not_supported": (
        "当前样本下观察到显著的相反证据，或结果未达到声明的最小效应要求。"
    ),
    "inconclusive": "当前证据不足以支持或否定该假设。",
    "mixed": "不同 primary tests 给出了不一致结果，无法形成单一结论。",
}

# Deterministic statements for each primary test conclusion
DETERMINISTIC_STATEMENTS: dict[str, str] = {
    "supported": "该检验的 adjusted p-value 不超过声明的显著性水平，"
    "方向满足且效应达到声明的最小效应，因此结果为 supported。",
    "not_supported": "该检验的 adjusted p-value 不超过声明的显著性水平，"
    "但结果指向相反方向或未达到声明的最小效应，因此结果为 "
    "not_supported。",
    "inconclusive": "该检验的 adjusted p-value 高于声明的显著性水平，"
    "因此结果为 inconclusive。",
}

# Follow-up whitelist (max 3, LLM must choose from these)
FOLLOWUP_WHITELIST = [
    "改变样本窗口",
    "尝试已声明可用的另一统计方法",
    "检查异常时期",
    "检查滞后设定",
    "增加明确的控制变量",
    "收集更长或更高质量数据",
    "执行 robustness",
]

# Trading-advice word scan (explicit boundaries, no semantic review)
FORBIDDEN_ADVICE_WORDS = [
    "buy",
    "sell",
    "long",
    "short",
    "position",
    "trade",
    "买入",
    "卖出",
    "做多",
    "做空",
    "建仓",
    "仓位",
]


class InterpretationError(ValueError):
    """Safe structured failure; never embeds raw LLM output or secrets."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


class InterpretationErrorCode:
    INVALID_INTERPRETATION_INPUT = "invalid_interpretation_input"
    ANALYSIS_RESULT_MISMATCH = "analysis_result_mismatch"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    LLM_NETWORK_NOT_AUTHORIZED = "llm_network_not_authorized"
    LLM_REQUEST_FAILED = "llm_request_failed"
    LLM_RESPONSE_TOO_LARGE = "llm_response_too_large"
    LLM_RESPONSE_INVALID = "llm_response_invalid"
    LLM_CONCLUSION_MISMATCH = "llm_conclusion_mismatch"
    UNKNOWN_TEST_REFERENCE = "unknown_test_reference"
    MISSING_TEST_INTERPRETATION = "missing_test_interpretation"
    UNGROUNDED_NUMERIC_CLAIM = "ungrounded_numeric_claim"
    FORBIDDEN_ADVICE = "forbidden_advice"
    INTERPRETATION_OUTPUT_CONFLICT = "interpretation_output_conflict"
    INTERPRETATION_OUTPUT_ERROR = "interpretation_output_error"


def fail_interpretation(code: str, message: str) -> NoReturn:
    raise InterpretationError(code, message)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> NoReturn:
    raise ValueError(value)


def _strict_json_load(payload: bytes) -> None:
    json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_number,
    )


def canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_model_bytes(model: StrictResearchModel) -> bytes:
    return canonical_bytes(
        model.model_dump(mode="json", exclude_computed_fields=True)
    )


def parse_strict(payload: bytes, model_type, label: str):
    try:
        _strict_json_load(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} failed strict JSON validation",
        )
    try:
        return model_type.model_validate_json(payload)
    except ValidationError:
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} failed strict domain validation",
        )


# ---------------------------------------------------------------------------
# EvidencePackage
# ---------------------------------------------------------------------------

class PrimaryTestEvidence(StrictResearchModel):
    test_id: Identifier
    variable_id: Identifier
    parameter: str
    estimate: float = Field(allow_inf_nan=False)
    standard_error: float = Field(allow_inf_nan=False)
    confidence_interval_lower: float = Field(allow_inf_nan=False)
    confidence_interval_upper: float = Field(allow_inf_nan=False)
    raw_p_value: float = Field(ge=0, le=1)
    adjusted_p_value: float = Field(ge=0, le=1)
    significance_level: float = Field(gt=0, lt=1)
    minimum_effect_size: float = Field(ge=0)
    direction: str
    conclusion: Literal["supported", "not_supported", "inconclusive"]
    deterministic_statement: str


class AnalysisEvidencePackage(StrictResearchModel):
    evidence_schema_version: Literal["1.0"] = "1.0"
    evidence_id: Identifier
    research_spec_sha256: Sha256Hex
    analysis_plan_sha256: Sha256Hex
    analysis_result_sha256: Sha256Hex
    data_ready_manifest_sha256: Sha256Hex

    original_hypothesis: str
    normalized_hypothesis: str
    claim_type: str
    method: str
    method_profile: str

    sample_size: int = Field(ge=0)
    sample_start: date
    sample_end: date
    transformations: list[str] = Field(default_factory=list)
    alignment_summary: str
    missing_data_summary: str

    overall_conclusion: Literal[
        "supported", "not_supported", "inconclusive", "mixed"
    ]
    primary_test_evidence: list[PrimaryTestEvidence] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    cannot_conclude: list[str] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)


def serialize_analysis_evidence_package(
    package: AnalysisEvidencePackage,
) -> bytes:
    return canonical_model_bytes(package)


def parse_analysis_evidence_package(
    payload: bytes,
) -> AnalysisEvidencePackage:
    return parse_strict(payload, AnalysisEvidencePackage, "evidence")


def calculate_analysis_evidence_package_sha256(
    package: AnalysisEvidencePackage,
) -> str:
    return sha256_hex(serialize_analysis_evidence_package(package))


def _derive_evidence_id(package: AnalysisEvidencePackage) -> str:
    pending = package.model_copy(update={"evidence_id": "pending"})
    return sha256_hex(serialize_analysis_evidence_package(pending))[:32]


# ---------------------------------------------------------------------------
# AnalysisInterpretation (LLM output, strictly validated)
# ---------------------------------------------------------------------------

class TestInterpretation(StrictResearchModel):
    test_id: Identifier
    explanation: str = Field(min_length=1, max_length=4000)


class AnalysisInterpretation(StrictResearchModel):
    interpretation_schema_version: Literal["1.0"] = "1.0"
    interpretation_id: Identifier
    evidence_sha256: Sha256Hex
    analysis_result_id: Identifier
    acknowledged_overall_conclusion: str
    language: Language
    headline: str = Field(min_length=1, max_length=200)
    plain_language_summary: str = Field(min_length=1, max_length=4000)
    test_interpretations: list[TestInterpretation] = Field(min_length=1)
    limitations_explanation: str = Field(min_length=1, max_length=4000)
    cannot_conclude: list[str] = Field(default_factory=list)
    suggested_followups: list[str] = Field(default_factory=list)
    model_metadata: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def serialize_analysis_interpretation(
    interpretation: AnalysisInterpretation,
) -> bytes:
    return canonical_model_bytes(interpretation)


def parse_analysis_interpretation(payload: bytes) -> AnalysisInterpretation:
    return parse_strict(payload, AnalysisInterpretation, "interpretation")


def calculate_analysis_interpretation_sha256(
    interpretation: AnalysisInterpretation,
) -> str:
    return sha256_hex(serialize_analysis_interpretation(interpretation))


def _derive_interpretation_id(
    interpretation: AnalysisInterpretation,
) -> str:
    pending = interpretation.model_copy(
        update={"interpretation_id": "pending"}
    )
    return sha256_hex(serialize_analysis_interpretation(pending))[:32]


__all__ = [
    "AnalysisEvidencePackage",
    "AnalysisInterpretation",
    "CONCLUSION_WORDING",
    "DETERMINISTIC_STATEMENTS",
    "EVIDENCE_SCHEMA_VERSION",
    "FOLLOWUP_WHITELIST",
    "FORBIDDEN_ADVICE_WORDS",
    "GENERAL_LIMITATIONS",
    "INTERPRETATION_SCHEMA_VERSION",
    "InterpretationError",
    "InterpretationErrorCode",
    "METHOD_LIMITATIONS",
    "PrimaryTestEvidence",
    "SUPPORTED_LANGUAGES",
    "SUPPORTED_STYLES",
    "TestInterpretation",
    "calculate_analysis_evidence_package_sha256",
    "calculate_analysis_interpretation_sha256",
    "canonical_bytes",
    "canonical_model_bytes",
    "fail_interpretation",
    "parse_analysis_evidence_package",
    "parse_analysis_interpretation",
    "parse_strict",
    "serialize_analysis_evidence_package",
    "serialize_analysis_interpretation",
    "sha256_hex",
]
