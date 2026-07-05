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
- **Node statuses** — `pending`, `in_progress`, `in_review`, `done`, `blocked`.
- **Exit codes** — `0` on success unless noted. `next` and the `should-*` switches use exit codes as
  signals (below).

## Start a run

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `init-run` | `--goal GOAL` (required), `--config FILE`, `--base-dir DIR` | Create a run directory (seeding its `config.json` from `--config`, or defaults) and print its path to stdout. |
| `load-dag` | `--run RUN` (required), `--file FILE` (required); `--force` (optional) | Import the plan's dependency tree from a JSON file. Rejects an empty tree and any node not `pending` (fresh plans start all-`pending`; statuses change only via `set-status`). Also refuses to overwrite a run whose existing DAG already has progress (non-`pending` nodes), which would reset it — pass `--force` to replace it (discards current node statuses). Prints the node count and the tree. |

## Schedule & track progress

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `next` | `--run RUN` (required) | Print the next ready node id on stdout (empty line when every node is `done`). If no node can be scheduled: exit **4** when work is still in flight, exit **3** when the run is STUCK — both with the unfinished nodes and their unmet deps on stderr. |
| `set-status` | `--run RUN`, `--node NODE`, `--status STATUS` (all required); `--force` (optional) | Set a node's status. Refuses to move a node to a work status (`in_progress`/`in_review`/`done`) while any dependency is not `done`. Also refuses to mark a node `done` unless its own `dep:<node>` gate reached consensus (or proceeded with flags) — a capped or never-reviewed node is rejected. `--force` overrides that gate requirement for recovery; the override is recorded only in run-log provenance (the `node_status_change` `forced` flag), so a `--force`d/un-gated node can still render `CLEAN` in the report. |
| `status` | `--run RUN` (required) | Print run progress as JSON (node counts by status). |

## Record gates & adjudicate verdicts

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `log` | `--run RUN`, `--event EVENT`, `--gate GATE`, `--round ROUND` (required), `--file FILE` | Append a Builder or Critic transcript to the run log. `--event` is `builder_output` or `critic_output` (other events are written by their own commands). `--file` supplies the payload; omit it for an empty payload. |
| `verdict` | `--run RUN`, `--gate GATE`, `--round ROUND` (required), `--max-rounds M`, `--resolutions FILE`, `--file FILE` (required) | Adjudicate the Critic's verdict deterministically into `CONSENSUS`, `CHANGES`, `PROCEED_WITH_FLAGS`, or `CAPPED`, honoring `blocking_severities`, `defer_severities`, and `strict_rebuttal`. `--round` must equal the CLI-derived next round for the gate (one past the number of prior `critic_verdict` events, i.e. consecutive starting at 1); a mismatch is rejected, so the round cap cannot be bypassed by skipping to the cap or repeating a round. `--resolutions` is the Builder's per-finding map (`id` → `fixed` / `deferred` / `wontfix`); `--max-rounds` overrides the cap (defaults to `max_rounds_plan`/`max_rounds_dep` by gate). Prints the outcome, the findings the Builder will fix, and any unresolved blocking findings; when the PLAN gate settles it also echoes the approved plan + DAG. |

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
| `clean` | `--run RUN` (required), `--force` | Delete a finished run's directory. Refuses any path that isn't a run dir (must contain `runlog.jsonl`) and any run still in progress unless `--force` is given. |

See the [README](../README.md) for the high-level workflow and configuration, and
[`skills/crucible/SKILL.md`](../skills/crucible/SKILL.md) for the full orchestration spec.
