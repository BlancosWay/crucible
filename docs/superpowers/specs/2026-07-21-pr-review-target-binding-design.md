# PR Review Target Binding Design

**Status:** Implemented
**Date:** 2026-07-21
**Finding:** Audit finding #4 - PR/local input normalization does not establish the reviewed revision
**Companion plan:** [`docs/superpowers/plans/2026-07-21-pr-review-target-binding.md`](../plans/2026-07-21-pr-review-target-binding.md)

> **Implemented (2026-07-21).** Shipped in `scripts/crucible/target.py` (schema, normalization,
> identity, confined materialization), the `normalize-target` / `repository-identity` / `load-target` /
> `show-target` / `materialize-target` CLI commands, `target_sha256` on every pr-review binding and
> peer attestation, the report `## Review target` section, and the migrated `pr-review` skill/reference/
> command/public docs. The command shapes below are the **as-built** interface: GitHub normalization
> takes stable **before/after** metadata (`--metadata-before` / `--metadata-after`), local normalization
> takes a single `--range`, archive identity is read from `show-target`, materialization is one-shot
> immediately after `load-target`, and every pr-review mutation/certification command shares one
> loaded-target guard. Guarded by `tests/test_target.py` and the skill/reference/docs guards.

## Problem

`pr-review` currently normalizes a target into `diff`, `changed-files`, and `intent`, but it does not
establish immutable source identity:

- GitHub PR normalization records branch names and a patch, not base/head commit SHAs or fork
  repository identity.
- Peers may read "surrounding real code" from the ambient checkout, which can be a different revision
  or lack files introduced by the PR.
- Local ranges use two-dot tip comparison, so unrelated base-only commits appear as reverse changes.
- Trusted-local execution is consented, but the protocol does not prove the executing checkout is the
  reviewed head commit.
- Existing schema-v2 bindings identify the candidate, DAG, and node, not the review target.

The result can be internally consistent while its evidence, citations, or execution refer to code
other than the submitted change.

## Goals

1. Resolve every `pr-review` target to deterministic identity before PLAN.
2. Bind that identity into every PLAN/thread/FINAL peer attestation and terminal.
3. Use PR-style merge-base semantics for local ranges.
4. Give static GitHub PR reviews a searchable snapshot of the exact head commit without executing it.
5. Require trusted-local execution to run at the recorded head commit.
6. Make reports prove the target repository/revision or explicitly state that a diff file is
   revision-unbound.
7. Add behavioral tests, including a real divergent temporary Git repository.

## Non-goals

- Do not execute GitHub PR or diff-file targets.
- Do not claim a patch file proves repository revision identity.
- Do not add target binding to `build` or `deep-dive` runs.
- Do not cryptographically prove a remote repository served honest content.
- Do not make the Crucible CLI fetch GitHub data; acquisition remains in the `pr-review` orchestrator.

## Approaches considered

### A. Engine-bound target manifest and pinned source archive - selected

Persist one validated target manifest, hash it into every pr-review gate binding, and materialize a
read-only source archive at the recorded head SHA.

This is the only approach that lets deterministic state prove all gates reviewed the same target.

### B. Skill-only normalization

Resolve SHAs and use three-dot diffs in the skill, then include the target in PLAN prose.

This is smaller, but later gates can still attest to candidates without a target binding. The defect
would remain orchestration-enforced rather than engine-enforced.

### C. Detached Git worktree for every GitHub PR

Fetch and check out the PR head locally.

This is convenient for search, but checkout and configured filters complicate the static-only trust
boundary. A GitHub-generated archive provides the needed source snapshot without a checkout.

## Target manifest

The orchestrator prepares `RUN/target.json` and `RUN/target.diff` through the deterministic CLI,
then loads them:

```bash
PYTHONPATH=scripts python3 -m crucible normalize-target ... \
  --output "$RUN/target.json" --diff-output "$RUN/target.diff"
PYTHONPATH=scripts python3 -m crucible load-target \
  --run "$RUN" --file "$RUN/target.json" --diff "$RUN/target.diff"
```

`load-target` validates the manifest and exact patch bytes, canonicalizes the manifest, appends one
`target_loaded` event containing the full canonical payload plus `target_sha256`, and writes the
canonical payload back to `RUN/target.json`.

The run-log event is authoritative. The file is a convenient scratch copy.

### Common fields

```json
{
  "version": 1,
  "kind": "github-pr",
  "revision_bound": true,
  "diff_sha256": "<64 lowercase hex>",
  "changed_files": ["path/to/file.py"],
  "intent": {
    "title": "Short intent",
    "body": "Full user/PR intent"
  }
}
```

