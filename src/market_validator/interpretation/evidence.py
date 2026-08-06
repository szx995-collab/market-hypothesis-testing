"""Deterministic evidence building (v0.4.0 Phase 3).

Python computes the EvidencePackage from the verified AnalysisResult; the
LLM never changes conclusions and never supplies numbers.
"""

from __future__ import annotations

from datetime import date

from market_validator.analysis.execution_models import (
    AnalysisResult,
    calculate_analysis_result_sha256,
    parse_analysis_result,
    serialize_analysis_result,
)
from market_validator.analysis.planning import (
    AnalysisPlan,
    calculate_analysis_plan_sha256,
    parse_analysis_plan,
)
from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    DETERMINISTIC_STATEMENTS,
    GENERAL_LIMITATIONS,
    METHOD_LIMITATIONS,
    PrimaryTestEvidence,
    _derive_evidence_id,
    calculate_analysis_evidence_package_sha256,
    fail_interpretation,
    serialize_analysis_evidence_package,
)
from market_validator.research.enums import ClaimType
from market_validator.research.models import ResearchSpec
from market_validator.research.serialization import (
    calculate_research_spec_sha256,
    parse_research_spec,
)

ANALYSIS_SPECS_VERSION = "1.0"


def derive_overall_conclusion(
    test_conclusions: list[str],
) -> str:
    """Deterministic aggregation; the LLM cannot change it."""
    if not test_conclusions:
        fail_interpretation(
            "invalid_interpretation_input",
            "no primary test conclusions are available",
        )
    unique = set(test_conclusions)
    if len(unique) == 1:
        return next(iter(unique))
    if unique == {"supported"}:
        return "supported"
    if unique == {"not_supported"}:
        return "not_supported"
    if unique == {"inconclusive"}:
        return "inconclusive"
    return "mixed"


def _transformations_of(plan: AnalysisPlan) -> list[str]:
    return sorted(
        {
            binding.transformation_plan.profile
            for binding in plan.variable_bindings
        }
    )


def _alignment_summary(plan: AnalysisPlan) -> str:
    alignment = plan.alignment_plan
    if alignment is None:
        return "no alignment contract"
    return (
        f"{alignment.alignment_profile}; join_policy="
        f"{alignment.join_policy}; max_staleness_days="
        f"{alignment.max_staleness_days}; missing_data_policy="
        f"{alignment.missing_data_policy}; no_lookahead="
        f"{alignment.no_lookahead}"
    )


def _missing_data_summary(plan: AnalysisPlan) -> str:
    alignment = plan.alignment_plan
    if alignment is None:
        return "not specified"
    return alignment.missing_data_policy


def _fixed_limitations(method: str) -> list[str]:
    method_specific = METHOD_LIMITATIONS.get(method, [])
    return list(method_specific) + list(GENERAL_LIMITATIONS)


def build_analysis_evidence_package(
    *,
    research_spec: ResearchSpec,
    analysis_plan: AnalysisPlan,
    analysis_result: AnalysisResult,
    data_ready_manifest_sha256: str,
    provenance: dict[str, str] | None = None,
) -> AnalysisEvidencePackage:
    """Compile the deterministic EvidencePackage (no raw rows, no secrets)."""
    spec_sha256 = calculate_research_spec_sha256(research_spec)
    plan_sha256 = calculate_analysis_plan_sha256(analysis_plan)
    result_sha256 = calculate_analysis_result_sha256(analysis_result)
    if (
        analysis_plan.research_spec_sha256 != spec_sha256
        or analysis_result.analysis_plan_sha256 != plan_sha256
        or analysis_result.data_ready_manifest_sha256
        != data_ready_manifest_sha256
        or analysis_result.data_ready_manifest_sha256
        != analysis_plan.data_ready_manifest_sha256
    ):
        fail_interpretation(
            "evidence_mismatch",
            "the analysis chain is not internally consistent",
        )
    test_evidence: list[PrimaryTestEvidence] = []
    for test in analysis_result.primary_test_results:
        test_evidence.append(
            PrimaryTestEvidence(
                test_id=test.test_id,
                variable_id=test.target_variable_id,
                parameter=test.parameter,
                estimate=test.estimate,
                standard_error=test.standard_error,
                confidence_interval_lower=test.confidence_interval_lower,
                confidence_interval_upper=test.confidence_interval_upper,
                raw_p_value=test.raw_p_value,
                adjusted_p_value=test.adjusted_p_value,
                significance_level=test.significance_level,
                minimum_effect_size=test.minimum_effect_size,
                direction=test.direction,
                conclusion=test.conclusion,
                deterministic_statement=DETERMINISTIC_STATEMENTS[
                    test.conclusion
                ],
            )
        )
    overall = derive_overall_conclusion(
        [test.conclusion for test in analysis_result.primary_test_results]
    )
    limitations = list(analysis_result.limitations)
    limitations.extend(_fixed_limitations(analysis_result.method))
    package = AnalysisEvidencePackage(
        evidence_schema_version="1.0",
        evidence_id="pending",
        research_spec_sha256=spec_sha256,
        analysis_plan_sha256=plan_sha256,
        analysis_result_sha256=result_sha256,
        data_ready_manifest_sha256=data_ready_manifest_sha256,
        original_hypothesis=research_spec.original_hypothesis,
        normalized_hypothesis=research_spec.normalized_hypothesis,
        claim_type=research_spec.claim_type.value,
        method=analysis_result.method,
        method_profile=analysis_result.method_profile,
        sample_size=analysis_result.sample_size,
        sample_start=research_spec.sample.start_date,
        sample_end=research_spec.sample.end_date,
        transformations=_transformations_of(analysis_plan),
        alignment_summary=_alignment_summary(analysis_plan),
        missing_data_summary=_missing_data_summary(analysis_plan),
        overall_conclusion=overall,
        primary_test_evidence=test_evidence,
        warnings=list(analysis_result.warnings),
        limitations=limitations,
        cannot_conclude=[
            "统计结果不构成因果证明",
            "未执行 robustness，稳健性未知",
            "本结果不适用于声明样本窗口之外的数据",
        ],
        provenance=dict(provenance or {}),
    )
    package = package.model_copy(
        update={"evidence_id": _derive_evidence_id(package)}
    )
    return package


def parse_evidence_strict(payload: bytes) -> AnalysisEvidencePackage:
    from market_validator.interpretation.models import (
        parse_analysis_evidence_package,
    )

    return parse_analysis_evidence_package(payload)


__all__ = [
    "ANALYSIS_SPECS_VERSION",
    "build_analysis_evidence_package",
    "derive_overall_conclusion",
    "parse_evidence_strict",
]
