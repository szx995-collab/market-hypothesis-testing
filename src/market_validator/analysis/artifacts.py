"""Deterministic persistence for price-change volatility analysis artifacts.

This module formats and verifies an already-computed analysis result. It does
not perform statistical calculations or alter the source data bundle.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from market_validator.analysis.price_change_volatility import (
    BootstrapConfidenceInterval,
    PriceChangeVolatilityParameters,
    PriceChangeVolatilityResult,
    RobustnessCheckResult,
    SampleSummary,
    VolatilityConclusion,
    VolatilityEstimate,
)
from market_validator.data.models import (
    Identifier,
    NonEmptyString,
    StrictDataModel,
)


ARTIFACT_SCHEMA_VERSION = "1.0"
ANALYSIS_TYPE = "price_change_volatility"
RESULT_FILENAME = "result.json"
REPORT_FILENAME = "report.md"
MANIFEST_FILENAME = "manifest.json"
_EXPECTED_FILENAMES = frozenset(
    {RESULT_FILENAME, REPORT_FILENAME, MANIFEST_FILENAME}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PriceChangeVolatilityArtifactError(RuntimeError):
    """Base error for price-change volatility artifact operations."""


class ArtifactPathError(PriceChangeVolatilityArtifactError):
    """Raised when an artifact path is unsafe or structurally invalid."""


class ArtifactIntegrityError(PriceChangeVolatilityArtifactError):
    """Raised when persisted bytes fail integrity or consistency checks."""


class ArtifactConflictError(PriceChangeVolatilityArtifactError):
    """Raised when an immutable target exists with different content."""


class ArtifactFileMetadata(StrictDataModel):
    """Manifest metadata for one immutable artifact file."""

    filename: NonEmptyString
    byte_size: int = Field(ge=0)
    sha256: Sha256Hex


class PriceChangeVolatilityArtifactManifest(StrictDataModel):
    """Strict manifest for one price-change volatility artifact package."""

    artifact_schema_version: Literal["1.0"] = ARTIFACT_SCHEMA_VERSION
    analysis_type: Literal["price_change_volatility"] = ANALYSIS_TYPE
    artifact_id: Identifier
    source_request_id: Identifier
    source_bundle_sha256: Sha256Hex
    parameters: PriceChangeVolatilityParameters
    main_conclusion: VolatilityConclusion
    final_conclusion: VolatilityConclusion
    result_file: ArtifactFileMetadata
    report_file: ArtifactFileMetadata

    @model_validator(mode="after")
    def validate_filenames(self) -> "PriceChangeVolatilityArtifactManifest":
        if self.result_file.filename != RESULT_FILENAME:
            raise ValueError(f"result_file.filename must be {RESULT_FILENAME!r}")
        if self.report_file.filename != REPORT_FILENAME:
            raise ValueError(f"report_file.filename must be {REPORT_FILENAME!r}")
        return self


class PersistedPriceChangeVolatilityArtifact(StrictDataModel):
    """Paths and hashes returned after an artifact is safely persisted."""

    artifact_id: Identifier
    artifact_path: Path
    result_path: Path
    report_path: Path
    manifest_path: Path
    result_sha256: Sha256Hex
    report_sha256: Sha256Hex
    manifest_sha256: Sha256Hex


class LoadedPriceChangeVolatilityArtifact(StrictDataModel):
    """Strictly verified contents returned by the artifact loader."""

    artifact_path: Path
    manifest: PriceChangeVolatilityArtifactManifest
    result: PriceChangeVolatilityResult
    report_markdown: str
    manifest_sha256: Sha256Hex


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact_id(result_sha256: str) -> str:
    # The full digest remains in the manifest. A 128-bit digest prefix keeps
    # deterministic directory names practical on Windows path-length limits.
    return f"price-change-volatility-{result_sha256[:32]}"


def serialize_price_change_volatility_result(
    result: PriceChangeVolatilityResult,
) -> bytes:
    """Serialize a validated result into its canonical persisted wire format."""

    if not isinstance(result, PriceChangeVolatilityResult):
        raise TypeError("result must be a validated PriceChangeVolatilityResult")
    payload = result.model_dump_json(
        indent=2,
        exclude_computed_fields=True,
    ).encode("utf-8")
    try:
        restored = PriceChangeVolatilityResult.model_validate_json(payload)
    except Exception as exc:  # pragma: no cover - defensive model contract check
        raise ArtifactIntegrityError(
            "serialized result cannot be strictly deserialized"
        ) from exc
    if restored != result:
        raise ArtifactIntegrityError("serialized result does not round-trip exactly")
    return payload


def _format_value(value: object) -> str:
    """Format an already-computed value without recalculating or rounding it."""

    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    if hasattr(value, "isoformat"):
        return str(getattr(value, "isoformat")())
    return str(value)


def _render_summary_table(
    shock: SampleSummary,
    reference: SampleSummary,
) -> list[str]:
    rows = (
        ("记录数", shock.count, reference.count),
        ("均值", shock.mean, reference.mean),
        ("中位数", shock.median, reference.median),
        ("样本标准差（ddof=1）", shock.sample_stddev, reference.sample_stddev),
        ("MAD", shock.mad, reference.mad),
        ("最小值", shock.minimum, reference.minimum),
        ("5% 分位数", shock.quantile_05, reference.quantile_05),
        ("25% 分位数", shock.quantile_25, reference.quantile_25),
        ("75% 分位数", shock.quantile_75, reference.quantile_75),
        ("95% 分位数", shock.quantile_95, reference.quantile_95),
        ("最大值", shock.maximum, reference.maximum),
    )
    lines = [
        "| 统计量 | 冲击窗口 | 参考窗口 |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| {label} | {_format_value(shock_value)} | "
        f"{_format_value(reference_value)} |"
        for label, shock_value, reference_value in rows
    )
    return lines


def _render_estimate(estimate: VolatilityEstimate) -> list[str]:
    confidence_interval: BootstrapConfidenceInterval = estimate.confidence_interval
    return [
        f"- 标准差比率（冲击/参考）：{_format_value(estimate.stddev_ratio)}",
        "- 95% percentile Bootstrap 置信区间："
        f"[{_format_value(confidence_interval.lower)}, "
        f"{_format_value(confidence_interval.upper)}]",
        f"- 主结论：{_format_value(estimate.conclusion)}",
    ]


def _render_robustness(check: RobustnessCheckResult) -> list[str]:
    return [
        f"### {_format_value(check.check_id)}",
        "",
        f"- 说明：{check.description}",
        f"- 冲击窗口纳入数：{check.estimate.shock.count}",
        f"- 参考窗口纳入数：{check.estimate.reference.count}",
        f"- 冲击窗口额外排除数：{check.shock_excluded_count}",
        f"- 参考窗口额外排除数：{check.reference_excluded_count}",
        f"- 标准差比率：{_format_value(check.estimate.stddev_ratio)}",
        "- 95% percentile Bootstrap 置信区间："
        f"[{_format_value(check.estimate.confidence_interval.lower)}, "
        f"{_format_value(check.estimate.confidence_interval.upper)}]",
        f"- 结论：{_format_value(check.estimate.conclusion)}",
        f"- 标准差比率方向大于 1：{str(check.ratio_direction_gt_one).lower()}",
        "",
    ]


def render_price_change_volatility_report(
    result: PriceChangeVolatilityResult,
) -> bytes:
    """Render a deterministic UTF-8 Markdown report from result fields only."""

    if not isinstance(result, PriceChangeVolatilityResult):
        raise TypeError("result must be a validated PriceChangeVolatilityResult")
    parameters = result.parameters
    exclusions = result.exclusions
    lines = [
        "# WTI 相邻报价价格变化波动比较",
        "",
        "## 1. 市场假设、H0/H1 和预设判定规则",
        "",
        f"- 市场假设：{result.hypothesis}",
        f"- H0：{result.null_hypothesis}",
        f"- H1：{result.alternative_hypothesis}",
        f"- 预设判定规则：{parameters.conclusion_rule}",
        "",
        "## 2. 数据事实",
        "",
        f"- 冲击窗口：{parameters.shock_window.start_date.isoformat()} 至 "
        f"{parameters.shock_window.end_date.isoformat()}",
        f"- 参考窗口：{parameters.reference_window.start_date.isoformat()} 至 "
        f"{parameters.reference_window.end_date.isoformat()}",
        f"- analysis_as_of：{parameters.analysis_as_of.isoformat()}",
        f"- 分组日期：{_format_value(parameters.grouping_date)}",
        f"- 转换：{result.transformation}",
        f"- 公式：{result.transformation_formula}",
        f"- 原始单位：{result.unit}",
        f"- 波动定义：{result.volatility_label}",
        "",
        *_render_summary_table(result.main.shock, result.main.reference),
        "",
        "## 3. 统计推断",
        "",
        f"- 主统计量：{parameters.statistic}",
        f"- 样本标准差 ddof：{parameters.stddev_ddof}",
        f"- MAD 定义：{parameters.mad_definition}",
        f"- 分位数方法：{parameters.quantile_method}",
        f"- Bootstrap 方法：{parameters.bootstrap_method}",
        f"- 组内重采样方式：{parameters.group_resampling}",
        f"- block length：{parameters.block_length}",
        f"- repetitions：{parameters.repetitions}",
        f"- random seed：{parameters.random_seed}",
        f"- 置信水平：{parameters.confidence_level}",
        *_render_estimate(result.main),
        "",
        "## 4. 两项稳健性检查",
        "",
    ]
    for check in result.robustness_checks:
        lines.extend(_render_robustness(check))
    lines.extend(
        [
            "## 5. 纳入与排除数量对账",
            "",
            f"- 转换层候选相邻有效价格对：{exclusions.source_candidate_pair_count}",
            f"- 转换层生成变化记录：{exclusions.transform_generated_count}",
            f"- provider missing 缺口排除：{exclusions.transform_gap_excluded_count}",
            f"- 转换层其他排除：{exclusions.transform_other_excluded_count}",
            f"- as-of 后排除：{exclusions.analysis_as_of_excluded_count}",
            f"- 窗口外排除：{exclusions.outside_windows_excluded_count}",
            f"- 冲击窗口纳入：{exclusions.shock_included_count}",
            f"- 参考窗口纳入：{exclusions.reference_included_count}",
            f"- 分析错误排除：{exclusions.analysis_error_excluded_count}",
            f"- transform warning 数：{result.transform_warning_count}",
            f"- transform error 数：{result.transform_error_count}",
            "",
            "## 6. warnings 和 errors",
            "",
            "### Warnings",
            "",
        ]
    )
    if result.warnings:
        lines.extend(
            f"- `{warning.code}`（count={warning.count}）：{warning.message}"
            for warning in result.warnings
        )
    else:
        lines.append("- 无")
    lines.extend(["", "### Errors", ""])
    if result.errors:
        lines.extend(f"- `{error.code}`：{error.message}" for error in result.errors)
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 7. 数据来源与审计信息",
            "",
            f"- 源 request ID：{result.source.request_id}",
            f"- 源 Bundle SHA-256：{result.source.bundle_sha256}",
            f"- Provider：{result.source.provider_id}",
            f"- Dataset：{result.source.dataset_id}",
            f"- 抓取时间（仅审计，不替代可得时间）：{result.source.retrieved_at.isoformat()}",
            f"- 源质量状态：{_format_value(result.source.quality_status)}",
            f"- 源读取行数：{result.source.rows_read}",
            f"- 源解析观测数：{result.source.observations_parsed}",
            f"- 源质量问题代码：{result.source.quality_issue_codes}",
            "",
            "## 8. 方法限制",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in result.limitations)
    lines.extend(
        [
            "",
            "## 9. 最终结论",
            "",
            f"- 最终结论：{_format_value(result.final_conclusion)}",
            f"- 理由：{result.final_conclusion_reason}",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _serialize_manifest(
    manifest: PriceChangeVolatilityArtifactManifest,
) -> bytes:
    return manifest.model_dump_json(
        indent=2,
        exclude_computed_fields=True,
    ).encode("utf-8")


def _metadata(filename: str, payload: bytes) -> ArtifactFileMetadata:
    return ArtifactFileMetadata(
        filename=filename,
        byte_size=len(payload),
        sha256=_sha256_bytes(payload),
    )


def _build_manifest(
    result: PriceChangeVolatilityResult,
    artifact_id: str,
    result_bytes: bytes,
    report_bytes: bytes,
) -> PriceChangeVolatilityArtifactManifest:
    return PriceChangeVolatilityArtifactManifest(
        artifact_id=artifact_id,
        source_request_id=result.source.request_id,
        source_bundle_sha256=result.source.bundle_sha256,
        parameters=result.parameters,
        main_conclusion=result.main.conclusion,
        final_conclusion=result.final_conclusion,
        result_file=_metadata(RESULT_FILENAME, result_bytes),
        report_file=_metadata(REPORT_FILENAME, report_bytes),
    )


def _reject_traversal(path: Path) -> None:
    if any(part == os.pardir for part in path.parts):
        raise ArtifactPathError(f"path traversal is not allowed: {path}")


def _path_is_symlink(path: Path) -> bool:
    return path.is_symlink()


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        if candidate.exists() and _path_is_symlink(candidate):
            raise ArtifactPathError(f"symbolic links are not allowed: {candidate}")


def _require_regular_file(path: Path) -> None:
    if _path_is_symlink(path):
        raise ArtifactPathError(f"symbolic links are not allowed: {path}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ArtifactPathError(f"required artifact file is missing: {path.name}") from exc
    if not stat.S_ISREG(mode):
        raise ArtifactPathError(f"artifact entry is not a regular file: {path.name}")


def _validate_artifact_directory(path: Path) -> None:
    _reject_traversal(path)
    _assert_no_symlink_components(path)
    if not path.exists() or not path.is_dir():
        raise ArtifactPathError(f"artifact path is not a directory: {path}")
    entries = {entry.name for entry in path.iterdir()}
    if entries != _EXPECTED_FILENAMES:
        missing = sorted(_EXPECTED_FILENAMES - entries)
        extra = sorted(entries - _EXPECTED_FILENAMES)
        raise ArtifactPathError(
            f"artifact directory structure mismatch; missing={missing}, extra={extra}"
        )
    for filename in _EXPECTED_FILENAMES:
        _require_regular_file(path / filename)


def _validate_expected_sha256(value: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ArtifactIntegrityError(
            "expected_manifest_sha256 must be 64 lowercase hexadecimal characters"
        )


def _validate_file_metadata(
    metadata: ArtifactFileMetadata,
    payload: bytes,
) -> None:
    if metadata.byte_size != len(payload):
        raise ArtifactIntegrityError(
            f"{metadata.filename} byte size does not match its manifest"
        )
    if metadata.sha256 != _sha256_bytes(payload):
        raise ArtifactIntegrityError(
            f"{metadata.filename} SHA-256 does not match its manifest"
        )


def _load_and_verify(
    artifact_path: Path,
    *,
    expected_manifest_sha256: str | None,
    enforce_directory_name: bool,
    expected_artifact_id: str | None = None,
) -> LoadedPriceChangeVolatilityArtifact:
    _validate_artifact_directory(artifact_path)
    manifest_bytes = (artifact_path / MANIFEST_FILENAME).read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    if expected_manifest_sha256 is not None:
        _validate_expected_sha256(expected_manifest_sha256)
        if manifest_sha256 != expected_manifest_sha256:
            raise ArtifactIntegrityError(
                "manifest SHA-256 does not match the external trust anchor"
            )
    try:
        manifest = PriceChangeVolatilityArtifactManifest.model_validate_json(
            manifest_bytes
        )
    except Exception as exc:
        raise ArtifactIntegrityError("manifest.json failed strict validation") from exc
    if _serialize_manifest(manifest) != manifest_bytes:
        raise ArtifactIntegrityError("manifest.json is not in canonical wire format")

    result_bytes = (artifact_path / RESULT_FILENAME).read_bytes()
    report_bytes = (artifact_path / REPORT_FILENAME).read_bytes()
    _validate_file_metadata(manifest.result_file, result_bytes)
    _validate_file_metadata(manifest.report_file, report_bytes)

    try:
        result = PriceChangeVolatilityResult.model_validate_json(result_bytes)
    except Exception as exc:
        raise ArtifactIntegrityError("result.json failed strict validation") from exc
    if serialize_price_change_volatility_result(result) != result_bytes:
        raise ArtifactIntegrityError("result.json is not in canonical wire format")

    calculated_artifact_id = _artifact_id(_sha256_bytes(result_bytes))
    if manifest.artifact_id != calculated_artifact_id:
        raise ArtifactIntegrityError("artifact ID does not match result.json SHA-256")
    if expected_artifact_id is not None and manifest.artifact_id != expected_artifact_id:
        raise ArtifactIntegrityError("artifact ID does not match the expected artifact ID")
    if enforce_directory_name and artifact_path.name != manifest.artifact_id:
        raise ArtifactIntegrityError("artifact directory name does not match artifact ID")

    if manifest.source_request_id != result.source.request_id:
        raise ArtifactIntegrityError("manifest source request ID does not match result")
    if manifest.source_bundle_sha256 != result.source.bundle_sha256:
        raise ArtifactIntegrityError("manifest source Bundle SHA-256 does not match result")
    if manifest.parameters != result.parameters:
        raise ArtifactIntegrityError("manifest parameters do not match result")
    if manifest.main_conclusion != result.main.conclusion:
        raise ArtifactIntegrityError("manifest main conclusion does not match result")
    if manifest.final_conclusion != result.final_conclusion:
        raise ArtifactIntegrityError("manifest final conclusion does not match result")

    expected_report = render_price_change_volatility_report(result)
    if report_bytes != expected_report:
        raise ArtifactIntegrityError(
            "report.md does not match the deterministic rendering of result.json"
        )
    try:
        report_markdown = report_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:  # pragma: no cover - rendering catches first
        raise ArtifactIntegrityError("report.md is not valid UTF-8") from exc
    return LoadedPriceChangeVolatilityArtifact(
        artifact_path=artifact_path,
        manifest=manifest,
        result=result,
        report_markdown=report_markdown,
        manifest_sha256=manifest_sha256,
    )


def load_price_change_volatility_artifact(
    artifact_path: str | Path,
    expected_manifest_sha256: str | None = None,
) -> LoadedPriceChangeVolatilityArtifact:
    """Strictly load and verify an immutable analysis artifact package."""

    return _load_and_verify(
        Path(artifact_path),
        expected_manifest_sha256=expected_manifest_sha256,
        enforce_directory_name=True,
    )


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_directory(temporary_path: Path, target_path: Path) -> None:
    os.rename(temporary_path, target_path)


def _create_temporary_directory(root: Path) -> Path:
    for _ in range(100):
        candidate = root / f".analysis-tmp-{secrets.token_hex(8)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise ArtifactPathError("could not allocate a unique temporary artifact directory")


def _safe_cleanup_temporary_directory(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, FileNotFoundError):  # pragma: no cover - defensive guard
        return
    if path.exists() and path.is_dir() and not _path_is_symlink(path):
        shutil.rmtree(path)


def _persisted_details(
    target_path: Path,
    manifest: PriceChangeVolatilityArtifactManifest,
    manifest_sha256: str,
) -> PersistedPriceChangeVolatilityArtifact:
    return PersistedPriceChangeVolatilityArtifact(
        artifact_id=manifest.artifact_id,
        artifact_path=target_path,
        result_path=target_path / RESULT_FILENAME,
        report_path=target_path / REPORT_FILENAME,
        manifest_path=target_path / MANIFEST_FILENAME,
        result_sha256=manifest.result_file.sha256,
        report_sha256=manifest.report_file.sha256,
        manifest_sha256=manifest_sha256,
    )


def persist_price_change_volatility_artifact(
    result: PriceChangeVolatilityResult,
    artifact_root: str | Path,
) -> PersistedPriceChangeVolatilityArtifact:
    """Atomically publish a deterministic, immutable analysis artifact package."""

    result_bytes = serialize_price_change_volatility_result(result)
    report_bytes = render_price_change_volatility_report(result)
    result_sha256 = _sha256_bytes(result_bytes)
    artifact_id = _artifact_id(result_sha256)
    manifest = _build_manifest(
        result,
        artifact_id,
        result_bytes,
        report_bytes,
    )
    manifest_bytes = _serialize_manifest(manifest)
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    expected_payloads = {
        RESULT_FILENAME: result_bytes,
        REPORT_FILENAME: report_bytes,
        MANIFEST_FILENAME: manifest_bytes,
    }

    root = Path(artifact_root)
    _reject_traversal(root)
    _assert_no_symlink_components(root)
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(root)
    if not root.is_dir():
        raise ArtifactPathError(f"artifact root is not a directory: {root}")
    target_path = root / artifact_id

    if target_path.exists() or _path_is_symlink(target_path):
        try:
            loaded = load_price_change_volatility_artifact(target_path)
        except PriceChangeVolatilityArtifactError as exc:
            raise ArtifactConflictError(
                "existing artifact is invalid or differs; immutable target was not overwritten"
            ) from exc
        actual_payloads = {
            filename: (target_path / filename).read_bytes()
            for filename in _EXPECTED_FILENAMES
        }
        if actual_payloads != expected_payloads:
            raise ArtifactConflictError(
                "existing artifact differs; immutable target was not overwritten"
            )
        return _persisted_details(target_path, loaded.manifest, loaded.manifest_sha256)

    temporary_path = _create_temporary_directory(root)
    published = False
    try:
        for filename in (RESULT_FILENAME, REPORT_FILENAME, MANIFEST_FILENAME):
            _write_new_file(temporary_path / filename, expected_payloads[filename])
        _load_and_verify(
            temporary_path,
            expected_manifest_sha256=manifest_sha256,
            enforce_directory_name=False,
            expected_artifact_id=artifact_id,
        )
        if target_path.exists() or _path_is_symlink(target_path):
            raise ArtifactConflictError(
                "artifact target appeared during publication and was not overwritten"
            )
        try:
            _publish_directory(temporary_path, target_path)
        except FileExistsError as exc:
            raise ArtifactConflictError(
                "artifact target already exists and was not overwritten"
            ) from exc
        published = True
    finally:
        if not published:
            _safe_cleanup_temporary_directory(temporary_path, root)

    loaded = load_price_change_volatility_artifact(
        target_path,
        expected_manifest_sha256=manifest_sha256,
    )
    return _persisted_details(target_path, loaded.manifest, loaded.manifest_sha256)


__all__ = [
    "ANALYSIS_TYPE",
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactConflictError",
    "ArtifactFileMetadata",
    "ArtifactIntegrityError",
    "ArtifactPathError",
    "LoadedPriceChangeVolatilityArtifact",
    "PersistedPriceChangeVolatilityArtifact",
    "PriceChangeVolatilityArtifactError",
    "PriceChangeVolatilityArtifactManifest",
    "load_price_change_volatility_artifact",
    "persist_price_change_volatility_artifact",
    "render_price_change_volatility_report",
    "serialize_price_change_volatility_result",
]
