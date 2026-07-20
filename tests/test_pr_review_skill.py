import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "pr-review" / "SKILL.md"
CMD = ROOT / "commands" / "pr-review.md"


def _norm(path: Path) -> str:
    """Lowercased, whitespace-collapsed, markdown emphasis/code markers (*, `) removed — so a
    canonical phrase assertion isn't defeated by bold/code spans or line wraps."""
    return " ".join(path.read_text().lower().replace("*", "").replace("`", "").split())


def _section(text: str, heading_substr: str) -> str:
    """Body of the markdown section whose heading contains `heading_substr`, from that heading to the
    next heading of the same-or-higher level (headings inside ``` fences ignored) — so a per-gate guard
    is scoped to exactly that gate."""
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


def _para(text: str, anchor: str) -> str:
    """The blank-line-delimited paragraph containing `anchor`."""
    for block in re.split(r"\n\s*\n", text):
        if anchor in block:
            return block
    raise AssertionError(f"paragraph with {anchor!r} not found")


def _flat(s: str) -> str:
    """Lowercased, whitespace-collapsed, emphasis/code/comment markers (*, `, #) removed."""
    return " ".join(s.lower().replace("*", "").replace("`", "").replace("#", " ").split())


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "the union verdict must be required to echo the bindings, not negated"


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text()
    assert text.startswith("---")
    assert re.search(r"^name:\s*pr-review\s*$", text, re.MULTILINE)
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)


def test_skill_is_symmetric_two_peer_not_builder_critic():
    low = _norm(SKILL)
    assert "peer" in low
    assert "symmetric" in low or "equal" in low
    assert "two equal peers" in low
    assert "alternates each round" in low
    # canonical: the builder/critic config names are slot labels only — no asymmetry (F1 guard)
    assert "slot labels only, no asymmetry" in low or ("slot labels" in low and "no asymmetry" in low)
    assert "no builder and no critic" in low


def test_skill_requires_resolved_run_config():
    text = SKILL.read_text()
    assert "RUN/config.json" in text or '"$RUN"/config.json' in text
    assert "authoritative for this run" in text


def test_skill_reuses_crucible_cli_for_decisions():
    text = SKILL.read_text()
    for cmd in ["init-run", "load-dag", "next", "verdict", "set-status", "report", "bindings"]:
        assert cmd in text, f"SKILL.md should reference `crucible {cmd}`"


def test_skill_binds_every_gate_to_merged_artifact():
    # Schema-2 binding handshake (symmetric), asserted PER GATE (section-scoped, not whole-document
    # substrings): every gate logs the merged artifact, asks `crucible bindings --gate <that gate>`,
    # seeds Peer B with the JSON as TRUSTED CLI METADATA, and the single serialized UNION verdict must
    # ECHO exactly that gate's hash fields — artifact+DAG at PLAN/FINAL, all three at dep:<thread>. A
    # gate that dropped `bindings`, echoed the wrong field set (a stray node hash at PLAN/FINAL), lost
    # the union framing, or negated the echo must FAIL here. (pr-review has no REPRODUCE gate.)
    text = SKILL.read_text()

    # PLAN — bindings --gate plan, trusted metadata, union verdict echoes ARTIFACT + DAG (no node), verdict --file
    plan = _flat(_section(text, "PLAN gate"))
    assert 'bindings --run "$run" --gate plan' in plan
    assert "trusted cli metadata" in plan
    assert "union" in plan
    assert re.search(r"echo\w*\s+artifact_sha256 \+ dag_sha256", plan)
    assert "node_sha256" not in plan, "PLAN carries no node hash"
    assert 'verdict --run "$run" --gate plan --round n --file' in plan
    _no_negated_echo(plan)

    # THREAD (dep:<thread>) — bindings dep, trusted metadata, union verdict echoes ALL THREE, verdict --file
    thread = _flat(_section(text, "THREAD gates"))
    assert 'bindings --run "$run" --gate "dep:$node"' in thread
    assert "trusted cli metadata" in thread
    assert "union" in thread
    assert "artifact_sha256 + dag_sha256 + node_sha256" in thread
    assert 'verdict --run "$run" --gate "dep:$node" --round n --file' in thread
    _no_negated_echo(thread)

    # FINAL — bindings --gate final, trusted metadata, union verdict echoes ARTIFACT + DAG (no node)
    final = _flat(_section(text, "FINAL gate"))
    assert 'bindings --run "$run" --gate final' in final
    assert "trusted cli metadata" in final
    assert "union" in final
    assert re.search(r"echo\w*\s+artifact_sha256 \+ dag_sha256", final)
    assert "node_sha256" not in final, "FINAL carries no node hash"
    _no_negated_echo(final)


