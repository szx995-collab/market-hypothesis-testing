"""TEST-ONLY synthetic offline example tests (v0.3.0 Phase 5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/v0.3_data_ready"
RUN_EXAMPLE = EXAMPLE / "run_example.py"
VERIFIER = ROOT / "scripts/verify_v0_3_offline_example.py"

EXAMPLE_SOURCE_FILES = [
    RUN_EXAMPLE,
    EXAMPLE / "README.md",
    *(EXAMPLE / "fixtures").glob("*.json"),
    *(EXAMPLE / "fixtures").glob("*.csv"),
]


def _run(*arguments: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


class OfflineExampleTest(unittest.TestCase):
    def test_source_tree_run_reaches_data_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            completed = _run(
                str(RUN_EXAMPLE), "--output-dir", str(output)
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "data_ready")
        self.assertTrue(summary["TEST_ONLY_SYNTHETIC_EXAMPLE"])
        self.assertTrue(summary["NOT_MARKET_DATA"])
        self.assertTrue(summary["NOT_ANALYSIS"])
        self.assertTrue(summary["NOT_INVESTMENT_ADVICE"])
        self.assertFalse(summary["analysis_artifact_generated"])
        self.assertEqual(summary["requirement_count"], 2)

    def test_verifier_console_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            completed = _run(str(VERIFIER), "--output-dir", str(output))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(report["checks"]["status_data_ready"])
        self.assertTrue(report["checks"]["no_analysis_artifact"])
        self.assertTrue(report["checks"]["requirement_count_2"])

    def test_verifier_module_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            completed = _run(
                "-m", "scripts.verify_v0_3_offline_example",
                "--output-dir", str(output),
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])

    def test_manifest_id_is_present_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            completed = _run(
                str(RUN_EXAMPLE), "--output-dir", str(output)
            )
            summary = json.loads(completed.stdout)
            manifest = json.loads(
                (output / "artifacts/data-ready-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(
            summary["data_ready_id"],
            manifest["data_ready_id"],
        )
        self.assertEqual(manifest["data_ready_schema_version"], "1.1")
        self.assertEqual(len(summary["data_ready_manifest_sha256"]), 64)

    def test_outputs_stay_inside_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            completed = _run(
                str(RUN_EXAMPLE), "--output-dir", str(output)
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertTrue(
            Path(summary["output_dir"]).resolve().is_relative_to(
                Path(tmp).resolve()
            )
            or str(summary["output_dir"]).startswith(
                tempfile.gettempdir()
            )
        )

    def test_example_never_references_network_or_credentials(self) -> None:
        forbidden = [
            "import urllib",
            "import requests",
            "urllib.request",
            "http://",
            "https://",
            "FRED_API_KEY",
            "os.environ",
            "getenv",
            "socket",
        ]
        for source in EXAMPLE_SOURCE_FILES:
            if source.suffix in (".csv",):
                continue
            if source.suffix == ".json":
                # fixtures may carry example.invalid URIs as provenance only
                text = source.read_text(encoding="utf-8")
                for token in ("urllib", "requests", "FRED_API_KEY", "socket"):
                    self.assertNotIn(
                        token,
                        text,
                        f"{source.name} must not reference {token}",
                    )
                continue
            text = source.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(
                    token,
                    text,
                    f"{source.name} must not reference {token}",
                )

    def test_rerun_requires_fresh_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            first = _run(str(RUN_EXAMPLE), "--output-dir", str(output))
            self.assertEqual(first.returncode, 0, first.stderr)
            second = _run(
                str(RUN_EXAMPLE), "--output-dir", str(output)
            )
            self.assertEqual(second.returncode, 1)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["status"], "failed")


if __name__ == "__main__":
    unittest.main()
