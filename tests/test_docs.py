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


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def _github_acquisition_block(text: str) -> str:
    """The single executable GitHub before/diff/after acquisition loop in `text`."""
    blocks = [b for b in _bash_blocks(text)
              if "normalize-target github" in b and "gh pr diff" in b]
    assert len(blocks) == 1, \
        "exactly one executable GitHub before/diff/after acquisition block is required"
    return blocks[0]


def _assert_github_acquisition_fails_closed(block: str) -> None:
    """The GitHub before/diff/after acquisition must fail CLOSED without a global `set -e` (Task 3,
    round 2): bounded 3-attempt retry, EACH gh read explicitly error-checked, `normalize-target` only
    after all three succeed, EVERY partial before/after/diff/target artifact discarded on any failure,
    a clear non-zero halt on exhaustion, and the metadata-drift retry preserved."""
    joined = re.sub(r"\\\n\s*", "", block)
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


def _assert_source_materialization_fails_closed(text: str, section_heading: str | None) -> None:
    """Both source-materialization kinds in `text` (optionally scoped to `section_heading`) fail
    closed (Task 3, round 3)."""
    scope = _section(text, section_heading) if section_heading else text
    blocks = _bash_blocks(scope)
    gh = [b for b in blocks if "materialize-target" in b and "gh api" in b]
    local = [b for b in blocks if "materialize-target" in b and "git -C" in b]
    assert len(gh) == 1 and len(local) == 1, \
        "exactly one GitHub and one local source-materialization block are required"
    _assert_block_fails_closed(gh[0], fetch=r"gh api", archive="source.tar.gz")
    _assert_block_fails_closed(local[0], fetch=r'git -C "\$LOCAL_REPO" archive', archive="source.tar")


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
    # echo requirement, legacy read-only) — a canonical phrase, not just the word "bound". The build
    # skill's single Critic verdict echoes the bindings; the two symmetric skills' SEPARATE peer
    # attestations echo them (no single serialized union verdict).
    for name in ("crucible", "deep-dive", "pr-review"):
        low = _flat((ROOT / "commands" / f"{name}.md").read_text())
        assert "gate decision is bound to the exact" in low, f"commands/{name}.md omits the binding handshake"
        assert "schema v2" in low, f"commands/{name}.md omits the schema-2 claim"
        assert "bindings" in low, f"commands/{name}.md omits the bindings"
        if name == "crucible":
            assert "verdict must echo" in low, f"commands/{name}.md omits the echo requirement"
        else:
            assert "peer attestation" in low and "echo" in low, \
                f"commands/{name}.md omits the peer-attestation echo requirement"
        assert "legacy / unverified" in low, f"commands/{name}.md omits the legacy behavior"


# --- Symmetric two-peer consensus documentation guards --------------------------------------------
# The deep-dive / pr-review companion skills settle each gate from TWO separately produced peer
# attestation files via `symmetric-verdict` (never the build-only `verdict`), assemble FINAL from
# `accepted-findings`, and emit the deterministic deliverable via `review-result`. The public docs
# must document those commands, the workflow-kind metadata, the derived pr recommendation, and the
# honest slot-proof scope (two configured slots attested, not cryptographic process identity).

def test_cli_docs_document_symmetric_workflow_commands():
    # Section-scoped canonical guard: docs/cli.md's symmetric section must document the two-peer
    # decision command (`symmetric-verdict --peer-a --peer-b`), `accepted-findings`, `review-result`
    # and its derived APPROVE|COMMENT|REQUEST_CHANGES recommendation, the CLEAN-vs-recommendation
    # separation, and the slot-proof scope. A doc that dropped a command or overclaimed process
    # identity must FAIL here.
    text = (ROOT / "docs" / "cli.md").read_text()
    sym = _flat(_section(text, "Symmetric workflows"))
    assert "symmetric-verdict" in sym
    assert "--peer-a" in sym and "--peer-b" in sym
    assert "accepted-findings" in sym
    assert "review-result" in sym
    assert "deep-dive" in sym and "pr-review" in sym
    assert "approve" in sym and "comment" in sym and "request_changes" in sym
    # workflow status (CLEAN/FLAGGED) is SEPARATE from the review recommendation
    assert "recommendation" in sym and ("separate" in sym or "distinct" in sym)
    # honest scope: proves two configured slots attested, not cryptographic process identity
    assert "two configured slots" in sym
    assert "cryptograph" in sym


