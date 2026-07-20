import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "crucible" / "SKILL.md"


def _section(text: str, heading_substr: str) -> str:
    """Body of the markdown section whose heading contains `heading_substr`, from that heading to the
    next heading of the same-or-higher level. Headings inside ``` fences are ignored so a per-gate
    guard is scoped to exactly that gate — not satisfied by another gate's text elsewhere."""
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


def _flat(s: str) -> str:
    """Lowercased, whitespace-collapsed, with emphasis/code/comment markers (*, `, #) removed, so a
    canonical phrase survives bold/`code` spans, line wraps, and bash-comment (`#`) prefixes."""
    return " ".join(s.lower().replace("*", "").replace("`", "").replace("#", " ").split())


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "a gate must require (not negate) the binding echo"


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text()
    assert text.startswith("---")
    assert re.search(r"^name:\s*crucible\s*$", text, re.MULTILINE)
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)


def test_skill_references_the_two_roles_and_gates():
    text = SKILL.read_text().lower()
    assert "builder" in text and "critic" in text
    assert "plan gate" in text or "plan stage" in text
    assert "dependency tree" in text


def test_skill_requires_resolved_run_config():
    text = SKILL.read_text()
    assert "RUN/config.json" in text or '"$RUN"/config.json' in text
    assert "authoritative for this run" in text


def test_skill_invokes_superpowers_subskills():
    text = SKILL.read_text()
    assert "writing-plans" in text
    assert "subagent-driven-development" in text


def test_skill_uses_cli_for_decisions():
    text = SKILL.read_text()
    for cmd in ["init-run", "load-dag", "next", "verdict", "set-status", "report",
                "bindings", "approve-plan"]:
        assert cmd in text, f"SKILL.md should reference `crucible {cmd}`"


def test_skill_binds_every_gate_to_reviewed_artifact():
    # Schema-2 binding handshake, asserted PER GATE (section-scoped, not whole-document substrings):
    # each gate must log the Builder artifact, ask `crucible bindings --gate <that gate>`, seed the
    # Critic with the JSON as TRUSTED CLI METADATA, and require the verdict to ECHO exactly that gate's
    # hash fields — artifact-only at REPRODUCE, artifact+DAG at PLAN/FINAL, all three at dep:<node>. A
    # gate that dropped `bindings`, echoed the wrong field set (e.g. a stray node hash at PLAN/FINAL, a
    # DAG hash at REPRODUCE), or negated the echo must FAIL here.
    text = SKILL.read_text()

    # REPRODUCE — log --file, bindings --gate reproduce, trusted metadata, echo ARTIFACT ONLY, verdict --file
    repro = _flat(_section(text, "REPRODUCE gate"))
    assert re.search(r"log --event builder_output --gate reproduce[^)]*--file", repro), \
        "REPRODUCE must log the Builder artifact with --file"
    assert 'bindings --run "$run" --gate reproduce' in repro
    assert "trusted cli metadata" in repro
    assert "verdict to echo artifact_sha256" in repro
    assert "dag_sha256" not in repro and "node_sha256" not in repro, "REPRODUCE binds the artifact only"
    assert 'verdict --run "$run" --gate reproduce --round n --file' in repro
    _no_negated_echo(repro)

    # PLAN — bindings --gate plan, trusted metadata, echo ARTIFACT + DAG (never a node hash), verdict --file
    plan = _flat(_section(text, "PLAN gate"))
    assert 'bindings --run "$run" --gate plan' in plan
    assert "trusted cli metadata" in plan
    assert re.search(r"echo (?:\w+ )?artifact_sha256 \+ dag_sha256", plan)
    assert "node_sha256" not in plan, "PLAN carries no node hash"
    assert 'verdict --run "$run" --gate plan --round n --file' in plan
    _no_negated_echo(plan)

    # dep:<node> — bindings --gate dep:$NODE, trusted metadata, echo ALL THREE hashes, verdict --file
    dep = _flat(_section(text, "IMPLEMENT gates"))
    assert 'bindings --run "$run" --gate "dep:$node"' in dep
    assert "trusted cli metadata" in dep
    assert "artifact_sha256 + dag_sha256 + node_sha256" in dep, "dep gate echoes all three hashes"
    assert 'verdict --run "$run" --gate "dep:$node" --round n --file' in dep
    _no_negated_echo(dep)

    # FINAL — bindings --gate final, trusted metadata, echo ARTIFACT + DAG (never a node hash)
    final = _flat(_section(text, "FINAL gate"))
    assert 'bindings --run "$run" --gate final' in final
    assert "trusted cli metadata" in final
    assert re.search(r"echo (?:\w+ )?artifact_sha256 \+ dag_sha256", final)
    assert "node_sha256" not in final, "FINAL carries no node hash"
    _no_negated_echo(final)


def test_skill_records_human_approval_with_approve_plan():
    # Scope to the Approval gate: `approve-plan` is recorded ONLY AFTER an explicit human OK (ordered
    # phrase, so misleading prose that records approval first cannot pass), a changed accepted plan/DAG
    # requires a fresh run (the accepted plan/DAG is immutable within a run), and with approval disabled
    # the skill must NOT call `approve-plan`.
    appr = _flat(_section(SKILL.read_text(), "Approval gate"))
    assert re.search(r"human explicitly approves.{0,90}approve-plan --run", appr), \
        "approve-plan must be recorded only AFTER the human explicitly approves"
    assert "fresh run" in appr
    assert "immutable within a run" in appr
    assert "do not call" in appr and "approve-plan" in appr, \
        "disabled approval must NOT call approve-plan"


def test_skill_does_not_hardcode_round_cap_override():
    # the cap must come from config; workflow examples should not pass --max-rounds
    text = SKILL.read_text()
    assert "--max-rounds 5" not in text


def test_skill_step6_requires_full_untruncated_plan_paste():
    # The Copilot-CLI surfacing step must require pasting the show-plan output IN FULL and forbid
    # truncating it (observed failure: piped through `tail`, pasted a partial plan).
    text = SKILL.read_text()
    assert "show-plan" in text
    # scope to the surfacing guidance: the 'in full' concept + a no-truncate/tail mechanism
    assert "in full" in text
    low = text.lower()
    assert "truncate" in low
    assert "tail" in low  # names the concrete truncation mechanism to avoid
