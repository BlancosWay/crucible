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
    ROOT / "commands" / "deep-dive.md",
    ROOT / "commands" / "pr-review.md",
    ROOT / "docs" / "install" / "copilot-cli.md",
    ROOT / "docs" / "install" / "claude-code.md",
    ROOT / "docs" / "install" / "codex.md",
]
RUN_CONFIG_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "skills" / "crucible" / "references" / "platform-notes.md",
    ROOT / "skills" / "deep-dive" / "SKILL.md",
    ROOT / "skills" / "deep-dive" / "references" / "platform-notes.md",
    ROOT / "skills" / "pr-review" / "SKILL.md",
    ROOT / "skills" / "pr-review" / "references" / "platform-notes.md",
]
LIVE_DEFAULT_DOCS = [*SOURCE_REFERENCE_DOCS, *RUN_CONFIG_DOCS]
NO_MODEL_LITERAL_FILES = [
    ROOT / "scripts" / "crucible" / "config.py",
    ROOT / "tests" / "test_config.py",
    ROOT / "tests" / "test_report.py",
    *LIVE_DEFAULT_DOCS,
    # deep-dive references not already covered via the config-referencing doc sets above
    # (platform-notes is covered via RUN_CONFIG_DOCS).
    ROOT / "skills" / "deep-dive" / "references" / "peer-prompt.md",
    ROOT / "skills" / "deep-dive" / "references" / "consensus-rubric.md",
    ROOT / "skills" / "deep-dive" / "references" / "investigation-thread.md",
    # pr-review references not already covered via RUN_CONFIG_DOCS (platform-notes is covered there).
    ROOT / "skills" / "pr-review" / "references" / "peer-prompt.md",
    ROOT / "skills" / "pr-review" / "references" / "consensus-rubric.md",
    ROOT / "skills" / "pr-review" / "references" / "review-thread.md",
]

# Docs that contain runnable workflow examples a user might copy/paste.
WORKFLOW_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "skills" / "deep-dive" / "SKILL.md",
    ROOT / "skills" / "pr-review" / "SKILL.md",
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


# --- Workflow-integrity (schema-2) documentation guards -------------------------------------------
# The public docs must document the artifact-binding handshake and the schema-2 legacy behavior so an
# operator can trust (and debug) the deterministic contract: content bindings, `bindings` /
# `approve-plan`, legal transitions, accepted-DAG immutability, legacy read-only, and report statuses.

def test_cli_docs_document_bindings_approval_and_legacy():
    low = (ROOT / "docs" / "cli.md").read_text().lower()
    assert "bindings" in low                       # the `bindings` command
    assert "approve-plan" in low                    # the `approve-plan` command
    assert "artifact_sha256" in low                 # the content binding
    assert "schema" in low                          # schema version 2
    assert "legacy" in low                          # legacy read-only behavior
    assert "transition" in low                      # legal node transitions
    assert "immutab" in low or "cannot change" in low  # accepted DAG immutability


def test_cli_docs_document_report_statuses():
    low = (ROOT / "docs" / "cli.md").read_text().lower()
    for status in ("clean", "flagged", "blocked", "invalid", "legacy", "in progress"):
        assert status in low, f"docs/cli.md omits the {status!r} report status"


def test_readme_documents_artifact_binding_and_legacy():
    low = (ROOT / "README.md").read_text().lower()
    assert "bound" in low or "binding" in low       # gate decisions bound to reviewed artifacts
    assert "schema" in low                          # schema-2 runs
    assert "legacy" in low                          # legacy read-only / unverified


def test_security_names_binding_and_phase_enforcement_without_overclaim():
    sec = (ROOT / "SECURITY.md").read_text()
    low = sec.lower()
    assert "binding" in low                          # content bindings
    assert "phase" in low or "transition" in low     # configured phase / transition enforcement
    # Honest scope: never claim tamper-proofing against an operator who can rewrite files/log bytes.
    assert "tamper-proof" not in low
    assert "tamper-resistant" not in low
    assert "tamper resistance" not in low


def test_changelog_records_workflow_integrity():
    low = (ROOT / "CHANGELOG.md").read_text().lower()
    assert "bind" in low                             # artifact/content binding
    assert "schema" in low                           # schema-2 runs
    assert "legacy" in low                           # legacy read-only behavior


def test_command_docs_mention_artifact_binding():
    for name in ("crucible", "deep-dive", "pr-review"):
        low = (ROOT / "commands" / f"{name}.md").read_text().lower()
        assert "bound" in low or "binding" in low, f"commands/{name}.md omits the binding handshake"
