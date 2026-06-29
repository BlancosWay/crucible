# Changelog

All notable changes to Crucible are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Crucible follows [Semantic Versioning](https://semver.org/). See
[RELEASING.md](RELEASING.md) for how releases are cut.

## [Unreleased]

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
