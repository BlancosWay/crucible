# Changelog

All notable changes to Crucible are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Crucible follows [Semantic Versioning](https://semver.org/). See
[RELEASING.md](RELEASING.md) for how releases are cut.

## [Unreleased]

### Fixed
- `set-status` now refuses to mark a node `done` unless its own `dep:<node>` gate reached
  consensus (or proceeded with flags); pass `--force` to override for recovery (recorded in the
  run-log). Previously a node whose gate was capped — or never reviewed at all — could be marked
  `done` and unblock its dependents, advancing the run past a halted/un-reviewed gate.

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