def test_cli_docs_document_workflow_kind_metadata():
    text = _flat((ROOT / "docs" / "cli.md").read_text())
    # init-run records an immutable workflow kind; the symmetric decision command is symmetric-only
    assert "--workflow" in text
    assert "build" in text and "deep-dive" in text and "pr-review" in text
    assert "symmetric-verdict" in text


def test_readme_documents_symmetric_two_peer_protocol():
    # The README companion sections must describe the TWO-PEER attestation protocol (separate peer
    # files settled by `symmetric-verdict`), not a single serialized union verdict.
    readme = _flat((ROOT / "README.md").read_text())
    assert "symmetric-verdict" in readme
    assert "peer-a.json" in readme or "peer attestation" in readme or \
        ("peer a" in readme and "peer b" in readme)
    # the OLD single-serialized-union verdict language must be gone
    raw = (ROOT / "README.md").read_text().lower()
    assert "recorded verdict is the union" not in raw
    assert "the union of their findings" not in raw


def test_security_documents_slot_proof_scope():
    # SECURITY must scope the peer proof honestly: two configured SLOTS attested to the same bound
    # candidate — not cryptographic proof that two distinct model processes ran.
    low = _flat((ROOT / "SECURITY.md").read_text())
    assert "two configured slots" in low
    assert "cryptograph" in low
    assert "not a cryptographic proof" in low or "does not cryptographically prove" in low
    assert "process" in low


def test_changelog_records_symmetric_two_peer():
    # Scope to [Unreleased]: the symmetric two-peer migration is recorded with the new commands.
    unreleased = _flat(_section((ROOT / "CHANGELOG.md").read_text(), "[Unreleased]"))
    assert "symmetric-verdict" in unreleased
    assert "two" in unreleased and "peer" in unreleased
    assert "accepted-findings" in unreleased and "review-result" in unreleased


def test_changelog_pr_review_entry_states_cli_extended_not_unmodified():
    # Scope to the [Unreleased] pr-review skill bullet: it must NOT claim the CLI was "unmodified" /
    # that there was "no CLI change" — the symmetric skills added the `symmetric-verdict` /
    # `accepted-findings` / `review-result` commands and the `--workflow` run metadata. It must state
    # the accurate scope (no CONFIG-SCHEMA change, but the CLI gained that workflow metadata + those
    # commands), positively, not merely ban a phrase. The historical release entries are out of scope.
    unreleased = _section((ROOT / "CHANGELOG.md").read_text(), "[Unreleased]")
    bullet = _flat(_bullet(unreleased, "New independent `pr-review` skill"))
    # accurate positive wording (implemented reality)
    assert "no config-schema change" in bullet
    assert "symmetric-verdict" in bullet
    assert "accepted-findings" in bullet
    assert "review-result" in bullet
    assert "workflow metadata" in bullet or "--workflow" in bullet
    # the false "unmodified CLI / no CLI change" claim must be gone from this live entry
    assert "unmodified" not in bullet
    assert "no cli/config-schema change" not in bullet
    assert "no cli change" not in bullet


def test_symmetric_design_marked_implemented_and_links_plan():
    design = (ROOT / "docs" / "superpowers" / "specs"
              / "2026-07-20-symmetric-consensus-design.md").read_text()
    assert re.search(r"\*\*Status:\*\*\s*implemented", design, re.IGNORECASE), \
        "the 2026-07-20 symmetric-consensus design must be marked implemented"
    assert "2026-07-20-symmetric-consensus.md" in design, "design must link its implementation plan"


# --- pr-review target-binding documentation guards ------------------------------------------------
# Finding #4: every pr-review input is pinned to an immutable review target (base/head OIDs, local
# merge-base, or patch-only), bound into every gate, materialized into a pinned static source
# snapshot, and executed only at the recorded head. The public docs must document the target commands,
# manifest variants, provenance, and safe-source/exact-head execution — with command/schema shape.

