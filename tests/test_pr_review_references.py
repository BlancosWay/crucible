import json
import re
from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "skills" / "pr-review" / "references"


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
                 "review-thread.md", "platform-notes.md"]:
        assert (REF / name).exists(), f"missing {name}"


def test_peers_are_symmetric_equals_not_builder_critic():
    low = _norm("peer-prompt.md")
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
    # reviews the actual code, not just the patch
    assert "actual code" in low or "real code" in low


def test_peer_prompt_treats_input_as_untrusted():
    low = _read("peer-prompt.md")
    assert "data, not instructions" in low
    # a PR body that says "approve" is an injection attempt, reported as a finding
    assert "injection" in low.lower()


def test_peer_prompt_defines_attestation_schema():
    text = _read("peer-prompt.md")
    assert "peer-a.json" in text and "peer-b.json" in text
    attest = next((b for b in _json_blocks(text)
                   if isinstance(b, dict) and b.get("peer") in ("A", "B")), None)
    assert attest is not None, "peer-prompt must show a peer attestation JSON example"
    for key in ("peer", "gate", "round", "verdict", "objections", "artifact_sha256"):
        assert key in attest, f"attestation schema missing {key!r}"


def test_peer_prompt_candidate_finding_carries_source_gate():
    low = _norm("peer-prompt.md")
    assert "source_gate" in low
    assert "objection" in low
    assert "candidate" in low


def test_peer_prompt_carries_the_review_lenses():
    # The distinctive review dimensions harvested from the pr-review-toolkit + crucible's critic prompt
    # must all be present, so no lens silently drops.
    low = _norm("peer-prompt.md")
    for lens in ["correctness", "silent failures", "test coverage", "type design", "comment",
                 "compliance", "load-bearing", "pr-intent match", "reuse", "simplification"]:
        assert lens in low, f"peer-prompt is missing the '{lens}' review lens"
    # the test-claim rule: a named-but-absent test is a blocker, and a pass is never fabricated
    assert "named-but-absent test" in low
    assert "blocker" in low
    assert "never fabricate a pass" in low


def test_peer_prompt_requires_trusted_local_execution_consent():
    low = _norm("peer-prompt.md")
    assert "local_execution_approved" in low
    assert "trusted local checkout" in low
    assert "when a runnable environment exists, run the focused tests" not in low


def test_peer_prompt_forbids_execution_without_exact_approval():
    low = _norm("peer-prompt.md")
    assert "local_execution_approved: yes" in low
    assert "exact approved command" in low
    assert "must not execute" in low
    for category in (
        "test runner", "build", "package manager", "target-module import",
        "repository script", "generated binary", "dependency installation",
        "interpreter over target modules", "plugin hook", "fallback", "retry",
    ):
        assert category in low


def test_review_thread_separates_static_evidence_from_execution_candidates():
    low = _norm("review-thread.md")
    assert "static evidence" in low
    assert "execution candidates" in low
    assert "consent required" in low
    assert "new command" in low and "fresh consent" in low


def test_consensus_rubric_is_dual_approve_and_grounded():
    low = _read("consensus-rubric.md").lower()
    assert "both peers" in low
    assert "symmetric-verdict" in low
    assert "evidence" in low or "citation" in low
    assert "not a vote" in low or "never a vote" in low
    assert "not an average" in low or "not by averaging" in low


def test_consensus_rubric_both_peers_attest_every_round():
    low = _norm("consensus-rubric.md")
    assert "both peers independently attest" in low
    assert "peer-a.json" in low and "peer-b.json" in low
    assert "iff neither" in low


def test_consensus_rubric_decides_from_objections_not_accepted_severity():
    low = _norm("consensus-rubric.md")
    assert "objection" in low
    assert re.search(r"objection[^.]{0,180}(consensus|gate progress|decided|never from)", low), \
        "consensus-rubric must state gate progress is decided from peer objections"


def test_consensus_rubric_bans_wontfix_for_peer_disputes():
    norm = _norm("consensus-rubric.md")
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,60}(--resolutions|wontfix)", norm), \
        "consensus-rubric must state a blocking peer objection is NEVER CLEARED via --resolutions/wontfix"
    assert "wontfix" in norm and "--resolutions" in norm
    for line in _read("consensus-rubric.md").splitlines():
        assert "-m crucible verdict " not in line, \
            f"pr-review must settle with symmetric-verdict, not the build-only verdict: {line!r}"


def test_consensus_rubric_cap_disagreement_is_flagged_not_forced():
    low = _read("consensus-rubric.md").lower()
    assert "max_rounds" in low
    assert "halt" in low and "proceed_with_flags" in low
    assert "flag" in low
    assert "both" in low


def test_consensus_rubric_recommendation_is_derived_not_voted():
    # The overall Approve/Comment/Request-changes recommendation is a deterministic projection of the
    # accepted finding set (via `review-result`), NOT a separate vote — preserving "consensus is not a
    # vote".
    low = _norm("consensus-rubric.md")
    assert "derived" in low
    assert "review-result" in low
    assert "approve" in low and "comment" in low and "request" in low
    assert "not a separate vote" in low or "not voted" in low or "never separately ballot" in low