Rules:

- unknown fields reject;
- `version` is exactly `1`;
- `changed_files` contains unique, non-empty repository-relative POSIX paths in deterministic order;
- intent fields are strings and remain untrusted report data;
- `diff_sha256` must match the exact bytes passed with `--diff`;
- only one target may be loaded; correction requires a fresh run;
- target loading must happen before `load-dag`, PLAN output, or any review protocol event.

### GitHub PR variant

```json
{
  "version": 1,
  "kind": "github-pr",
  "revision_bound": true,
  "repository": "base-owner/base-repo",
  "pr_number": 123,
  "url": "https://github.com/base-owner/base-repo/pull/123",
  "base": {
    "repository": "base-owner/base-repo",
    "ref": "main",
    "sha": "<40 lowercase hex>"
  },
  "head": {
    "repository": "fork-owner/base-repo",
    "ref": "feature",
    "sha": "<40 lowercase hex>"
  },
  "is_cross_repository": true,
  "diff_sha256": "<64 lowercase hex>",
  "changed_files": ["src/a.py"],
  "intent": {"title": "...", "body": "..."}
}
```

The orchestrator resolves these values with `gh pr view` fields including `baseRefOid`,
`headRefOid`, `headRepository`, `headRepositoryOwner`, and `isCrossRepository`, read **before and
after** `gh pr diff`; a change in any immutable identity field between the two reads rejects the target
so the whole acquisition can be retried. Branch names are display metadata; SHAs and repository
identities are authoritative, and intent comes from the stable before/after title/body. Because
`normalize-target` hashes whatever diff bytes it is handed, the acquisition must fail **closed** (see
[Error handling](#error-handling)): each `gh` read is error-checked and normalization proceeds only
after all three succeed, so a failed `gh pr diff` can never feed an empty/truncated patch that stable
metadata would still normalize.

### Local range variant

```json
{
  "version": 1,
  "kind": "local-range",
  "revision_bound": true,
  "repository": "https://github.com/owner/repo.git",
  "base": {"ref": "main", "sha": "<40 lowercase hex>"},
  "head": {"ref": "feature", "sha": "<40 lowercase hex>"},
  "merge_base_sha": "<40 lowercase hex>",
  "diff_sha256": "<64 lowercase hex>",
  "changed_files": ["src/a.py"],
  "intent": {"title": "Local range review", "body": "..."}
}
```

Repository identity must never persist credentials from a remote URL. Normalize a public host/path
with userinfo, query, and fragment removed; if no safe remote identity exists, use
`local:<sha256(real repository path)>`. Reports do not expose the local path.

`normalize-target local` takes a single `--range` (`base..head` or `base...head`, both normalized
identically as two ref names) and resolves, with argument-vector `git` subprocesses (never
`shell=True`):

```bash
BASE_SHA=$(git rev-parse --verify "$BASE^{commit}")
HEAD_SHA=$(git rev-parse --verify "$HEAD^{commit}")
MERGE_BASE_SHA=$(git merge-base "$BASE_SHA" "$HEAD_SHA")
git diff --binary "$MERGE_BASE_SHA..$HEAD_SHA"
git diff --name-only "$MERGE_BASE_SHA..$HEAD_SHA"
```

The generated review set therefore always matches PR-style three-dot semantics; intent comes from
`--intent`, not commit messages.

### Diff-file variant

```json
{
  "version": 1,
  "kind": "diff-file",
  "revision_bound": false,
  "repository": null,
  "diff_sha256": "<64 lowercase hex>",
  "changed_files": ["src/a.py"],
  "intent": {"title": "Patch review", "body": "..."}
}
```

A diff file proves patch bytes only. Peers must not read ambient repository files as if they were
bound context. The report states `Revision: unbound (patch identity only)`.

## Engine integration

### Target owner

Add `scripts/crucible/target.py` as the single owner of:

- `ReviewTarget` schema parsing and canonical serialization;
- exact diff verification;
- `target_sha256`;
- `target_from_events(events)`;
- target event integrity checks.

### CLI

Add:

```bash
crucible normalize-target github --metadata-before PR-BEFORE.json --metadata-after PR-AFTER.json \
  --diff PR.diff --output TARGET.json --diff-output TARGET.diff
crucible normalize-target local --repo REPO --range BASE..HEAD --intent INTENT.json \
  --output TARGET.json --diff-output TARGET.diff
crucible normalize-target diff --diff PATCH --intent INTENT.json \
  --output TARGET.json --diff-output TARGET.diff
crucible repository-identity --repo REPO
crucible load-target --run RUN --file TARGET.json --diff TARGET.diff
crucible show-target --run RUN
crucible materialize-target --run RUN --archive SOURCE.tar.gz
```

`normalize-target` does not mutate a run:

- `github` consumes the exact JSON emitted by the documented `gh pr view --json ...` command read
  **before and after** `gh pr diff`; every immutable identity field must match between the two reads
  or the target is rejected so the orchestrator can retry;
- `local` takes one `--range BASE..HEAD|BASE...HEAD` (never separate base/head flags), resolves refs
  and merge base with argument-vector `git` subprocesses (never `shell=True`), and emits the
  merge-base-to-head patch;
- `diff` hashes the supplied patch and marks it revision-unbound.

`repository-identity --repo` prints the credential-free identity local normalization records, and
`show-target` prints the authoritative loaded target (from the `target_loaded` event, never the
scratch file). These make normalization and identity comparison executable and directly testable
rather than prose-only.

The command is valid only for schema-v2 `pr-review` runs and rejects:

- missing, malformed, or duplicate targets;
- use after DAG/PLAN/review work begins;
- manifest/diff hash disagreement;
- target events in `build` or `deep-dive` runs.

For `pr-review`, `load-dag`, `log`, `bindings`, verdict/result commands, and approval share **one**
loaded-target guard (`_require_pr_review_target`): each requires a single valid loaded target. Read-only
report/status commands remain usable and render the run `INVALID` or `IN PROGRESS` rather than crashing.
Source-materialization integrity is validated centrally (`validate_source_materialization`) so a
missing/duplicate/late/wrong-kind/hash-mismatched `source_materialized` fails the run closed, and
`scripts/check.py` runs its own repo checks under an **isolated Git environment** so target-normalization
`git` subprocesses in tests cannot mutate the developer's branch or index.

`materialize-target` uses a confined Python archive reader. It rejects absolute paths, `..` path
escapes, symlinks, hard links, devices, FIFOs, duplicate normalized paths, more than 100,000 members,
more than 1 GiB of declared regular-file data, and any member that would resolve outside
`RUN/source`; it extracts regular files/directories only. It never invokes repository code or
archive-provided helpers. An unsafe archive leaves the review patch-only with source context marked
unavailable.

### Bindings

Extend schema-v2 binding keys with optional `target_sha256`.

For every `pr-review` PLAN/thread/FINAL binding:

```json
{
  "artifact_sha256": "...",
  "dag_sha256": "...",
  "node_sha256": "...",
  "target_sha256": "..."
}
```

`node_sha256` remains gate-specific. `target_sha256` is required for every pr-review gate and absent
for `build` and `deep-dive`.

Extend:

- `BindingSet`;
- `event_bindings`;
- `PeerAttestation`;
- peer raw/parsed fidelity and outer binding validation;
- terminal and accepted-set binding validation.

Existing generic exact-binding comparisons then enforce target consistency without a second
recommendation or consensus path.

### Workflow and result validation

`workflow_issues` reports:

- `missing` when a newly initialized pr-review run has not loaded its target and no DAG/PLAN work has
  started;
- `invalid` when DAG/PLAN/review work exists without a target;
- a target is malformed, duplicated, or loaded late;
- a build/deep-dive run records a target;
- any pr-review protocol event lacks or mismatches `target_sha256`.

Finish-time result commands reject the same states. A target cannot be replaced after acceptance
because the append-only run-log permits only one `target_loaded` event.

### Report

For `pr-review`, render a `## Review target` section before the workflow summary:

- GitHub PR: URL, base repository/ref/SHA, head repository/ref/SHA, cross-repository flag, patch hash;
- local range: repository identity, base/head/merge-base SHAs, patch hash;
- diff file: patch hash and explicit revision-unbound status.

All untrusted labels and intent text use existing sanitization.

## Source snapshots

Materialization is **one-shot**, immediately after `load-target` and before any DAG/PLAN/review work,
into the **absent** `RUN/source` (the CLI refuses a pre-existing destination). The head repository/SHA
come **only** from `show-target`'s authoritative event payload — never an ambient archive variable —
and wrapper stripping is derived from the target *kind*, not a caller flag. Materialization **fails
closed and is non-fatal**: any stale archive is removed first, `SOURCE_AVAILABLE` defaults to `no`, the
fetch/archive **and** `materialize-target` are each explicitly checked (never relying on a global
`set -e`), any failure discards the partial archive and leaves `SOURCE_AVAILABLE=no`, and only a clean
fetch **and** materialize set `SOURCE_AVAILABLE=yes`. Both peers receive the identical `SOURCE_AVAILABLE`
status; when it is `no` the review stays patch-only and runtime-verified claims are treated as
unverified (see [Error handling](#error-handling)).

### GitHub PR

After loading the target, emit the authoritative loaded manifest, read the head repository/SHA from it
(never an ambient variable), require them non-empty, remove any stale archive, then fetch the GitHub
codeload archive for that exact head and materialize it — each step explicitly checked:

```bash
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN/loaded-target.json"
HEAD_REPOSITORY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["repository"])' "$RUN/loaded-target.json")
HEAD_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' "$RUN/loaded-target.json")
test -n "$HEAD_REPOSITORY" && test -n "$HEAD_SHA"
rm -f "$RUN/source.tar.gz"
SOURCE_AVAILABLE=no
if gh api "repos/$HEAD_REPOSITORY/tarball/$HEAD_SHA" > "$RUN/source.tar.gz"
then
  if PYTHONPATH=scripts python3 -m crucible materialize-target \
    --run "$RUN" --archive "$RUN/source.tar.gz"
  then
    SOURCE_AVAILABLE=yes
  else
    rm -f "$RUN/source.tar.gz"
  fi
else
  rm -f "$RUN/source.tar.gz"
fi
```

Peers read/search `RUN/source` plus `RUN/target.diff`. They never execute files from this snapshot.
Fork heads use `head.repository` from the manifest. Unsafe archive members are never materialized.

### Local range

Static review uses an archive of the exact local head commit under `RUN/source`, not ambient files.
Read the recorded repository identity + head SHA from the authoritative manifest, then gate the archive
on **one** explicit compound `if`: the parses are non-empty, `$LOCAL_REPO` is a directory whose
`repository-identity` **equals** the recorded identity, and its `git rev-parse HEAD` **equals** the
recorded head SHA — every check `&&`-joined so a mismatch **short-circuits past** the archive (the
snippets do not rely on a global `set -e`). Only then does an explicit `git -C "$LOCAL_REPO" archive`
run (never ambient git; never `rev-parse`/archive in an unrelated repo when `$LOCAL_REPO` is missing)
and materialize the uncompressed tar; any parse/identity/head/archive failure removes `source.tar`,
keeps `SOURCE_AVAILABLE=no`, and never invokes materialize:

```bash
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN/loaded-target.json"
RECORDED_REPOSITORY_IDENTITY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$RUN/loaded-target.json")
HEAD_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' "$RUN/loaded-target.json")
OBSERVED_REPOSITORY=
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$HEAD_SHA" && test -d "$LOCAL_REPO"
then
  OBSERVED_REPOSITORY=$(PYTHONPATH=scripts python3 -m crucible repository-identity --repo "$LOCAL_REPO")
fi
rm -f "$RUN/source.tar"
SOURCE_AVAILABLE=no
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$HEAD_SHA" && test -d "$LOCAL_REPO" \
  && test "$OBSERVED_REPOSITORY" = "$RECORDED_REPOSITORY_IDENTITY" \
  && test "$(git -C "$LOCAL_REPO" rev-parse HEAD)" = "$HEAD_SHA" \
  && git -C "$LOCAL_REPO" archive --format=tar --output "$RUN/source.tar" "$HEAD_SHA"
then
  if PYTHONPATH=scripts python3 -m crucible materialize-target \
    --run "$RUN" --archive "$RUN/source.tar"
  then
    SOURCE_AVAILABLE=yes
  else
    rm -f "$RUN/source.tar"
  fi
else
  rm -f "$RUN/source.tar"
fi
```

### Diff file

No source snapshot is inferred: a diff-file target sets `SOURCE_AVAILABLE=no` and never archives or
materializes. Review remains patch-only unless the user supplies a trusted local range, which becomes a
separate `local-range` target.

## Trusted-local execution

Execution remains available only for `local-range`.

Before showing any command for consent (after PLAN consensus):

1. verify the source repository is the manifest repository with
   `crucible repository-identity --repo REPO` equal to the recorded `repository` identity;
2. verify the execution checkout is clean (`git status --porcelain` empty);
3. verify `git rev-parse HEAD == target.head.sha`;
4. otherwise refuse execution and offer static-only continuation or an exact detached
   worktree-at-SHA command set followed by fresh exact-command consent;
5. record the observed repository identity, head SHA, clean status, and approved command list in the
   execution evidence handed to both peers and rendered in the review provenance.

Exact-command consent (and its arbitrary-code warning) stays separate from posting consent. GitHub PR
and diff-file targets remain non-executable regardless of archive availability.

## Error handling

- PR metadata without immutable SHAs/repository identity: halt normalization.
- GitHub before/diff/after acquisition fails **closed**, never relying on a global `set -e`: each of
  the three `gh pr view`/`gh pr diff` reads is explicitly error-checked and `normalize-target` runs
  only after **all three** succeed. A failed read leaves an **empty or truncated** artifact that stable
  before/after metadata would otherwise let `normalize-target` hash into a target, so any failure — a
  non-zero `gh` read or a drifted identity field — discards every partial before/after/diff/target
  artifact and retries, bounded to three attempts and halting clearly on exhaustion.
- Source snapshot unavailable or unsafe — a failed/truncated/stale `gh api` fetch or `git -C ... archive`,
  or a rejected `materialize-target`: materialization fails **closed** and **non-fatal**, never relying on
  a global `set -e`. Remove any stale archive before the fetch/archive; explicitly check the fetch/archive
  **and** `materialize-target`; on any failure discard (`rm -f`) the partial archive and leave
  `SOURCE_AVAILABLE=no`; only a clean fetch and materialize set `SOURCE_AVAILABLE=yes`. When
  `SOURCE_AVAILABLE=no`, the review continues **patch-only** with source context unavailable: do not read
  ambient files, do not pre-create/read a partial `RUN/source`, hand both peers the identical status, and
  treat runtime-verified claims as **unverified**.
- Local source identity/head gate: for the `local-range` path the recorded-identity equality
  (`repository-identity --repo "$LOCAL_REPO"` equal to the recorded `repository`) and the recorded-HEAD
  equality (`git -C "$LOCAL_REPO" rev-parse HEAD` equal to `head.sha`) are **checked conditions in the
  same `if`** that gates the archive — all `&&`-joined and computed against a validated `$LOCAL_REPO`
  (a directory), **never bare commands** that a missing global `set -e` would ignore, and **never**
  running `git rev-parse`/`archive` in an unrelated/ambient repo when `$LOCAL_REPO` is missing. Any
  parse/identity/head mismatch **short-circuits** past the archive, keeps `SOURCE_AVAILABLE=no`, never
  invokes `materialize-target`, and continues **patch-only** with source unavailable.
- Local ref missing or ambiguous: reject before `load-target`.
- Local base/head have no merge base: reject.
- Manifest/path/hash mismatch: reject without appending.
- Target changed after load: start a fresh run.
- Diff file with no bound revision: never claim source-level context or runtime verification.

No broad catch or success-shaped fallback is introduced.

## Testing

### Engine tests

- manifest variants parse and reject malformed/unknown fields;
- exact patch hash is validated;
- target load is pr-review-only, exactly once, and before DAG/PLAN work;
- archive extraction rejects path traversal, links, and special files;
- `target_sha256` appears on every pr-review gate binding and peer attestation;
- missing/mismatched target bindings fail before append;
- workflow/report/results fail closed on absent, duplicate, late, or corrupt target state;
- build/deep-dive bindings remain byte-for-shape compatible.

### Behavioral normalization tests

Create a temporary Git repository where `main` and `feature` diverge:

- two-dot diff includes base-only reverse changes;
- normalized merge-base-to-head diff includes only feature changes;
- input spelled with two or three dots resolves to the same manifest and patch.

Add fixtures for cross-repository GitHub metadata and assert fork head repository/SHA are preserved.

An end-to-end **documented-path** test drives the divergent fixture through the real CLI (`init-run`
→ `normalize-target local --range` → `load-target` → `load-dag` → `log` plan → `bindings` → `report`)
and asserts the manifest base/head/merge-base SHAs, a feature-only patch, `target_sha256` present in
the PLAN bindings, and a report `## Review target` section matching the manifest — so the protocol
tests cannot regress to token-only assertions.

### Protocol tests

Require current skill/reference/command docs to:

- request immutable GitHub OIDs and repository identity;
- load a target before PLAN;
- use a pinned head archive for static reads;
- avoid ambient source for diff files;
- verify exact local head before consented execution;
- use merge-base normalization, not raw two-dot diff;
- surface target identity in the report.

Run focused target/integrity/CLI/report/protocol tests, the full suite, `scripts/check.py`, and
`git diff --check`.

## Migration and compatibility

- Existing runs without a target remain valid for `build` and `deep-dive`.
- New `pr-review` runs require a target.
- Existing schema-v2 pr-review runs with no DAG/PLAN work may load a target and continue. Runs that
  already began DAG/PLAN/review work without `target_loaded` are `INVALID`; start a fresh run.
- No config-schema change is required.
- Run-log schema version remains 2 because the new target event and optional binding key are additive;
  workflow-specific validation enforces their presence.
