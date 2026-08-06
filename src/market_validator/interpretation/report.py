"""Markdown validation report + persistence (v0.4.0 Phase 3).

The report is rendered deterministically by Python: every number comes from
the EvidencePackage; the LLM text is inserted verbatim in the explanation
sections. Persistence mirrors the execution-run layout (staging -> hash
verification -> atomic rename -> create-only).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    persist_immutable_bytes,
)
from market_validator.interpretation.models import (
    AnalysisEvidencePackage,
    AnalysisInterpretation,
    CONCLUSION_WORDING,
    InterpretationErrorCode,
    _derive_interpretation_id,
    calculate_analysis_evidence_package_sha256,
    calculate_analysis_interpretation_sha256,
    canonical_bytes,
    fail_interpretation,
    parse_analysis_evidence_package,
    parse_analysis_interpretation,
    serialize_analysis_evidence_package,
    serialize_analysis_interpretation,
    sha256_hex,
)
from market_validator.interpretation.prompting import PROMPT_VERSION


def _fmt_number(value: float) -> str:
    return f"{value:.6g}"


def render_validation_report_markdown(
    evidence_package: AnalysisEvidencePackage,
    interpretation: AnalysisInterpretation,
    *,
    model_identifier: str,
) -> bytes:
    """Deterministic 11-section Markdown report."""
    conclusion = evidence_package.overall_conclusion
    conclusion_text = CONCLUSION_WORDING[conclusion]

    rows = []
    for item in evidence_package.primary_test_evidence:
        rows.append(
            f"| {item.test_id} | {_fmt_number(item.estimate)} | "
            f"[{_fmt_number(item.confidence_interval_lower)}, "
            f"{_fmt_number(item.confidence_interval_upper)}] | "
            f"{_fmt_number(item.raw_p_value)} | "
            f"{_fmt_number(item.adjusted_p_value)} | "
            f"{_fmt_number(item.significance_level)} | {item.conclusion} |"
        )
    table = "\n".join(rows)

    test_explanations = "\n".join(
        f"- **{item.test_id}**：{item.explanation}"
        for item in interpretation.test_interpretations
    )
    limitations = "\n".join(
        f"- {item}" for item in evidence_package.limitations
    )
    cannot_conclude = "\n".join(
        f"- {item}" for item in interpretation.cannot_conclude
    )
    followups = "\n".join(
        f"- {item}" for item in interpretation.suggested_followups
    )
    provenance_lines = "\n".join(
        f"- {key}: {value}"
        for key, value in sorted(evidence_package.provenance.items())
    )

    report = f"""# 假设验证结果

## 结论

{conclusion_text}

## 原始假设

{evidence_package.original_hypothesis}

## 研究设定

- 方法：{evidence_package.method}（{evidence_package.method_profile}）
- 样本窗口：{evidence_package.sample_start} 至 {evidence_package.sample_end}
- 样本量：{evidence_package.sample_size}
- 变换：{", ".join(evidence_package.transformations) or "无"}
- 对齐：{evidence_package.alignment_summary}
- 缺失数据处理：{evidence_package.missing_data_summary}

## 主要统计结果

| test | estimate | confidence interval | raw p | adjusted p | alpha | conclusion |
| --- | --- | --- | --- | --- | --- | --- |
{table}

## 通俗解释

{interpretation.plain_language_summary}

## 各检验解释

{test_explanations}

## 数据与方法限制

{limitations}

## 不能得出的结论

{cannot_conclude}

## 后续验证建议

{followups}

## Provenance

- analysis result sha256: {evidence_package.analysis_result_sha256}
- evidence package sha256: {calculate_analysis_evidence_package_sha256(evidence_package)}
- interpretation sha256: {calculate_analysis_interpretation_sha256(interpretation)}
{provenance_lines}

## 免责声明

