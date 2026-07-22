import json
import os
import re
import shutil
import subprocess
import sys
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
    """The pinned source snapshot is materialized on TWO separate, self-contained, executable paths.
    GitHub **reuses** the exact ``head.tar.gz`` codeload archive already fetched during acquisition (the
    head ``repository@headRefOid`` snapshot the patch was derived from) — never a second ``gh api``
    fetch (F1: a re-fetch could observe a moved head). Local archives the exact recorded head via
    ``git -C "$LOCAL_REPO" archive`` and materializes ``source.tar`` — never one shared command that
    (a) leaves the head repository/SHA unset, (b) archives with ambient ``git archive``, or (c) feeds
    ``source.tar.gz`` to ``materialize-target`` even when the local path wrote ``source.tar``."""
    low = _flat(section)
    assert "show-target --run" in low and "loaded-target.json" in low, \
        "materialization must emit the authoritative loaded manifest before parsing local head identity"

    gh, local = _source_materialization_blocks(section)

    # GitHub reuses the acquisition head.tar.gz — no re-fetch, no git archive.
    assert "gh api" not in gh, "GitHub materialization must reuse head.tar.gz, never re-fetch via gh api"
    assert not re.search(r"git\s+archive", gh), "GitHub path must not archive with git"
    assert 'test -s "$RUN"/head.tar.gz' in gh, \
        "GitHub path must require the reused head.tar.gz archive is present and non-empty"
    assert _capture(r"materialize-target[^\n]*--archive\s+(\S+)", gh) == "head.tar.gz", \
        "GitHub must materialize the reused head.tar.gz (no re-fetch, no archive-path mismatch)"

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
        "local must materialize source.tar (never the GitHub head.tar.gz — no archive-path mismatch)"

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

    assert re.search(rf"(?:if|&&)\s+{fetch}", folded), \
        "the fetch/archive must be checked (`if ...` or the final `&&` conjunct of a compound if), " \
        "never an unchecked bare command"
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
    """(github_block, local_block) — the two executable source-materialization blocks in `section`.
    GitHub reuses the acquisition ``head.tar.gz`` (no ``git -C``); local archives via ``git -C``."""
    blocks = _bash_blocks(section)
    gh = [b for b in blocks
          if "materialize-target" in b and "head.tar.gz" in b and "git -C" not in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1 and len(local) == 1, \
        "exactly one GitHub and one local source-materialization block are required"
    return gh[0], local[0]


def _assert_github_reuse_fails_closed(block: str) -> None:
    """The GitHub materialization REUSES the acquisition ``head.tar.gz`` and fails closed + non-fatal
    (F1): it never re-fetches (`gh api`) or archives (`git archive`), defaults `SOURCE_AVAILABLE=no`,
    requires the reused archive is present/non-empty (`test -s`) AND checks `materialize-target` in the
    same guard, sets `SOURCE_AVAILABLE=yes` only after materialize succeeds, and NEVER deletes the
    reused acquisition archive (it is authoritative, not a partial)."""
    folded = re.sub(r"\\\n\s*", "", block)
    assert "SOURCE_AVAILABLE=no" in folded, "must default SOURCE_AVAILABLE=no (fail closed)"
    assert "SOURCE_AVAILABLE=yes" in folded, "must set SOURCE_AVAILABLE=yes only on success"
    assert "gh api" not in folded, "GitHub materialization must reuse head.tar.gz, never re-fetch"
    assert not re.search(r"git\s+archive", folded), "GitHub materialization must not archive with git"
    assert re.search(r'if\s+test -s "\$RUN"/head\.tar\.gz', folded), \
        "must require the reused head.tar.gz is present/non-empty before materialize"
    assert re.search(
        r'test -s "\$RUN"/head\.tar\.gz\s*&&\s*PYTHONPATH=\S+ python3 -m crucible materialize-target',
        folded), \
        "materialize-target must be &&-guarded by the reused-archive check (never a bare command)"
    assert folded.index("materialize-target") < folded.index("SOURCE_AVAILABLE=yes"), \
        "SOURCE_AVAILABLE=yes must be set only AFTER materialize succeeds"
    assert not re.search(r"rm -f[^\n]*head\.tar\.gz", folded), \
        "the reused acquisition archive is authoritative and must never be deleted here"


def _assert_source_materialization_fails_closed(section: str) -> None:
    """Both source-materialization kinds fail closed, and the section documents the NON-FATAL,
    patch-only continuation when the snapshot is unavailable (Task 3, round 3; F1 for GitHub reuse)."""
    gh, local = _source_materialization_blocks(section)
    _assert_github_reuse_fails_closed(gh)
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


def _assert_local_source_identity_gates_archive(section: str) -> None:
    """Task 3, round 4: the local source block ran its `test -n`, repository-identity equality, and
    recorded-HEAD checks as BARE commands. The snippets deliberately do NOT rely on a global `set -e`,
    so a mismatch was ignored and `git archive` + `materialize-target` still succeeded. The archive
    must instead be gated by ONE explicit compound `if`: parse `RECORDED_REPOSITORY_IDENTITY` +
    `HEAD_SHA`, compute `OBSERVED_REPOSITORY` only when those parses (and a valid `$LOCAL_REPO`) hold,
    then require — all `&&`-joined so any mismatch short-circuits past the archive — non-empty parses,
    `OBSERVED_REPOSITORY == RECORDED_REPOSITORY_IDENTITY`, `git rev-parse HEAD == HEAD_SHA`, and finally
    `git -C "$LOCAL_REPO" archive`. Reused verbatim by SKILL.md and platform-notes.md."""
    blocks = _bash_blocks(section)
    local = [b for b in blocks
             if "materialize-target" in b and 'git -C "$LOCAL_REPO" archive' in b]
    assert len(local) == 1, "exactly one executable local source-materialization block is required"
    folded = re.sub(r"\\\n\s*", "", local[0])

    # The recorded-identity variable is renamed to align with the execution gate; the old bare name is gone.
    assert "RECORDED_REPOSITORY_IDENTITY=" in folded, \
        "the local path must parse RECORDED_REPOSITORY_IDENTITY from the loaded manifest"
    assert not re.search(r"RECORDED_REPOSITORY(?!_IDENTITY)", folded), \
        "the old bare RECORDED_REPOSITORY name must be gone (use RECORDED_REPOSITORY_IDENTITY)"

    # OBSERVED_REPOSITORY is initialized empty (safe under `set -u`) and only assigned under a guard —
    # never an unconditional `repository-identity` that could run git against a missing/ambient repo.
    assert re.search(r"(?m)^\s*OBSERVED_REPOSITORY=\s*$", folded), \
        "OBSERVED_REPOSITORY must be initialized empty before the guarded computation (set -u safe)"
    assert re.search(
        r'(?m)^\s+OBSERVED_REPOSITORY=\$\(PYTHONPATH=\S+ python3 -m crucible '
        r'repository-identity --repo "\$LOCAL_REPO"\)', folded), \
        "OBSERVED_REPOSITORY must be computed via repository-identity inside a guard, not unconditionally"

    # ONE compound `if` gates the archive: the non-empty parses, the identity equality, and the recorded
    # HEAD equality are all `&&`-joined conjuncts ending in the archive, so any mismatch short-circuits.
    assert re.search(
        r'if\b[^\n]*?test -n "\$RECORDED_REPOSITORY_IDENTITY"[^\n]*?'
        r'&&[^\n]*?test -n "\$HEAD_SHA"[^\n]*?'
        r'&&[^\n]*?test "\$OBSERVED_REPOSITORY" = "\$RECORDED_REPOSITORY_IDENTITY"[^\n]*?'
        r'&&[^\n]*?test "\$\(git -C "\$LOCAL_REPO" rev-parse HEAD\)" = "\$HEAD_SHA"[^\n]*?'
        r'&&[^\n]*?git -C "\$LOCAL_REPO" archive', folded), \
        "the archive must be gated by ONE compound `if` with the identity + recorded-HEAD checks " \
        "&&-joined ahead of it (a mismatch must skip the archive/materialize)"

    # $LOCAL_REPO directory validity is a conjunct of the gate, checked BEFORE any git runs against it,
    # so a missing/empty $LOCAL_REPO never runs `git rev-parse`/archive in an unrelated/ambient repo.
    assert 'test -d "$LOCAL_REPO"' in folded, \
        "the gate must validate $LOCAL_REPO is a directory before running git against it"
    gate = next((ln for ln in folded.splitlines()
                 if 'git -C "$LOCAL_REPO" rev-parse HEAD' in ln), "")
    assert 'test -d "$LOCAL_REPO"' in gate and \
        gate.index('test -d "$LOCAL_REPO"') < gate.index('git -C "$LOCAL_REPO" rev-parse'), \
        "the $LOCAL_REPO directory check must precede `git rev-parse HEAD` in the compound gate"

    # The gated checks must NOT also appear as bare, ignorable standalone commands (the round-4 defect).
    assert not re.search(r'(?m)^\s*test -n "\$RECORDED_REPOSITORY_IDENTITY"', folded), \
        "the non-empty identity parse must gate the archive inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test "\$OBSERVED_REPOSITORY" = "\$RECORDED_REPOSITORY_IDENTITY"', folded), \
        "the identity-equality check must gate the archive inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test "\$\([^\n]*repository-identity', folded), \
        'the identity check must not be a bare inline `test "$(... repository-identity ...)"` command'
    assert not re.search(r'(?m)^\s*test "\$\(git -C "\$LOCAL_REPO" rev-parse HEAD\)"', folded), \
        "the recorded-HEAD check must gate the archive inside `if`, not run as a bare command"


def _no_negated_echo(sec: str) -> None:
    assert not re.search(r"\b(?:do not|don't|never|not)\s+echo\b", sec), \
        "each peer attestation must be required to echo the bindings, not negated"


def _github_acquisition_block(section: str) -> str:
    """The single executable GitHub before/base/head/after acquisition loop in `section`."""
    blocks = [b for b in _bash_blocks(section) if "normalize-target github" in b]
    assert len(blocks) == 1, \
        "exactly one executable GitHub before/base/head/after acquisition block is required"
    return blocks[0]


def assert_github_acquisition_fails_closed(block: str) -> None:
    """The GitHub before/compare/merge-base/head/after acquisition must fail CLOSED without relying on a
    global `set -e`, and must DERIVE the patch PR-style from the merge-base and head snapshots — never
    `gh pr diff` and never a base-tip two-dot diff (F1).

    The loop must: bound retries to 3; use NO `gh pr diff` anywhere; explicitly error-check the two
    `gh pr view` reads (before/after), the exact-OID `gh api .../compare/...` fetch, and the two
    `gh api .../tarball/...` archive fetches (merge-base + head); pass
    `--compare-metadata`/`--merge-base-archive`/`--head-archive` to `normalize-target` (never the removed
    `--base-archive`), run ONLY after every step succeeds; discard EVERY partial
    before/after/compare/merge-base/head/target artifact on any failure; halt clearly once the attempts
    are exhausted; and preserve the metadata-drift retry (a non-zero `normalize-target` exit re-enters
    the loop)."""
    joined = re.sub(r"\\\n\s*", "", block)          # fold shell line-continuations to logical lines
    flat = " ".join(joined.split())

    assert "for ATTEMPT in 1 2 3" in flat, "acquisition must retry within a bounded 3-attempt loop"
    assert "ok=1" in flat, "each attempt must reset the success flag before the gh reads"
    assert "gh pr diff" not in flat, "the live protocol must not use gh pr diff (derive from snapshots)"

    view_lines = [ln.strip() for ln in joined.splitlines() if "gh pr view" in ln]
    assert len(view_lines) == 2, f"expected exactly 2 gh pr view reads, found {len(view_lines)}"
    for ln in view_lines:
        assert "|| ok=0" in ln, \
            f"each gh pr view read must record failure explicitly (|| ok=0): {ln!r}"

    compare_lines = [ln.strip() for ln in joined.splitlines() if "gh api" in ln and "compare/" in ln]
    assert len(compare_lines) == 1, \
        f"expected exactly 1 gh api compare fetch, found {len(compare_lines)}"
    assert 'compare/$BASE_OID...$HEAD_OID' in flat, \
        "the compare must use the exact base/head OIDs (never branch names)"
    assert "|| ok=0" in compare_lines[0], \
        f"the compare fetch must record failure explicitly (|| ok=0): {compare_lines[0]!r}"
    assert "merge_base_commit" in flat, "acquisition must parse merge_base_commit.sha from compare.json"

    api_lines = [ln.strip() for ln in joined.splitlines() if "gh api" in ln and "tarball" in ln]
    assert len(api_lines) == 2, f"expected exactly 2 gh api tarball fetches, found {len(api_lines)}"
    for ln in api_lines:
        assert "|| ok=0" in ln, \
            f"each archive fetch must record failure explicitly (|| ok=0): {ln!r}"
    assert "merge-base.tar.gz" in flat and "head.tar.gz" in flat, \
        "acquisition must fetch merge-base.tar.gz and head.tar.gz (merge-base/head OID snapshots)"
    assert "tarball/$MERGE_BASE_OID" in flat and "tarball/$HEAD_OID" in flat, \
        "the two tarball fetches must pin the merge-base and head OIDs"
    assert "tarball/$BASE_OID" not in flat, \
        "acquisition must not fetch a base-tip tarball (the patch is derived from the merge base)"

    assert re.search(
        r'\[\s*"\$ok"\s*=\s*1\s*\]\s*&&\s*PYTHONPATH=\S+ python3 -m crucible normalize-target github',
        flat), \
        "normalize-target github must be guarded by the success flag — run only after every step"
    assert "--compare-metadata" in flat and "--merge-base-archive" in flat and "--head-archive" in flat, \
        "normalize-target github must consume the compare metadata + merge-base/head snapshots"
    assert "--base-archive" not in flat, "the removed base-tip --base-archive flag must not appear"
    assert re.search(r"normalize-target github.*?then\s+break", flat), \
        "normalize-target must gate the loop break so metadata drift (non-zero exit) retries"

    rm = re.search(r"rm -f [^\n]*", joined)
    assert rm, "acquisition must clean up partial artifacts on failure"
    for name in ("pr-before.json", "pr-after.json", "compare.json", "merge-base.tar.gz",
                 "head.tar.gz", "target.json", "target.diff"):
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
    # materialize), not a bare diff/triple. GitHub requests immutable OIDs + fork identity and DERIVES
    # the patch from base/head snapshots (never `gh pr diff`); local uses a single merge-base `--range`.
    low = _norm("platform-notes.md")
    assert "normalize-target" in low
    assert "load-target" in low
    assert "materialize-target" in low
    assert "gh pr diff" not in low            # the patch is derived from merge-base/head snapshots, not gh pr diff
    assert "--compare-metadata" in low  # GitHub pins the fork point via the exact-OID compare endpoint
    assert "--merge-base-archive" in low and "--head-archive" in low  # GitHub derives from merge-base/head OID snapshots
    assert "--base-archive" not in low  # the stale base-tip snapshot flag is gone
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


def _codeload_targz(path, wrapper, files):
    """A real gzip'd GitHub codeload tarball (single ``wrapper/`` prefix over a repo tree)."""
    import io
    import tarfile
    with tarfile.open(path, "w:gz") as tar:
        d = tarfile.TarInfo(f"{wrapper}/"); d.type = tarfile.DIRTYPE; d.mode = 0o755
        tar.addfile(d)
        seen = {""}
        for rel in sorted(files):
            parts = rel.split("/")
            for i in range(1, len(parts)):
                sub = "/".join(parts[:i])
                if sub not in seen:
                    di = tarfile.TarInfo(f"{wrapper}/{sub}/"); di.type = tarfile.DIRTYPE
                    di.mode = 0o755
                    tar.addfile(di)
                    seen.add(sub)
            info = tarfile.TarInfo(f"{wrapper}/{rel}"); info.size = len(files[rel])
            tar.addfile(info, io.BytesIO(files[rel]))
    return path


def test_platform_notes_github_acquisition_never_normalizes_after_failed_step(tmp_path):
    # Behavioral proof (F1): run the DOCUMENTED loop with a fake `gh` (real `python3`/`crucible`). A
    # failed compare or merge-base/head archive fetch never reaches `normalize-target`, so no target is
    # written and the loop halts non-zero — while the identical loop with every step succeeding derives
    # the PR-style patch from the MERGE-BASE and head snapshots and writes the target. The base branch
    # has advanced past the fork point (baseRefOid != merge_base), and the base-only file never leaks in.
    # No `gh pr diff`, no base-tip tarball, no `set -e`.
    section = _section(_read("platform-notes.md"), "Input normalization")
    loop = _github_acquisition_block(section)

    base_tip_oid, merge_base_oid, head_oid = "a" * 40, "b" * 40, "2" * 40
    mb_tar = tmp_path / "merge-base-fixture.tar.gz"
    head_tar = tmp_path / "head-fixture.tar.gz"
    # The fork-point (merge-base) and head snapshots differ only in feature.py; shared.py is unchanged.
    _codeload_targz(mb_tar, "base-repo-bbb", {"feature.py": b"v1\n", "shared.py": b"shared\n"})
    _codeload_targz(head_tar, "fork-repo-222", {"feature.py": b"v2\n", "shared.py": b"shared\n"})
    stable_json = json.dumps({
        "number": 1, "url": "https://github.com/base/repo/pull/1", "title": "t", "body": "b",
        "files": [{"path": "feature.py"}], "baseRefName": "main", "baseRefOid": base_tip_oid,
        "headRefName": "feat", "headRefOid": head_oid,
        "headRepository": {"nameWithOwner": "fork/repo"}, "headRepositoryOwner": {"login": "fork"},
        "isCrossRepository": True,
    })
    # The base repo's exact-OID compare reports the fork point as merge_base (the base advanced past it).
    compare_json = json.dumps({
        "base_commit": {"sha": base_tip_oid},
        "merge_base_commit": {"sha": merge_base_oid},
        "status": "diverged",
        "files": [{"filename": "feature.py", "status": "modified"}],
    })
    compare_fixture = tmp_path / "compare-fixture.json"
    compare_fixture.write_text(compare_json)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "pr view" ]; then\n'
        f"  printf '%s\\n' '{stable_json}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "api" ]; then\n'
        "  case \"$2\" in\n"
        f'    *"compare/{base_tip_oid}...{head_oid}") cat "$COMPARE_JSON"; exit "${{GH_COMPARE_EXIT:-0}}" ;;\n'
        f'    *"tarball/{merge_base_oid}") cat "$MERGE_BASE_TAR"; exit "${{GH_MERGE_BASE_EXIT:-0}}" ;;\n'
        f'    *"tarball/{head_oid}") cat "$HEAD_TAR"; exit "${{GH_HEAD_EXIT:-0}}" ;;\n'
        "  esac\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)

    def _run(**overrides) -> subprocess.CompletedProcess:
        for stale in ("target.json", "target.diff", "compare.json", "merge-base.tar.gz",
                      "head.tar.gz", "pr-before.json", "pr-after.json"):
            (tmp_path / stale).unlink(missing_ok=True)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   PR="1", RUN=str(tmp_path), COMPARE_JSON=str(compare_fixture),
                   MERGE_BASE_TAR=str(mb_tar), HEAD_TAR=str(head_tar))
        env.update({k: str(v) for k, v in overrides.items()})
        # Deliberately NO `set -e`: the loop must fail closed on its own explicit checks.
        return subprocess.run(["bash", "-c", "set -u\n" + loop], cwd=ROOT, env=env,
                              capture_output=True, text=True)

    for knob in ("GH_COMPARE_EXIT", "GH_MERGE_BASE_EXIT", "GH_HEAD_EXIT"):
        fail = _run(**{knob: "1"})
        assert fail.returncode != 0, f"a failed {knob} step must halt non-zero:\n{fail.stderr}"
        assert not (tmp_path / "target.json").exists(), \
            f"no target may be normalized after a failed {knob} step"
        assert not (tmp_path / "target.diff").exists(), \
            f"no target patch may be written after a failed {knob} step"

    ok = _run()
    assert ok.returncode == 0, f"the happy path must succeed:\n{ok.stderr}"
    assert (tmp_path / "target.json").exists(), "the happy path must normalize a target"
    assert (tmp_path / "target.diff").exists(), "the happy path must write the derived patch"
    # The head archive is preserved for reuse by materialize-target (never re-fetched).
    assert (tmp_path / "head.tar.gz").exists(), "the head archive must remain for materialize reuse"
    manifest = json.loads((tmp_path / "target.json").read_text())
    assert manifest["kind"] == "github-pr"
    assert manifest["changed_files"] == ["feature.py"]  # only the feature change, never a base-only file
    assert manifest["merge_base_sha"] == merge_base_oid  # the recorded fork point, not the advanced tip
    assert manifest["base"]["sha"] == base_tip_oid and manifest["base"]["sha"] != merge_base_oid
    derived = (tmp_path / "target.diff").read_bytes().decode()
    assert "-v1" in derived and "+v2" in derived, \
        "the derived patch must be the merge-base->head snapshot diff"
    assert "shared.py" not in derived, "an unchanged file must not appear in the derived merge-base patch"


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
    materialize-target` records its invocation + honours MATERIALIZE_EXIT, `-m crucible
    repository-identity` echoes `${OBSERVED_IDENTITY:-$RECORDED_IDENTITY}` (so a wrong observed identity
    can be injected), and `git ... rev-parse HEAD` echoes `${OBSERVED_HEAD:-$RECORDED_HEAD}` (so a wrong
    observed head can be injected). `$LOCAL_REPO` points at a real directory so `test -d` passes on the
    happy path. Returns a `run(**env)` callable that resets RUN, executes the block (deliberately NO
    `set -e`), and reports (CompletedProcess, materialize_invoked, archive_left)."""
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "checkout").mkdir()          # $LOCAL_REPO is a real directory (so `test -d` passes)
    run_dir = tmp_path / "run"
    materialize_log = tmp_path / "materialize.log"
    git_log = tmp_path / "git.log"           # every `git` invocation the block makes (rev-parse/archive)
    recorded_identity = loaded_target.get("repository", "")
    recorded_head = loaded_target.get("head", {}).get("sha", "")

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
        'printf "%s\\n" "$*" >> "$GIT_LOG"\n'         # record every git call (proves none run if repo missing)
        'for a in "$@"; do\n'
        '  if [ "$a" = "rev-parse" ]; then printf "%s\\n" "${OBSERVED_HEAD:-$RECORDED_HEAD}"; exit 0; fi\n'
        "done\n"
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
        '    printf "%s\\n" "${OBSERVED_IDENTITY:-$RECORDED_IDENTITY}"\n'
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
        git_log.write_text("")
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            RUN=str(run_dir),
            LOCAL_REPO=str(tmp_path / "checkout"),
            REAL_PYTHON=sys.executable,
            MATERIALIZE_LOG=str(materialize_log),
            GIT_LOG=str(git_log),
            RECORDED_IDENTITY=recorded_identity,
            RECORDED_HEAD=recorded_head,
        )
        env.update({k: str(v) for k, v in overrides.items()})
        proc = subprocess.run(
            ["bash", "-c", "set -u\n" + block + '\nprintf "SOURCE_AVAILABLE=%s\\n" "$SOURCE_AVAILABLE"\n'],
            cwd=ROOT, env=env, capture_output=True, text=True)
        invoked = bool(materialize_log.read_text().strip())
        return proc, invoked, archive_tar.exists() or archive_gz.exists()

    return run


def test_platform_notes_source_materialization_github_reuses_head_archive(tmp_path):
    # Behavioral proof (F1): the DOCUMENTED GitHub materialization block REUSES the acquisition
    # head.tar.gz — it never re-fetches. A missing/empty archive never reaches materialize-target
    # (patch-only, SOURCE_AVAILABLE=no); a present archive materializes (SOURCE_AVAILABLE=yes); a
    # rejected materialization stays source-unavailable but NEVER deletes the authoritative archive.
    gh_block, _local = _source_materialization_blocks(
        _section(_read("platform-notes.md"), "Input normalization"))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    run_dir = tmp_path / "run"
    materialize_log = tmp_path / "materialize.log"
    py = bindir / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"-m crucible materialize-target"*)\n'
        '    printf "%s\\n" "$*" >> "$MATERIALIZE_LOG"\n'
        '    exit "${MATERIALIZE_EXIT:-0}" ;;\n'
        '  *) exec "$REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    py.chmod(0o755)

    def run(create_archive=True, **overrides):
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir()
        if create_archive:
            (run_dir / "head.tar.gz").write_bytes(b"codeload-archive-bytes")
        materialize_log.write_text("")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", RUN=str(run_dir),
                   REAL_PYTHON=sys.executable, MATERIALIZE_LOG=str(materialize_log))
        env.update({k: str(v) for k, v in overrides.items()})
        proc = subprocess.run(
            ["bash", "-c", "set -u\n" + gh_block
             + '\nprintf "SOURCE_AVAILABLE=%s\\n" "$SOURCE_AVAILABLE"\n'],
            cwd=ROOT, env=env, capture_output=True, text=True)
        invoked = bool(materialize_log.read_text().strip())
        return proc, invoked

    # Happy path: reused head.tar.gz present + materialize ok -> source available, archive preserved.
    proc, invoked = run(create_archive=True, MATERIALIZE_EXIT="0")
    assert proc.returncode == 0 and invoked, f"the happy path must materialize:\n{proc.stderr}"
    assert "SOURCE_AVAILABLE=yes" in proc.stdout
    assert (run_dir / "head.tar.gz").exists(), "the reused acquisition archive is never deleted"

    # Rejected materialize -> stays unavailable, archive NOT deleted (it is authoritative).
    proc, invoked = run(create_archive=True, MATERIALIZE_EXIT="1")
    assert invoked, "a present archive must attempt materialize-target"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a rejected materialization stays source-unavailable"
    assert (run_dir / "head.tar.gz").exists(), "a rejected materialization must not delete the archive"

    # Missing archive -> never reaches materialize-target, stays patch-only.
    proc, invoked = run(create_archive=False)
    assert proc.returncode == 0, f"a missing archive must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "a missing head.tar.gz must NEVER reach materialize-target"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a missing archive stays source-unavailable"


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


def test_platform_notes_local_source_identity_gates_archive():
    # Finding (Task 3, round 4): the local source block's `test -n`, repository-identity equality, and
    # recorded-HEAD checks were BARE commands, so without a global `set -e` a mismatch was ignored and
    # the archive/materialize still ran. platform-notes must gate the archive on ONE compound `if`.
    _assert_local_source_identity_gates_archive(
        _section(_read("platform-notes.md"), "Input normalization"))


def test_platform_notes_local_source_identity_and_head_gate_archive(tmp_path):
    # Behavioral proof (Task 3, round 4): run the DOCUMENTED local materialization block under `bash`
    # with NO `set -e`. A wrong observed repository identity and a wrong observed HEAD must EACH
    # short-circuit the compound gate so `git archive` never runs, `materialize-target` is never
    # invoked, no source.tar is left, and the review stays patch-only (SOURCE_AVAILABLE=no, exit 0).
    # The happy path (matching identity + head) still archives and materializes.
    _gh, local = _source_materialization_blocks(
        _section(_read("platform-notes.md"), "Input normalization"))
    run = _materialization_harness(
        tmp_path, local, {"repository": "github.com/octo/repo.git", "head": {"sha": "b" * 40}})

    # Wrong repository identity: OBSERVED != RECORDED -> gate short-circuits before the archive.
    proc, invoked, archive_left = run(OBSERVED_IDENTITY="evil.example/other.git")
    assert proc.returncode == 0, f"an identity mismatch must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "a wrong repository identity must NEVER reach materialize-target"
    assert not archive_left, "a wrong repository identity must not archive/leave a source.tar"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "an identity mismatch stays source-unavailable"

    # Wrong observed HEAD: identity matches but rev-parse HEAD != recorded head.sha -> short-circuits.
    proc, invoked, archive_left = run(OBSERVED_HEAD="c" * 40)
    assert proc.returncode == 0, f"a head mismatch must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "a wrong recorded HEAD must NEVER reach materialize-target"
    assert not archive_left, "a wrong recorded HEAD must not archive/leave a source.tar"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a head mismatch stays source-unavailable"

    # Missing $LOCAL_REPO: the `test -d` guard must skip the whole gate so NO git (rev-parse/archive)
    # ever runs — never against an unrelated/ambient repo (e.g. the reviewer's own cwd).
    proc, invoked, archive_left = run(LOCAL_REPO="")
    assert proc.returncode == 0, f"a missing $LOCAL_REPO must be non-fatal (patch-only):\n{proc.stderr}"
    assert not invoked, "a missing $LOCAL_REPO must NEVER reach materialize-target"
    assert not archive_left, "a missing $LOCAL_REPO must not archive/leave a source.tar"
    assert "SOURCE_AVAILABLE=no" in proc.stdout, "a missing $LOCAL_REPO stays source-unavailable"
    assert (tmp_path / "git.log").read_text().strip() == "", \
        "a missing $LOCAL_REPO must not run git (rev-parse/archive) in an unrelated/ambient repo"

    # Happy path: matching identity + head -> archive then materialize -> source available.
    proc, invoked, _ = run(GIT_ARCHIVE_EXIT="0", MATERIALIZE_EXIT="0")
    assert proc.returncode == 0 and invoked, "the happy path must archive then materialize"
    assert "SOURCE_AVAILABLE=yes" in proc.stdout, "a materialized snapshot marks the source available"


def _execution_gate_block(section: str) -> str:
    """The single executable trusted-local execution-safety verification block in `section`."""
    blocks = [b for b in _bash_blocks(section)
              if "status --porcelain" in b and "CHECKOUT_VERIFIED" in b]
    assert len(blocks) == 1, \
        "exactly one executable trusted-local execution verification block is required"
    return blocks[0]


def _assert_execution_gate_is_compound(section: str) -> None:
    """F2: the trusted-local execution-safety proof must be ONE explicit compound `if`/`&&` gate that
    does not rely on a global `set -e`. `CHECKOUT_VERIFIED` defaults to `no`; the gate `&&`-joins, in a
    single `if`, non-empty recorded identity + head, a valid `$LOCAL_REPO` directory (checked BEFORE any
    git runs against it), observed-identity equality, `git status --porcelain` succeeding AND empty, and
    `git rev-parse HEAD` succeeding AND equal to the recorded head — and only then sets
    `CHECKOUT_VERIFIED=yes`. None of those checks may appear as bare, ignorable standalone commands."""
    block = _execution_gate_block(section)
    folded = re.sub(r"\\\n\s*", "", block)

    assert re.search(r"(?m)^\s*CHECKOUT_VERIFIED=no\s*$", folded), \
        "CHECKOUT_VERIFIED must default to no (fail closed) before the gate"

    assert re.search(
        r'if\b[^\n]*?test -n "\$RECORDED_REPOSITORY_IDENTITY"[^\n]*?'
        r'&&[^\n]*?test -n "\$RECORDED_HEAD_SHA"[^\n]*?'
        r'&&[^\n]*?test -d "\$LOCAL_REPO"[^\n]*?'
        r'&&[^\n]*?test "\$OBSERVED_REPOSITORY" = "\$RECORDED_REPOSITORY_IDENTITY"[^\n]*?'
        r'&&[^\n]*?git -C "\$LOCAL_REPO" status --porcelain[^\n]*?'
        r'&&[^\n]*?test -z "\$STATUS"[^\n]*?'
        r'&&[^\n]*?git -C "\$LOCAL_REPO" rev-parse HEAD[^\n]*?'
        r'&&[^\n]*?test "\$HEAD_NOW" = "\$RECORDED_HEAD_SHA"', folded), \
        "the gate must be ONE compound `if` with identity/head/clean-tree/exact-head checks &&-joined"

    gate = next((ln for ln in folded.splitlines() if "rev-parse HEAD" in ln), "")
    assert 'test -d "$LOCAL_REPO"' in gate and \
        gate.index('test -d "$LOCAL_REPO"') < gate.index('git -C "$LOCAL_REPO" status'), \
        "the $LOCAL_REPO directory check must precede any git command in the compound gate"

    idx_gate = folded.index("if ")
    idx_yes = folded.index("CHECKOUT_VERIFIED=yes")
    assert idx_yes > idx_gate, "CHECKOUT_VERIFIED=yes must be set only inside/after the compound gate"

    # None of the verification checks may run as a bare, ignorable standalone command (the F2 defect).
    assert not re.search(r'(?m)^\s*test "\$OBSERVED_REPOSITORY" = "\$RECORDED_REPOSITORY_IDENTITY"', folded), \
        "the identity-equality check must gate execution inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test -z "\$\(git -C "\$LOCAL_REPO" status', folded), \
        "the clean-tree check must gate execution inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test "\$\(git -C "\$LOCAL_REPO" rev-parse HEAD\)"', folded), \
        "the recorded-head check must gate execution inside `if`, not run as a bare command"


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


def test_platform_notes_execution_gate_is_one_compound_if():
    # F2: the execution-safety proof must be ONE compound `if`/`&&` gate (not bare commands that a
    # missing global `set -e` would ignore); only a passing gate may reach consent/execution.
    _assert_execution_gate_is_compound(
        _section(_read("platform-notes.md"), "Execution Safety Gate"))


def test_platform_notes_execution_gate_bash_behavior(tmp_path):
    # Behavioral proof (F2): run the DOCUMENTED execution gate under `bash` with NO `set -e` and a fake
    # git + crucible. Wrong identity, a dirty checkout, a failing `git status`, a wrong head, and a
    # missing $LOCAL_REPO must EACH leave CHECKOUT_VERIFIED=no (never reaching consent/execution); only
    # the fully-matching happy path verifies. A missing $LOCAL_REPO must never run git in an ambient repo.
    block = _execution_gate_block(_section(_read("platform-notes.md"), "Execution Safety Gate"))

    recorded_identity = "github.com/octo/repo.git"
    recorded_head = "a" * 40
    bindir = tmp_path / "bin"
    bindir.mkdir()
    run_dir = tmp_path / "run"
    git_log = tmp_path / "git.log"

    py = bindir / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"-m crucible show-target"*)\n'
        f'    printf \'{{"repository": "{recorded_identity}", "head": {{"sha": "{recorded_head}"}}}}\' ;;\n'
        '  *"-m crucible repository-identity"*)\n'
        '    printf "%s\\n" "${OBSERVED_IDENTITY:-$RECORDED_IDENTITY}" ;;\n'
        '  *) exec "$REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    py.chmod(0o755)
    git = bindir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GIT_LOG"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "status" ]; then printf "%s" "${STATUS_OUT:-}"; exit "${GIT_STATUS_EXIT:-0}"; fi\n'
        '  if [ "$a" = "rev-parse" ]; then printf "%s\\n" "${OBSERVED_HEAD:-$RECORDED_HEAD}"; exit 0; fi\n'
        "done\n"
        "exit 0\n"
    )
    git.chmod(0o755)

    def run(**overrides):
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir()
        git_log.write_text("")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", RUN=str(run_dir),
                   LOCAL_REPO=str(tmp_path / "checkout"), REAL_PYTHON=sys.executable,
                   GIT_LOG=str(git_log), RECORDED_IDENTITY=recorded_identity,
                   RECORDED_HEAD=recorded_head)
        (tmp_path / "checkout").mkdir(exist_ok=True)
        env.update({k: str(v) for k, v in overrides.items()})
        proc = subprocess.run(
            ["bash", "-c", "set -u\n" + block
             + '\nprintf "CHECKOUT_VERIFIED=%s\\n" "$CHECKOUT_VERIFIED"\n'],
            cwd=ROOT, env=env, capture_output=True, text=True)
        return proc

    # Happy path: matching identity + clean tree + exact head -> verified.
    ok = run()
    assert ok.returncode == 0, ok.stderr
    assert "CHECKOUT_VERIFIED=yes" in ok.stdout, f"the matching checkout must verify:\n{ok.stdout}"

    # Wrong repository identity -> not verified.
    bad = run(OBSERVED_IDENTITY="evil.example/other.git")
    assert "CHECKOUT_VERIFIED=no" in bad.stdout, "a wrong repository identity must never verify"

    # Dirty checkout (porcelain non-empty) -> not verified.
    dirty = run(STATUS_OUT=" M src/a.py")
    assert "CHECKOUT_VERIFIED=no" in dirty.stdout, "a dirty checkout must never verify"

    # git status failure -> not verified.
    statusfail = run(GIT_STATUS_EXIT="1")
    assert "CHECKOUT_VERIFIED=no" in statusfail.stdout, "a failing git status must never verify"

    # Wrong head -> not verified.
    wronghead = run(OBSERVED_HEAD="b" * 40)
    assert "CHECKOUT_VERIFIED=no" in wronghead.stdout, "a wrong head must never verify"

    # Missing $LOCAL_REPO -> not verified AND no git runs against an ambient/unrelated repo.
    missing = run(LOCAL_REPO="")
    assert "CHECKOUT_VERIFIED=no" in missing.stdout, "a missing $LOCAL_REPO must never verify"
    assert git_log.read_text().strip() == "", \
        "a missing $LOCAL_REPO must not run git (status/rev-parse) in an ambient repo"


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
