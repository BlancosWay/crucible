import json
import re
from pathlib import Path

import pytest

from crucible.cli import _load_resolutions

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


def _section(text: str, heading_substr: str) -> str:
    """Body of the markdown section whose heading contains `heading_substr`, from that heading to the
    next heading of the same-or-higher level (headings inside ``` fences ignored)."""
    lines = text.splitlines()
    in_fence = False
    start = None
    start_level = 0
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+\S", ln)
        if not m:
            continue
        level = len(m.group(1))
        if start is None:
            if heading_substr in ln:
                start, start_level = i, level
            continue
        if level <= start_level:
            return "\n".join(lines[start:i])
    assert start is not None, f"section {heading_substr!r} not found"
    return "\n".join(lines[start:])


def _bullet(text: str, anchor: str) -> str:
    """The Markdown list item (a `- **anchor…**` bullet plus its wrapped continuation lines) whose
    first line contains `anchor`, up to the next top-level bullet, heading, or blank line."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if anchor in l), None)
    assert start is not None, f"bullet {anchor!r} not found"
    out = [lines[start]]
    for l in lines[start + 1:]:
        if re.match(r"^\s*-\s+\*\*", l) or re.match(r"^#{1,6}\s", l) or l.strip() == "":
            break
        out.append(l)
    return "\n".join(out)


def _para(text: str, anchor: str) -> str:
    """The blank-line-delimited paragraph containing `anchor`."""
    for block in re.split(r"\n\s*\n", text):
        if anchor in block:
            return block
    raise AssertionError(f"paragraph with {anchor!r} not found")


def _flat(s: str) -> str:
    """Lowercased, whitespace-collapsed, emphasis/code/comment markers (*, `, #) removed."""
    return " ".join(s.lower().replace("*", "").replace("`", "").replace("#", " ").split())


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
    # Section-scoped, canonical phrases (not isolated words that a negation could satisfy): the
    # Content-bindings section must document the per-gate binding field shape, the echo requirement,
    # and approval only after explicit human OK; the schema/legacy bullet must state the actual
    # guarantee (schema v2, immutability, legacy read-only + fresh run); node transitions are enforced.
    text = (ROOT / "docs" / "cli.md").read_text()

    binds = _flat(_section(text, "Content bindings & human approval"))
    assert "trusted cli metadata" in binds
    assert "artifact_sha256 for reproduce" in binds                       # per-gate field shape …
    assert "artifact_sha256 + dag_sha256 for plan/final" in binds
    assert "artifact_sha256 + dag_sha256 + node_sha256 for dep" in binds
    assert "the critic echoes it and verdict requires an exact match" in binds
    assert "after the human explicitly approves" in binds                 # approve-plan ordering

    legacy = _flat(_bullet(text, "Schema version & legacy runs"))
    assert "schema_version: 2" in legacy
    assert "binds every gate decision to the exact reviewed artifact" in legacy
    assert "immutable after acceptance" in legacy
    assert "legacy / unverified" in legacy and "never clean" in legacy
    assert "start a fresh run" in legacy

    trans = _flat(_bullet(text, "Node statuses"))
    assert "transitions are enforced" in trans


def test_cli_docs_document_report_statuses():
    # Scope to the Report-statuses section so a status word appearing incidentally elsewhere can't
    # satisfy the guard; every configured status must be defined here.
    section = _flat(_section((ROOT / "docs" / "cli.md").read_text(), "Report statuses"))
    for status in ("legacy / unverified", "invalid", "blocked", "flagged", "clean", "in progress"):
        assert status in section, f"docs/cli.md Report-statuses section omits the {status!r} status"


def test_readme_documents_artifact_binding_and_legacy():
    # Scope to the README binding paragraph and assert the canonical guarantee (bound-to-artifact,
    # schema v2, the echoed hash fields, legacy read-only + fresh run) so negated/isolated words fail.
    para = _flat(_para((ROOT / "README.md").read_text(), "Every gate decision is bound"))
    assert "bound to the exact reviewed artifact" in para
    assert "schema v2" in para
    assert "echo those artifact_sha256/dag_sha256/node_sha256" in para
    assert "legacy" in para and "legacy / unverified" in para
    assert "cannot be mutated" in para
    assert "fresh run" in para


def test_security_names_binding_and_phase_enforcement_without_overclaim():
    # Scope to the determinism bullet: it must name content bindings + configured phase/transition
    # enforcement as the guarantee, and honestly scope it (no tamper-proofing claim against an
    # operator who can rewrite files/log bytes).
    sec = (ROOT / "SECURITY.md").read_text()
    bullet = _flat(_bullet(sec, "Determinism over judgment, bound to content"))
    assert "binds every gate decision to the exact reviewed artifact" in bullet
    assert "content bindings" in bullet
    assert "phase order" in bullet
    assert "transitions" in bullet
    assert "not a claim of resistance to an operator" in bullet          # honest scope, in-context
    low = sec.lower()
    assert "tamper-proof" not in low
    assert "tamper-resistant" not in low
    assert "tamper resistance" not in low


def test_changelog_records_workflow_integrity():
    # Scope to the [Unreleased] section and assert the canonical workflow-integrity entry (bound
    # artifact, schema v2, content bindings, legacy read-only, fresh run) — not isolated words.
    unreleased = _flat(_section((ROOT / "CHANGELOG.md").read_text(), "[Unreleased]"))
    assert "every gate decision is bound to the exact reviewed artifact" in unreleased
    assert "schema_version: 2" in unreleased or "schema v2" in unreleased
    assert "content bindings" in unreleased
    assert "legacy" in unreleased and "legacy / unverified" in unreleased
    assert "fresh run" in unreleased


def test_command_docs_mention_artifact_binding():
    # Each command doc must state the canonical binding handshake (bound-to-artifact, schema v2, the
    # verdict-must-echo bindings, legacy read-only) — a canonical phrase, not just the word "bound".
    for name in ("crucible", "deep-dive", "pr-review"):
        low = _flat((ROOT / "commands" / f"{name}.md").read_text())
        assert "gate decision is bound to the exact" in low, f"commands/{name}.md omits the binding handshake"
        assert "schema v2" in low, f"commands/{name}.md omits the schema-2 claim"
        assert "bindings" in low and "verdict must echo" in low, f"commands/{name}.md omits the echo requirement"
        assert "legacy / unverified" in low, f"commands/{name}.md omits the legacy behavior"


# --- --resolutions grammar guards: the skill/rubric examples must match the CLI parser --------------
# `_load_resolutions` rejects a bare `wontfix`/`deferred` (a resolution that clears a finding without a
# fix must carry the object form with a non-empty `rationale`). A user copy/pasting a documented
# example must not hit a runtime rejection, so every executable `--resolutions` example in the skill
# and rubric docs is parsed through the real CLI loader here.

RESOLUTION_EXAMPLE_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "skills" / "crucible" / "references" / "consensus-rubric.md",
]


