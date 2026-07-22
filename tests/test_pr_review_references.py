import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "skills" / "pr-review" / "references"


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


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def _basename(token: str) -> str:
    """Trailing path component of a shell token, with quotes/backticks stripped."""
    return token.split("/")[-1].strip().strip('"').strip("'").strip("`")


def _capture(pattern: str, block: str) -> str:
    m = re.search(pattern, block)
    assert m, f"expected {pattern!r} in:\n{block}"
    return _basename(m.group(1))


def _assert_source_materialization_is_executable_per_kind(section: str) -> None:
    """The pinned source snapshot is materialized on TWO separate, self-contained, executable paths —
    never one shared command that (a) claims the head repository/SHA are authoritative yet leaves the
    variables unset, (b) archives with ambient `git archive`, or (c) feeds `source.tar.gz` to
    `materialize-target` even when the local path wrote `source.tar`."""
    low = _flat(section)
    assert "show-target --run" in low and "loaded-target.json" in low, \
        "materialization must emit the authoritative loaded manifest before parsing head identity"

    blocks = _bash_blocks(section)
    gh = [b for b in blocks if "materialize-target" in b and "gh api" in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1, "exactly one executable GitHub source-materialization block is required"
    assert len(local) == 1, "exactly one executable local source-materialization block is required"
    gh, local = gh[0], local[0]

    assert '["head"]["repository"]' in gh and '["head"]["sha"]' in gh, \
        "GitHub path must parse head.repository/head.sha from the loaded manifest"
    assert "HEAD_REPOSITORY=" in gh and "HEAD_SHA=" in gh, \
        "GitHub path must assign HEAD_REPOSITORY/HEAD_SHA — never reference them unset"
    assert 'test -n "$HEAD_REPOSITORY"' in gh and 'test -n "$HEAD_SHA"' in gh, \
        "GitHub path must require non-empty head identity before fetching"
    assert 'gh api "repos/$HEAD_REPOSITORY/tarball/$HEAD_SHA"' in gh, \
        "GitHub path must fetch the tarball of the exact recorded head"
    assert _capture(r"gh api[^\n]*>\s*(\S+)", gh) == "source.tar.gz", \
        "GitHub fetch must write source.tar.gz"
    assert _capture(r"materialize-target[^\n]*--archive\s+(\S+)", gh) == "source.tar.gz", \
        "GitHub must materialize the fetched source.tar.gz (no archive-path mismatch)"
    assert not re.search(r"git\s+archive", gh), "GitHub path must not archive with git"
    assert "gh api" not in local, "local path must not fetch a GitHub tarball"

    assert '["repository"]' in local and '["head"]["sha"]' in local, \
        "local path must parse repository/head.sha from the loaded manifest"
    assert "HEAD_SHA=" in local, "local path must assign HEAD_SHA — never reference it unset"
    assert 'test -n "$HEAD_SHA"' in local, "local path must require a non-empty head SHA"
    assert 'repository-identity --repo "$LOCAL_REPO"' in local, \
        "local path must verify the caller's $LOCAL_REPO identity"
    assert local.index("repository-identity") < local.index("git -C"), \
        "the recorded-identity check must precede the archive"
    assert re.search(
        r'git -C "\$LOCAL_REPO" archive --format=tar --output \S+ "\$HEAD_SHA"', local), \
        'local path must archive the exact head via `git -C "$LOCAL_REPO" archive` (never ambient)'
    assert _capture(r"--output\s+(\S+)", local) == "source.tar", "local archive must write source.tar"
    assert _capture(r"materialize-target[^\n]*--archive\s+(\S+)", local) == "source.tar", \
        "local must materialize source.tar (never the GitHub source.tar.gz — no archive-path mismatch)"

    assert not re.search(r"git\s+archive", section), \
        "a bare `git archive` (ambient repo) is forbidden — archive only via `git -C \"$LOCAL_REPO\"`"


def _assert_block_fails_closed(block: str, fetch: str, archive: str) -> None:
    """One source-materialization block (GitHub or local) must fail CLOSED and NON-FATAL (Task 3,
    round 3): the live `gh api ... > source.tar.gz` fetch / local `git -C ... archive` were UNCHECKED, so
    a failed/truncated/stale archive relied on later or global shell behaviour instead of explicitly
    switching to source-unavailable. The block must remove any stale archive BEFORE the fetch/archive,
    default `SOURCE_AVAILABLE=no`, `if`-check the fetch/archive (never a bare command), `if`-check
    materialize-target so it is unreachable on a failed fetch/archive, set `SOURCE_AVAILABLE=yes` only
    after materialize succeeds, and `rm -f` the partial archive on every failure branch."""
    folded = re.sub(r"\\\n\s*", "", block)
    esc = re.escape(archive)
    rm = rf"rm -f [^\n]*{esc}(?![\w.])"
    materialize = r"PYTHONPATH=\S+ python3 -m crucible materialize-target"

    assert "SOURCE_AVAILABLE=no" in folded, "must default SOURCE_AVAILABLE=no (fail closed)"
    assert "SOURCE_AVAILABLE=yes" in folded, "must set SOURCE_AVAILABLE=yes only on success"

    first_rm = re.search(rm, folded)
    first_fetch = re.search(fetch, folded)
    assert first_rm and first_fetch, "a stale archive must be removed and the fetch/archive attempted"
    assert first_rm.start() < first_fetch.start(), \
        "any stale archive must be removed BEFORE the fetch/archive"

    assert re.search(rf"if\s+{fetch}", folded), \
        "the fetch/archive must be explicitly checked (`if ...`), never unchecked"
    assert not re.search(rf"(?m)^\s*{fetch}", folded), \
        "the fetch/archive must not be a bare unchecked command"

    assert re.search(rf"if\s+{materialize}", folded), \
        "materialize-target must be explicitly checked (`if ...`)"
    assert not re.search(rf"(?m)^\s*{materialize}", folded), \
        "materialize-target must not be a bare unchecked command (would run on a failed fetch)"

    assert folded.index("materialize-target") < folded.index("SOURCE_AVAILABLE=yes"), \
        "SOURCE_AVAILABLE=yes must be set only AFTER materialize succeeds"
    assert len(re.findall(rm, folded)) >= 2, \
        "the partial archive must be discarded (rm -f) on failure, not left behind"


def _source_materialization_blocks(section: str) -> tuple[str, str]:
    """(github_block, local_block) — the two executable source-materialization blocks in `section`."""
    blocks = _bash_blocks(section)
    gh = [b for b in blocks if "materialize-target" in b and "gh api" in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1 and len(local) == 1, \
        "exactly one GitHub and one local source-materialization block are required"
    return gh[0], local[0]


def _assert_source_materialization_fails_closed(section: str) -> None:
    """Both source-materialization kinds fail closed, and the section documents the NON-FATAL,
    patch-only continuation when the snapshot is unavailable (Task 3, round 3)."""
    gh, local = _source_materialization_blocks(section)
    _assert_block_fails_closed(gh, fetch=r"gh api", archive="source.tar.gz")
    _assert_block_fails_closed(local, fetch=r'git -C "\$LOCAL_REPO" archive', archive="source.tar")

    low = _flat(section)
    assert "source_available=no" in low, \
        "the section must document the fail-closed SOURCE_AVAILABLE=no status"
    assert "patch-only" in low or "patch only" in low, \
        "on a source failure the review must continue patch-only (non-fatal)"
    assert "unverified" in low, \
        "when source is unavailable, runtime-verified claims must be marked unverified"
    assert any(k in low for k in ("diff-file", "diff file", "diff mode")), \
        "the diff-file target must be covered (sets SOURCE_AVAILABLE=no, never archives)"


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "each peer attestation must be required to echo the bindings, not negated"


def _github_acquisition_block(section: str) -> str:
    """The single executable GitHub before/diff/after acquisition loop in `section`."""
    blocks = [b for b in _bash_blocks(section)
              if "normalize-target github" in b and "gh pr diff" in b]
    assert len(blocks) == 1, \
        "exactly one executable GitHub before/diff/after acquisition block is required"
    return blocks[0]


def assert_github_acquisition_fails_closed(block: str) -> None:
    """The GitHub before/diff/after acquisition must fail CLOSED without relying on a global `set -e`.

    Finding (Task 3, round 2): the examples ran `gh pr view`/`gh pr diff` unchecked, so a failed command
    left an empty/truncated artifact that stable before/after metadata still let `normalize-target`
    hash into a target. The loop must instead: bound retries to 3; explicitly error-check EACH of the
    three `gh` reads; run `normalize-target` ONLY after all three succeed; discard EVERY partial
    before/after/diff/target artifact on any failure; halt clearly once the attempts are exhausted; and
    preserve the metadata-drift retry (a non-zero `normalize-target` exit re-enters the loop)."""
    joined = re.sub(r"\\\n\s*", "", block)          # fold shell line-continuations to logical lines
    flat = " ".join(joined.split())

    assert "for ATTEMPT in 1 2 3" in flat, "acquisition must retry within a bounded 3-attempt loop"
    assert "ok=1" in flat, "each attempt must reset the success flag before the gh reads"

    gh_lines = [ln.strip() for ln in joined.splitlines()
                if "gh pr view" in ln or "gh pr diff" in ln]
    assert len(gh_lines) == 3, f"expected exactly 3 gh acquisition commands, found {len(gh_lines)}"
    for ln in gh_lines:
        assert "|| ok=0" in ln, \
            f"each gh command must record failure explicitly (|| ok=0), never rely on set -e: {ln!r}"

    assert re.search(
        r'\[\s*"\$ok"\s*=\s*1\s*\]\s*&&\s*PYTHONPATH=\S+ python3 -m crucible normalize-target github',
        flat), \
        "normalize-target github must be guarded by the success flag — run only after all three reads"

    assert re.search(r"normalize-target github.*?then\s+break", flat), \
        "normalize-target must gate the loop break so metadata drift (non-zero exit) retries"

    rm = re.search(r"rm -f [^\n]*", joined)
    assert rm, "acquisition must clean up partial artifacts on failure"
    for name in ("pr-before.json", "pr-after.json", "pr.diff", "target.json", "target.diff"):
        assert name in rm.group(0), f"cleanup must remove the partial {name} on any failure"

    assert re.search(r'\[\s*"\$ATTEMPT"\s*-lt\s*3\s*\][^\n]*exit 1', joined), \
        "after the 3rd attempt the acquisition must halt clearly (exit non-zero)"
    assert re.search(r"echo[^\n]*(fail|abort|halt)", joined, re.I), \
        "the halt must announce the acquisition failure"


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


def test_platform_notes_github_acquisition_fails_closed():
    # Finding (Task 3, round 2): the documented `gh pr view`/`gh pr diff` reads were unchecked, so a
    # failed command left an empty/truncated artifact that stable before/after metadata still let
    # `normalize-target` hash into a target. The acquisition loop must fail closed (section-scoped).
    section = _section(_read("platform-notes.md"), "Input normalization")
    assert_github_acquisition_fails_closed(_github_acquisition_block(section))


def test_platform_notes_github_acquisition_never_normalizes_after_failed_step(tmp_path):
    # Behavioral proof: run the DOCUMENTED loop with a `gh` that fails on `gh pr diff`. Because a failed
    # read never reaches `normalize-target`, no target is written and the loop halts non-zero — while the
    # identical loop with a `gh` that succeeds does reach normalize and writes the target. No `set -e`.
    section = _section(_read("platform-notes.md"), "Input normalization")
    loop = _github_acquisition_block(section)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stable_json = json.dumps({
        "number": 1, "url": "https://github.com/o/r/pull/1", "title": "t", "body": "b",
        "files": [{"path": "src/a.py"}], "baseRefName": "main", "baseRefOid": "1" * 40,
        "headRefName": "feat", "headRefOid": "2" * 40,
        "headRepository": {"nameWithOwner": "o/r"}, "headRepositoryOwner": {"login": "o"},
        "isCrossRepository": False,
    })
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "pr view" ]; then\n'
        f"  printf '%s\\n' '{stable_json}'\n"
        "  exit 0\n"
        'elif [ "$1 $2" = "pr diff" ]; then\n'
        "  echo 'diff --git a/src/a.py b/src/a.py'\n"
        '  exit "${GH_DIFF_EXIT:-0}"\n'
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)

    def _run(diff_exit: str) -> subprocess.CompletedProcess:
        for stale in ("target.json", "target.diff", "pr.diff", "pr-before.json", "pr-after.json"):
            (tmp_path / stale).unlink(missing_ok=True)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   PR="1", RUN=str(tmp_path), GH_DIFF_EXIT=diff_exit)
        # Deliberately NO `set -e`: the loop must fail closed on its own explicit checks.
        return subprocess.run(["bash", "-c", "set -u\n" + loop], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    fail = _run("1")
    assert fail.returncode != 0, f"a failed gh pr diff must halt non-zero:\n{fail.stderr}"
    assert not (tmp_path / "target.json").exists(), \
        "no target may be normalized after a failed diff step"
    assert not (tmp_path / "target.diff").exists(), \
        "no target patch may be written after a failed diff step"

    ok = _run("0")
    assert ok.returncode == 0, f"the happy path must succeed:\n{ok.stderr}"
    assert (tmp_path / "target.json").exists(), "the happy path must normalize a target"
    assert (tmp_path / "target.diff").exists(), "the happy path must write the target patch"


def test_platform_notes_materializes_pinned_source_per_kind():
    # Finding (Task 3, round 1): the shared materialization command left the head repository/SHA unset,
    # archived with ambient `git archive`, and always fed `source.tar.gz` to materialize-target even for
    # the local path that writes `source.tar`. platform-notes must document two separate executable paths.
    _assert_source_materialization_is_executable_per_kind(
        _section(_read("platform-notes.md"), "Input normalization"))


def test_platform_notes_source_materialization_fails_closed():
    # Finding (Task 3, round 3): the `gh api ... > source.tar.gz` fetch and the local `git -C ... archive`
    # were UNCHECKED, so a failed/truncated/stale archive relied on later/global shell behaviour instead
    # of explicitly switching to source-unavailable. platform-notes must fail closed and NON-FATAL.
    _assert_source_materialization_fails_closed(
        _section(_read("platform-notes.md"), "Input normalization"))


def _materialization_harness(tmp_path, block: str, loaded_target: dict):
    """Fake `gh`/`git`/`python3` on PATH so the DOCUMENTED materialization block runs under `bash`
    without touching the network or a real repo: `python3 -c` parsing stays real, `-m crucible
    materialize-target` records its invocation + honours MATERIALIZE_EXIT, and `-m crucible
    repository-identity` echoes the recorded identity so the hard identity gate passes. Returns a
    `run(**env)` callable that resets RUN, executes the block (deliberately NO `set -e`), and reports
    (CompletedProcess, materialize_invoked, source_left, archive_left)."""
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    run_dir = tmp_path / "run"
    materialize_log = tmp_path / "materialize.log"
    recorded_identity = loaded_target.get("repository", "")

    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "api" ]; then\n'
        "  printf 'partial-tarball-bytes'\n"          # truncated archive written via redirection
        '  exit "${GH_API_EXIT:-0}"\n'
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)

    git = bindir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "--output" ] && out="$a"; prev="$a"; done\n'
        "[ -n \"$out\" ] && printf 'partial-tar-bytes' > \"$out\"\n"
        'exit "${GIT_ARCHIVE_EXIT:-0}"\n'
    )
    git.chmod(0o755)

    py = bindir / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"-m crucible materialize-target"*)\n'
        '    printf "%s\\n" "$*" >> "$MATERIALIZE_LOG"\n'
        '    exit "${MATERIALIZE_EXIT:-0}" ;;\n'
        '  *"-m crucible repository-identity"*)\n'
        '    printf "%s\\n" "$RECORDED_IDENTITY"\n'
        "    exit 0 ;;\n"
        '  *) exec "$REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    py.chmod(0o755)

    archive_gz = run_dir / "source.tar.gz"
    archive_tar = run_dir / "source.tar"

    def run(**overrides):
        if run_dir.exists():
            import shutil
            shutil.rmtree(run_dir)
        run_dir.mkdir()
        (run_dir / "loaded-target.json").write_text(json.dumps(loaded_target))
        materialize_log.write_text("")
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            RUN=str(run_dir),
            LOCAL_REPO=str(tmp_path / "checkout"),
            REAL_PYTHON=sys.executable,
            MATERIALIZE_LOG=str(materialize_log),
            RECORDED_IDENTITY=recorded_identity,
        )
        env.update({k: str(v) for k, v in overrides.items()})
        proc = subprocess.run(
            ["bash", "-c", "set -u\n" + block + '\nprintf "SOURCE_AVAILABLE=%s\\n" "$SOURCE_AVAILABLE"\n'],
            cwd=ROOT, env=env, capture_output=True, text=True)
        invoked = bool(materialize_log.read_text().strip())
        return proc, invoked, archive_tar.exists() or archive_gz.exists()

    return run