def test_cli_docs_document_target_commands_and_manifest_variants():
    text = (ROOT / "docs" / "cli.md").read_text()
    sec = _flat(_section(text, "Target binding"))
    for cmd in ("normalize-target", "load-target", "show-target",
                "materialize-target", "repository-identity"):
        assert cmd in sec, f"docs/cli.md Target-binding section omits {cmd}"
    # the three manifest kinds
    assert "github-pr" in sec and "local-range" in sec and "diff-file" in sec
    # immutable identity bound into every gate + merge-base local semantics + patch-only diff-file
    assert "target_sha256" in sec
    assert "merge-base" in sec or "merge_base" in sec
    assert "revision_bound" in sec or "revision-unbound" in sec or "patch identity" in sec
    # safe, one-shot source materialization into a pinned snapshot
    assert "run/source" in sec or "source snapshot" in sec


def test_readme_documents_immutable_target_provenance():
    sec = _flat(_section((ROOT / "README.md").read_text(), "Companion skill: `pr-review`"))
    assert "target" in sec
    assert "immutable" in sec or "pinned" in sec
    # base/head commit identity (not branch names) + local merge-base + patch-only diff-file
    assert "baserefoid" in sec or "head sha" in sec or "base/head" in sec or "commit" in sec
    assert "merge-base" in sec or "merge base" in sec
    assert "diff-file" in sec or "diff file" in sec


def test_security_documents_pinned_source_and_exact_head_execution():
    bullet = _flat(_bullet((ROOT / "SECURITY.md").read_text(), "Pinned review target"))
    # a static, read-only source snapshot that is never executed, extracted via a confined archive path
    assert "snapshot" in bullet
    assert "archive" in bullet
    assert "traversal" in bullet or "symlink" in bullet
    assert "never execute" in bullet or "not executed" in bullet or "never executed" in bullet
    # trusted-local execution runs only at the recorded head commit
    assert "recorded head" in bullet or "head commit" in bullet or "exact head" in bullet


def test_changelog_records_target_binding_finding4():
    unreleased = _flat(_section((ROOT / "CHANGELOG.md").read_text(), "[Unreleased]"))
    assert "normalize-target" in unreleased
    assert "load-target" in unreleased
    assert "materialize-target" in unreleased
    assert "target_sha256" in unreleased
    assert "merge-base" in unreleased or "merge base" in unreleased


def test_target_binding_design_marked_implemented_and_links_plan():
    design = (ROOT / "docs" / "superpowers" / "specs"
              / "2026-07-21-pr-review-target-binding-design.md").read_text()
    assert re.search(r"\*\*Status:\*\*\s*implemented", design, re.IGNORECASE), \
        "the 2026-07-21 pr-review-target-binding design must be marked implemented"
    assert "2026-07-21-pr-review-target-binding.md" in design, "design must link its implementation plan"


def test_target_binding_design_source_snapshots_are_executable_per_kind():
    # Finding (Task 3, round 1): the design's source-snapshot commands must be executable and separate —
    # GitHub parses the authoritative head repository/SHA and materializes source.tar.gz; local verifies
    # the recorded identity, archives the exact head via an explicit `git -C "$LOCAL_REPO"` (never
    # ambient), and materializes source.tar. No shared command with unset head variables or a mismatched
    # archive path.
    design = (ROOT / "docs" / "superpowers" / "specs"
              / "2026-07-21-pr-review-target-binding-design.md").read_text()
    snapshots = _section(design, "Source snapshots")
    gh_sec = _section(snapshots, "GitHub PR")
    local_sec = _section(snapshots, "Local range")
    gh_blocks, local_blocks = _bash_blocks(gh_sec), _bash_blocks(local_sec)
    assert len(gh_blocks) == 1 and len(local_blocks) == 1, \
        "each source-snapshot kind documents exactly one executable command block"
    gh, local = gh_blocks[0], local_blocks[0]

    # GitHub: authoritative parse -> require non-empty -> codeload tarball -> materialize source.tar.gz.
    assert "loaded-target.json" in gh, "GitHub design block must read the authoritative loaded manifest"
    assert '["head"]["repository"]' in gh and '["head"]["sha"]' in gh
    assert 'test -n "$HEAD_REPOSITORY"' in gh and 'test -n "$HEAD_SHA"' in gh
    assert 'gh api "repos/$HEAD_REPOSITORY/tarball/$HEAD_SHA" > "$RUN/source.tar.gz"' in gh
    assert '--archive "$RUN/source.tar.gz"' in gh
    assert not re.search(r"git\s+archive", gh_sec), "GitHub design section must not archive with git"

    # Local: recorded-identity check BEFORE the archive -> explicit `git -C` -> materialize source.tar.
    assert "loaded-target.json" in local, "local design block must read the authoritative loaded manifest"
    assert '["repository"]' in local and '["head"]["sha"]' in local
    assert 'test -n "$HEAD_SHA"' in local
    assert 'repository-identity --repo "$LOCAL_REPO"' in local
    assert local.index("repository-identity") < local.index("git -C"), \
        "the recorded-identity check must precede the archive"
    assert re.search(
        r'git -C "\$LOCAL_REPO" archive --format=tar --output "\$RUN/source.tar" "\$HEAD_SHA"', local), \
        'local design block must archive via `git -C "$LOCAL_REPO" archive` (never ambient)'
    assert '--archive "$RUN/source.tar"' in local
    assert "source.tar.gz" not in local, "local path must not feed the GitHub source.tar.gz to materialize"


