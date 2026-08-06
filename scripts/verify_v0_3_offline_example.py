#!/usr/bin/env python3
"""CI/release acceptance: run the TEST-ONLY synthetic offline v0.3 example.

Requires an empty output directory (default: TEMP). Exits non-zero if the
example does not reach Data Ready or if verification fails.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "v0.3_data_ready"

_spec = importlib.util.spec_from_file_location(
    "v03_data_ready_run_example",
    EXAMPLE_DIR / "run_example.py",
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the v0.3 offline synthetic example."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="new empty output directory (default: TEMP)",
    )
    args = parser.parse_args(argv)
    output_dir = args.output_dir or Path(
        tempfile.mkdtemp(prefix="v0.3-verify-example-")
    )
    summary = _module.run_example(output_dir)
    checks = {
        "TEST_ONLY_SYNTHETIC_EXAMPLE": summary.get(
            "TEST_ONLY_SYNTHETIC_EXAMPLE"
        ),
        "NOT_MARKET_DATA": summary.get("NOT_MARKET_DATA"),
        "NOT_ANALYSIS": summary.get("NOT_ANALYSIS"),
        "NOT_INVESTMENT_ADVICE": summary.get("NOT_INVESTMENT_ADVICE"),
        "status_data_ready": summary.get("status") == "data_ready",
        "requirement_count_2": summary.get("requirement_count") == 2,
        "no_analysis_artifact": summary.get("analysis_artifact_generated")
        is False,
        "has_data_ready_id": bool(summary.get("data_ready_id")),
        "has_manifest_sha256": bool(
            summary.get("data_ready_manifest_sha256")
        ),
    }
    ok = all(checks.values())
    report = {
        "ok": ok,
        "checks": checks,
        "summary": summary,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
