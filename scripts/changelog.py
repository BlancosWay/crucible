#!/usr/bin/env python3
"""CHANGELOG utilities for Crucible releases. Pure stdlib, no network.

Subcommands:
  section <version>   Print the body of the '## [<version>]' CHANGELOG section
                      (used by .github/workflows/release.yml to build release notes).
  check --base <ref>  PR guard: if the diff <ref>...HEAD touches a *shipped* path
                      (runtime behavior), require that CHANGELOG.md adds a new entry
                      under '## [Unreleased]' (or a new dated '## [x.y.z]' release
                      section). Skipped if any PR commit message contains
                      '[skip changelog]'. Tolerant of a base that has no CHANGELOG yet.

Exit 0 on success (or when no entry is required), 1 on a guard failure.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

# Paths whose changes affect what users run and therefore must be noted in the
# CHANGELOG. Docs, tests, CI, and the manifests (handled by the release step) are
# intentionally excluded to keep the guard low-friction.
SHIPPED_PREFIXES = ("scripts/", "skills/", "commands/")

# Canonical SemVer (semver.org) core — no anchors, so it embeds inside larger patterns
# (e.g. a dated CHANGELOG release heading). tests/test_version_consistency.py reuses this
# for the full-string manifest version check, so the two can never drift apart.
SEMVER_CORE = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)

# A release "cut" is a CANONICAL, DATED heading: '## [x.y.z] - YYYY-MM-DD'. Undated or
# malformed headings must NOT count, or a PR could bypass the changelog guard with one.
# Accept either an ASCII hyphen or an em dash between version and date.
_VERSION_HEADING = re.compile(r"^## \[(" + SEMVER_CORE + r")\] [-\u2014] \d{4}-\d{2}-\d{2}\s*$", re.M)


def extract_section(text: str, version: str) -> str:
    """Return the body (no heading) of the '## [<version>]' section, trimmed."""
    head = re.compile(r"^## \[" + re.escape(version) + r"\]")
    out: list[str] = []
    capturing = False
    for line in text.splitlines():
        if not capturing:
            if head.match(line):
                capturing = True
            continue
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip("\n")


def unreleased_body(text: str) -> str:
    return extract_section(text, "Unreleased")


def requires_changelog(paths) -> bool:
    return any(p.startswith(SHIPPED_PREFIXES) for p in paths)


def _version_sections(text: str) -> set:
    """Versions that have a '## [x.y.z]' heading (ignores '## [Unreleased]')."""
    return set(_VERSION_HEADING.findall(text))


def _content_lines_list(body: str) -> list:
    """Real entry content from a CHANGELOG section body, preserving duplicates/order:
    non-blank lines that are not Markdown sub-headings ('#', '##', '###', ...)."""
    return [
        ln.strip()
        for ln in body.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _content_lines(body: str) -> set:
    """Set of real entry content lines (see ``_content_lines_list``)."""
    return set(_content_lines_list(body))


def added_changelog_entry(base_text: str, head_text: str) -> bool:
    """True iff head adds, relative to base, EITHER a newly added dated
    '## [x.y.z]' release section that has real content, OR a new non-blank,
    non-heading content line under '## [Unreleased]'. Heading-only additions —
    a bare '### Fixed', or a bare dated heading with no body — do NOT count; a
    shipped change must record actual content. Detection is by line MULTIPLICITY,
    so re-adding a line identical to an existing one still counts as an addition."""
    for version in _version_sections(head_text) - _version_sections(base_text):
        if _content_lines(extract_section(head_text, version)):
            return True
    base_counts = Counter(_content_lines_list(unreleased_body(base_text)))
    head_counts = Counter(_content_lines_list(unreleased_body(head_text)))
    return any(head_counts[ln] > base_counts[ln] for ln in head_counts)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _git_show_or_empty(ref_path: str) -> str:
    """git show <ref>:<path>, or '' if it doesn't exist on that ref (e.g. first CHANGELOG)."""
    proc = subprocess.run(["git", "show", ref_path], cwd=ROOT, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def cmd_section(version: str) -> int:
    body = extract_section(CHANGELOG.read_text(encoding="utf-8"), version)
    if not body:
        print(f"changelog: no '## [{version}]' section found", file=sys.stderr)
        return 1
    print(body)
    return 0


def cmd_check(base: str) -> int:
    # On a pull_request build HEAD is the ephemeral MERGE commit, so scan every
    # commit in the PR range (base..HEAD) for the escape hatch, not just HEAD.
    if "[skip changelog]" in _git("log", f"{base}..HEAD", "--format=%B"):
        print("changelog: '[skip changelog]' found in a PR commit - guard skipped.")
        return 0
    changed = [p for p in _git("diff", "--name-only", f"{base}...HEAD").splitlines() if p]
    if not requires_changelog(changed):
        print("changelog: no shipped paths changed - entry not required.")
        return 0
    if "CHANGELOG.md" not in changed:
        print("changelog: shipped paths changed but CHANGELOG.md was not updated. "
              "Add an entry under '## [Unreleased]' (or '[skip changelog]' in a PR commit if truly N/A).",
              file=sys.stderr)
        return 1
    base_text = _git_show_or_empty(f"{base}:CHANGELOG.md")
    head_text = CHANGELOG.read_text(encoding="utf-8")
    if not added_changelog_entry(base_text, head_text):
        print("changelog: CHANGELOG.md changed but added no new '## [Unreleased]' line and no new "
              "dated '## [x.y.z]' section vs the base - record your shipped change.",
              file=sys.stderr)
        return 1
    print("changelog: OK - shipped change is recorded in the CHANGELOG.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Crucible CHANGELOG utilities.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("section", help="print a release section body")
    s.add_argument("version")
    c = sub.add_parser("check", help="guard that shipped changes record a CHANGELOG entry")
    c.add_argument("--base", required=True, help="base git ref to diff against (e.g. origin/main)")
    args = p.parse_args(argv)
    if args.cmd == "section":
        return cmd_section(args.version)
    return cmd_check(args.base)


if __name__ == "__main__":
    raise SystemExit(main())
