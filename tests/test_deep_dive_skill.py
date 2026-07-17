import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "deep-dive" / "SKILL.md"
CMD = ROOT / "commands" / "deep-dive.md"


def _norm(path: Path) -> str:
    """Lowercased, whitespace-collapsed, markdown emphasis/code markers (*, `) removed — so a
    canonical phrase assertion isn't defeated by bold/code spans or line wraps."""
    return " ".join(path.read_text().lower().replace("*", "").replace("`", "").split())


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text()
    assert text.startswith("---")
    assert re.search(r"^name:\s*deep-dive\s*$", text, re.MULTILINE)
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)


def test_skill_is_symmetric_two_peer_not_builder_critic():
    # canonical positive phrases a negated/wrong skill could NOT contain (de-emphasized)
    low = _norm(SKILL)
    assert "peer" in low
    assert "symmetric" in low or "equal" in low
    assert "two equal peers" in low
    assert "alternates each round" in low


def test_skill_requires_resolved_run_config():
    text = SKILL.read_text()
    assert "RUN/config.json" in text or '"$RUN"/config.json' in text
    assert "authoritative for this run" in text


def test_skill_reuses_crucible_cli_for_decisions():
    text = SKILL.read_text()
    for cmd in ["init-run", "load-dag", "next", "verdict", "set-status", "report"]:
        assert cmd in text, f"SKILL.md should reference `crucible {cmd}`"


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
    # explicit negation, not just the word "vote"
    assert "not a vote" in low or "never a vote" in low
    assert "not an average" in low or "not by averaging" in low


def test_skill_bans_wontfix_for_peer_disputes():
    # the deep-dive skill must never instruct clearing a blocking peer dispute via
    # `--resolutions`/`wontfix`. Canonical ban phrasing (never CLEARED with the mechanism) so wrong
    # text like "never forget to pass --resolutions" fails; plus no `--resolutions` in a verdict example.
    norm = _norm(SKILL)   # de-emphasized so markdown `**never**` doesn't break the phrase
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,40}(--resolutions|wontfix)", norm), \
        "SKILL must state a blocking peer dispute is NEVER CLEARED via --resolutions/wontfix"
    for line in SKILL.read_text().splitlines():
        if "crucible verdict" in line and "--resolutions" in line:
            raise AssertionError(f"deep-dive SKILL must not invoke --resolutions in a verdict example: {line!r}")


def test_skill_advances_thread_on_proceed_with_flags():
    # F1: on a thread gate, PROCEED_WITH_FLAGS must set the node done + continue (not leave it
    # in_progress), else `crucible next`/`status` treats the run as stuck / in-flight forever.
    low = _norm(SKILL)
    assert "proceed_with_flags" in low
    assert re.search(r"proceed_with_flags[^.]{0,200}set-status[^.]{0,60}done", low), \
        "SKILL must set a PROCEED_WITH_FLAGS thread node to done and continue"


def test_skill_both_peers_review_every_round():
    # both peers review the merged set each round; consensus needs both to sign off (canonical)
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
    # the deep-dive skill must reference its OWN references, never crucible's
    assert "skills/crucible/references" not in SKILL.read_text()


def test_deep_dive_docs_are_covered_by_the_model_id_owner():
    # the established owner (tests/test_docs.py) must list the deep-dive live docs + references in its
    # guards. Import the owner module and assert on normalized Path values (its lists are built with
    # ROOT / "skills" / … joins, so a slash-substring check would be brittle).
    import importlib.util
    spec = importlib.util.spec_from_file_location("owner_docs", ROOT / "tests" / "test_docs.py")
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    dd = ROOT / "skills" / "deep-dive"
    assert dd / "SKILL.md" in td.NO_MODEL_LITERAL_FILES
    for ref in ("peer-prompt.md", "consensus-rubric.md",
                "investigation-thread.md", "platform-notes.md"):
        assert dd / "references" / ref in td.NO_MODEL_LITERAL_FILES, f"{ref} not guarded by test_docs"
    assert ROOT / "commands" / "deep-dive.md" in td.SOURCE_REFERENCE_DOCS
    assert dd / "SKILL.md" in td.RUN_CONFIG_DOCS
    assert dd / "references" / "platform-notes.md" in td.RUN_CONFIG_DOCS


def test_command_file_exists_with_frontmatter_and_no_dangling_ref_tokens():
    text = CMD.read_text()
    assert text.startswith("---")
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)
    assert "deep-dive" in text.lower()
    # must not embed a `references/<x>.md` token (validate_structure resolves those and would
    # break); point at the SKILL instead.
    assert not re.search(r"references/[a-z0-9-]+\.md", text)


def test_changelog_records_deep_dive():
    # The deep-dive feature must be recorded in the CHANGELOG — under [Unreleased] before its
    # release, or in its dated release section after. It is a permanent historical record, so a
    # later release that does not touch deep-dive must not make this fail: assert it appears
    # anywhere in the CHANGELOG.
    text = (ROOT / "CHANGELOG.md").read_text().lower()
    assert "deep-dive" in text or "deep dive" in text


def test_readme_and_agents_mention_the_second_skill():
    for rel in ("README.md", "AGENTS.md", "CLAUDE.md"):
        assert "deep-dive" in (ROOT / rel).read_text().lower(), f"{rel} omits deep-dive"
