import json
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


def _json_blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for body in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        out.append(json.loads(body))
    return out


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "each peer attestation must be required to echo the bindings, not negated"


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


def test_peer_prompt_defines_attestation_schema():
    # Each peer emits its OWN attestation file (peer-a.json / peer-b.json) with a schema (slot,
    # gate/round, APPROVE/REQUEST_CHANGES verdict, objections, echoed bindings) — not one union
    # verdict. Assert both filenames AND the JSON schema.
    text = _read("peer-prompt.md")
    assert "peer-a.json" in text and "peer-b.json" in text
    attest = next((b for b in _json_blocks(text)
                   if isinstance(b, dict) and b.get("peer") in ("A", "B")), None)
    assert attest is not None, "peer-prompt must show a peer attestation JSON example"
    for key in ("peer", "gate", "round", "verdict", "objections", "artifact_sha256"):
        assert key in attest, f"attestation schema missing {key!r}"


def test_peer_prompt_candidate_finding_carries_source_gate():
    # A candidate finding (the accepted result) is keyed by (source_gate, id) and is DISTINCT from a
    # peer objection (a defect in the candidate). Assert the finding schema names source_gate.
    low = _norm("peer-prompt.md")
    assert "source_gate" in low
    assert "objection" in low
    assert "candidate" in low


def test_consensus_rubric_is_dual_approve_and_grounded():
    low = _read("consensus-rubric.md").lower()
    assert "both peers" in low or "dual" in low          # both must approve
    assert "symmetric-verdict" in low                     # decided by `crucible symmetric-verdict`
    assert "evidence" in low or "citation" in low
    # F5: consensus is explicitly NOT a vote/average — assert the negating phrase, not just the word
    assert "not a vote" in low or "never a vote" in low
    assert "not an average" in low or "not by averaging" in low


def test_consensus_rubric_both_peers_attest_every_round():
    # Symmetry within every round — assert the canonical positive phrasing (a doc saying only one peer
    # reviews, or omitting the separate-attestation / APPROVE-iff-neither rule, must FAIL),
    # whitespace-normalized so a line wrap can't hide it.
    low = _norm("consensus-rubric.md")
    assert "both peers independently attest" in low
    assert "peer-a.json" in low and "peer-b.json" in low   # separate per-peer files, no union verdict
    assert "iff neither" in low                            # CONSENSUS iff neither peer has a blocker


def test_consensus_rubric_decides_from_objections_not_accepted_severity():
    # Gate progress is decided from peer OBJECTIONS (defects in the candidate), never from the
    # severity of an accepted candidate finding — so a candidate that accepts a blocker can still
    # reach consensus when both peers attest the set is accurate and complete.
    low = _norm("consensus-rubric.md")
    assert "objection" in low
    assert re.search(r"objection[^.]{0,180}(consensus|gate progress|decided|never from)", low), \
        "consensus-rubric must state gate progress is decided from peer objections"
    assert "accepts a blocker" in low or "accept a blocker" in low or "accepted blocker" in low


def test_consensus_rubric_bans_wontfix_for_peer_disputes():
    # A blocking peer objection is NEVER cleared by `--resolutions`/`wontfix` — symmetric-verdict has
    # no such flag. Assert the canonical ban phrasing on de-emphasized text; and that no symmetric
    # decision example passes `--resolutions`.
    norm = _norm("consensus-rubric.md")
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,60}(--resolutions|wontfix)", norm), \
        "consensus-rubric must state a blocking peer objection is NEVER CLEARED via --resolutions/wontfix"
    assert "wontfix" in norm and "--resolutions" in norm
    for line in _read("consensus-rubric.md").splitlines():
        assert "-m crucible verdict " not in line, \
            f"deep-dive must settle with symmetric-verdict, not the build-only verdict: {line!r}"
        if "python3 -m crucible" in line:
            assert "--resolutions" not in line, \
                f"deep-dive must not invoke --resolutions in a decision example: {line!r}"


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


