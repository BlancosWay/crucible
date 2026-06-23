#!/usr/bin/env python3
"""Deterministic guard: the published version must identify the shipped code.

Both plugin manifests must declare the SAME, valid-SemVer version, and that
version must have a matching dated section in CHANGELOG.md (so you cannot bump a
manifest without recording the release). An '## [Unreleased]' section must also
exist for in-flight changes. Pure stdlib (unittest), no network — so the release
workflow can run it without installing pytest.
"""
from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Reuse the SAME canonical SemVer core that scripts/changelog.py uses for release-cut
# headings, so the manifest check and the changelog guard can never drift apart.
_spec = importlib.util.spec_from_file_location("changelog", ROOT / "scripts" / "changelog.py")
_changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_changelog)
SEMVER = re.compile(r"^" + _changelog.SEMVER_CORE + r"$")


def _plugin_version() -> str:
    return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def _marketplace_version() -> str:
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    return data["plugins"][0]["version"]


class TestVersionConsistency(unittest.TestCase):
    def test_manifests_agree(self):
        self.assertEqual(
            _plugin_version(),
            _marketplace_version(),
            "plugin.json and marketplace.json must declare the same version",
        )

    def test_version_is_semver(self):
        self.assertRegex(_plugin_version(), SEMVER, "manifest version must be valid SemVer (X.Y.Z)")

    def test_semver_rejects_malformed(self):
        for bad in ("1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.3-", "1.2.3+", "v1.2.3",
                    "1.2.x", "", "1.2.3-alpha..1", "1.2.3-01"):
            self.assertNotRegex(bad, SEMVER, f"should reject {bad!r}")

    def test_changelog_has_dated_section_for_current_version(self):
        version = _plugin_version()
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            re.compile(r"^## \[" + re.escape(version) + r"\] [-\u2014] \d{4}-\d{2}-\d{2}", re.M),
            f"CHANGELOG.md must have a dated '## [{version}] - YYYY-MM-DD' section "
            f"matching the manifest version (record the release before/with the bump)",
        )

    def test_changelog_has_unreleased_section(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [Unreleased]", text, "CHANGELOG.md must keep an '## [Unreleased]' section")

    def test_current_version_section_is_nonempty(self):
        # The release workflow publishes the current version's section as the release
        # notes; guard here (a REQUIRED check) that those notes are non-empty, so a
        # version bump with an empty section can never reach a tag.
        version = _plugin_version()
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        body = _changelog.extract_section(text, version)
        self.assertTrue(
            body.strip(),
            f"CHANGELOG '## [{version}]' section is empty — add release notes "
            f"(release.yml publishes this section as the GitHub Release body)",
        )


if __name__ == "__main__":
    unittest.main()
