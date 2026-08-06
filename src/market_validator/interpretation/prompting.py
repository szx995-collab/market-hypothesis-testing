"""Prompt compilation (v0.4.0 Phase 3).

The prompt is a fixed template plus the EvidencePackage; the LLM receives
no raw data, no credentials, and no numeric computation tasks.
"""

from __future__ import annotations

from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    FOLLOWUP_WHITELIST,
    FORBIDDEN_ADVICE_WORDS,
    SUPPORTED_LANGUAGES,
    SUPPORTED_STYLES,
    canonical_bytes,
    serialize_analysis_evidence_package,
)

PROMPT_VERSION = "interpretation-prompt-v1"

_PROMPT_TEMPLATE = """You are an interpreter for a verified statistical analysis result. You are NOT a statistical calculator.

Fixed rules:
- You must NOT change the given conclusions.
- You must NOT recompute any statistic.
- You must NOT add any numbers, percentages, currency values, decimals, or scientific notation to your text.
- You must NOT claim causation.
- You must NOT provide investment or trading advice (no buy/sell/position/trade language, in any language).
- You can only explain the given EvidencePackage.
- Return only a single JSON object matching the required schema; no markdown fences, no extra text.

Language: {language}
Style: {style}
EvidencePackage (canonical JSON):
{evidence_json}

Required JSON schema:
{{
  "acknowledged_overall_conclusion": "exactly one of supported|not_supported|inconclusive|mixed (must equal the package overall_conclusion)",
  "language": "{language}",
  "headline": "short headline without any numbers",
  "plain_language_summary": "plain-language explanation without any numbers",
  "test_interpretations": [
    {{"test_id": "one test id from the package", "explanation": "explanation without numbers"}}
  ],
  "limitations_explanation": "explanation of the listed limitations without numbers",
  "cannot_conclude": ["short statements of what cannot be concluded"],
  "suggested_followups": ["up to 3 items, each exactly one of: {followups}"],
  "model_metadata": {{"model": "your model identifier if known"}},
  "warnings": []
}}
"""


def build_interpretation_prompt(
    evidence_package: AnalysisEvidencePackage,
    *,
    language: str,
    style: str = "concise",
) -> str:
    """Compile the fixed prompt; deterministic for identical inputs."""
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("unsupported language")
    if style not in SUPPORTED_STYLES:
        raise ValueError("unsupported style")
    evidence_json = serialize_analysis_evidence_package(
        evidence_package
    ).decode("utf-8")
    return _PROMPT_TEMPLATE.format(
        language=language,
        style=style,
        evidence_json=evidence_json,
        followups=" | ".join(FOLLOWUP_WHITELIST),
    )


def prompt_payload_sha256(prompt: str) -> str:
    from market_validator.interpretation.models import sha256_hex

    return sha256_hex(canonical_bytes({"prompt": prompt, "version": PROMPT_VERSION}))


__all__ = ["PROMPT_VERSION", "build_interpretation_prompt", "prompt_payload_sha256"]