def _resolution_map_examples(text: str) -> list[dict]:
    """Every inline-code JSON object in `text` that is a top-level `--resolutions` map
    (`{finding_id: resolution}`) — i.e. a non-empty dict whose EVERY value is a bare resolution
    keyword (`fixed`/`deferred`/`wontfix`) or an object carrying a `"resolution"` key. The inner
    object form `{"resolution": …, "rationale": …}` is deliberately excluded (its `rationale` value
    is not a resolution), so only genuine top-level resolution maps are validated."""
    examples: list[dict] = []
    for span in re.findall(r"`([^`]*)`", text):
        span = span.strip()
        if not (span.startswith("{") and span.endswith("}")):
            continue
        try:
            obj = json.loads(span)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or not obj:
            continue
        def _is_resolution(v):
            return v in ("fixed", "deferred", "wontfix") or (isinstance(v, dict) and "resolution" in v)
        if all(_is_resolution(v) for v in obj.values()):
            examples.append(obj)
    return examples


def test_resolution_examples_in_docs_match_cli_grammar(tmp_path):
    # Every documented `--resolutions` example must be EXECUTABLE: the real `_load_resolutions` accepts
    # it, and every non-fixed resolution uses the object+rationale form the CLI requires (a bare
    # `wontfix`/`deferred` would be rejected at runtime, breaking the documented workflow).
    total = 0
    for doc in RESOLUTION_EXAMPLE_DOCS:
        examples = _resolution_map_examples(doc.read_text())
        assert examples, f"{doc} has no --resolutions example to validate"
        for obj in examples:
            total += 1
            path = tmp_path / "res.json"
            path.write_text(json.dumps(obj))
            _load_resolutions(str(path))  # must not raise: a rejected example is a broken doc
            for fid, val in obj.items():
                res = val if isinstance(val, str) else val.get("resolution")
                if res in ("wontfix", "deferred"):
                    assert isinstance(val, dict) and isinstance(val.get("rationale"), str) \
                        and val["rationale"].strip(), \
                        f"{doc}: {fid} is a bare {res!r}; use the object+rationale form"
    assert total >= 2, "expected at least the SKILL.md and consensus-rubric.md resolution examples"


def test_load_resolutions_rejects_bare_nonfixed_resolution(tmp_path):
    # The grammar the docs must match: a bare `wontfix`/`deferred` (clearing a finding with no recorded
    # reason) is rejected; the object+rationale form is accepted. This is the guard the docs would trip
    # if an example regressed to the bare form.
    for res in ("wontfix", "deferred"):
        bare = tmp_path / "bare.json"
        bare.write_text(json.dumps({"F1": res}))
        with pytest.raises(ValueError, match="rationale"):
            _load_resolutions(str(bare))
        ok = tmp_path / "ok.json"
        ok.write_text(json.dumps({"F1": {"resolution": res, "rationale": "recorded reason"}}))
        _load_resolutions(str(ok))  # object form with a rationale is accepted
