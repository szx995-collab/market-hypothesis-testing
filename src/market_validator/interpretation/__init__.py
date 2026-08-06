"""LLM result interpretation (v0.4.0 Phase 3).

Python computes the deterministic EvidencePackage and the overall
conclusion; the LLM only explains; grounding validation rejects invented
numbers, wrong conclusions, and trading advice; the Markdown report is
rendered deterministically.
"""

from __future__ import annotations

from datetime import datetime, timezone

from market_validator.analysis.execution_models import (
    AnalysisResult,
    calculate_analysis_result_sha256,
)
from market_validator.analysis.planning import AnalysisPlan
from market_validator.interpretation.evidence import (
    build_analysis_evidence_package,
)
from market_validator.interpretation.llm_client import (
    ChatCompletionsHTTPClient,
    FixtureLLMClient,
    InterpretationLLMClient,
)
from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    AnalysisInterpretation,
    InterpretationErrorCode,
    _derive_interpretation_id,
    calculate_analysis_interpretation_sha256,
    fail_interpretation,
    parse_analysis_interpretation,
)
from market_validator.interpretation.prompting import (
    build_interpretation_prompt,
)
from market_validator.interpretation.report import (
    persist_analysis_interpretation,
    render_validation_report_markdown,
    verify_persisted_analysis_interpretation,
)
from market_validator.interpretation.validation import (
    validate_interpretation_matches_evidence,
)
from market_validator.research.models import ResearchSpec


def interpret_analysis_result(
    *,
    research_spec: ResearchSpec,
    analysis_plan: AnalysisPlan,
    analysis_result: AnalysisResult,
    data_ready_manifest_sha256: str,
    llm_client: InterpretationLLMClient,
    language: str = "zh-CN",
    style: str = "concise",
    model_identifier: str = "fixture-v1",
    provenance: dict[str, str] | None = None,
    created_at: datetime | None = None,
    result_root: str | None = None,
) -> dict[str, object]:
    """End-to-end: evidence -> prompt -> LLM -> grounding -> report."""
    evidence_package = build_analysis_evidence_package(
        research_spec=research_spec,
        analysis_plan=analysis_plan,
        analysis_result=analysis_result,
        data_ready_manifest_sha256=data_ready_manifest_sha256,
        provenance=provenance,
    )
    prompt = build_interpretation_prompt(
        evidence_package, language=language, style=style
    )
    raw = llm_client.generate_interpretation_json(
        evidence_package,
        language=language,
        style=style,
        prompt=prompt,
    )
    try:
        candidate = AnalysisInterpretation(
            **{
                **raw,
                "interpretation_schema_version": "1.0",
                "interpretation_id": "pending",
                "evidence_sha256": (
                    calculate_analysis_evidence_package_sha256(
                        evidence_package
                    )
                ),
                "analysis_result_id": analysis_result.analysis_result_id,
            }
        )
    except Exception:
        fail_interpretation(
            InterpretationErrorCode.LLM_RESPONSE_INVALID,
            "the LLM response does not satisfy the interpretation schema",
        )
    validate_interpretation_matches_evidence(
        evidence_package,
        candidate,
        expected_analysis_result_id=analysis_result.analysis_result_id,
    )
    interpretation = candidate.model_copy(
        update={
            "interpretation_id": _derive_interpretation_id(candidate)
        }
    )
    report = render_validation_report_markdown(
        evidence_package,
        interpretation,
        model_identifier=model_identifier,
    )
    result: dict[str, object] = {
        "evidence_package": evidence_package,
        "interpretation": interpretation,
        "report": report,
    }
    if result_root is not None:
        timestamp = created_at or datetime.now(timezone.utc)
        persisted = persist_analysis_interpretation(
            evidence_package=evidence_package,
            interpretation=interpretation,
            validation_report=report,
            model_identifier=model_identifier,
            result_root=result_root,
            created_at=timestamp,
        )
        result["persisted_path"] = persisted
    return result


def calculate_analysis_evidence_package_sha256(
    package: AnalysisEvidencePackage,
) -> str:
    from market_validator.interpretation.models import (
        calculate_analysis_evidence_package_sha256 as _calc,
    )

    return _calc(package)


__all__ = [
    "ChatCompletionsHTTPClient",
    "FixtureLLMClient",
    "InterpretationLLMClient",
    "build_analysis_evidence_package",
    "calculate_analysis_interpretation_sha256",
    "interpret_analysis_result",
    "parse_analysis_interpretation",
    "persist_analysis_interpretation",
    "render_validation_report_markdown",
    "validate_interpretation_matches_evidence",
    "verify_persisted_analysis_interpretation",
]
