import json
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
    fetch (F1). Local archives the exact recorded head via ``git -C "$LOCAL_REPO" archive`` and
    materializes ``source.tar`` — never one shared command that (a) leaves the head repository/SHA
    unset, (b) archives with ambient ``git archive``, or (c) feeds ``source.tar.gz`` to
    ``materialize-target`` even when the local path wrote ``source.tar``. Reused verbatim by SKILL.md
    and platform-notes.md so both live surfaces carry the identical fix."""
    low = _flat(section)
    # The authoritative loaded manifest is emitted (show-target -> loaded-target.json) before any local
    # head identity is read — the head repository/SHA come only from it, never an ambient variable.
    assert "show-target --run" in low and "loaded-target.json" in low, \
        "materialization must emit the authoritative loaded manifest before parsing local head identity"

    blocks = _bash_blocks(section)
    gh = [b for b in blocks
          if "materialize-target" in b and "head.tar.gz" in b and "git -C" not in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1, "exactly one executable GitHub source-materialization block is required"
    assert len(local) == 1, "exactly one executable local source-materialization block is required"
    gh, local = gh[0], local[0]

    # GitHub: REUSE the acquisition head.tar.gz (no re-fetch, no git archive), require it present, and
    # materialize exactly that head.tar.gz.
    assert "gh api" not in gh, "GitHub materialization must reuse head.tar.gz, never re-fetch via gh api"
    assert not re.search(r"git\s+archive", gh), "GitHub path must not archive with git"
    assert 'test -s "$RUN"/head.tar.gz' in gh, \
        "GitHub path must require the reused head.tar.gz archive is present and non-empty"
    assert _capture(r"materialize-target[^\n]*--archive\s+(\S+)", gh) == "head.tar.gz", \
        "GitHub must materialize the reused head.tar.gz (no re-fetch, no archive-path mismatch)"
    assert "gh api" not in local, "local path must not fetch a GitHub tarball"

    # Local: parse repository/head.sha, REQUIRE non-empty, verify repository-identity(--repo $LOCAL_REPO)
    # equals the recorded identity BEFORE any archive, archive the exact head via an explicit
    # `git -C "$LOCAL_REPO"` (never ambient), and materialize exactly that source.tar.
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

    # No bare, ambient `git archive` anywhere in the section: it must always be scoped to $LOCAL_REPO.
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
    blocks = _bash_blocks(section)
    gh = [b for b in blocks
          if "materialize-target" in b and "head.tar.gz" in b and "git -C" not in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1 and len(local) == 1, \
        "exactly one GitHub and one local source-materialization block are required"
    _assert_github_reuse_fails_closed(gh[0])
    _assert_block_fails_closed(local[0], fetch=r'git -C "\$LOCAL_REPO" archive', archive="source.tar")

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


def _no_plain_verdict(text: str) -> None:
    """Prose may name `--resolutions` to state the ban; only command-invocation lines are forbidden
    from carrying it (or the build-only `verdict`)."""
    for line in text.splitlines():
        assert "-m crucible verdict " not in line and not line.rstrip().endswith("-m crucible verdict"), \
            f"symmetric skill must not invoke the build-only `verdict`: {line!r}"
        if "python3 -m crucible" in line:
            assert "--resolutions" not in line, \
                f"symmetric-verdict takes no --resolutions: {line!r}"


def _github_acquisition_block(section: str) -> str:
    """The single executable GitHub before/compare/merge-base/head/after acquisition loop in `section`."""
    blocks = [b for b in _bash_blocks(section) if "normalize-target github" in b]
    assert len(blocks) == 1, \
        "exactly one executable GitHub before/compare/merge-base/head/after acquisition block is required"
    return blocks[0]


def _assert_github_acquisition_fails_closed(block: str) -> None:
    """The GitHub before/compare/merge-base/head/after acquisition must fail CLOSED without a global
    `set -e`, and DERIVE the patch PR-style from the merge-base and head snapshots — never `gh pr diff`
    and never a base-tip two-dot diff (F1): bounded 3-attempt retry, the two `gh pr view` reads
    (before/after), the exact-OID `gh api .../compare/...` fetch, and the two `gh api .../tarball/...`
    archive fetches (merge-base + head) each explicitly error-checked, `normalize-target` (with
    `--compare-metadata`/`--merge-base-archive`/`--head-archive`) only after every step succeeds, EVERY
    partial before/after/compare/merge-base/head/target artifact discarded on any failure, a clear
    non-zero halt on exhaustion, and the metadata-drift retry preserved."""
    joined = re.sub(r"\\\n\s*", "", block)
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


def test_skill_initializes_with_pr_review_workflow_kind():
    text = SKILL.read_text()
    init_lines = [l for l in text.splitlines() if "init-run" in l and "python3 -m crucible" in l]
    assert init_lines, "SKILL.md must show the init-run command"
    assert any("--workflow pr-review" in l for l in init_lines), \
        "init-run must pass --workflow pr-review to select the symmetric two-peer flow"


def test_skill_reuses_crucible_cli_for_decisions():
    text = SKILL.read_text()
    for cmd in ["init-run", "load-dag", "next", "symmetric-verdict", "set-status", "report",
                "bindings", "accepted-findings", "review-result"]:
        assert cmd in text, f"SKILL.md should reference `crucible {cmd}`"


def test_skill_intro_states_cli_extended_not_unmodified():
    # The intro must not call the CLI "unmodified": the symmetric flow adds the `--workflow` run
    # metadata + the `symmetric-verdict`/`accepted-findings`/`review-result` commands (a CLI change),
    # even though the config SCHEMA is unchanged. Require that accurate positive wording (scoped to the
    # "for all bookkeeping" intro paragraph), not just a phrase ban, so the intro does not contradict
    # the command protocol the rest of the skill teaches.
    para = _flat(_para(SKILL.read_text(), "for all bookkeeping"))
    assert "unmodified" not in para
    assert "no config-schema change" in para
    assert "--workflow" in para
    assert "symmetric-verdict" in para


def test_skill_settles_gates_with_symmetric_verdict_never_plain_verdict():
    text = SKILL.read_text()
    assert "symmetric-verdict" in text
    _no_plain_verdict(text)


def test_skill_produces_separate_peer_attestation_files_with_schema():
    text = SKILL.read_text()
    assert "peer-a.json" in text and "peer-b.json" in text
    attest = next((b for b in _json_blocks(text)
                   if isinstance(b, dict) and b.get("peer") in ("A", "B")), None)
    assert attest is not None, "SKILL.md must show a peer attestation JSON example"
    for key in ("peer", "gate", "round", "verdict", "summary", "objections", "artifact_sha256"):
        assert key in attest, f"peer attestation schema missing {key!r}"
    assert attest["verdict"] in ("APPROVE", "REQUEST_CHANGES")
    assert isinstance(attest["objections"], list)


def test_skill_logs_structured_finding_set_for_dep_and_final():
    text = SKILL.read_text()
    fs = next((b for b in _json_blocks(text)
               if isinstance(b, dict) and isinstance(b.get("findings"), list) and b["findings"]), None)
    assert fs is not None, "SKILL.md must show the structured candidate finding-set JSON"
    finding = fs["findings"][0]
    for key in ("source_gate", "id", "severity", "location", "claim", "suggestion"):
        assert key in finding, f"candidate finding schema missing {key!r}"


def test_skill_distinguishes_candidate_findings_from_peer_objections():
    low = _norm(SKILL)
    assert "objection" in low
    assert "candidate" in low
    assert re.search(r"objection[^.]{0,160}(consensus|gate progress|decided)", low) or \
        re.search(r"(consensus|gate progress|decided)[^.]{0,160}objection", low), \
        "SKILL must state gate progress is decided from peer objections"


def test_skill_binds_every_gate_to_merged_artifact():
    # Schema-2 binding handshake (symmetric two-peer), asserted PER GATE (section-scoped): every gate
    # logs the candidate, asks `crucible bindings --gate <that gate>`, seeds Peer B with the JSON as
    # TRUSTED CLI METADATA, and BOTH peer attestation files ECHO exactly that gate's hash fields —
    # artifact+DAG at PLAN/FINAL, all three at dep:<thread> — then `symmetric-verdict --peer-a
    # --peer-b` decides. (pr-review has no REPRODUCE gate.)
    text = SKILL.read_text()

    plan = _flat(_section(text, "PLAN gate"))
    assert 'bindings --run "$run" --gate plan' in plan
    assert "trusted cli metadata" in plan
    assert "peer-a.json" in plan and "peer-b.json" in plan
    assert re.search(r"echo\w*\s+artifact_sha256 \+ dag_sha256", plan)
    assert "node_sha256" not in plan, "PLAN carries no node hash"
    assert ('symmetric-verdict --run "$run" --gate plan --round n --peer-a "$run"/peer-a.json '
            '--peer-b "$run"/peer-b.json') in plan
    _no_negated_echo(plan)

    thread = _flat(_section(text, "THREAD gates"))
    assert 'bindings --run "$run" --gate "dep:$node"' in thread
    assert "trusted cli metadata" in thread
    assert "artifact_sha256 + dag_sha256 + node_sha256" in thread
    assert ('symmetric-verdict --run "$run" --gate "dep:$node" --round n --peer-a "$run"/peer-a.json '
            '--peer-b "$run"/peer-b.json') in thread
    _no_negated_echo(thread)

    final = _flat(_section(text, "FINAL gate"))
    assert 'bindings --run "$run" --gate final' in final
    assert "trusted cli metadata" in final
    assert re.search(r"echo\w*\s+artifact_sha256 \+ dag_sha256", final)
    assert "node_sha256" not in final, "FINAL carries no node hash"
    assert 'symmetric-verdict --run "$run" --gate final --round n' in final
    _no_negated_echo(final)


def test_skill_assembles_final_from_accepted_findings():
    final = _flat(_section(SKILL.read_text(), "FINAL gate"))
    assert 'accepted-findings --run "$run"' in final
    assert "source_gate: final" in final or 'source_gate": "final' in final


def test_skill_uses_review_result_as_deliverable():
    finish = _flat(_section(SKILL.read_text(), "Finish"))
    assert 'review-result --run "$run"' in finish


def test_skill_records_human_approval_with_approve_plan():
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
    assert re.search(r"never\s+clear(?:ed|s)?[^.]{0,60}(--resolutions|wontfix)", norm), \
        "SKILL must state a blocking peer objection is NEVER CLEARED via --resolutions/wontfix"
    _no_plain_verdict(SKILL.read_text())


def test_skill_advances_thread_on_proceed_with_flags():
    low = _norm(SKILL)
    assert "proceed_with_flags" in low
    assert re.search(r"proceed_with_flags[^.]{0,200}set-status[^.]{0,60}done", low), \
        "SKILL must set a PROCEED_WITH_FLAGS thread node to done and continue"


def test_skill_both_peers_attest_every_round():
    low = _norm(SKILL)
    assert "both peers independently attest" in low
    assert "peer-a.json" in low and "peer-b.json" in low
    assert "candidate finding set" in low


def test_skill_surfaces_findings_on_copilot():
    low = SKILL.read_text().lower()
    assert "copilot" in low
    assert "report" in low or "findings" in low
    assert "in full" in low
    assert "truncate" in low and "tail" in low


def test_skill_does_not_modify_crucible_skill_paths():
    assert "skills/crucible/references" not in SKILL.read_text()


def test_skill_normalizes_target_into_immutable_manifest_before_plan():
    # The input is resolved to a deterministic target MANIFEST + exact patch through the CLI, loaded as
    # the run's one immutable target, and (github/local) materialized into a pinned source snapshot —
    # before PLAN. Scope to the normalization section and assert the executable command shapes, not
    # keywords; reject the old branch-name-only `gh pr view` and the raw two-dot `git diff <range>`.
    setup = _flat(_section(SKILL.read_text(), "Normalize the input"))
    assert "normalize-target github" in setup
    assert "normalize-target local" in setup
    assert "normalize-target diff" in setup
    assert "load-target --run" in setup
    assert "materialize-target --run" in setup
    # GitHub immutable OIDs + fork identity, read stably before AND after the diff.
    for field in ("baserefoid", "headrefoid", "headrepository",
                  "headrepositoryowner", "iscrossrepository"):
        assert field in setup, f"github normalization must request the {field} field"
    assert "--metadata-before" in setup and "--metadata-after" in setup
    # Local: one merge-base `--range`, never a raw two-dot tip diff or the old branch-name-only view.
    assert "--range" in setup
    assert "git diff <range>" not in setup
    assert "gh pr view <n> --json title,body,files" not in setup


def test_skill_github_acquisition_fails_closed():
    # Finding (Task 3, round 2): the SKILL's `gh pr view`/`gh pr diff` reads were unchecked, so a failed
    # command left an empty/truncated artifact that stable before/after metadata still let
    # `normalize-target` hash into a target. The live skill's acquisition loop must fail closed.
    section = _section(SKILL.read_text(), "Normalize the input")
    _assert_github_acquisition_fails_closed(_github_acquisition_block(section))


def test_skill_peer_attestation_binds_target_sha256():
    # Every pr-review gate binds the immutable review target, so the peer attestation example must echo
    # target_sha256 alongside the artifact/DAG/node hashes.
    attest = next((b for b in _json_blocks(SKILL.read_text())
                   if isinstance(b, dict) and b.get("peer") in ("A", "B")), None)
    assert attest is not None, "SKILL.md must show a peer attestation JSON example"
    assert "target_sha256" in attest, "pr-review peer attestation must echo target_sha256"


def test_skill_reads_pinned_source_not_ambient_checkout():
    # Both peers read the pinned snapshot (`RUN/source`) + the exact `RUN/target.diff`, never ambient
    # checkout files that may be a different revision.
    low = _norm(SKILL)
    assert "run/source" in low
    assert "target.diff" in low
    assert "never ambient" in low or "not ambient" in low or "never the ambient" in low


def test_skill_materializes_pinned_source_per_kind():
    # Finding (Task 3, round 1): the shared materialization command claimed the head repository/SHA are
    # authoritative but left the variables unset, archived with ambient `git archive`, and always fed
    # `source.tar.gz` to materialize-target even when the local path wrote `source.tar`. The live skill
    # must instead document two separate, self-contained, executable paths.
    _assert_source_materialization_is_executable_per_kind(
        _section(SKILL.read_text(), "Normalize the input"))


def test_skill_source_materialization_fails_closed():
    # Finding (Task 3, round 3): the live `gh api ... > source.tar.gz` fetch (and the local
    # `git -C ... archive`) were UNCHECKED — a failed/truncated/stale archive relied on later or global
    # shell behaviour instead of explicitly switching to source-unavailable. Each kind must fail closed
    # and NON-FATAL: stale archive removed, SOURCE_AVAILABLE default no, fetch/archive + materialize
    # if-checked, partial archive discarded on failure, yes only on success; the review continues
    # patch-only with source explicitly unavailable.
    _assert_source_materialization_fails_closed(
        _section(SKILL.read_text(), "Normalize the input"))


def test_skill_local_source_identity_gates_archive():
    # Finding (Task 3, round 4): the local source block ran `test -n`, the repository-identity equality,
    # and the recorded-HEAD check as BARE commands. Without a global `set -e` a mismatch was ignored and
    # `git archive` + materialize still ran. The archive must be gated by ONE compound `if` whose
    # &&-joined conjuncts (non-empty parses, OBSERVED == RECORDED identity, rev-parse HEAD == HEAD_SHA)
    # short-circuit past the archive on any mismatch.
    _assert_local_source_identity_gates_archive(
        _section(SKILL.read_text(), "Normalize the input"))


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

    assert folded.index("if ") < folded.index("CHECKOUT_VERIFIED=yes"), \
        "CHECKOUT_VERIFIED=yes must be set only inside/after the compound gate"

    assert not re.search(r'(?m)^\s*test "\$OBSERVED_REPOSITORY" = "\$RECORDED_REPOSITORY_IDENTITY"', folded), \
        "the identity-equality check must gate execution inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test -z "\$\(git -C "\$LOCAL_REPO" status', folded), \
        "the clean-tree check must gate execution inside `if`, not run as a bare command"
    assert not re.search(r'(?m)^\s*test "\$\(git -C "\$LOCAL_REPO" rev-parse HEAD\)"', folded), \
        "the recorded-head check must gate execution inside `if`, not run as a bare command"


def test_skill_execution_verifies_exact_head_at_recorded_sha():
    # Trusted-local execution must prove the checkout is the recorded head revision (repository
    # identity + clean tree + exact head sha) before any consent; a mismatch falls back to static-only
    # or a detached worktree-at-SHA that itself needs fresh consent. Remote/diff targets never execute.
    low = _flat(_section(SKILL.read_text(), "Execution Safety Gate"))
    assert "repository-identity" in low
    assert "rev-parse head" in low
    assert "status --porcelain" in low
    assert "recorded head" in low or "head.sha" in low or "recorded head sha" in low
    assert "worktree" in low
    assert "fresh consent" in low
    assert "github pr" in low and "never execute locally" in low
    assert "diff file" in low or "diff-file" in low


def test_skill_derives_recommendation_from_review_result_and_gates_posting_on_consent():
    low = SKILL.read_text().lower()
    # derived Approve/Comment/Request-changes recommendation from the deterministic review-result
    assert "recommendation" in low
    assert "request-changes" in low or "request changes" in low
    assert "review-result" in low
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
    # posting uses the DETERMINISTIC recommendation from review-result, not model prose
    assert "deterministic recommendation" in norm or ("review-result" in norm and "recommendation" in norm)


def test_skill_posting_uses_deterministic_review_result():
    # Scope to the Finish section: posting draws the recommendation + findings from `review-result`
    # (the deterministic projection), never from model prose.
    finish = _flat(_section(SKILL.read_text(), "Finish"))
    assert "review-result" in finish
    assert "recommendation" in finish
    assert "gh pr review" in finish


def test_skill_has_a_distinct_execution_safety_gate():
    low = _norm(SKILL)
    assert "execution safety gate" in low
    assert "after plan consensus" in low
    assert "exact commands" in low
    assert "arbitrary code" in low
    assert "fresh consent" in low


def test_skill_execution_gate_is_one_compound_if():
    # F2: the SKILL's trusted-local execution-safety proof must be ONE compound `if`/`&&` gate (not
    # bare commands a missing global `set -e` would ignore); only a passing gate may reach
    # consent/execution.
    _assert_execution_gate_is_compound(_section(SKILL.read_text(), "Execution Safety Gate"))


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


def test_command_uses_symmetric_commands_not_plain_verdict():
    low = _norm(CMD)
    assert "symmetric-verdict" in low
    assert "--workflow pr-review" in low
    assert "review-result" in low
    _no_plain_verdict(CMD.read_text())


# The docs-integration guards below are owned by the `docs` node (they assert the pr-review live docs
# are wired into the test_docs.py owner lists, the CHANGELOG, and the README/AGENTS/CLAUDE).


def test_pr_review_docs_are_covered_by_the_model_id_owner():
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
    text = (ROOT / "CHANGELOG.md").read_text().lower()
    assert "pr-review" in text or "pr review" in text


def test_all_docs_mention_the_third_skill():
    for rel in ("README.md", "AGENTS.md", "CLAUDE.md", ".codex/INSTALL.md",
                "docs/install/copilot-cli.md", "docs/install/claude-code.md",
                "docs/install/codex.md", "docs/cli.md", "CHANGELOG.md"):
        assert "pr-review" in (ROOT / rel).read_text().lower(), f"{rel} omits pr-review"
    # README Layout must list both the skill dir and the command entry point.
    readme = (ROOT / "README.md").read_text()
    assert "skills/pr-review/" in readme
    assert "commands/pr-review.md" in readme
