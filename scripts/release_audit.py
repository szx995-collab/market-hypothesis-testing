"""Offline, redacted pre-release repository safety audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


IGNORED_DIRECTORIES = {
    ".git",
    ".market_validator",
    ".release-check",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "venv",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".csv",
    ".in",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_FILENAMES = {
    ".env",
    ".netrc",
    ".pypirc",
    "auth.json",
    "id_ed25519",
    "id_rsa",
}
SAFE_SECRET_MARKERS = {
    "example",
    "fake",
    "never-expose",
    "not-public",
    "redacted",
    "sentinel",
    "synthetic",
    "test",
}


def _git_files(root: Path) -> list[Path] | None:
    if not (root / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return [
        root / item.decode("utf-8", errors="strict")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def _fallback_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
        and not any(
            part.endswith(".egg-info") for part in path.relative_to(root).parts
        )
    ]


def _looks_like_safe_fixture(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in SAFE_SECRET_MARKERS)


def _scan_text(relative_path: str, text: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    patterns = (
        (
            "private_key_material",
            re.compile("-----BEGIN " + "PRIVATE KEY-----"),
        ),
        (
            "github_access_token",
            re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
        ),
        (
            "aws_access_key",
            re.compile("AK" + r"IA[0-9A-Z]{16}"),
        ),
        (
            "windows_user_directory",
            re.compile("C:" + r"\\Users\\(?!<)[^\\\s]+", re.IGNORECASE),
        ),
        (
            "posix_user_directory",
            re.compile(r"/(?:home|Users)/(?!<)[A-Za-z0-9._-]+/"),
        ),
    )
    assignment = re.compile(
        r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*"
        r"[\"']([A-Za-z0-9_./+=-]{20,})[\"']"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in patterns:
            if pattern.search(line):
                violations.append(
                    {"path": relative_path, "line": line_number, "rule": rule}
                )
        for match in assignment.finditer(line):
            if not _looks_like_safe_fixture(match.group(1)):
                violations.append(
                    {
                        "path": relative_path,
                        "line": line_number,
                        "rule": "credential_assignment",
                    }
                )
    return violations


def audit_repository(root: Path) -> tuple[dict[str, object], int]:
    root = root.resolve(strict=True)
    tracked = _git_files(root)
    git_metadata_present = tracked is not None
    files = tracked if tracked is not None else _fallback_files(root)
    violations: list[dict[str, object]] = []
    scanned_count = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        if any(part in IGNORED_DIRECTORIES for part in Path(relative).parts):
            continue
        if any(part.endswith(".egg-info") for part in Path(relative).parts):
            continue
        lowered_name = path.name.casefold()
        if lowered_name in SENSITIVE_FILENAMES or (
            lowered_name.startswith(".env.") and lowered_name != ".env.example"
        ):
            violations.append({"path": relative, "line": None, "rule": "sensitive_filename"})
        if path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        scanned_count += 1
        violations.extend(_scan_text(relative, text))

    license_present = any(root.glob("LICENSE*")) or any(root.glob("COPYING*"))
    blockers: list[str] = []
    if not git_metadata_present:
        blockers.append("git_metadata_absent")
    if not license_present:
        blockers.append("license_not_selected")
    payload: dict[str, object] = {
        "ok": not violations,
        "scan_scope": "git_tracked_files" if git_metadata_present else "release_candidates_fallback",
        "git_metadata_present": git_metadata_present,
        "files_scanned": scanned_count,
        "violations": violations,
        "release_blockers": blockers,
        "release_ready": not violations and not blockers,
    }
    return payload, 0 if not violations else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        payload, exit_code = audit_repository(args.root)
    except (OSError, ValueError):
        payload = {
            "ok": False,
            "error": "repository could not be audited safely",
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