def test_platform_notes_requires_separate_attestations_and_symmetric_verdict():
    # The per-platform realization must specify TWO independent attestation files (peer-a.json /
    # peer-b.json) settled by `symmetric-verdict --peer-a --peer-b` — not one serialized union.
    low = _norm("platform-notes.md")
    assert "both peers independently attest" in low
    assert "peer-a.json" in low and "peer-b.json" in low
    assert "never record only one peer" in low
    assert 'symmetric-verdict --run "$run"' in low
    assert "--peer-a" in low and "--peer-b" in low


def test_platform_notes_states_slot_proof_not_process_identity():
    # The CLI proves the two configured SLOTS attested to the same bound candidate; it does not
    # cryptographically prove two distinct model processes ran (runtime independence is a platform
    # property). This honest scoping must be stated where peers are realized.
    low = _norm("platform-notes.md")
    assert "two configured slots" in low
    assert "cryptograph" in low
    assert "not a cryptographic proof" in low or "does not cryptographically prove" in low
    assert "process" in low


def test_peer_prompt_echoes_cli_bindings_in_attestation():
    # Schema-2 (symmetric): scope to the peer prompt's "Binding echo" section. EACH peer attestation
    # the CLI consumes must ECHO the deterministic `crucible bindings` fields as trusted CLI metadata,
    # verbatim; a missing or mismatched value is rejected before any decision.
    be = _flat(_section(_read("peer-prompt.md"), "Binding echo"))
    assert "each peer attestation" in be
    assert "crucible bindings" in be
    assert "trusted cli metadata" in be
    assert "artifact_sha256" in be
    assert "echo" in be and "verbatim" in be
    assert "symmetric-verdict" in be
    assert "rejects a missing or mismatched value" in be
    _no_negated_echo(be)


def test_platform_notes_report_labels_are_symmetric_peer_headers():
    # The run report renders `Peer A` / `Peer B` HEADERS for the symmetric workflow (not Builder/Critic
    # labels), sourced from the builder/critic config slots for model/effort provenance. Running the
    # symmetric flow requires the `--workflow` run metadata + the symmetric commands (a CLI change),
    # even though the config SCHEMA is unchanged. Scope to the "Report labels" section so this fails on
    # the stale "Builder/Critic labels ... no CLI or config change is needed" wording.
    rl = _flat(_section(_read("platform-notes.md"), "Report labels"))
    assert "peer a" in rl and "peer b" in rl
    assert "header" in rl                       # renders Peer A/Peer B headers, not Builder/Critic labels
    assert "builder" in rl and "critic" in rl and "slot" in rl   # sourced from the builder/critic slots
    assert "model" in rl and "effort" in rl     # model/effort provenance
    assert "no config-schema change" in rl
    assert "--workflow" in rl
    assert "symmetric-verdict" in rl or "symmetric commands" in rl
    # the false "no CLI or config change is needed" claim must be gone
    assert "no cli or config change" not in rl


def test_platform_notes_bindings_are_trusted_cli_metadata():
    # Scope to platform-notes' "Binding handshake" section: the seed bindings are the exact `crucible
    # bindings` JSON as TRUSTED CLI METADATA — NOT content copied from the reviewed (untrusted)
    # artifact — each peer attestation echoes them, and a mismatch is rejected before any decision.
    bh = _flat(_section(_read("platform-notes.md"), "Binding handshake"))
    assert "crucible bindings" in bh
    assert "trusted cli metadata" in bh
    assert "not content copied from the reviewed" in bh
    assert "each peer attestation echoes it" in bh
    assert "rejects a missing or mismatched value" in bh
    _no_negated_echo(bh)


def test_consensus_rubric_records_binding_handshake():
    # Scope to the stop-criteria doc's "decision is bound" section: each peer attestation echoes the
    # gate's CLI bindings, a missing/mismatched binding is rejected before any outcome, and the
    # accepted artifact is immutable (a change requires a fresh run).
    db = _flat(_section(_read("consensus-rubric.md"), "decision is bound"))
    assert "crucible bindings" in db
    assert "artifact_sha256" in db
    assert "echo" in db
    assert "rejected before any outcome" in db
    assert "fresh run" in db
    _no_negated_echo(db)
