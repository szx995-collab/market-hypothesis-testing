"""Grounding validation (v0.4.0 Phase 3).

Strict checks that the LLM interpretation is grounded in the deterministic
EvidencePackage: conclusion identity, test-id exactness, language, length
limits, no invented numbers, no forbidden advice, no unknown fields.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    AnalysisInterpretation,
    FOLLOWUP_WHITELIST,
    FORBIDDEN_ADVICE_WORDS,
    SUPPORTED_LANGUAGES,
    InterpretationErrorCode,
    calculate_analysis_evidence_package_sha256,
    fail_interpretation,
)

# Reject any digit, percent/currency signs, decimals, or scientific
# notation appearing in LLM free-text fields. Text is NFKC-normalized
# first so fullwidth/Arabic-Indic digits collapse to ASCII digits.
_NUMERIC_PATTERN = re.compile(
    r"[0-9]|%|‰|[$€£¥]|[eE][+\-]?[0-9]"
)


def _normalized(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    # strip all Unicode format characters (ZWJ/ZWNJ/WJ/BOM) and soft hyphens
    stripped = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Cf" and char != "­"
    )
    return re.sub(r"\s+", "", stripped)

_MAX_FIELD_LENGTH = 4000
_MAX_FOLLOWUPS = 3


def _reject_numeric_claims(text: str, label: str) -> None:
    if _NUMERIC_PATTERN.search(_normalized(text)):
        fail_interpretation(
            InterpretationErrorCode.UNGROUNDED_NUMERIC_CLAIM,
            f"{label} contains a numeric claim that is not in the evidence",
        )


def _reject_forbidden_advice(text: str, label: str) -> None:
    lowered = _normalized(text).lower()
    for word in FORBIDDEN_ADVICE_WORDS:
        if word.lower() in lowered:
            fail_interpretation(
                InterpretationErrorCode.FORBIDDEN_ADVICE,
                f"{label} contains trading advice language",
            )


def _check_text_field(text: str, label: str) -> None:
    if len(text) > _MAX_FIELD_LENGTH:
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} exceeds the length limit",
        )
    _reject_numeric_claims(text, label)
    _reject_forbidden_advice(text, label)


def validate_interpretation_matches_evidence(
    evidence_package: AnalysisEvidencePackage,
    interpretation: AnalysisInterpretation,
    *,
    expected_analysis_result_id: str,
) -> None:
    """Every grounding rule; fail fast on the first violation."""
    evidence_sha256 = calculate_analysis_evidence_package_sha256(
        evidence_package
    )
    if interpretation.evidence_sha256 != evidence_sha256:
        fail_interpretation(
            InterpretationErrorCode.EVIDENCE_MISMATCH,
            "the interpretation does not bind this evidence package",
        )
    if interpretation.analysis_result_id != expected_analysis_result_id:
        fail_interpretation(
            InterpretationErrorCode.ANALYSIS_RESULT_MISMATCH,
            "the interpretation binds the wrong analysis result",
        )
    if (
        interpretation.acknowledged_overall_conclusion
        != evidence_package.overall_conclusion
    ):
        fail_interpretation(
            InterpretationErrorCode.LLM_CONCLUSION_MISMATCH,
            "the LLM conclusion does not match the deterministic conclusion",
        )
    if interpretation.language not in SUPPORTED_LANGUAGES:
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            "unsupported language",
        )
    evidence_test_ids = {
        item.test_id for item in evidence_package.primary_test_evidence
    }
    interpretation_test_ids = [
        item.test_id for item in interpretation.test_interpretations
    ]
    if len(interpretation_test_ids) != len(set(interpretation_test_ids)):
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            "the interpretation duplicates a test reference",
        )
    if set(interpretation_test_ids) != evidence_test_ids:
        missing = evidence_test_ids - set(interpretation_test_ids)
        unknown = set(interpretation_test_ids) - evidence_test_ids
        if unknown:
            fail_interpretation(
                InterpretationErrorCode.UNKNOWN_TEST_REFERENCE,
                "the interpretation references an unknown test",
            )
        if missing:
            fail_interpretation(
                InterpretationErrorCode.MISSING_TEST_INTERPRETATION,
                "the interpretation omits a primary test",
            )
    _check_text_field(interpretation.headline, "headline")
    _check_text_field(
        interpretation.plain_language_summary, "plain_language_summary"
    )
    for item in interpretation.test_interpretations:
        _check_text_field(item.explanation, "explanation")
    _check_text_field(
        interpretation.limitations_explanation, "limitations_explanation"
    )
    for item in interpretation.cannot_conclude:
        _check_text_field(item, "cannot_conclude")
    for item in interpretation.warnings:
        _check_text_field(item, "warnings")
    if len(interpretation.suggested_followups) > _MAX_FOLLOWUPS:
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            "too many suggested followups",
        )
    for followup in interpretation.suggested_followups:
        if followup not in FOLLOWUP_WHITELIST:
            fail_interpretation(
                InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
                "a suggested followup is outside the whitelist",
            )


__all__ = [
    "validate_interpretation_matches_evidence",
]
