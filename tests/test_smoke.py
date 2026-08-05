"""Smoke tests for the installed module entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class DoctorCommandTest(unittest.TestCase):
    def test_doctor_returns_successful_json(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        source_path = str(repository_root / "src")
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not existing_pythonpath
            else os.pathsep.join((source_path, existing_pythonpath))
        )

        completed = subprocess.run(
            [sys.executable, "-m", "market_validator", "doctor"],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["stage"], "scaffold")


if __name__ == "__main__":
    unittest.main()
