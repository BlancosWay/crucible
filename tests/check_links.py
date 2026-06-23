#!/usr/bin/env python3
"""Check that internal Markdown links in the repo resolve to real files.

Pure stdlib, no network. Scans every Markdown file for relative links (skipping
http(s):// and pure #anchors) and verifies the target path exists. Exit code
0 = all good, 1 = one or more broken links.

Run:  python3 tests/check_links.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Markdown inline links [text](target), excluding external URLs, mailto, and
# pure in-page anchors.
LINK_RE = re.compile(r"\]\((?!https?://|mailto:|#)([^)]+)\)")

# Fenced code blocks (``` or ~~~). Example Markdown shown inside docs/specs/plans
# must not be parsed as real links.
FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$\n?")


def strip_code_fences(text: str) -> str:
    return FENCE_RE.sub("", text)


SKIP_DIRS = {".git", ".venv"}

# Brainstorming/planning artifacts (superpowers workflow) legitimately embed
# illustrative example Markdown — example commands, file contents, and link
# syntax inside nested code fences. Those aren't live repo links, and nested
# same-length fences can't be reliably stripped, so skip the plans tree as a
# source. `runs/` holds generated, git-ignored run reports (model output), which
# may contain illustrative paths and are not part of the repo's doc surface.
# Specs are still checked.
SKIP_PREFIXES = ("docs/superpowers/plans/", "runs/")

broken: list[str] = []
checked = 0

for md in ROOT.rglob("*.md"):
    if any(part in SKIP_DIRS for part in md.parts):
        continue
    rel_posix = md.relative_to(ROOT).as_posix()
    if any(rel_posix.startswith(pfx) for pfx in SKIP_PREFIXES):
        continue
    text = strip_code_fences(md.read_text(encoding="utf-8"))
    for raw in LINK_RE.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target:
            continue  # pure anchor like (#section)
        checked += 1
        resolved = (md.parent / target).resolve()
        if not resolved.exists():
            broken.append(f"{md.relative_to(ROOT)} -> {raw}")

print(f"Markdown internal links checked: {checked}")
if broken:
    print(f"\nFAIL ({len(broken)} broken link(s)):")
    for b in broken:
        print(f"  - {b}")
    sys.exit(1)
print("PASS - all internal Markdown links resolve.")
sys.exit(0)
