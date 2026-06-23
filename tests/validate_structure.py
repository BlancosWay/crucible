#!/usr/bin/env python3
"""Structural validation for the Crucible plugin + Python package.

Pure stdlib, no network. Validates the plugin manifests, the orchestrator skill,
the command, the deterministic Python package, that the manifests agree on a
SemVer version, that referenced ``references/*.md`` docs resolve, and that no
secret is hardcoded in a manifest. Exit code 0 = all pass, 1 = failures.

Run:  python3 tests/validate_structure.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# Deterministic helper modules the skill/CLI depend on.
PACKAGE_MODULES = ("__init__", "__main__", "config", "dag", "verdict", "runlog", "report", "cli")

failures: list[str] = []
checks = 0


def check(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        failures.append(msg)


def parse_frontmatter(path: Path) -> dict | None:
    """Return top-level frontmatter keys -> raw value."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    fm: dict[str, str] = {}
    for ln in lines[1:end]:
        if ln and not ln[0].isspace() and ":" in ln:
            k, v = ln.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def load_json(rel: str) -> dict | None:
    p = ROOT / rel
    check(p.exists(), f"missing file: {rel}")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        check(False, f"invalid JSON in {rel}: {e}")
        return None


# --- 1. Manifests ---------------------------------------------------------
plugin = load_json(".claude-plugin/plugin.json")
plugin_version = None
if plugin is not None:
    check(plugin.get("name") == "crucible", "plugin.json name should be 'crucible'")
    plugin_version = plugin.get("version")
    check(bool(plugin_version) and bool(SEMVER.match(str(plugin_version))),
          f"plugin.json version must be valid SemVer, got {plugin_version!r}")
    # Convention-based discovery: Copilot CLI and Claude Code both auto-discover
    # skills/ and commands/. Declaring them as explicit plugin.json fields breaks
    # Claude Code discovery, so they must be absent (mirrors the TradingDesk rule).
    for field in ("skills", "commands", "agents", "mcpServers"):
        check(field not in plugin,
              f"plugin.json must NOT declare '{field}' (explicit dir fields break Claude Code "
              f"discovery; rely on convention instead)")
    # The conventional locations must still exist on disk.
    for rel in ("skills", "commands"):
        check((ROOT / rel).exists(), f"missing conventional plugin path: {rel}")

market = load_json(".claude-plugin/marketplace.json")
if market is not None:
    plugins = market.get("plugins", [])
    names = [p.get("name") for p in plugins]
    check("crucible" in names, "marketplace.json must list a 'crucible' plugin")
    entry = next((p for p in plugins if p.get("name") == "crucible"), {})
    check(bool(entry.get("source")), "marketplace.json crucible entry missing 'source'")
    if plugin_version is not None:
        check(entry.get("version") == plugin_version,
              f"marketplace.json crucible version {entry.get('version')!r} != plugin.json {plugin_version!r}")

# No secret hardcoded in any manifest.
for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "config.example.json"):
    p = ROOT / rel
    if p.exists():
        raw = p.read_text(encoding="utf-8").lower()
        check("apikey=" not in raw and "api_key" not in raw and "secret" not in raw,
              f"{rel} must not hardcode a secret/API key")

# --- 2. Skill -------------------------------------------------------------
skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
check(len(skill_files) >= 1, "no skills/*/SKILL.md found")
for sf in skill_files:
    fm = parse_frontmatter(sf)
    check(fm is not None and bool(fm.get("name")), f"{sf}: missing skill 'name'")
    check(fm is not None and bool(fm.get("description")), f"{sf}: missing skill 'description'")

# --- 3. Command -----------------------------------------------------------
cmd_files = sorted((ROOT / "commands").glob("*.md"))
check(len(cmd_files) >= 1, "no commands/*.md found")
for cf in cmd_files:
    fm = parse_frontmatter(cf)
    check(fm is not None and bool(fm.get("description")), f"{cf}: missing command 'description'")

# --- 4. Python package ----------------------------------------------------
pkg = ROOT / "scripts" / "crucible"
check(pkg.is_dir(), "missing scripts/crucible package")
for mod in PACKAGE_MODULES:
    check((pkg / f"{mod}.py").exists(), f"missing scripts/crucible/{mod}.py")
check((ROOT / "pytest.ini").exists(), "missing pytest.ini (sets pythonpath=scripts)")

# --- 5. Cross-references resolve ------------------------------------------
# Every `references/<x>.md` mentioned in the skill, command, or README must exist
# under skills/crucible/references/. (docs/ and tests/ embed illustrative examples
# and are intentionally not scanned as ref sources.)
ref_re = re.compile(r"references/([a-z0-9-]+\.md)")
refs_dir = ROOT / "skills" / "crucible" / "references"
scan = [ROOT / "skills" / "crucible" / "SKILL.md", ROOT / "README.md", *sorted(refs_dir.glob("*.md")), *cmd_files]
for md in scan:
    if not md.exists():
        continue
    text = md.read_text(encoding="utf-8")
    for ref in set(ref_re.findall(text)):
        check((refs_dir / ref).exists(),
              f"{md.relative_to(ROOT)} references references/{ref} which is missing")

# The reference docs the orchestrator depends on.
for ref in ("critic-prompt.md", "builder-prompt.md", "consensus-rubric.md",
            "dependency-tree.md", "platform-notes.md"):
    check((refs_dir / ref).exists(), f"missing reference doc: references/{ref}")

# --- Report ---------------------------------------------------------------
print(f"Structural checks run: {checks}")
if failures:
    print(f"\nFAIL ({len(failures)} issue(s)):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS - all structural checks passed.")
sys.exit(0)