def test_skill_records_human_approval_with_approve_plan():
    # Scope to the Optional-human-approval paragraph: `approve-plan` is recorded ONLY AFTER an explicit
    # human OK (ordered phrase), a changed accepted plan/DAG requires a fresh run, and with approval
    # disabled the skill skips it (approve-plan rejects) rather than record meaningless provenance.
    appr = _flat(_para(SKILL.read_text(), "Optional human approval"))
    assert re.search(r"human explicitly approves.{0,90}approve-plan --run", appr), \
        "approve-plan must be recorded only AFTER the human explicitly approves"
    assert "fresh run" in appr
    assert "approve-plan rejects" in appr, "disabled approval must skip approve-plan, not record it"


def test_skill_does_not_hardcode_round_cap_override():
    assert "--max-rounds 5" not in SKILL.read_text()


def test_skill_commands_are_pythonpath_prefixed():
    for line in SKILL.read_text().splitlines():
        if "python3 -m crucible" in line:
            assert "PYTHONPATH=scripts python3 -m crucible" in line


def test_skill_grounds_consensus_in_evidence_not_votes():
    low = SKILL.read_text().lower()
    assert "evidence" in low or "citation" in low
    assert "code" in low and "data" in low
    assert "not a vote" in low or "never a vote" in low
    assert "not an average" in low or "not by averaging" in low


def test_skill_bans_wontfix_for_peer_disputes():
    norm = _norm(SKILL)
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,40}(--resolutions|wontfix)", norm), \
        "SKILL must state a blocking peer dispute is NEVER CLEARED via --resolutions/wontfix"
    for line in SKILL.read_text().splitlines():
        if "crucible verdict" in line and "--resolutions" in line:
            raise AssertionError(f"pr-review SKILL must not invoke --resolutions in a verdict example: {line!r}")


def test_skill_advances_thread_on_proceed_with_flags():
    # On a thread gate, PROCEED_WITH_FLAGS must set the node done + continue (not leave it in_progress),
    # else `crucible next`/`status` treats the run as stuck / in-flight forever.
    low = _norm(SKILL)
    assert "proceed_with_flags" in low
    assert re.search(r"proceed_with_flags[^.]{0,200}set-status[^.]{0,60}done", low), \
        "SKILL must set a PROCEED_WITH_FLAGS thread node to done and continue"


def test_skill_both_peers_review_every_round():
    low = _norm(SKILL)
    assert "both peers review the merged set every round" in low
    assert "union of both peers" in low or "deduped union" in low


def test_skill_surfaces_findings_on_copilot():
    low = SKILL.read_text().lower()
    assert "copilot" in low
    assert "report" in low or "findings" in low
    assert "in full" in low
    assert "truncate" in low and "tail" in low


def test_skill_does_not_modify_crucible_skill_paths():
    # the pr-review skill must reference its OWN references, never crucible's
    assert "skills/crucible/references" not in SKILL.read_text()


def test_skill_normalizes_gh_or_local_diff_input():
    low = _norm(SKILL)
    assert "normaliz" in low                       # input-normalization step
    assert "gh pr diff" in low or "gh pr view" in low
    assert "git diff" in low
    assert "diff, changed-files, intent" in low or "changed-files" in low