TARGET_BINDING_PLAN = (ROOT / "docs" / "superpowers" / "plans"
                       / "2026-07-21-pr-review-target-binding.md")
TARGET_BINDING_DESIGN = (ROOT / "docs" / "superpowers" / "specs"
                         / "2026-07-21-pr-review-target-binding-design.md")


def test_target_binding_plan_github_acquisition_fails_closed():
    # Finding (Task 3, round 2): the plan's Step-3 `gh pr view`/`gh pr diff` reads were unchecked, so a
    # failed command left an empty/truncated artifact that stable before/after metadata still let
    # `normalize-target` hash into a target. The plan's executable loop must fail closed.
    _assert_github_acquisition_fails_closed(
        _github_acquisition_block(TARGET_BINDING_PLAN.read_text()))


def test_target_binding_design_github_acquisition_is_fail_closed():
    # Finding (Task 3, round 2): the design must state the fail-closed acquisition contract — each of the
    # three `gh` reads is checked (not only metadata drift), any failure discards the partial artifacts,
    # retries are bounded, exhaustion halts, and normalize runs only after all three succeed — because
    # `normalize-target` will hash whatever (possibly empty/truncated) diff bytes it is handed.
    design = TARGET_BINDING_DESIGN.read_text()
    errors = _flat(_section(design, "Error handling"))
    assert "gh pr view" in errors and "gh pr diff" in errors, \
        "error handling must scope the fail-closed rule to the gh acquisition commands"
    assert "empty" in errors or "truncat" in errors, \
        "error handling must name the empty/truncated-artifact hazard"
    assert "all three" in errors or "each" in errors, \
        "error handling must require every gh read to succeed, not only stable metadata"
    assert "discard" in errors or "clean" in errors or "remove" in errors, \
        "error handling must discard partial artifacts on failure"
    assert "set -e" in errors, "error handling must warn against relying on a global set -e"
    assert re.search(r"3 attempt|three attempt|bounded", errors), \
        "error handling must bound the retries"
    assert "halt" in errors, "error handling must halt clearly once attempts are exhausted"

    variant = _flat(_section(design, "GitHub PR variant"))
    assert "fail" in variant and "closed" in variant, \
        "the GitHub PR variant prose must state the acquisition fails closed"


def test_target_binding_design_source_materialization_fails_closed():
    # Finding (Task 3, round 3): the design's source-snapshot blocks ran `gh api ... > source.tar.gz` and
    # `git -C ... archive` UNCHECKED, so a failed/truncated/stale archive relied on later/global shell
    # behaviour. Each kind must fail closed and non-fatal.
    _assert_source_materialization_fails_closed(TARGET_BINDING_DESIGN.read_text(), "Source snapshots")


def test_target_binding_design_source_snapshot_errors_fail_closed():
    # The Error-handling contract must state the source-snapshot fail-closed rule: remove the stale
    # archive, explicitly check the fetch/archive AND materialize, discard the partial archive on
    # failure, don't rely on a global `set -e`, and continue patch-only (non-fatal) with SOURCE_AVAILABLE
    # marked no so both peers get the same status and runtime claims stay unverified.
    errors = _flat(_section(TARGET_BINDING_DESIGN.read_text(), "Error handling"))
    assert "source_available=no" in errors, \
        "error handling must mark the source unavailable (SOURCE_AVAILABLE=no) on any snapshot failure"
    assert "gh api" in errors and "archive" in errors, \
        "error handling must scope the fail-closed rule to the fetch/archive commands"
    assert "materialize" in errors, "error handling must require materialize-target to be checked too"
    assert "discard" in errors or "remove" in errors or "rm -f" in errors, \
        "error handling must discard the partial/stale archive on failure"
    assert "set -e" in errors, "error handling must warn against relying on a global set -e"
    assert "patch-only" in errors or "patch only" in errors, \
        "on a source failure the review must continue patch-only (non-fatal)"
    assert "unverified" in errors, \
        "when source is unavailable, runtime-verified claims must be marked unverified"