def test_platform_notes_source_materialization_github_fetch_failure_stays_patch_only(tmp_path):
    # Behavioral proof (Task 3, round 3): run the DOCUMENTED GitHub materialization block with a `gh api`
    # that fails. A failed fetch must NEVER invoke materialize-target and must leave no source/archive,
    # while the review continues patch-only (non-fatal, SOURCE_AVAILABLE=no). The happy path reaches
    # materialize and reports SOURCE_AVAILABLE=yes.
    gh, _local = _source_materialization_blocks(
        _section(_read("platform-notes.md"), "Input normalization"))
    run = _materialization_harness(
        tmp_path, gh, {"head": {"repository": "octo/repo", "sha": "a" * 40}})

    proc, invoked, archive_left = run(GH_API_EXIT="1")
    assert proc.returncode == 0, f"a failed fetch must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "materialize-target must NEVER run after a failed GitHub fetch"
    assert not archive_left, "a truncated GitHub archive must be discarded, not left behind"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a failed fetch must leave the source unavailable"

    proc, invoked, _ = run(GH_API_EXIT="0", MATERIALIZE_EXIT="0")
    assert proc.returncode == 0 and invoked, "the happy path must fetch then materialize"
    assert "SOURCE_AVAILABLE=yes" in proc.stdout, "a materialized snapshot marks the source available"

    proc, invoked, archive_left = run(GH_API_EXIT="0", MATERIALIZE_EXIT="1")
    assert invoked, "a successful fetch must still attempt materialize-target"
    assert not archive_left, "a rejected archive must be discarded, not left behind"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a rejected materialization stays source-unavailable"


