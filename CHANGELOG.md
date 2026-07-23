# Changelog

All notable changes to Crucible are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Crucible follows [Semantic Versioning](https://semver.org/). See
[RELEASING.md](RELEASING.md) for how releases are cut.

## [Unreleased]

## [0.19.1] - 2026-07-22

### Fixed
- **Local-range target normalization is hardened against ambient/hostile git configuration.**
  `crucible normalize-target local` now derives the recorded patch and changed-file set with
  `--no-ext-diff --no-textconv --no-color` and neutralized global/system git config and attributes,
  so an attribute-driven `diff.<driver>` can no longer execute during normalization or replace the
  recorded patch and its `diff_sha256`, and a non-ASCII changed-file name is read NUL-delimited
  (`--name-only -z`) instead of being rejected for git C-quoting.
- **Diff-file target changed-file parsing handles git C-quoted, deleted, renamed, and mode-only
  paths.** `crucible normalize-target diff` now un-quotes git's C-style path quoting and reads paths
  from `---`/`+++` hunk headers and `rename from`/`rename to` lines (falling back to the `diff --git`
  header only for pure mode-only/binary changes), so a plain `git diff` touching a non-ASCII path — or
  deleting/renaming one — is accepted with the correct paths instead of failing with a misleading
  "must use POSIX separators" error.
- **Source archives with case-insensitively colliding member paths are rejected deterministically.**
  `safe_extract_source_archive` now rejects an archive whose members — or a shared parent directory —
  differ only in letter case (e.g. `Foo/a.txt` + `foo/b.txt`), which on a case-insensitive filesystem
  (macOS/Windows) would otherwise silently collapse and diverge the extracted snapshot (and its
  `diff_sha256`) from the archive. The accept/reject decision is now the same on every platform.
- **The run report cautions when a github-pr review has an empty content diff.** Because a PR's patch
  and changed-file set are derived solely from the base/head archives (which omit submodule pointer
  OIDs), the report now flags a github-pr target whose content diff is empty, noting that submodule
  pointer bumps or other non-content changes are not captured and must be verified from the PR's file
  list.

## [0.19.0] - 2026-07-22

### Added
- **Symmetric two-peer consensus for the `deep-dive` / `pr-review` skills.** The symmetric skills now
  settle every gate from **two separately produced peer attestation files** via a new
  `symmetric-verdict --peer-a --peer-b` command, instead of one serialized union `verdict`. `init-run`
  gains an immutable `--workflow build|deep-dive|pr-review` kind; dependency/FINAL candidates are
  structured **finding sets** (`source_gate`/`id`/`severity`/`location`/`claim`/`suggestion`) kept
  distinct from **peer objections** (gate progress is decided from objections, so a candidate that
  *accepts* a blocker can still reach consensus); `accepted-findings` emits the deterministic accepted
  dependency union and `review-result` is the Finish-time deliverable (the finding set, plus a derived
  `APPROVE`/`COMMENT`/`REQUEST_CHANGES` recommendation for `pr-review` — `deep-dive` omits it). The
  report renders **Peer A** / **Peer B** and a `Review recommendation:` line kept **separate** from the
  workflow `CLEAN`/`FLAGGED` status. The build `crucible` skill and `verdict` semantics are unchanged.
  The proof is that **two configured slots** attested to the same candidate — **not** cryptographic
  model-process identity. Both symmetric skills, their references, commands, README, `docs/cli.md`, and
  SECURITY were migrated to the two-peer protocol. Design:
  `docs/superpowers/specs/2026-07-20-symmetric-consensus-design.md`; plan:
  `docs/superpowers/plans/2026-07-20-symmetric-consensus.md`. Guarded by `tests/test_symmetric.py`,
  the CLI/report/workflow suites, and the skill/reference/docs guards.
- **Workflow integrity: every gate decision is bound to the exact reviewed artifact (schema v2).**
  New runs record `schema_version: 2` and bind each gate to canonical SHA-256 **content bindings** —
  `artifact_sha256` over the exact Builder-artifact bytes (CRLF-preserving) and `dag_sha256` /
  `node_sha256` over the status-free DAG / node definition. The CLI gains `bindings` (the trusted
  metadata the Critic verdict must **echo**) and `approve-plan` (records human approval of the
  accepted plan/DAG); `verdict` rejects a missing or mismatched binding before recording any decision.
  It enforces the configured **phase order** (REPRODUCE → PLAN → optional approval → dependencies →
  optional FINAL) and legal node **transitions** — `pending -> done`, reviewing a `pending` node, and
  FINAL before completion are now rejected — and **freezes** the accepted plan/DAG and each reviewed
  node (any change requires a fresh run). `report` is configuration-aware: `CLEAN` requires every
  configured phase present, ordered, accepted, and currently bound, with new
  `INVALID` / `BLOCKED` / `FLAGGED` / `IN PROGRESS` statuses surfacing violations. A pre-schema-2
  **legacy** run stays readable (`report`/`status`/`clean`/`critic-lenses`) but is `LEGACY /
  UNVERIFIED`, never `CLEAN`, and refuses mutation — start a fresh run (no migration is inferred). All
  three skills (`crucible`, `deep-dive`, `pr-review`) and their references now run the binding
  handshake (log the artifact → `crucible bindings` → seed as trusted CLI metadata → the Critic/peer
  verdict echoes the hashes). Design:
  `docs/superpowers/specs/2026-07-19-workflow-integrity-design.md`; plan:
  `docs/superpowers/plans/2026-07-19-workflow-integrity.md`. Guarded by `tests/test_integrity.py`,
  `tests/test_workflow.py`, `tests/test_workflow_integrity.py`, `tests/test_report.py`, and the
  skill/reference/docs guards.
- **New independent `pr-review` skill (`skills/pr-review/`, `/pr-review <pr-or-diff>`).** A two-model
  **symmetric** adversarial *review* of a pull request: two **equal peers** (no Builder/Critic
  asymmetry) review a **GitHub PR** (via `gh`) or a **local diff** independently against the real
  code, cross-examine, and converge on an **evidence-grounded consensus finding set** plus a
  **derived** Approve/Comment/Request-changes recommendation. Review lenses (correctness,
  error-handling/silent-failures, tests, type design, comments, guideline compliance, reuse/ownership,
  load-bearing-claim audit, PR-intent) are harvested from Anthropic's `pr-review-toolkit` and
  crucible's own critic prompt. It **reuses the `crucible` CLI with no config-schema change** — but,
  mirroring how `deep-dive` was added, the CLI itself gained the `--workflow` run metadata and the
  symmetric `symmetric-verdict` / `accepted-findings` / `review-result` commands — and is
  **read-only** over the target by default —
  posting the review to the PR happens only for a GitHub PR, only after consensus, and only with the
  human's explicit per-run consent. New `skills/pr-review/` (SKILL + 4 references), `commands/pr-review.md`,
  `tests/test_pr_review_references.py` + `tests/test_pr_review_skill.py`, registered additively in
  `tests/validate_structure.py` and the `tests/test_docs.py` guards. Design:
  `docs/superpowers/specs/2026-07-17-pr-review-skill-design.md`.
- **Immutable `pr-review` target binding (audit finding #4).** Every `pr-review` input is now pinned to
  an immutable review target before PLAN. New `normalize-target github|local|diff` commands emit a
  canonical manifest + exact patch — a GitHub PR pinned to base/head **OIDs** + fork identity read
  stably **before and after** fetching the compare metadata and the merge-base/head snapshots, with the
  immutable patch **derived** PR-style from the base repo's exact-OID
  `compare/<baseRefOid>...<headRefOid>` endpoint (its `merge_base_commit.sha` fork-point snapshot →
  the head `repository@headRefOid` snapshot, recorded as `merge_base_sha`) — never a server-recomputed
  PR diff or a base-tip two-dot diff, so a base-only commit made on the base branch after the fork never
  appears as a reverse change; a local single `--range` recorded with `merge_base..head`
  **merge-base** semantics (no raw two-dot tip diff); or a diff file as patch identity only
  (`revision_bound: false`).
  `load-target` records the one `target_loaded` event and `target_sha256`; `show-target` prints the
  authoritative target; `repository-identity` fingerprints a local checkout credential-free; and
  `materialize-target` extracts a pinned, read-only `RUN/source` snapshot of the exact head commit
  through a confined archive reader (rejects path traversal, links, special files, duplicate paths, and
  member/byte caps — and is never executed). Every `pr-review` gate binding and peer attestation now
  carries `target_sha256`, the report renders a `## Review target` section, and trusted-local execution
  runs only at the recorded head commit (clean checkout at `head.sha`). `build`/`deep-dive` runs are
  unchanged (no target). Design:
  `docs/superpowers/specs/2026-07-21-pr-review-target-binding-design.md`; plan:
  `docs/superpowers/plans/2026-07-21-pr-review-target-binding.md`. Guarded by `tests/test_target.py`
  and the skill/reference/docs guards.

### Changed
- **Builder prompt: human-style code comments.** The "Writing code comments" guidance now bans
  safeguard genre-label comment preambles (`Defense-in-depth:`, `Belt-and-suspenders:`, `For
  safety:`) and directs compatibility / cross-version / rollback *justification* (the Load-Bearing
  Assumptions derivation) to the commit / PR / plan rather than the source, and nudges long inline
  explanations the same way. Standard `TODO`/`FIXME`/`NOTE`/`HACK` tags are unaffected. Prompt text
  only; no CLI/behavior change. Guarded by `tests/test_references.py`.

### Fixed
- **`pr-review` posting maps the recommendation to the GitHub review state (and drops the unbacked
  inline-comments claim).** The optional consented posting flow documented `gh pr review <n> --comment`
  "with inline comments", but `gh pr review` posts only a body + a single review state and cannot carry
  per-line inline comments, and the hardcoded `--comment` meant a derived Approve/Request-changes
  recommendation never reached the GitHub review state. The docs now map the deterministic
  `review-result` recommendation to the state — `APPROVE` → `--approve`, `COMMENT` → `--comment`,
  `REQUEST_CHANGES` → `--request-changes` (findings in the body) — under the same per-run posting
  consent, and no longer promise inline comments the command cannot post. Guarded by
  `tests/test_pr_review_references.py`.
- **Gate decisions record the effective per-gate policy (round cap + on_cap).** A gate's terminal
  event (`gate_consensus` / `gate_proceeded_with_flags` / `gate_capped`) recorded its bindings and
  open findings but not the effective round cap actually used — so a `--max-rounds` override (which the
  run config alone cannot show) left the report unable to reconstruct the decision as made. Both
  `verdict` and `symmetric-verdict` now record the effective `max_rounds` and `on_cap` on the terminal
  event (the always-halting reproduce gate records `on_cap: halt` regardless of the configured value),
  and the report surfaces them on each gate's outcome line (a legacy terminal without them stays
  readable). This is the in-scope provenance fix; defending against an operator who rewrites
  `RUN/config.json` remains out of scope per `SECURITY.md`. Guarded by `tests/test_cli.py` and
  `tests/test_report.py`.
- **`crucible clean` refuses an unfinished run (not just an incomplete DAG).** The delete guard keyed
  only on DAG node completion, so a run still in REPRODUCE/PLAN (no `dag.json` yet — classified as
  "nothing in progress, safe to remove") and a run with all nodes done but FINAL still pending were
  both deletable without `--force`, destroying active provenance. `clean` now refuses (without
  `--force`) unless every configured phase has positively concluded — PLAN, plus REPRODUCE / FINAL
  when enabled, with a DAG loaded and every node `done`; any uncertainty (unreadable config/DAG, an
  unconcluded phase, a capped/halted gate) preserves the run. `--force` still removes anything.
  Guarded by `tests/test_cli.py`.
- **The run report neutralizes Markdown image/link injection and code-span id breakout.** Untrusted
  fields (goals, Critic/peer finding ids and claims, DAG node ids/titles, gate names) are sanitized
  before rendering, but `_san` previously escaped only HTML and table/heading breakers — a value like
  `![](http://tracker)` still rendered an **active** `<img>` (a tracking pixel / content injection)
  when `report.md` was opened in a Markdown renderer, and an untrusted id wrapped in a backtick code
  span could break out of it (Markdown code spans ignore backslash escapes). `_san` now also escapes
  the Markdown image/link brackets `[`/`]`, and every remaining untrusted id is rendered as plain
  `_san` text rather than inside a code span (the dependency table, gate headers, and the
  findings/objections/resolutions lists), matching the summary convention. Guarded by
  `tests/test_report.py`.
- **Release notes require real content, not a bare heading.** The release workflow published the
  version's CHANGELOG section as the GitHub Release body, gated only by a non-empty check
  (`changelog.py section` + `test -s`), so a heading-only body such as `### Added` passed even though
  the changelog guard (`_content_lines`) counts it as no real entry. `changelog.py section` now fails
  closed on a section with no real content, and the version-consistency guard checks real entries
  (not just `body.strip()`), closing the inconsistency. Guarded by `tests/test_version_consistency.py`.
- **The CHANGELOG guard now covers `config.defaults.json`.** A change to the shipped default config
  (default Builder/Critic models, round caps, `on_cap`, severities) affects what every run resolves,
  but `requires_changelog` matched only `scripts/`/`skills/`/`commands/` and so did not require a
  CHANGELOG entry for it. `config.defaults.json` is now a recognized shipped runtime file. Guarded by
  `tests/test_changelog.py`.
- **`crucible load-dag` rejects a blank/whitespace or non-stripped node id.** A dependency-tree node
  whose `id` was blank, whitespace-only, or carried surrounding whitespace previously passed
  `DAG.from_dict` (only emptiness was checked) yet its `dep:<id>` gate was rejected by the gate
  validator — so the node loaded and was scheduled by `next` but could never be reviewed, wedging the
  run. `load-dag` now rejects such an id at load, matching the gate rule and the documented kebab-case
  schema. Guarded by `tests/test_dag.py`.
- **`pr-review` derives the GitHub snapshot patch from the exact archive bytes and modes (no
  attribute-driven rewriting).** The merge-base→head snapshot trees are now built from the **raw**
  archive bytes and archive-derived file modes with git plumbing that can never run a
  clean/smudge/EOL/working-tree-encoding filter: each regular file is hashed via `git hash-object -w
  --stdin --no-filters` (never `git add`) and inserted into an isolated index with its archive mode
  (`100755` iff the extracted file carries an executable bit, else `100644`) through one NUL-delimited
  `git update-index -z --index-info`, then `git write-tree`. Ambient/global git **attributes** are
  neutralized (`GIT_ATTR_NOSYSTEM=1`, `core.attributesFile=/dev/null`) alongside the existing config
  neutralization, so a hostile in-tree `.gitattributes`/`.gitignore` in a snapshot can no longer
  rewrite reviewed bytes (e.g. `text=auto`/EOL normalization), drop a member, or mask an
  executable-mode change before the derived patch is produced. The changed-file set is read
  NUL-delimited (`git diff --name-only -z`) so a path with a space/tab/newline is carried exactly, and
  `safe_extract_source_archive` preserves the archive's executable bit masked to ordinary permission
  bits (`& 0o777`; setuid/setgid/sticky are never written). Guarded by `tests/test_target.py`.
- **`pr-review` stores the source crash-repair receipt adjacent to `RUN/source`, not inside it.** The
  materialized `RUN/source` snapshot now holds **exactly** the reviewed archive members; the
  crash-repair receipt (`target_sha256`/`archive_sha256`/`kind`) is written to the adjacent run-state
  path `RUN/source.receipt.json` instead of a reserved file inside the tree, so a reviewed repository
  file that happens to use that name materializes unchanged and is visible to peers. The receipt is
  written **before** the source staging rename and `source_materialized` is appended only after, so
  every crash boundary stays idempotently repairable by a same-archive retry while a different or
  unreadable archive/target is rejected (the materialization is immutable, never silently
  overwritten). Guarded by `tests/test_target.py` and `tests/test_cli.py`.
- **`pr-review` trusts the snapshot-derived changed-file set (no false rejections on renames / large
  PRs).** GitHub PR normalization now derives `changed_files` **solely** from the immutable
  merge-base→head snapshot patch and no longer requires it to equal GitHub's own `files` view. That view
  (the `gh pr view --json files` metadata and the compare `files` list) paginates/truncates on large PRs
  and applies rename detection — reporting a rename as one new path where the historyless snapshot diff,
  without rename detection, shows the old+new pair — so the previous strict-equality gate falsely
  rejected legitimate PRs. The changed-file list is also removed from the immutable before/after identity
  tuple (title/body still trigger a retry; a paginated/reordered/rename-detected `files` drift no longer
  does), and compare-metadata validation still proves `base_commit.sha == baseRefOid` with a valid
  `merge_base_commit.sha` while tolerating an absent/truncated compare `files` list. The exact-OID
  compare/merge-base archive protocol is unchanged. Guarded by `tests/test_target.py` and
  `tests/test_report.py`.

### Security
- **Gate decisions are content-bound and phase-ordered (schema v2), not eyeballed.** The CLI now
  binds every gate decision to the exact reviewed artifact via canonical SHA-256 content bindings the
  Critic verdict must echo, enforces the configured phase order and legal node transitions, and
  freezes the accepted plan/DAG/node — so a substituted or edited artifact is rejected rather than
  certified, and `CLEAN` requires a complete, ordered, currently-bound workflow. This is a determinism
  guarantee derived from the append-only run log, not a defense against an operator who can rewrite
  arbitrary files or run-log bytes (no signing key, no sandbox); pre-schema-2 legacy runs are
  read-only and reported `LEGACY / UNVERIFIED`. See `SECURITY.md` and `docs/cli.md`.
- **`pr-review` gates reviewed-code execution on explicit consent (execution trust boundary).**
  Reviewing a change is now separated from executing it: a **GitHub PR URL/number** and a
  **diff-file** review are **static/CI-only** and never execute the reviewed code locally. Running
  tests or builds is available only for a **trusted local checkout**, after a new **Execution Safety
  Gate** (post-PLAN-consensus) shows the **exact commands**, warns they run **arbitrary code** with
  your file/credential/environment/network access, and obtains explicit, exact-command **consent**.
  Declining continues the review static-only with runtime results `unverified` (never a fabricated
  pass); a new or changed command needs fresh consent; consent does not imply sandboxing; and
  execution consent stays separate from posting consent. Instruction/policy change only — no
  `scripts/crucible/` or `config.defaults.json` change. Guarded by
  `tests/test_pr_review_execution_safety.py`. Design:
  `docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md`.

## [0.18.0] - 2026-07-16

### Added
- **Analytical-claim ("load-bearing assumption") grounding discipline in the Builder, Critic, and
  consensus prompts.** A *load-bearing analytical conclusion* — one used to justify safety,
  compatibility, scope, or *skipping* work/tests (e.g. backward/forward-compatible, deterministic
  under replay, no version bump needed, idempotent, concurrency-safe, no data loss) — must now be
  treated as an **argument, not a fact**: the Builder either shows its derivation or tags it
  `assumption — Critic must verify`, and for state that outlives a single execution derives **both**
  cross-version directions across a rolling deploy and rollback (a new **Load-Bearing Assumptions
  register** at the PLAN gate). The Critic independently **re-derives** every such claim at **both**
  the plan and diff gates (the Builder's conclusion carries no evidentiary weight), tries to
  construct a concrete failing case, and is severity-calibrated so trivial claims never block. The
  consensus rubric makes an undischarged load-bearing assumption a **non-deferrable** open blocking
  finding and names `strict_rebuttal: true` as the deterministic lever for `wontfix`-blocking.
  Guarded by new `tests/test_references.py` assertions; language is generic (no domain hardcoding).
- **Config-driven Critic "lenses" (`critic_checklists`).** An optional list of **absolute** paths to
  operator-provided checklist files, appended to the Critic prompt at each gate as additive "lenses"
  — a generic hook for injecting domain risk priors (e.g. a replay/versioning checklist) without
  hardcoding any domain into Crucible. A new `crucible critic-lenses` command reads them
  **fail-closed** (rejecting a relative path, a symlink, a missing/non-regular file, or one over a
  64 KiB cap) and emits them as fenced DATA that stays subordinate to `critic-prompt.md` and the
  verdict schema. Empty by default; lenses are operator config, never sourced from the reviewed tree.
  New `scripts/crucible/lenses.py` + `tests/test_lenses.py`, with `test_config.py`/`test_cli.py`
  coverage.

## [0.17.0] - 2026-07-15

### Added
- **New independent `deep-dive` skill (`skills/deep-dive/`, `/deep-dive <question>`).** A two-model
  **symmetric** adversarial *investigation* against actual code or data: two **equal peers** (no
  Builder/Critic asymmetry) investigate independently, cross-examine, and converge on an
  **evidence-grounded consensus finding set** — each round both peers review the merged candidate set
  and the recorded verdict is the **union** of their findings (`APPROVE` iff neither peer has a
  blocking finding), consensus is grounded in re-verifiable citations (never a vote or an average),
  and a blocking peer dispute is never cleared with `--resolutions`/`wontfix` (resolved against the
  source or surfaced as a flagged unresolved dispute with both positions). It **reuses the existing,
  unmodified `crucible` CLI** with no config-schema change; the existing `crucible` skill, CLI, and
  tests are unchanged. `tests/test_docs.py` was extended **additively** (its no-default-model-id /
  run-config guards now also cover the second skill's docs), and `tests/validate_structure.py` was
  refactored **additively** into an importable `main()` with a per-skill `REQUIRED_REFS` map (so each
  skill's own references are validated) — with no existing crucible assertion weakened.

## [0.16.0] - 2026-07-13

### Added
- **Existing-owner discipline in the Builder and Critic prompts.** The Builder now searches for an
  established owner of a responsibility before placing new logic and prefers extending it over a new
  or inline home (recording a negative search as `unverified`, never proof of absence). The Critic
  now attacks reuse-bypass / misplaced ownership at both gates — scoped at the diff gate so it never
  re-opens a placement the terminal PLAN already blessed — with severity calibrated so a placement
  objection blocks only when the bypassed owner can be cited in the repo (taste is never blocking).

## [0.15.0] - 2026-07-12

### Changed
- **The default model roles are Claude Opus 4.8 max for Builder and GPT-5.5 xhigh for Critic.**
  Explicit configuration overrides remain supported, and existing runs keep their resolved
  `RUN/config.json` values.

## [0.14.0] - 2026-07-11

### Changed
- **Shipped defaults now come from one authoritative `config.defaults.json` file.** Runtime code
  and tests load or derive from it, eliminating synchronized model-value edits across Python,
  examples, and reports.
- **Orchestration now reads each run's resolved `config.json`.** Model dispatch honors overrides
  without duplicating shipped model identifiers in skills, platform notes, commands, or install
  documentation.

## [0.13.0] - 2026-07-10

### Changed
- **The default model roles are now GPT-5.6 Sol max for Builder and Claude Opus 4.8 max for
  Critic.** Configuration overrides remain supported, so existing explicit model selections keep
  their behavior.

## [0.12.1] - 2026-07-10

### Changed
- **The Builder must reconcile completeness claims against a fresh tool run.**
  `references/builder-prompt.md`'s "Ground every claim" now requires any universal claim
  (`all`/`every`/`only`/`none`, or "the N affected X") to be backed by a tool run this turn and
  reconciled item-by-item (asserted count == hit count, every hit accounted for); an unreconciled
  universal is `unverified`. Closes an enumeration-miss class where a claimed-complete list silently
  dropped items.

## [0.12.0] - 2026-07-09

### Changed
- **Loop-bypass overrides now require an audited rationale.** A `wontfix` or `deferred` resolution
  must use the object form `{"resolution": "wontfix", "rationale": "…"}` with a non-empty
  `rationale` (a bare `"wontfix"`/`"deferred"` that clears a finding without a recorded reason is
  rejected by `crucible verdict --resolutions`), and `crucible set-status --force` now requires a
  new `--rationale` argument recorded on the `node_status_change` event. `references/builder-prompt.md`
  and `docs/cli.md` document the requirement.
- **The run report Summary now surfaces audited overrides.** `report.py`'s `## Summary` gains an
  **Overrides** line listing every `set-status --force` node advance and every `wontfix`/`deferred`
  rebuttal (across all rounds) with its recorded rationale — derived purely from the run log and
  sanitized — so a low-effort or missing reason is visible at a glance.
- **The Critic now treats a declared-but-absent test as a blocker.** `references/critic-prompt.md`
  splits test verification into *existence* (grep-checkable even with no runnable environment — a
  `test_plan` naming a test that was never written is a blocker) and *result* (needs a runnable
  environment — `unverified` if it cannot be run), reserving `unverified` for the pass/fail of a
  test that provably exists.
- **Documented the Superpowers version Crucible targets.** README Install and every prerequisite
  surface (`docs/install/*.md`, `.codex/INSTALL.md`, `CLAUDE.md`, `AGENTS.md`, `NOTICE`) now state
  Crucible needs **Superpowers v5.1.0+** and is **last tested against v6.0.3**; the Copilot/Claude
  install docs and `CLAUDE.md` now call the code-review reviewer the **`requesting-code-review`
  reviewer template** (not the `superpowers:code-reviewer` named agent Superpowers removed in v5.1.0).

## [0.11.1] - 2026-07-07

### Changed
- **The Builder role now follows code-comment best-practices.**
  `references/builder-prompt.md` gains a "Writing code comments" section: comment the *why* not the
  *what*, keep comments concise, document assumptions/edge-cases and workarounds, use
  `TODO`/`FIXME`/`NOTE`/`HACK` tags, prefer docstrings for public APIs, avoid obvious/brace/
  commented-out-code noise, and never let a comment lie (update comments when the code changes).
- **The Critic now flags comments that lie and leftover commented-out code.**
  `references/critic-prompt.md` adds these to the dependency-diff attack list and clarifies the
  "no style nits" boundary: a comment that contradicts the code or is left stale by the change is a
  correctness finding, while a merely missing or terse comment is not.

## [0.11.0] - 2026-07-05

### Fixed
- the PLAN settling-echo and dep-node validation no longer crash on a corrupt/malformed
  `dag.json`: a concluded gate never reports failure (exit 1) because of a bad `dag.json`, and a
  malformed `dag.json` gives a clean `crucible:` error instead of an `AttributeError` traceback.
- the run report no longer renders `CLEAN` when a `done` node lacks an accepted (consensus/proceed)
  `dep` gate — e.g. a `set-status --force`d node that bypassed review — such an otherwise-`CLEAN`
  run is instead `FLAGGED` and the node(s) are named.
- `set-status` now refuses to mark a node `done` unless its own `dep:<node>` gate reached
  consensus (or proceeded with flags); pass `--force` to override for recovery (recorded in the
  run-log). Previously a node whose gate was capped — or never reviewed at all — could be marked
  `done` and unblock its dependents, advancing the run past a halted/un-reviewed gate.
- `verdict` now derives the review round from run history (one past the number of prior
  `critic_verdict` events for the gate) and rejects any `--round` that isn't the next consecutive
  round. Previously the caller-asserted `--round` could skip straight to the cap (immediate
  `CAPPED`/`PROCEED_WITH_FLAGS`) or repeat a round forever, bypassing the round cap.
- `load-dag` now refuses to overwrite a run whose DAG already has progress (which would reset
  `done`/`in_progress` nodes to `pending`); pass `--force` to override. Previously an accidental
  re-run silently wiped a run's node statuses.
- `config` now rejects unknown nested keys under `builder`/`critic` (e.g. a misspelled `model`)
  instead of silently keeping the typo and falling back to the default model/effort.
- `show-plan` (and the settling-verdict echo) now render the plan that was actually approved — the
  plan `builder_output` at or before the gate's consensus/proceed round, not a later edit — and
  `show-plan` refuses to treat a capped (halted) plan gate as approved.

## [0.10.2] - 2026-07-04

### Changed
- **The Critic's code-review gates now dispatch the superpowers `requesting-code-review`
  `code-reviewer.md` template on a `general-purpose` subagent (on the critic model), instead of the
  removed `superpowers:code-reviewer` named agent.** Superpowers removed that named agent in v5.1.0
  (it now ships the reviewer only as a prompt template), so the old
  `agent_type: "superpowers:code-reviewer"` dispatch failed on every run and silently fell back. The
  PLAN gate already seeded the plan-document-reviewer template this way; the IMPLEMENT/FINAL gates
  now match, and `references/platform-notes.md`, `references/critic-prompt.md`, `SKILL.md`, the
  README, and the install docs are updated to say so. The platform's built-in `code-review` agent
  remains a documented last-resort fallback for when a subagent model can't be pinned. Docs/tests
  only — the deterministic `crucible` CLI is unchanged.

## [0.10.1] - 2026-07-04

### Fixed
- **`crucible report` no longer crashes the CLI on Python 3.11.** The 0.10.0 "Unresolved by
  severity" line put the `\u00b7` separator's backslash inside an f-string `{...}` expression, which
  is a `SyntaxError` before Python 3.12 (PEP 701) — so importing `report.py` broke the whole CLI for
  users on the documented 3.11 floor. The join now happens outside the f-string. A new
  `Minimum Python` CI job runs the suite under 3.11 (the `tests` job only used the newest `3.x`, so
  this class of newer-only syntax slipped through) to keep it from regressing.

## [0.10.0] - 2026-07-04

### Added
- **The run report now opens with a deterministic `## Summary` banner.** Above the dependency tree,
  `crucible report` renders an overall run status — `CLEAN` / `FLAGGED` / `BLOCKED` / `IN PROGRESS`
  (BLOCKED > FLAGGED > CLEAN > IN PROGRESS precedence) — plus gate-outcome counts
  (`total · consensus · flagged · capped`) and the total unresolved blocking findings with a
  per-gate id breakdown. It is derived purely from the run-log's own terminal events
  (`gate_consensus` / `gate_proceeded_with_flags` / `gate_capped`) and DAG node statuses — no model
  calls, no Critic prose, and every interpolated value is a literal label, an integer, or a
  sanitized id, so it adds no injection surface.
- **The `## Summary` banner now breaks unresolved findings down by severity.** Below the
  unresolved-findings count, it adds an `Unresolved by severity: N blocker · M major` line in
  canonical severity order (zero counts omitted), derived by cross-referencing each gate's last
  `critic_verdict` payload for each open finding's severity — deterministic and from the run-log
  only (an id whose severity can't be found is counted under `unknown`). Per-gate findings keep
  their original emission order elsewhere in the report, preserving provenance.

### Changed
- **Copilot CLI: the approved plan + dependency tree must be surfaced *in full*.** `SKILL.md`
  Stage 1 step 6 and `references/platform-notes.md`'s Copilot surfacing guidance now require pasting
  the `crucible show-plan` output into the reply in full — the complete plan + dependency tree — and
  forbid piping it through `head`/`tail`/`grep`/`sed` or otherwise truncating it to a fragment, since
  the collapsed bash output is not what the human sees.
- **The Builder role now must ground every claim in a tool run.** `references/builder-prompt.md`
  requires that any statement about the code, tests, or environment come from a tool run *this turn*
  (cite the `file:line` or observed output), forbids inventing flags/paths/APIs/config keys, and
  requires labeling anything unverified — so the Builder can't advance a confident-but-unchecked
  assertion.
- **The Critic now verifies test evidence and nudges bug-fix reproductions.**
  `references/critic-prompt.md` tells the Critic to verify the Builder's cited test evidence and,
  when a node declares a `test_plan` and that evidence is missing or dubious and a runnable
  environment is available, to run the focused `test_plan` and cite the observed result (degrading
  to *unverified* rather than fabricating a pass — not a blanket re-run). It also flags a clearly
  behavioral bug-fix plan that ships no failing reproduction (and neither enables the reproduce gate
  nor states a waiver) as a soft, waivable finding.

## [0.9.0] - 2026-07-03

### Added
- **`crucible verdict` now prints the two finding lists the Builder faces to the terminal (stderr).**
  After the outcome token, it surfaces *Findings the Builder will fix* (findings the Builder resolved
  `fixed`) and, logged separately, *Unresolved blocking findings* (the deterministic still-open blockers
  not marked `fixed`) — each only when non-empty and in a detailed `id [severity] location: claim` form.
  A non-`fixed` resolution that failed to clear (in practice a `wontfix` under `strict_rebuttal`) is tagged
  on its line. The lists go to stderr so the machine-readable outcome (`CONSENSUS`/`CHANGES`/…) stays alone
  on stdout.

### Fixed
- **The approved plan + dependency tree are now echoed deterministically when the PLAN gate settles.**
  `crucible verdict` prints the approved plan + DAG to stderr on `CONSENSUS`/`PROCEED_WITH_FLAGS` for the
  `plan` gate (the outcome token stays alone on stdout), so the final plan/DAG is always visible before
  implementation instead of relying on a separately-invoked `show-plan` that the orchestrator could skip.
  The echo is best-effort — skipped without error (no masking placeholder) when no DAG is loaded, since
  `verdict` is decoupled from the DAG — and `show-plan` behavior is unchanged.

### Changed
- **Crucible now makes "each node owns its own documentation" an explicit, Critic-enforced rule.**
  `references/dependency-tree.md` and `references/builder-prompt.md` state that a dependency node
  includes the documentation + `CHANGELOG` updates for *its own* deliverable (docs live with the code;
  a docs-only node is reserved for standalone documentation not tied to a specific code change).
  `references/critic-prompt.md` now flags a behavior-changing node that omits its own
  documentation/`CHANGELOG` at **both** gates — the PLAN reviewer (plan / dependency tree) and the
  IMPLEMENT/FINAL reviewer (dependency diff).
- **Copilot CLI: the orchestrator now surfaces the approved plan + dependency tree in its response.**
  Because the Copilot CLI renders bash-tool output collapsed/truncated, the plan + DAG that `verdict`
  echoes at PLAN settlement (and `show-plan`, gate outcomes, and unresolved findings) were not visible
  to the human. `references/platform-notes.md` and `SKILL.md` now direct the assistant to surface the
  approved plan + dependency tree in its reply (via `show-plan`) before implementing, and not to
  suppress the settling `verdict`'s stderr.

## [0.8.0] - 2026-07-01

### Changed
- **`load-dag` and `log` now echo the plan/DAG details to the terminal when logged.**
  `crucible load-dag` prints the dependency tree in true build (topological) order right after
  `loaded N nodes`, and `crucible log` prints the payload it recorded under an
  `event/gate/round/size` header (or flags a missing `--file` as an empty payload) — so the plan
  and dependency tree are visible *as they are recorded*, not only later via `show-plan` after PLAN
  consensus. `show-plan` reuses the same renderer.

### Fixed
- **Console output no longer crashes on non-ASCII under an ASCII/`C` locale.** The CLI reconfigures
  stdout/stderr to escape uncodable characters (`backslashreplace`) instead of aborting with
  `UnicodeEncodeError` (previously `report`/`show-plan` could crash on a non-ASCII goal/title). File
  provenance (runlog, `dag.json`, `report.*`) stays UTF-8. This also corrects the dependency-tree
  "build order" label, which had rendered nodes in input order rather than topological order.

## [0.7.0] - 2026-06-29

### Added
- **Optional REPRODUCE gate for bug fixes (default off).** New config flag `reproduce_gate`
  (default `false`) + `crucible should-reproduce` command (yes/no, exit 0/1). When enabled, a
  Stage 0 runs before PLAN: the Builder reproduces the bug with a failing test via
  `superpowers:systematic-debugging` and the Critic confirms it fails for the stated reason —
  unconfirmed bugs halt before any planning, and the reproduction test carries forward as the
  fix's done-signal. `reproduce` is now a valid gate (plan round cap). Default off = unchanged.

## [0.6.1] - 2026-06-29

### Fixed
- **Install docs point at the GitHub marketplace, not an author-local path.** The Copilot CLI and
  Claude Code "just run it" steps (README, `CLAUDE.md`, `docs/install/`) now add the marketplace
  from GitHub — `copilot plugin marketplace add BlancosWay/crucible` /
  `/plugin marketplace add BlancosWay/crucible` — instead of `~/personal/crucible`, so a first-time
  install works with no clone.

## [0.6.0] - 2026-06-29

### Changed
- **Runs default to `~/.crucible/runs`, never the target repo.** `init-run` now writes under
  `~/.crucible/runs` by default (override with `--base-dir` or `$CRUCIBLE_RUNS_DIR`), so running
  Crucible inside any project no longer drops a `runs/` dir in that repo. Unique timestamped run
  dirs mean concurrent runs across projects coexist; clear all with `rm -rf ~/.crucible/runs`.
- **`clean` refuses an in-progress run.** `crucible clean` will not delete a run whose DAG has
  nodes still `pending`/`in_progress`/`in_review` (exits non-zero); pass `--force` to override.
  Prevents wiping a run that is still building. The skill's Finish step cleans only after all
  nodes are `done`.

## [0.5.0] - 2026-06-29

### Added
- **`clean` command + Finish cleanup step.** `crucible clean --run <dir>` deletes a finished run's
  directory (logs, report, and all scratch) and refuses any path without a `runlog.jsonl`, so a
  typo can't wipe an unrelated dir. The skill's Finish stage now removes the run data once the
  report is captured (and notes `rm -rf runs/` to clear all prior runs).

## [0.4.0] - 2026-06-29

### Changed
- **Crucible scratch files stay out of version control.** The orchestrator now writes every
  scratch artifact (`dag.json`, `plan.md`, `verdict.json`, `res.json`, node diffs) under the
  git-ignored run dir (`"$RUN"/`) instead of the working-tree root, and reviews diffs against the
  base branch — so a run never stages/commits temp files, in-repo or as an installed plugin. Root
  scratch names are also git-ignored as a safety net.

## [0.3.0] - 2026-06-29

### Added
- **`show-plan` prints the approved plan + DAG to the terminal.** After the PLAN gate reaches
  consensus, `crucible show-plan --run <dir>` echoes the final plan artifact and the dependency
  tree (in build order) so the operator sees exactly what was approved before implementation
  begins; it exits non-zero until the plan gate has concluded. The orchestrator runs it as PLAN
  step 6.

## [0.2.0] - 2026-06-28

### Added
- **Optional human-approval gate (default off).** New config flag `human_approval` (default
  `false`) plus a `crucible should-approve --run <dir>` command (prints `yes`/`no`, exits 0/1,
  mirroring `should-final`). When enabled, the orchestrator pauses after PLAN-gate consensus and
  waits for explicit human OK before any implementation; default keeps the fully automated
  behavior unchanged. SKILL.md adds the deterministic approval-gate step keyed off the token.
- **Local-install packaging parity.** Crucible can now be installed directly as a local plugin
  (`copilot plugin marketplace add ~/personal/crucible` → `copilot plugin install
  crucible@crucible-marketplace`). Added per-platform install guides
  (`docs/install/{copilot-cli,claude-code,codex}.md`), `CLAUDE.md`/`AGENTS.md`/`.codex/INSTALL.md`,
  `LICENSE` (MIT), `NOTICE` (Superpowers attribution), and `SECURITY.md`; enriched the plugin and
  marketplace manifests (author/homepage/repository/keywords/owner) and added an Install section to
  the README. No skill/CLI behavior change.
- **`should-final` command (M6).** `crucible should-final --run <dir>` deterministically reports
  whether the FINAL gate should run (prints `yes`/`no`, exits 0/1) from the `final_review` config
  flag, so the orchestrator gates Stage 3 on it instead of eyeballing the flag (it was previously
  parsed and documented but consumed by no code path).
- **`on_cap: proceed_with_flags` is now functional (H2).** Reaching a gate's round cap with
  unresolved blocking findings under `on_cap: proceed_with_flags` now yields a distinct
  `PROCEED_WITH_FLAGS` outcome (instead of silently behaving like `halt`/`CAPPED`): `crucible
  verdict` prints it, records a `gate_proceeded_with_flags` event with the carried finding ids,
  and the report renders a "PROCEEDED WITH FLAGS … N unresolved finding(s) carried" line. The
  report now also resolves a gate's outcome from the latest terminal event in log order. Default
  `on_cap: halt` is unchanged.

### Fixed
- **Reject empty `blocking_severities` (CFG-002).** A config with `blocking_severities: []` made
  every `REQUEST_CHANGES` verdict fail consistency (no finding can ever block), so a gate could
  never legitimately request changes; `Config._validate` now requires at least one blocking
  severity. Empty `defer_severities` stays allowed (nothing is deferrable).
- **Markdown report neutralizes raw HTML in untrusted fields (RPT-001).** `_san` now escapes
  `&`/`<`/`>` (in addition to `|` and backticks), so untrusted model output (the goal and the
  Critic's verdict/summary/finding fields) can no longer render as live HTML — e.g. a finding
  claim `<img src=x onerror=…>` — when `report.md` is opened in an HTML-permitting Markdown
  renderer. The raw provenance code fences stay verbatim in Markdown (code-fence content renders
  literally), and `render_html` now escapes only those fence bodies, so the HTML report stays
  fully escaped without double-escaping the already-escaped inline fields.
- **Round-5 orchestration-invariant & durability hardening.**
  - `crucible verdict` / `log` now reject a `dep:<id>` gate whose `<id>` is not a node in the
    run's dependency tree; a typo'd/ghost dependency previously recorded a verdict (and a
    terminal outcome) under a non-existent node (C1).
  - `crucible set-status` now refuses to set a node `in_progress`/`in_review`/`done` while any of
    its dependencies is unfinished; a node could previously be marked done out of order, letting
    `next` schedule and skip dependent work (C2).
  - `crucible verdict` now refuses to re-decide a gate that already logged a terminal outcome
    (`gate_consensus`/`gate_proceeded_with_flags`/`gate_capped`); an accidental rerun could
    otherwise silently rewrite the gate's apparent decision in the report (C3).
  - `crucible verdict --resolutions` now rejects a `deferred` resolution on a finding whose
    severity is not in `defer_severities` (deferring a blocker was a silent no-op logged as if
    meaningful) (O5-B).
  - Config `builder`/`critic` `model` and `effort` must now be **non-empty** strings; an empty
    string previously validated into an unusable dispatch config (C6).
  - Run-log writes are now crash-durable (S1): each appended record is `fsync`-ed, and
    `dag.json`/`config.json` are written via an atomic temp-file + `os.replace`, so a crash
    mid-write can never leave a torn file.
  - Docs: the dependency-tree schema now states imported nodes must be `pending` (matching the
    round-4 importer) (O5-A); the no-subagent fallback `log` command shows the required `--run`
    and `--file` (C5); a Copilot agent-type packaging caveat + fallback is noted (C4); and the
    consensus definition now reflects the rebuttal path, not just a Critic `APPROVE` (O5-C).
- **Round-4 orchestration & validation hardening.**
  - SKILL.md PLAN loop now reloads a Critic-corrected dependency tree on `CHANGES`: it repeats
    from the DAG emit/`load-dag` step (was "repeat from step 3", which skipped the reload), so a
    revised DAG is never ignored while Stage 2 walks a stale one (G1).
  - `crucible load-dag` now rejects a freshly imported plan whose nodes are not all `pending`; a
    node baked as `done`/`in_progress` previously let `next` schedule its dependents and silently
    skip its work. Statuses change only via `set-status` (G2).
  - `crucible verdict` and `crucible log` now validate `--gate` (`plan` | `final` | `dep:<id>`); a
    typo like `finale` was previously logged under a bogus gate using the dependency round cap (G3).
  - SKILL.md Stage 3 `should-final` snippet now sets a `RUN_FINAL` flag and guards the FINAL
    dispatch with `if [ "$RUN_FINAL" = 1 ]`; the prior `case` arms were no-ops and the dispatch ran
    unconditionally, so `no` did not actually skip the final gate (G4).
  - `Config.from_dict` now rejects a non-object (top-level list/string/number/null) config with a
    clean `crucible: config must be a JSON object` error instead of a raw `AttributeError` (G5).
  - `crucible verdict --resolutions` now rejects a `null` resolution value as malformed instead of
    logging it and treating the finding as unresolved (G6).
  - Added in-process CLI unit tests (the rest of the CLI suite runs via subprocess), documented the
    reserved `in_review` status and the intentional verdict digest-plus-raw provenance, clarified
    that rounds are 1-based and per-gate in SKILL.md, and gitignored `.coverage` (P1–P4).
- **Round-3 robustness fixes.**
  - `crucible verdict --resolutions` now rejects a resolution that targets an unknown finding id
    (e.g. a typo) with a clear error, instead of silently ignoring the intended rebuttal (O1).
  - `crucible log` now requires `--gate` and `--round`; a gateless provenance entry was silently
    dropped from the report (O2).
  - The CHANGELOG CI guard now detects an added line even when it is textually identical to an
    existing one (line multiplicity, not set membership) (O3).
  - The report renders `_(empty)_` for an empty Builder/Critic output instead of an empty code
    fence (O4).
- **Report renders `critic_output` (N4).** The no-subagent-fallback Critic's full raw review
  (logged as `critic_output`) is now rendered in the report (in an injection-safe fenced block),
  matching `builder_output`; previously it was logged but silently omitted.
- **Scalar/field type validation (N3).** The config, DAG, and verdict parsers now reject
  wrong-typed scalar fields with a clear `crucible: …` error instead of a raw `TypeError` or a
  silent mis-load: `max_rounds_*` / verdict `round` must be real integers; `defer`/`blocking`
  severities must be lists (no char-explosion); `builder`/`critic` `model`/`effort` must be
  strings; and node ids, edge endpoints, and finding ids must be non-empty strings.
- **`log` command hardening (N1, N2).** `crucible log --event` is now restricted to
  `builder_output`/`critic_output`; it can no longer append a CLI-managed event (e.g.
  `gate_consensus`, `critic_verdict`) and forge a report outcome/verdict. Logged payloads are now
  stored as **raw text** (no JSON parse + re-serialize), preserving exact whitespace/key order for
  full-fidelity provenance.
- **Smaller robustness & portability fixes.**
  - `defer_severities` and `blocking_severities` must now be **disjoint** — a severity in both
    previously let a `deferred` resolution clear a blocking finding (L1).
  - `crucible verdict --round` now requires `>= 1` (rounds are 1-based) instead of silently
    accepting 0/negative (L4).
  - `crucible verdict --max-rounds` override now also requires `>= 1` (N5).
  - The Markdown report now escapes backticks in untrusted Critic text, so a stray backtick can
    no longer break the surrounding inline formatting (N6).
  - The internal Markdown link checker now rejects links that resolve outside the repo, and is
    refactored into testable functions (N7).
  - The run-directory slug no longer keeps a trailing hyphen after truncation (L3).
  - Workflow examples in the README and skill now use `python3` (matching CI/CONTRIBUTING), and
    `scripts/check.py` also finds a Windows (`.venv/Scripts/python.exe`) interpreter (L2).
- **Input type validation (M7).** The DAG, verdict, and resolutions parsers now reject
  wrong-typed JSON with a clear error instead of silently corrupting data or raising a cryptic
  `AttributeError`/`TypeError`. In particular a node `files` given as a string (previously
  char-exploded into `['s','r','c',…]`) now requires a list of strings; a non-object dependency
  tree / verdict / resolutions file, a non-list `nodes`/`edges`/`findings`, and a non-object
  node/edge/finding are all rejected (surfaced cleanly via the M5 handler).
- **Clean CLI errors (M5).** The CLI now reports malformed JSON, a missing required field, an
  invalid value, a dependency cycle, an unknown `set-status` node, a missing file, and a corrupt
  run-log as a concise `crucible: …` message on stderr with exit code 1, instead of a raw Python
  traceback. `dag.set_status` now raises a descriptive `ValueError("unknown node: …")`. The
  existing explicit exits (gate/round mismatch, verdict consistency, empty DAG, invalid
  resolution) are unchanged.
- **Deep-merge nested config (M4).** A partial `builder`/`critic` override (e.g. only `model`)
  now keeps the default sibling keys (e.g. `effort`) instead of replacing the whole nested dict
  and silently dropping them. A non-object `builder`/`critic` now raises a clear `ValueError`.
- **Strict boolean config (M3).** `strict_rebuttal` and `final_review` now require a real JSON
  boolean. Previously they were coerced with `bool(...)`, so a quoted `"false"` silently became
  `True` (inverting intent); an invalid value now raises a clear `ValueError`.
- **Report provenance (M2).** The run report now renders the full Builder output, the
  Critic's raw verdict text, and the Builder's per-finding resolutions (rebuttals) — not just
  the structured Critic findings. Builder output and raw verdicts are emitted in
  injection-safe fenced code blocks (the fence is always longer than any backtick run in the
  content; the HTML path still escapes `&<>`), honoring the design's "report and audit read
  the full raw text directly from the log" and consensus-rubric's "rebuttals are shown in the
  report."
- **Resilient run-log reads (M1).** `RunLog.read_events()` no longer lets a single bad line
  brick `crucible report` for an otherwise-complete run. A torn/partial final record (the
  realistic crash artifact — `append` writes one record per `write`) is now skipped with a
  stderr warning, while interior corruption, a complete-but-invalid record, or a non-object /
  missing-`event` line raises a clear, line-numbered `RunLogCorruptError` instead of a raw
  `JSONDecodeError`. Reads are byte-based so a torn multi-byte UTF-8 tail no longer crashes.
- **UTF-8 read/write symmetry (H4).** `save_dag` (`dag.json`), the run config snapshot
  (`config.json`), and `load_config` now read/write text as explicit UTF-8 (the writes also
  use `ensure_ascii=False`). Previously these used the platform default encoding, so under a
  non-UTF-8 locale (`LC_ALL=C`, or a typical Windows code page) a non-ASCII DAG node title or
  config value raised `UnicodeEncodeError`/`UnicodeDecodeError` while every read forced UTF-8.
- **Verdict/severity consistency (H1).** `crucible verdict` now rejects an internally
  contradictory Critic verdict — `APPROVE` with an open blocking finding, or
  `REQUEST_CHANGES` with no blocking finding — checked against the run's configured
  `blocking_severities` before anything is logged or decided. The severity-authoritative
  consensus decision (and the Builder rebuttal flow) is unchanged.
- **`next` distinguishes "done" from "stuck" (H3).** `crucible next` used to print an empty
  line and exit `0` both when every node was `done` and when the run was stuck (a `blocked`
  node, or a node waiting on an unfinished dependency), so the IMPLEMENT loop could terminate
  silently with work undone. It now exits `0` only on a ready node or genuine completion, and
  exits non-zero with a stderr report otherwise — `3` (stuck) or `4` (work in flight). `load-dag`
  now also rejects an empty (0-node) dependency tree. SKILL.md Stage 2 checks the exit code.

## [0.1.0] - 2026-06-22

Initial release: a two-model adversarial planning + implementation workflow on top of
[Superpowers](https://github.com/obra/superpowers).

### Added
- **Two-model adversarial loop.** A **Builder** model (default Opus 4.8) plans and implements; a
  **Critic** model (default GPT-5.5 xhigh) adversarially reviews the plan, the dependency tree, and
  each dependency. The Critic is realized as a **superpowers reviewer** on the critic model at
  every gate: the writing-plans **plan-document-reviewer** (+ brainstorming **spec-document-reviewer**)
  at the PLAN gate, and the **code-reviewer** agent at the IMPLEMENT/FINAL gates. Each gate loops
  until **consensus** (Critic `APPROVE`) or a
  configured per-gate round cap, after which an `on_cap` policy (`halt` / `proceed_with_flags`) applies.
- **Deterministic Python core** (`scripts/crucible/`): `config` (models, caps, policies), `dag`
  (cycle-detection, topological order, ready-set, status), `verdict` (consensus decision +
  resolutions/rebuttals), `runlog` (append-only provenance with full raw text), `report`
  (deterministic Markdown/HTML), and a thin `cli` exposing them.
- **Superpowers orchestrator** (`skills/crucible/SKILL.md` + `references/`) that drives the loop and
  calls the CLI for every deterministic decision, plus a `/crucible` command and plugin manifests.
- **Governance & CI.** Structural validation, Markdown-link, ShellCheck, pytest, changelog, and
  release-dry-run gates; a tag-driven release workflow; owner/Dependabot squash auto-merge;
  Dependabot for Actions; CODEOWNERS; PR/issue templates; a `scripts/check.py` unified local check
  wired as a `.githooks/pre-commit` hook; and a version⇄CHANGELOG consistency guard.