def test_target_binding_plan_source_materialization_fails_closed():
    # Finding (Task 3, round 3): the plan's Step-3 materialization ran the fetch/archive + materialize
    # unchecked. The plan's executable blocks must mirror the fail-closed, non-fatal flow.
    _assert_source_materialization_fails_closed(TARGET_BINDING_PLAN.read_text(), None)



# --- Companion runtime-guidance guards (docs/cli.md Conventions, AGENTS.md, CLAUDE.md) -------------
# The three top-level runtime-guidance surfaces must teach the CURRENT symmetric two-peer protocol —
# `--workflow` init, separate Peer A / Peer B attestation files settled by `symmetric-verdict`,
# `accepted-findings` before FINAL, the Finish `review-result` (with pr-review's deterministic
# recommendation + preserved execution/posting safety), and the honest slot-proof scope — and must NOT
# teach the superseded single-union / merged-set verdict. Section-scoped so a stale sentence in a live
# surface fails HERE even though docs/cli.md's detailed "Symmetric workflows" section is already
# correct; these guards exist so a future protocol migration cannot miss the companion summaries.

STALE_SYMMETRIC_MECHANISM = (
    "verdict is the union",
    "union of their findings",
    "union of both peers",
    "review the merged set",
    "merged set",
)


def _assert_no_stale_union(scope: str, where: str):
    for phrase in STALE_SYMMETRIC_MECHANISM:
        assert phrase not in scope, f"{where} still teaches the superseded {phrase!r} mechanism"


def test_cli_docs_conventions_gate_bullet_uses_two_peer_protocol():
    # The Conventions "Gates" bullet must route each symmetric round through `symmetric-verdict` from
    # two separate peer attestations (not a single union verdict), while keeping the pr-review
    # execution-safety scope in the same bullet.
    bullet = _flat(_bullet((ROOT / "docs" / "cli.md").read_text(), "**Gates**"))
    assert "symmetric-verdict" in bullet
    assert "peer attestation" in bullet
    assert "not a single union verdict" in bullet
    assert "static/ci-only" in bullet
    assert "consent" in bullet
    _assert_no_stale_union(bullet, "docs/cli.md Conventions Gates bullet")


@pytest.mark.parametrize("doc", ["AGENTS.md", "CLAUDE.md"])
def test_companion_deepdive_section_uses_two_peer_protocol(doc):
    # Required implemented commands/terms (not merely an absent phrase): workflow-kind init, the
    # symmetric decision command, separate Peer A / Peer B attestations, the FINAL assembly and Finish
    # deliverable, the honest slot-proof scope, and an explicit negation of the single-union verdict.
    section = _flat(_section((ROOT / doc).read_text(), "Companion skill: deep-dive"))
    assert "--workflow deep-dive" in section
    assert "symmetric-verdict" in section
    assert "peer a" in section and "peer b" in section
    assert "attestation" in section
    assert "accepted-findings" in section
    assert "review-result" in section
    assert "two configured slots" in section
    assert "cryptograph" in section and "process" in section
    assert "no single serialized union verdict" in section
    _assert_no_stale_union(section, f"{doc} deep-dive companion section")


@pytest.mark.parametrize("doc", ["AGENTS.md", "CLAUDE.md"])
def test_companion_prreview_section_uses_two_peer_protocol(doc):
    # pr-review companion summary: same symmetric two-peer decision command, a DETERMINISTIC derived
    # recommendation from `review-result`, and the preserved execution + posting safety scope.
    section = _flat(_section((ROOT / doc).read_text(), "Companion skill: pr-review"))
    assert "--workflow pr-review" in section
    assert "symmetric-verdict" in section
    assert "review-result" in section
    assert "deterministic" in section and "recommendation" in section
    assert "approve/comment/request-changes" in section
    assert "static/ci-only" in section
    assert "consent" in section
    assert "posting" in section
    _assert_no_stale_union(section, f"{doc} pr-review companion section")


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
