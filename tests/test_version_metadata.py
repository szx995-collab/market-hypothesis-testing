"""Version metadata consistency tests (v0.3.0 release)."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "0.3.0"


class VersionMetadataTest(unittest.TestCase):
    def test_pyproject_version(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            metadata = tomllib.load(handle)
        self.assertEqual(metadata["project"]["version"], EXPECTED)

    def test_package_version(self):
        import market_validator

        self.assertEqual(market_validator.__version__, EXPECTED)

    def test_changelog_release_heading(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{EXPECTED}] - ", changelog)
        unreleased = changelog.split("## [Unreleased]")
        self.assertGreaterEqual(len(unreleased), 2)
        heading = re.search(
            rf"## \[{re.escape(EXPECTED)}\] - \d{{4}}-\d{{2}}-\d{{2}}",
            changelog,
        )
        self.assertIsNotNone(heading)

    def test_changelog_links(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(
            f"[Unreleased]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v{EXPECTED}...HEAD", changelog
        )
        self.assertIn(
            f"[{EXPECTED}]: https://github.com/szx995-collab/market-hypothesis-testing/compare/v0.2.0...v{EXPECTED}", changelog
        )

    def test_release_notes_exist(self):
        self.assertTrue((ROOT / "docs" / "releases" / f"v{EXPECTED}.md").is_file())

    def test_tag_expectation_helper(self):
        # The annotated tag v0.3.0 must point at the release commit; this
        # helper keeps the expectation in one place for release tooling.
        self.assertEqual("v" + EXPECTED, "v0.3.0")


if __name__ == "__main__":
    unittest.main()
