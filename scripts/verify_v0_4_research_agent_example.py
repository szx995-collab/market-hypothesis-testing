"""Release-only verifier for the v0.4 research agent offline example.

Runs examples/v0.4_research_agent/run_example.py in a temporary directory
and checks the 13 acceptance points. This script is intentionally NOT part
of the CI matrix; it is a one-shot release acceptance run.

Usage:
    python scripts/verify_v0_4_research_agent_example.py

Exit code 0 with {"ok": true, ...} on success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SCRIPT = (
    REPO_ROOT / "examples" / "v0.4_research_agent" / "run_example.py"
)

FORBIDDEN_IN_REPORT = (
    "api_key",
    "api-key",
    "DEEPSEEK_API_KEY",
    "FRED_API_KEY",
    "Authorization",
)
ABSOLUTE_USER_PATTERNS = (r"C:\\Users\\", r"/home/", r"/Users/")
TRADING_ADVICE_WORDS = ("买入", "卖出", "建仓", "仓位", "trading signal")


def _run_example(output_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(EXAMPLE_SCRIPT),
            "--output",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )


def _no_symlinks(root: Path) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if path.is_symlink():
            found.append(str(path))
    return found


def verify() -> dict:
    with tempfile.TemporaryDirectory(prefix="v0.4-example-") as tmp:
        output = Path(tmp) / "run"
        source_csv = (
            REPO_ROOT / "examples" / "v0.4_research_agent"
        ).resolve()
        csv_before = {}
        for csv_path in sorted(source_csv.glob("fixtures/*.csv")):
            csv_before[csv_path.name] = csv_path.read_bytes()

        completed = _run_example(output)
        if completed.returncode != 0:  # acceptance point 2
            raise AssertionError(
                f"example exited {completed.returncode}: "
                f"{completed.stdout}\n{completed.stderr}"
            )

        payload = json.loads(completed.stdout)  # acceptance point 3
        if not payload.get("valid"):  # acceptance point 1
            raise AssertionError("stdout JSON valid=false")
        if payload.get("status") != "completed":  # acceptance point 4
            raise AssertionError(f"status={payload.get('status')}")

        sys.path.insert(0, str(REPO_ROOT / "src"))
        from market_validator.agent.session import (
            ResearchSession,
            load_latest_research_session,
            load_verified_artifact,
        )
        session: ResearchSession = load_latest_research_session(output)  # acceptance point 5
        for reference in session.artifact_references:  # acceptance point 6
            load_verified_artifact(output, reference)

        analysis_result_ref = next(
            (
                ref
                for ref in session.artifact_references
                if ref.artifact_type == "analysis_result"
            ),
            None,
        )
        if analysis_result_ref is None:  # acceptance point 7
            raise AssertionError("no analysis_result artifact reference")
        load_verified_artifact(output, analysis_result_ref)

        interpretations_root = (  # acceptance point 8
            output / "analysis-interpretations" / payload["analysis_result_id"]
        )
        manifest_found = False
        for entry in sorted(interpretations_root.iterdir()):
            manifest = entry / "interpretation-manifest.json"
            if manifest.is_file():
                json.loads(manifest.read_text(encoding="utf-8"))
                manifest_found = True
        if not manifest_found:
            raise AssertionError("interpretation manifest missing")

        report_path = Path(payload["report_path"])  # acceptance point 9
        if not report_path.is_file():
            raise AssertionError(f"report missing: {report_path}")
        report = report_path.read_text(encoding="utf-8")

        for expected in ("结论", "统计", "限制", "免责"):  # point 10
            if expected not in report:
                raise AssertionError(f"report lacks section marker {expected}")
        for forbidden in FORBIDDEN_IN_REPORT:  # point 11
            if forbidden.lower() in report.lower():
                raise AssertionError(f"report contains {forbidden}")
        for pattern in ABSOLUTE_USER_PATTERNS:
            if pattern in report:
                raise AssertionError(f"report contains absolute path {pattern}")
        for word in TRADING_ADVICE_WORDS:
            if word in report:
                raise AssertionError(f"report contains trading advice {word}")

        symlinks = _no_symlinks(output)  # acceptance point 12
        if symlinks:
            raise AssertionError(f"symlinks found: {symlinks[:3]}")

        for name, before in csv_before.items():  # acceptance point 13
            now = (source_csv / "fixtures" / name).read_bytes()
            if now != before:
                raise AssertionError(f"source CSV modified: {name}")

        return {
            "ok": True,
            "status": payload["status"],
            "report_verified": True,
            "artifacts_verified": True,
            "session_id": payload["session_id"],
            "analysis_result_id": payload["analysis_result_id"],
            "overall_conclusion": payload["overall_conclusion"],
        }


def main() -> int:
    try:
        result = verify()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
