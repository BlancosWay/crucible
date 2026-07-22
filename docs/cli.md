# Crucible CLI reference

The `crucible` CLI is the **deterministic backbone** the orchestrator skill drives. It owns every
piece of bookkeeping — the DAG walk, round counting, consensus decisions, provenance logging, and
the run report — so those are decided by code, never eyeballed by a model. You normally don't run
these commands by hand; the skill does. They're documented here for transparency and debugging.

## Invocation

```bash
PYTHONPATH=scripts python3 -m crucible <command> [arguments]
```

Every command except `init-run` operates on an existing run and takes `--run <dir>`, where `<dir>`
is the path `init-run` printed.

## Conventions

- **Runs directory** — `init-run` creates the run under `--base-dir`, else `$CRUCIBLE_RUNS_DIR`, else
  `~/.crucible/runs`, so nothing is written into the target repo.
- **Gates** — a gate is `plan`, `final`, or a dependency **node id** (one IMPLEMENT gate per node).
  The companion `deep-dive` skill reuses this same CLI, where a node id is an **investigation thread**
  (`dep:<thread>`) and each round is settled by `symmetric-verdict` from two separate peer
  attestations, not a single union verdict (see
  [Symmetric workflows](#symmetric-workflows-deep-dive--pr-review)). The companion `pr-review` skill
  likewise reuses it, where a node id is a **review thread** (`dep:<thread>`); that review is
  **static/CI-only** for a PR-URL or diff-file target and executes a reviewed change only for a
  **trusted local checkout** after exact-command **consent** (a skill-level Execution Safety Gate,
  not a CLI feature).
- **Node statuses** — `pending`, `in_progress`, `in_review`, `done`, `blocked`. Transitions are
  enforced: `pending -> in_progress | blocked`, `in_progress -> in_review | done | blocked | pending`,
  `in_review -> in_progress | done | blocked | pending`, `blocked -> pending`, and `done` is terminal.
  An illegal jump (e.g. `pending -> done`, or reviewing a node while `pending`) is rejected;
  `set-status --force --status done` remains the explicit, rationale-bearing recovery override.
- **Schema version & legacy runs** — new runs record `schema_version: 2` in `run_start` (not user
  configuration; it cannot be overridden). Schema-2 binds every gate decision to the exact reviewed
  artifact via canonical SHA-256 **content bindings** (`artifact_sha256`, `dag_sha256`, `node_sha256`)
  and enforces the configured phase order; the accepted plan/DAG and each reviewed node are
  **immutable** after acceptance. A run with no schema version (or `< 2`) is **legacy**: `report`,
  `status`, `clean`, and `critic-lenses` stay readable but the report is `LEGACY / UNVERIFIED` (never
  `CLEAN`), and the mutating commands (`load-dag`, `log`, `verdict`, `set-status`, `approve-plan`,
  `show-plan`) refuse — start a fresh run. No migration command is provided.
- **Exit codes** — `0` on success unless noted. `next` and the `should-*` switches use exit codes as
  signals (below).

## Start a run

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `init-run` | `--goal GOAL` (required), `--config FILE`, `--base-dir DIR`, `--workflow build\|deep-dive\|pr-review` | Create a run directory (seeding its `config.json` from `--config`, or defaults) and print its path to stdout. `--workflow` records the immutable workflow kind on `run_start` (default `build`, the asymmetric Builder/Critic flow); `deep-dive`/`pr-review` select the symmetric two-peer flow and are documented under [Symmetric workflows](#symmetric-workflows-deep-dive--pr-review). |
| `load-dag` | `--run RUN` (required), `--file FILE` (required); `--force` (optional) | Import the plan's dependency tree from a JSON file. Rejects an empty tree and any node not `pending` (fresh plans start all-`pending`; statuses change only via `set-status`). Also refuses to overwrite a run whose existing DAG already has progress (non-`pending` nodes), which would reset it — pass `--force` to replace it (discards current node statuses). Prints the node count and the tree. |

## Schedule & track progress

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `next` | `--run RUN` (required) | Print the next ready node id on stdout (empty line when every node is `done`). If no node can be scheduled: exit **4** when work is still in flight, exit **3** when the run is STUCK — both with the unfinished nodes and their unmet deps on stderr. |
| `set-status` | `--run RUN`, `--node NODE`, `--status STATUS` (all required); `--force`, `--rationale TEXT` (optional) | Set a node's status. Refuses to move a node to a work status (`in_progress`/`in_review`/`done`) while any dependency is not `done`. Also refuses to mark a node `done` unless its own `dep:<node>` gate reached consensus (or proceeded with flags) — a capped or never-reviewed node is rejected. `--force` overrides that gate requirement for recovery and **requires `--rationale`** (a non-empty reason); the override and its rationale are recorded in run-log provenance (the `node_status_change` `forced` flag + `rationale`) and surfaced in the report, and a `--force`d/un-gated node prevents an otherwise-`CLEAN` run from rendering `CLEAN` — the report instead reads `FLAGGED` and names the nodes done without an accepted review gate. |
| `status` | `--run RUN` (required) | Print run progress as JSON (node counts by status). |

## Record gates & adjudicate verdicts

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `log` | `--run RUN`, `--event EVENT`, `--gate GATE`, `--round ROUND` (required), `--file FILE` | Append a Builder or Critic transcript to the run log. `--event` is `builder_output` or `critic_output` (other events are written by their own commands). A schema-2 `builder_output` **requires `--file`** with a non-empty payload, must be logged for the CLI-derived current round, is rejected after that gate has a terminal event, and records the artifact's `artifact_sha256` (the exact bytes are hashed, CRLF-preserving). `critic_output` payload is optional (omit `--file` for empty). |
| `verdict` | `--run RUN`, `--gate GATE`, `--round ROUND` (required), `--max-rounds M`, `--resolutions FILE`, `--file FILE` (required) | Adjudicate the Critic's verdict deterministically into `CONSENSUS`, `CHANGES`, `PROCEED_WITH_FLAGS`, or `CAPPED`, honoring `blocking_severities`, `defer_severities`, and `strict_rebuttal`. The verdict JSON must **echo** the gate's content bindings (`artifact_sha256` + gate-specific `dag_sha256`/`node_sha256` from `bindings`); a missing or mismatched binding is rejected **before** any decision is logged. `--round` must equal the CLI-derived next round for the gate (one past the number of prior `critic_verdict` events, i.e. consecutive starting at 1); a mismatch is rejected, so the round cap cannot be bypassed by skipping to the cap or repeating a round. `--resolutions` is the Builder's per-finding map (`id` → `fixed` / `deferred` / `wontfix`; a `wontfix` or `deferred` entry must use the object form `{"resolution": "wontfix", "rationale": "…"}` with a non-empty `rationale`, since it clears a finding without a fix — a bare `"wontfix"`/`"deferred"` is rejected); `--max-rounds` overrides the cap (defaults to `max_rounds_plan`/`max_rounds_dep` by gate). Prints the outcome, the findings the Builder will fix, and any unresolved blocking findings; when the PLAN gate settles it also echoes the approved plan + DAG. The validated bindings are persisted on `critic_verdict` and every terminal event for provenance. **Build workflow only** — a symmetric (`deep-dive`/`pr-review`) run is rejected with a message directing the orchestrator to `symmetric-verdict`. |

## Content bindings & human approval

Schema-2 commands that bind a gate decision to the exact reviewed artifact. Both require the current
schema (a legacy run has no verifiable bindings/approval).

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `bindings` | `--run RUN`, `--gate GATE`, `--round ROUND` (all required) | Print the deterministic content bindings for a gate/round as machine-readable JSON — `artifact_sha256` for `reproduce`; `artifact_sha256` + `dag_sha256` for `plan`/`final`; `artifact_sha256` + `dag_sha256` + `node_sha256` for `dep:<id>`. The orchestrator appends this JSON to the Critic seed as **trusted CLI metadata**; the Critic echoes it and `verdict` requires an exact match. Validates the same stage prerequisites the `log`/`verdict` handshake enforces. |
| `approve-plan` | `--run RUN` (required) | Record human approval of the accepted plan as a `plan_approved` event binding the accepted plan/DAG hashes. Requires `human_approval: true`, an accepted PLAN gate whose `dag_sha256` still matches the current tree, and rejects a duplicate or stale approval (and rejects entirely when `human_approval` is disabled). When configured, dependency and FINAL gates then require this recorded approval. Skills call it only **after the human explicitly approves**. |

## Config-driven gate switches

Each prints `yes`/`no` and exits `0`/`1`, so the skill gates a phase on the run config instead of
eyeballing it. All take `--run RUN` (required).

| Command | Gates on | Default |
|---------|----------|---------|
| `should-reproduce` | Stage 0 REPRODUCE gate (`reproduce_gate`) | `no` |
| `should-approve` | Human-approval pause after PLAN consensus (`human_approval`) | `no` |
| `should-final` | Stage 3 FINAL gate (`final_review`) | `yes` |

## Inspect & finish

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `show-plan` | `--run RUN` (required) | Print the approved plan + dependency tree — specifically the plan `builder_output` at or before the gate's consensus/proceed round, never a later edit. Refuses until the PLAN gate has reached consensus (or proceeded with flags) — a capped (halted) plan gate is not treated as approved — so the operator sees exactly what was approved before any implementation. |
| `report` | `--run RUN` (required), `--html`, `--open` | Render the run report from the log (Markdown, or HTML with `--html`), print it, and write it into the run dir. `--open` also opens it in a browser (best-effort). |
| `clean` | `--run RUN` (required), `--force` | Delete a **finished** run's directory. Refuses any path that isn't a run dir (must contain `runlog.jsonl`), and refuses a run that hasn't finished every configured phase — PLAN (plus REPRODUCE / FINAL when enabled) each reached consensus/proceeded and every node `done` — unless `--force` is given, so a run still in REPRODUCE/PLAN (no DAG yet) or with all nodes done but FINAL pending is preserved, not silently deleted. |
| `critic-lenses` | `--run RUN` (required) | Print the operator's configured Critic lenses (`critic_checklists`) as one fenced block — each lens file's contents, labelled with size + a short sha256 — for the orchestrator to append to the Critic seed as additive DATA (subordinate to `critic-prompt.md` and the verdict schema). Fail-closed: a missing / relative / symlink / oversized lens prints to stderr and exits non-zero. Prints nothing when `critic_checklists` is unset. |

## Symmetric workflows (deep-dive / pr-review)

The `deep-dive` and `pr-review` companion skills run a **symmetric two-peer** flow instead of the
asymmetric Builder/Critic loop. `init-run --workflow deep-dive|pr-review` records the immutable
workflow kind, and every gate is settled by **two separately produced peer attestation files** rather
than a single `verdict`. The build workflow behavior and `verdict` semantics are unchanged.

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `symmetric-verdict` | `--run RUN`, `--gate GATE`, `--round ROUND` (required), `--peer-a FILE`, `--peer-b FILE` (required), `--max-rounds M` | Atomically decide a symmetric gate from a **Peer A** and a **Peer B** attestation file. Each attestation is `{"peer": "A"\|"B", "gate", "round", "verdict": "APPROVE"\|"REQUEST_CHANGES", "summary", "objections": [Finding…], + echoed bindings}`; both files must **echo** the exact `crucible bindings` (a mismatch in either is rejected before any append). The decision is computed from the **union of the two peers' objections** — `CONSENSUS` iff neither peer has a blocking objection, else `CHANGES` / `CAPPED` / `PROCEED_WITH_FLAGS` — never from an accepted finding's severity. For a `dep:`/`final` gate the bound Builder artifact is a structured **finding set** (`{"summary", "findings": [{"source_gate", "id", "severity", "location", "claim", "suggestion"}]}`) that is validated and persisted as the accepted set on an advancing outcome. Takes **no** `--resolutions`. Rejects a `build` run (which uses `verdict`). |
| `accepted-findings` | `--run RUN` (required) | Print the deterministic union of accepted **dependency** finding sets (DAG topological order, keyed by `(source_gate, id)`) as canonical JSON. The FINAL candidate is assembled from this output plus cross-cutting `source_gate: final` findings; the CLI rejects a FINAL set that drops or alters an accepted dependency finding. Fails closed on an incomplete or out-of-scope run. |
| `review-result` | `--run RUN` (required) | The Finish-time deliverable: `{"workflow", "findings": [...], "unresolved_objections": [...]}`, plus — for `pr-review` only — the authoritative `target_sha256` binding the result to the reviewed target and a derived `recommendation`: any accepted finding in `blocking_severities` or any unresolved blocking objection → `REQUEST_CHANGES`; any other accepted finding → `COMMENT`; none → `APPROVE`. `deep-dive` omits `recommendation` and `target_sha256` (an investigation returns a finding set). Rejects a `build` run, a corrupt/absent target, or an incomplete symmetric run. |

The report renders **Peer A** / **Peer B** headers (from the two configured slots) and, for
`pr-review`, a `**Review recommendation:**` line. That review recommendation is **separate** from the
workflow status (`CLEAN` / `FLAGGED` / …): a `CLEAN` run is never treated as synonymous with an
`APPROVE`.

**Trust boundary — slot proof, not process identity.** A symmetric decision proves that **two
configured slots** (`A` and `B`) each supplied a valid attestation bound to the same candidate, and
records each slot's configured model/effort. It **does not cryptographically prove** that two distinct
model *processes* produced the files — runtime peer independence is a platform/orchestrator property,
not a CLI guarantee.

## Target binding (pr-review)

A `pr-review` run pins its input to one **immutable review target** before PLAN, and every gate binds
its `target_sha256` (a `build`/`deep-dive` run has no target). The orchestrator prepares the manifest +
exact patch, loads it once, and — for a revision-bound target — materializes a pinned, read-only
**source snapshot**; only `load-target` / `materialize-target` mutate a run.

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `normalize-target github` | `--metadata-before FILE`, `--metadata-after FILE`, `--compare-metadata FILE`, `--merge-base-archive FILE`, `--head-archive FILE`, `--output FILE`, `--diff-output FILE` | Normalize a GitHub PR into a `github-pr` manifest + **derived** exact patch **without** mutating a run, using immutable PR-style **merge-base** semantics. Consumes the `gh pr view --json …` documents captured **before and after** fetching the compare metadata and the merge-base/head snapshots; every immutable identity field (number/url/title/body, base/head repository + `baseRefOid`/`headRefOid`, `isCrossRepository`) must match between the two reads or the target is rejected (retry the acquisition). The changed-file list is **not** an identity field — GitHub's `files` view paginates/truncates and rename-detects, so it may differ between the reads without the target changing. The patch is **derived** from the `--merge-base-archive` (base `repository@merge_base_commit.sha`, the fork point the base repo's exact-OID `--compare-metadata` (`compare/<baseRefOid>...<headRefOid>`) payload reports) and the `--head-archive` (head `repository@headRefOid`) codeload snapshots — never `gh pr diff`, a base-tip two-dot diff, or a caller-supplied patch — so a base-only commit made after the fork never appears as a reverse change; the compare `base_commit.sha` must equal `baseRefOid` and the `merge_base_commit.sha` is recorded as `merge_base_sha`. `changed_files` is derived **solely** from the snapshot patch; GitHub's paginated/rename-detected `files` views are informational and never gate it (no strict external file-list equality is required). Branch names are display metadata; OIDs and repository identities are authoritative. |
| `normalize-target local` | `--repo DIR`, `--range BASE..HEAD`, `--intent FILE`, `--output FILE`, `--diff-output FILE` | Normalize a local Git range into a `local-range` manifest + patch. One `--range` (`BASE..HEAD` or `BASE...HEAD`, both normalize to `merge_base..head`) — no separate base/head flags — records base/head/**merge-base** SHAs and a credential-free `repository` identity, and always diffs merge-base..head so a base-only commit is never a reverse change. |
| `normalize-target diff` | `--diff FILE`, `--intent FILE`, `--output FILE`, `--diff-output FILE` | Normalize a bare patch into a `diff-file` manifest (`revision_bound: false`) — **patch identity only**, no repository/base/head and no source snapshot. |
| `repository-identity` | `--repo DIR` | Print the credential-free repository identity local normalization would record (sanitized remote URL, else `local:<sha256(real path)>`), so a checkout can be compared to a recorded local-range identity without exposing credentials or a local path. |
| `load-target` | `--run RUN`, `--file FILE`, `--diff FILE` | Record the one immutable `target_loaded` event for a `pr-review` run: validate the manifest, verify `--diff` bytes hash to the manifest `diff_sha256`, write the canonical scratch (`target.json` + `target.diff`) **before** appending the canonical payload + `target_sha256`. Valid **once**, before `load-dag`/PLAN/any review event; a correction requires a **fresh run**. **Crash-repairable:** a rerun with the SAME inputs repairs the scratch without a duplicate append (or appends if a crash left no event); a DIFFERENT target is rejected. |
| `show-target` | `--run RUN` | Print the authoritative loaded target (from the `target_loaded` event, never the scratch file) as canonical JSON. Fails closed when none is loaded or the event is duplicate/malformed/hash-mismatched. |
| `materialize-target` | `--run RUN`, `--archive FILE` | Extract a pinned, read-only source snapshot into an absent `RUN/source`, one-shot, for a revision-bound target immediately after `load-target`. Confined extraction rejects absolute/`..`/backslash paths, symlinks/hardlinks/devices/FIFOs, duplicate normalized paths, and archives over the 100 000-member / 1 GiB caps; wrapper stripping is derived from the target **kind** (github-pr strips its codeload wrapper; local-range preserves paths). A diff-file target has no snapshot. A canonical receipt (`target_sha256`, `archive_sha256`, `kind`) is written inside `RUN/source`; extraction → receipt → atomic rename → append, so a crash is **idempotently repairable** — a same-archive rerun appends without re-extracting (append crashed after the source exists), restores a lost source without a duplicate event (event exists, source gone), and rejects a mismatched receipt/event without deleting an authoritative source. |

Every `pr-review` gate's `crucible bindings` output therefore also carries `target_sha256`, which each
peer attestation echoes and `symmetric-verdict` verifies. The report renders a `## Review target`
section (base/head/merge-base SHAs, or the revision-unbound diff-file status) from the same event.

## Report statuses

For a schema-2 run, `report` derives its status from the run's resolved configuration and the
append-only log, in this precedence (highest first):

1. **`LEGACY / UNVERIFIED`** — a pre-schema-2 run; its provenance cannot be content-bound (never `CLEAN`).
2. **`INVALID`** — a recorded binding no longer matches the current artifact/tree, an approval is stale, or an illegal/out-of-order transition is in the log.
3. **`BLOCKED`** — a required gate capped with unresolved findings.
4. **`FLAGGED`** — a gate proceeded with flags, or a node completed outside an accepted review gate (`--force`).
5. **`CLEAN`** — every configured phase is present, ordered, accepted, and currently bound, and all nodes are `done`.
6. **`IN PROGRESS`** — a required configured phase or node is still incomplete.

The summary names any missing or invalid required phase explicitly. The report is deterministic from
the log plus the current DAG; it does not claim tamper resistance.

See the [README](../README.md) for the high-level workflow and configuration, and
[`skills/crucible/SKILL.md`](../skills/crucible/SKILL.md) for the full orchestration spec.
