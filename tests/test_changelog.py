"""Tests for the CHANGELOG guard helpers in scripts/changelog.py.

`pytest.ini` puts `scripts/` on the path, so `changelog` imports directly.
"""

from changelog import added_changelog_entry, requires_changelog

UNREL = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-06-22\n- initial\n"


def _with_unreleased(*lines: str) -> str:
    body = "\n".join(lines)
    return f"# Changelog\n\n## [Unreleased]\n{body}\n\n## [0.1.0] - 2026-06-22\n- initial\n"


def test_no_change_is_not_an_entry():
    assert added_changelog_entry(UNREL, UNREL) is False


def test_requires_changelog_covers_shipped_paths_and_config_defaults():
    # F12: a change to the shipped default config affects what every run resolves and must be noted,
    # but the guard previously matched only scripts/skills/commands. config.defaults.json is now a
    # recognized shipped runtime file; unrelated root files / docs / tests / CI still do not trigger.
    assert requires_changelog(["scripts/crucible/cli.py"]) is True
    assert requires_changelog(["skills/pr-review/SKILL.md"]) is True
    assert requires_changelog(["commands/anything.md"]) is True
    assert requires_changelog(["config.defaults.json"]) is True
    assert requires_changelog(["README.md"]) is False
    assert requires_changelog(["tests/test_changelog.py"]) is False
    assert requires_changelog(["docs/cli.md"]) is False
    # any shipped path in a mixed set still triggers the guard
    assert requires_changelog(["README.md", "config.defaults.json"]) is True
    assert requires_changelog([]) is False


def test_new_bullet_under_unreleased_counts():
    head = _with_unreleased("- Added a new gate option")
    assert added_changelog_entry(UNREL, head) is True


def test_heading_only_addition_does_not_count():
    # Adding a bare '### Fixed' with no content must NOT satisfy the guard.
    head = _with_unreleased("### Fixed")
    assert added_changelog_entry(UNREL, head) is False


def test_heading_plus_bullet_counts():
    head = _with_unreleased("### Fixed", "- Fixed the thing")
    assert added_changelog_entry(UNREL, head) is True


def test_new_dated_version_section_counts():
    base = "# Changelog\n\n## [Unreleased]\n"
    head = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-07-01\n- shipped\n"
    assert added_changelog_entry(base, head) is True


def test_bare_dated_heading_without_body_does_not_count():
    # A new dated section with no content line must NOT satisfy the guard.
    base = "# Changelog\n\n## [Unreleased]\n"
    head = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-07-01\n"
    assert added_changelog_entry(base, head) is False


def test_dated_heading_with_subheading_only_does_not_count():
    base = "# Changelog\n\n## [Unreleased]\n"
    head = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-07-01\n### Added\n"
    assert added_changelog_entry(base, head) is False


def test_tolerates_empty_base():
    head = _with_unreleased("- first ever entry")
    assert added_changelog_entry("", head) is True


def test_re_added_identical_line_counts(tmp_path=None):
    # O3: a shipped change whose new line duplicates an existing line must still count.
    base = _with_unreleased("- alpha")
    head = _with_unreleased("- alpha", "- alpha")
    assert added_changelog_entry(base, head) is True


def test_removing_a_line_is_not_an_entry():
    base = _with_unreleased("- alpha", "- beta")
    head = _with_unreleased("- alpha")
    assert added_changelog_entry(base, head) is False