def test_skill_derives_recommendation_and_gates_posting_on_consent():
    low = SKILL.read_text().lower()
    # derived Approve/Comment/Request-changes recommendation (not a separate vote)
    assert "recommendation" in low
    assert "request-changes" in low or "request changes" in low
    # optional posting is read-only by default + consented + only after consensus
    assert "read-only" in low
    assert "consent" in low
    assert "gh pr review" in low
    # specific guardrails, not just generic words (F2): only after consensus, only for the GitHub-PR
    # input, and never automatically / before consensus / for a local diff.
    norm = _norm(SKILL)
    assert "only after consensus" in norm
    assert "only for the github-pr input" in norm or "only for the github pr input" in norm
    assert "never post automatically" in norm


def test_skill_has_a_distinct_execution_safety_gate():
    low = _norm(SKILL)
    assert "execution safety gate" in low
    assert "after plan consensus" in low
    assert "exact commands" in low
    assert "arbitrary code" in low
    assert "fresh consent" in low


def test_skill_remote_and_diff_inputs_never_execute_locally():
    low = _norm(SKILL)
    assert "github pr" in low and "never execute locally" in low
    assert "diff file" in low and "never execute locally" in low
    assert "existing ci" in low


def test_skill_declined_execution_continues_static_only():
    low = _norm(SKILL)
    assert "continue without execution" in low
    assert "static" in low and "unverified" in low
    assert "posting consent" in low


def test_command_file_exists_with_frontmatter_and_no_dangling_ref_tokens():
    text = CMD.read_text()
    assert text.startswith("---")
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)
    assert "pr-review" in text.lower()
    # must not embed a `references/<x>.md` token (validate_structure resolves README/command tokens
    # against skills/crucible/references and would break); point at the SKILL instead.
    assert not re.search(r"references/[a-z0-9-]+\.md", text)
    # command doc must reference the authoritative shipped defaults (test_docs owner guard, node docs)
    assert "config.defaults.json" in text


# The docs-integration guards below are owned by the `docs` node (they assert the pr-review live docs
# are wired into the test_docs.py owner lists, the CHANGELOG, and the README/AGENTS/CLAUDE).


def test_pr_review_docs_are_covered_by_the_model_id_owner():
    # the established owner (tests/test_docs.py) must list the pr-review live docs + references in its
    # guards. Import the owner module and assert on normalized Path values (its lists are built with
    # ROOT / "skills" / … joins, so a slash-substring check would be brittle).
    import importlib.util
    spec = importlib.util.spec_from_file_location("owner_docs", ROOT / "tests" / "test_docs.py")
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    pr = ROOT / "skills" / "pr-review"
    assert pr / "SKILL.md" in td.NO_MODEL_LITERAL_FILES
    for ref in ("peer-prompt.md", "consensus-rubric.md", "review-thread.md", "platform-notes.md"):
        assert pr / "references" / ref in td.NO_MODEL_LITERAL_FILES, f"{ref} not guarded by test_docs"
    assert ROOT / "commands" / "pr-review.md" in td.SOURCE_REFERENCE_DOCS
    assert pr / "SKILL.md" in td.RUN_CONFIG_DOCS
    assert pr / "references" / "platform-notes.md" in td.RUN_CONFIG_DOCS
    # SKILL must also stay in WORKFLOW_DOCS (F1) — that list drives the no-hardcoded-round-cap guard.
    assert pr / "SKILL.md" in td.WORKFLOW_DOCS


def test_changelog_records_pr_review():
    # Recorded under [Unreleased] before release, or in its dated section after — assert it appears
    # anywhere in the CHANGELOG (a permanent historical record).
    text = (ROOT / "CHANGELOG.md").read_text().lower()
    assert "pr-review" in text or "pr review" in text


def test_all_docs_mention_the_third_skill():
    # Every doc that wires deep-dive must also wire pr-review (F2) — reverting any one of them fails here.
    for rel in ("README.md", "AGENTS.md", "CLAUDE.md", ".codex/INSTALL.md",
                "docs/install/copilot-cli.md", "docs/install/claude-code.md",
                "docs/install/codex.md", "docs/cli.md", "CHANGELOG.md"):
        assert "pr-review" in (ROOT / rel).read_text().lower(), f"{rel} omits pr-review"
    # README Layout must list both the skill dir and the command entry point.
    readme = (ROOT / "README.md").read_text()
    assert "skills/pr-review/" in readme
    assert "commands/pr-review.md" in readme
