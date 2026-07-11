import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = json.loads((ROOT / "config.defaults.json").read_text())
SOURCE_REFERENCE_DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "commands" / "crucible.md",
    ROOT / "docs" / "install" / "copilot-cli.md",
    ROOT / "docs" / "install" / "claude-code.md",
    ROOT / "docs" / "install" / "codex.md",
]
RUN_CONFIG_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "skills" / "crucible" / "references" / "platform-notes.md",
]
LIVE_DEFAULT_DOCS = [*SOURCE_REFERENCE_DOCS, *RUN_CONFIG_DOCS]
NO_MODEL_LITERAL_FILES = [
    ROOT / "scripts" / "crucible" / "config.py",
    ROOT / "tests" / "test_config.py",
    ROOT / "tests" / "test_report.py",
    *LIVE_DEFAULT_DOCS,
]

# Docs that contain runnable workflow examples a user might copy/paste.
WORKFLOW_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "README.md",
    ROOT / "docs" / "superpowers" / "plans" / "2026-06-22-crucible-implementation.md",
]


def test_no_hardcoded_round_cap_override_in_workflow_examples():
    # The cap must come from run config; workflow command examples must not pass the
    # override. (The argv test form '"--max-rounds", "5"' is intentionally different and ok.)
    for p in WORKFLOW_DOCS:
        text = p.read_text()
        assert "--max-rounds 5 --file" not in text, f"{p} hardcodes the round-cap override"


def test_workflow_commands_are_runnable_with_pythonpath():
    # Every bare 'python3 -m crucible' in SKILL/README must be prefixed with PYTHONPATH=scripts.
    for p in [ROOT / "skills" / "crucible" / "SKILL.md", ROOT / "README.md"]:
        for line in p.read_text().splitlines():
            if "python3 -m crucible" in line:
                assert "PYTHONPATH=scripts python3 -m crucible" in line, f"unprefixed command in {p}: {line}"


def test_live_consumers_do_not_duplicate_default_model_ids():
    for path in NO_MODEL_LITERAL_FILES:
        text = path.read_text()
        for role in ("builder", "critic"):
            model = DEFAULTS[role]["model"]
            assert model not in text, f"{path} duplicates {role} default model {model}"


def test_live_docs_reference_authoritative_configuration():
    for path in SOURCE_REFERENCE_DOCS:
        assert "config.defaults.json" in path.read_text(), f"{path} omits defaults source"

    for path in RUN_CONFIG_DOCS:
        text = path.read_text()
        assert "config.json" in text, f"{path} omits resolved run config"


def test_live_docs_do_not_restate_builder_or_critic_defaults():
    inline_default = re.compile(r"\b(?:Defaults:\s*)?(?:Builder|Critic)\s*=\s*", re.IGNORECASE)
    for path in LIVE_DEFAULT_DOCS:
        assert not inline_default.search(path.read_text()), f"{path} restates role defaults"