def test_review_thread_reuses_dag_schema():
    low = _read("review-thread.md").lower()
    for key in ["nodes", "edges", "depends_on", "topological"]:
        assert key in low
    # test_plan reframed as the re-runnable evidence/verification plan
    assert "test_plan" in low
    assert "evidence" in low or "verif" in low
    # adaptive decomposition (single node vs thread-per-concern)
    assert "single node" in low
    assert "per concern" in low or "one thread per concern" in low


def test_platform_notes_dispatch_two_peers_from_run_config():
    low = _read("platform-notes.md").lower()
    assert "config.json" in low
    assert "general-purpose" in low
    assert "peer" in low


def test_platform_notes_requires_separate_attestations_and_symmetric_verdict():
    low = _norm("platform-notes.md")
    assert "both peers independently attest" in low
    assert "peer-a.json" in low and "peer-b.json" in low
    assert "never record only one peer" in low
    assert 'symmetric-verdict --run "$run"' in low
    assert "--peer-a" in low and "--peer-b" in low


def test_platform_notes_states_slot_proof_not_process_identity():
    low = _norm("platform-notes.md")
    assert "two configured slots" in low
    assert "cryptograph" in low
    assert "not a cryptographic proof" in low or "does not cryptographically prove" in low
    assert "process" in low


def test_peer_prompt_echoes_cli_bindings_in_attestation():
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
    bh = _flat(_section(_read("platform-notes.md"), "Binding handshake"))
    assert "crucible bindings" in bh
    assert "trusted cli metadata" in bh
    assert "not content copied from the reviewed" in bh
    assert "each peer attestation echoes it" in bh
    assert "rejects a missing or mismatched value" in bh
    _no_negated_echo(bh)


def test_consensus_rubric_records_binding_handshake():
    db = _flat(_section(_read("consensus-rubric.md"), "decision is bound"))
    assert "crucible bindings" in db
    assert "artifact_sha256" in db
    assert "echo" in db
    assert "rejected before any outcome" in db
    assert "fresh run" in db
    _no_negated_echo(db)


def test_platform_notes_normalizes_gh_or_local_diff_input():
    # Input is resolved to an immutable target via the CLI target pipeline (normalize -> load ->
    # materialize), not a bare diff/triple. GitHub requests immutable OIDs + fork identity; local uses
    # a single merge-base `--range`, never a raw two-dot `git diff <range>`.
    low = _norm("platform-notes.md")
    assert "normalize-target" in low
    assert "load-target" in low
    assert "materialize-target" in low
    assert "gh pr diff" in low                # GitHub PR path still snapshots the patch via gh pr diff
    for field in ("baserefoid", "headrefoid", "headrepository",
                  "headrepositoryowner", "iscrossrepository"):
        assert field in low, f"platform-notes must request the {field} field"
    assert "--range" in low
    assert "git diff <range>" not in low


def test_platform_notes_execution_verifies_repository_identity_and_head():
    # Trusted-local execution proves the checkout is the recorded head revision before consent:
    # repository identity + clean tree + exact head sha; a mismatch is static-only or a detached
    # worktree-at-SHA requiring fresh consent.
    low = _flat(_section(_read("platform-notes.md"), "Execution Safety Gate"))
    assert "repository-identity" in low
    assert "rev-parse head" in low
    assert "status --porcelain" in low
    assert "recorded" in low and "head" in low
    assert "worktree" in low
    assert "fresh consent" in low


def test_peer_prompt_attestation_binds_target_sha256():
    attest = next((b for b in _json_blocks(_read("peer-prompt.md"))
                   if isinstance(b, dict) and b.get("peer") in ("A", "B")), None)
    assert attest is not None, "peer-prompt must show a peer attestation JSON example"
    assert "target_sha256" in attest, "pr-review peer attestation must echo target_sha256"


def test_peer_prompt_reads_pinned_source_not_ambient():
    low = _norm("peer-prompt.md")
    assert "run/source" in low
    assert "target.diff" in low
    assert "never ambient" in low or "not ambient" in low or "never the ambient" in low


def test_review_thread_gathers_evidence_from_pinned_source():
    low = _norm("review-thread.md")
    assert "run/source" in low
    assert "target.diff" in low or "patch" in low


def test_platform_notes_posting_is_readonly_by_default_and_consented():
    low = _read("platform-notes.md").lower()
    assert "read-only" in low
    assert "consent" in low                  # only with the human's consent
    assert "gh pr review" in low             # posting mechanism
    assert "review-result" in low            # posts the deterministic recommendation/findings
    # never automatic, never before consensus
    assert "never automatic" in low


def test_platform_notes_requires_trusted_local_exact_command_consent():
    low = _norm("platform-notes.md")
    assert "trusted local checkout" in low
    assert "exact commands" in low
    assert "local_execution_approved" in low
    assert "github pr" in low and "diff file" in low
    assert "never execute locally" in low