本报告不构成投资或交易建议；统计结果不证明因果关系。
"""
    return report.encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_relative_component(name: str, label: str) -> None:
    if not name or name in (".", ".."):
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} is not a valid relative component",
        )
    if "/" in name or "\\" in name or ":" in name:
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} is not a valid relative component",
        )
    if Path(name).is_absolute():
        fail_interpretation(
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
            f"{label} is not a valid relative component",
        )


def persist_analysis_interpretation(
    *,
    evidence_package: AnalysisEvidencePackage,
    interpretation: AnalysisInterpretation,
    validation_report: bytes,
    model_identifier: str,
    result_root: str | Path,
    created_at: datetime,
) -> Path:
    """Create-only interpretation directory with staged atomic commit."""
    root = Path(result_root)
    _validate_relative_component(
        interpretation.analysis_result_id, "analysis result id"
    )
    _validate_relative_component(
        interpretation.interpretation_id, "interpretation id"
    )
    final_dir = (
        root
        / "analysis-interpretations"
        / interpretation.analysis_result_id
        / interpretation.interpretation_id
    )
    if final_dir.exists():
        fail_interpretation(
            InterpretationErrorCode.INTERPRETATION_OUTPUT_CONFLICT,
            "the interpretation directory already exists",
        )
    parent = final_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(str(final_dir) + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True)
        evidence_bytes = serialize_analysis_evidence_package(
            evidence_package
        )
        interpretation_bytes = serialize_analysis_interpretation(
            interpretation
        )
        files: dict[str, str] = {}
        (staging / "evidence-package.json").write_bytes(evidence_bytes)
        files["evidence-package.json"] = sha256_hex(evidence_bytes)
        (staging / "interpretation.json").write_bytes(
            interpretation_bytes
        )
        files["interpretation.json"] = sha256_hex(interpretation_bytes)
        (staging / "validation-report.md").write_bytes(validation_report)
        files["validation-report.md"] = sha256_hex(validation_report)
        manifest = {
            "manifest_schema_version": "1.0",
            "analysis_result_sha256": (
                evidence_package.analysis_result_sha256
            ),
            "evidence_package_sha256": (
                calculate_analysis_evidence_package_sha256(
                    evidence_package
                )
            ),
            "interpretation_sha256": (
                calculate_analysis_interpretation_sha256(interpretation)
            ),
            "validation_report_sha256": sha256_hex(validation_report),
            "prompt_version": PROMPT_VERSION,
            "model_identifier": model_identifier,
            "created_at": created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        manifest_bytes = canonical_bytes(manifest)
        (staging / "interpretation-manifest.json").write_bytes(
            manifest_bytes
        )
        files["interpretation-manifest.json"] = sha256_hex(manifest_bytes)
        for relative, expected in files.items():
            if _file_sha256(staging / relative) != expected:
                fail_interpretation(
                    InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
                    "a staged interpretation file failed hash verification",
                )
        os.rename(staging, final_dir)
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, Exception) and hasattr(error, "code"):
            raise
        fail_interpretation(
            InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
            "the interpretation could not be persisted",
        )
    return final_dir


def verify_persisted_analysis_interpretation(
    result_root: str | Path,
    analysis_result_id: str,
    interpretation_id: str,
) -> AnalysisInterpretation:
    """Reload and hash-verify a persisted interpretation directory."""
    root = Path(result_root)
    _validate_relative_component(analysis_result_id, "analysis result id")
    _validate_relative_component(interpretation_id, "interpretation id")
    final_dir = (
        root
        / "analysis-interpretations"
        / analysis_result_id
        / interpretation_id
    )
    manifest_path = final_dir / "interpretation-manifest.json"
    if not manifest_path.is_file():
        fail_interpretation(
            InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
            "the interpretation manifest is missing",
        )
    try:
        manifest = __import__("json").loads(
            manifest_path.read_bytes().decode("utf-8")
        )
        interpretation = parse_analysis_interpretation(
            (final_dir / "interpretation.json").read_bytes()
        )
        evidence = parse_analysis_evidence_package(
            (final_dir / "evidence-package.json").read_bytes()
        )
    except Exception as error:
        fail_interpretation(
            InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
            "the persisted interpretation failed strict reload",
        )
    expected_files = {
        "evidence-package.json": manifest.get("evidence_package_sha256"),
        "interpretation.json": manifest.get("interpretation_sha256"),
        "validation-report.md": manifest.get("validation_report_sha256"),
    }
    for relative, expected in expected_files.items():
        if expected is None:
            fail_interpretation(
                InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
                "the interpretation manifest is incomplete",
            )
        if _file_sha256(final_dir / relative) != expected:
            fail_interpretation(
                InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
                "a persisted interpretation file failed hash verification",
            )
    return interpretation


__all__ = [
    "persist_analysis_interpretation",
    "render_validation_report_markdown",
    "verify_persisted_analysis_interpretation",
]