def test_platform_notes_source_materialization_local_failure_stays_patch_only(tmp_path):
    # Behavioral proof (Task 3, round 3): run the DOCUMENTED local materialization block with a
    # `git -C ... archive` that fails, then with a materialize-target that rejects. Neither may invoke
    # the next step with a broken archive nor leave a partial source/archive; the review stays patch-only.
    _gh, local = _source_materialization_blocks(
        _section(_read("platform-notes.md"), "Input normalization"))
    run = _materialization_harness(
        tmp_path, local, {"repository": "github.com/octo/repo.git", "head": {"sha": "b" * 40}})

    proc, invoked, archive_left = run(GIT_ARCHIVE_EXIT="1")
    assert proc.returncode == 0, f"a failed archive must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "materialize-target must NEVER run after a failed local archive"
    assert not archive_left, "a truncated local archive must be discarded, not left behind"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a failed archive must leave the source unavailable"

    proc, invoked, archive_left = run(GIT_ARCHIVE_EXIT="0", MATERIALIZE_EXIT="1")
    assert invoked, "a successful archive must still attempt materialize-target"
    assert not archive_left, "a rejected archive must be discarded, not left behind"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a rejected materialization stays source-unavailable"

    proc, invoked, _ = run(GIT_ARCHIVE_EXIT="0", MATERIALIZE_EXIT="0")
    assert proc.returncode == 0 and invoked, "the happy path must archive then materialize"
    assert "SOURCE_AVAILABLE=yes" in proc.stdout, "a materialized snapshot marks the source available"


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
