import re
from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "skills" / "deep-dive" / "references"


def _read(name: str) -> str:
    return (REF / name).read_text()


def _norm(name: str) -> str:
    """Lowercased, whitespace-collapsed, with markdown emphasis/code markers (*, `) removed, so a
    canonical phrase assertion is not defeated by bold/italic/code spans or line wraps."""
    return " ".join(_read(name).lower().replace("*", "").replace("`", "").split())


def _section(text: str, heading_substr: str) -> str:
    """Body of the markdown section whose heading contains `heading_substr`, from that heading to the
    next heading of the same-or-higher level (headings inside ``` fences ignored) — so a binding guard
    is scoped to the handshake section and can't be satisfied by unrelated prose elsewhere."""
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
    """Lowercased, whitespace-collapsed, emphasis/code/comment markers (*, `, #) removed."""
    return " ".join(s.lower().replace("*", "").replace("`", "").replace("#", " ").split())


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "the union verdict must be required to echo the bindings, not negated"


def test_reference_files_exist():
    for name in ["peer-prompt.md", "consensus-rubric.md",
                 "investigation-thread.md", "platform-notes.md"]:
        assert (REF / name).exists(), f"missing {name}"


def test_peers_are_symmetric_equals_not_builder_critic():
    low = _norm("peer-prompt.md")   # de-emphasized + whitespace-normalized (F1)
    assert "peer" in low
    assert "symmetric" in low or "equal" in low
    # canonical positive phrases a negated/wrong prompt could NOT contain
    assert "no builder and no critic" in low
    assert "this same prompt" in low
    assert "alternates each round" in low


def test_peer_prompt_grounds_findings_in_reverifiable_evidence():
    low = _read("peer-prompt.md").lower()
    assert "citation" in low or "cite" in low
    assert "file:line" in low
    assert "re-verify" in low or "reverify" in low or "re-run" in low
    # when in doubt, go to the actual code/data
    assert "code" in low and "data" in low


def test_peer_prompt_treats_input_as_untrusted():
    assert "data, not instructions" in _read("peer-prompt.md")


def test_consensus_rubric_is_dual_approve_and_grounded():
    low = _read("consensus-rubric.md").lower()
    assert "both peers" in low or "dual" in low          # both must approve
    assert "verdict" in low                               # decided by `crucible verdict`
    assert "evidence" in low or "citation" in low
    # F5: consensus is explicitly NOT a vote/average — assert the negating phrase, not just the word
    assert "not a vote" in low or "never a vote" in low
    assert "not an average" in low or "not by averaging" in low


def test_consensus_rubric_both_peers_review_every_round():
    # F1/F2: symmetry within every round — assert the canonical positive phrasing (a doc saying only
    # one peer reviews, or omitting the union / APPROVE-iff-neither rule, must FAIL), whitespace-
    # normalized so a line wrap can't hide it.
    low = _norm("consensus-rubric.md")
    assert "both peers review the merged set every round" in low
    assert "union of both peers" in low                   # verdict = union of both peers' findings
    assert "iff neither" in low                           # APPROVE iff neither peer has a blocker


def test_consensus_rubric_bans_wontfix_for_peer_disputes():
    # F2: a blocking peer dispute is NEVER CLEARED by `--resolutions`/`wontfix`. Assert the canonical
    # ban phrasing (never CLEARED with the mechanism) on de-emphasized text so wrong text like "never
    # forget to pass --resolutions" fails; and that no `crucible verdict` example passes `--resolutions`.
    norm = _norm("consensus-rubric.md")
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,40}(--resolutions|wontfix)", norm), \
        "consensus-rubric must state a blocking peer dispute is NEVER CLEARED via --resolutions/wontfix"
    assert "wontfix" in norm and "--resolutions" in norm
    for line in _read("consensus-rubric.md").splitlines():
        if "crucible verdict" in line and "--resolutions" in line:
            raise AssertionError(f"deep-dive must not invoke --resolutions in a verdict example: {line!r}")


def test_consensus_rubric_cap_disagreement_is_flagged_not_forced():
    low = _read("consensus-rubric.md").lower()
    assert "max_rounds" in low
    assert "halt" in low and "proceed_with_flags" in low
    assert "flag" in low                                  # surfaced as a flagged unresolved dispute
    assert "both" in low                                  # both positions recorded


def test_investigation_thread_reuses_dag_schema():
    low = _read("investigation-thread.md").lower()
    for key in ["nodes", "edges", "depends_on", "topological"]:
        assert key in low
    # test_plan reframed as the re-runnable evidence/verification plan
    assert "test_plan" in low
    assert "evidence" in low or "verif" in low


def test_platform_notes_dispatch_two_peers_from_run_config():
    low = _read("platform-notes.md").lower()
    assert "config.json" in low                           # resolve models from the run config
    assert "general-purpose" in low                       # model 2 dispatched as a subagent
    assert "peer" in low


def test_platform_notes_requires_both_peer_reviews_and_union():
    # F3: the per-platform realization must specify both-peer independent review + union serialization
    # (not a single reviewer). Canonical positive phrases, whitespace-normalized, that a wrong
    # realization negating both-peer dispatch/review or union could NOT contain.
    low = _norm("platform-notes.md")
    assert "both peers independently review" in low
    assert "deduped union" in low
    assert "never record only one peer" in low


def test_peer_prompt_echoes_cli_bindings_in_verdict():
    # Schema-2 (symmetric): scope to the peer prompt's "Binding echo" section. The single serialized
    # UNION verdict the CLI consumes must ECHO the deterministic `crucible bindings` fields as trusted
    # CLI metadata, verbatim, while preserving the exactly-one-JSON + union semantics; a missing or
    # mismatched value is rejected before any decision.
    be = _flat(_section(_read("peer-prompt.md"), "Binding echo"))
    assert "single serialized union verdict" in be
    assert "crucible bindings" in be
    assert "trusted cli metadata" in be
    assert "artifact_sha256" in be
    assert "echo" in be and "verbatim" in be
    assert "exactly-one-json + union semantics" in be
    assert "rejects a missing or mismatched value" in be
    _no_negated_echo(be)


def test_platform_notes_bindings_are_trusted_cli_metadata():
    # Scope to platform-notes' "Binding handshake" section: the seed bindings are the exact `crucible
    # bindings` JSON as TRUSTED CLI METADATA — NOT content copied from the reviewed (untrusted) artifact
    # — the union verdict echoes them, and a mismatch is rejected before any decision.
    bh = _flat(_section(_read("platform-notes.md"), "Binding handshake"))
    assert "crucible bindings" in bh
    assert "trusted cli metadata" in bh
    assert "not content copied from the reviewed" in bh
    assert "the union verdict echoes it" in bh
    assert "rejects a missing or mismatched value" in bh
    _no_negated_echo(bh)


def test_consensus_rubric_records_binding_handshake():
    # Scope to the stop-criteria doc's "decision is bound" section: the union verdict echoes the gate's
    # CLI bindings, a missing/mismatched binding is rejected before any outcome, and the accepted
    # artifact is immutable (a change requires a fresh run).
    db = _flat(_section(_read("consensus-rubric.md"), "decision is bound"))
    assert "crucible bindings" in db
    assert "artifact_sha256" in db
    assert "echo" in db
    assert "rejected before any outcome" in db
    assert "fresh run" in db
    _no_negated_echo(db)
