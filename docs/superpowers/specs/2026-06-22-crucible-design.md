# Crucible — Design

**Date:** 2026-06-22
**Status:** Draft for review
**Repo:** `~/personal/crucible`

## Goal

A two-model **adversarial** workflow, built on **Superpowers**, in which a **Builder** model
plans and implements while a **Critic** model adversarially reviews at every gate — the plan,
the dependency tree, and each dependency as it is implemented — looping through configured
stages until the Critic signs off (**consensus**) or a configured round cap is reached.

## Non-Goals

- Not a general N-model council. Exactly **two** roles (Builder, Critic), each one model.
- Not a replacement for Superpowers. Crucible *orchestrates* Superpowers skills and injects an
  adversarial two-model loop at each gate.
- Not an autonomous merger. On an unresolved round cap, Crucible **halts and surfaces** to the
  human; it never silently ships unreviewed work.

## Roles (exactly two models)

| Role | Responsibility | Default model |
|------|----------------|---------------|
| **Builder** (model 1) | Brainstorm, plan, build the dependency tree, implement each node, revise in response to critiques | `claude-opus-4.8` (effort `max`) |
| **Critic** (model 2) | Adversarially review each Builder artifact; emit a structured verdict with findings | `gpt-5.5` (effort `xhigh`) |

Models and efforts are configurable (`config.json`). The defaults match the user's established
two-model workflow (Opus builds, GPT-5.5 critiques at high intensity).

### Realization on agent runtimes

The Builder is the **main session**. At each gate the Builder dispatches the **Critic as a
subagent** with a model override:

- **Copilot CLI** (primary): `task` tool with `model` / `reasoning_effort` override.
- **Claude Code / Codex**: their native subagent dispatch with a per-agent model.
- **No-subagent fallback**: the orchestrator runs the Critic prompt as a separate, clearly
  delimited pass and records it the same way.

`skills/crucible/references/platform-notes.md` documents the exact mapping per runtime.

## Stages (adversarial gates)

```
        ┌────────────────────────── PLAN gate ──────────────────────────┐
goal ─▶ │ Builder: brainstorm + writing-plans ⇒ plan + dependency tree   │
        │ Critic: review plan completeness + DAG correctness             │
        │ loop until APPROVE or max_rounds_plan                          │
        └───────────────────────────────────────────────────────────────┘
                                  │ approved plan + DAG
                                  ▼
        ┌──────────── IMPLEMENT gate, per DAG node (topo order) ─────────┐
        │ Builder: implement node (subagent-driven-development / TDD)    │
        │ Critic: review node diff (spec, correctness, security, quality)│
        │ loop until APPROVE or max_rounds_dep ⇒ mark node done          │
        └───────────────────────────────────────────────────────────────┘
                                  │ all nodes done
                                  ▼
        ┌────────────────────── FINAL gate (optional) ──────────────────┐
        │ Critic: whole-implementation review ⇒ run report              │
        └───────────────────────────────────────────────────────────────┘
```

1. **PLAN gate.** Builder runs Superpowers `brainstorming` (if the goal needs shaping) and
   `writing-plans` to produce (a) an implementation plan and (b) a **dependency tree** (DAG of
   implementation tasks). Critic adversarially reviews both: missing tasks, wrong/missing edges,
   bad ordering, hidden coupling, untestable tasks, scope creep. Loop until consensus or
   `max_rounds_plan`.
2. **IMPLEMENT gates — one per DAG node, in topological order.** Builder implements the node
   following Superpowers `subagent-driven-development` (TDD, frequent commits). Critic
   adversarially reviews **that node's diff only**: spec compliance, correctness, security,
   regressions, code quality. Loop until consensus or `max_rounds_dep`, then mark the node
   `done` and advance to the next ready node.
3. **FINAL gate (optional, `final_review: true`).** Critic reviews the whole assembled
   implementation once. Then the deterministic report is rendered from the run-log.

## Dependency tree (DAG)

Produced at the end of the PLAN gate and stored as JSON.

```json
{
  "nodes": [
    {
      "id": "auth-model",
      "title": "User auth model",
      "description": "Define User schema + password hashing in src/auth/model.py",
      "files": ["src/auth/model.py", "tests/auth/test_model.py"],
      "test_plan": "pytest tests/auth/test_model.py",
      "status": "pending"
    }
  ],
  "edges": [
    { "from": "auth-routes", "depends_on": "auth-model" }
  ]
}
```

- `status` ∈ `pending | in_progress | in_review | done | blocked`.
- A deterministic Python module (`dag.py`) owns: parse + **validate acyclic** (raise on cycle),
  `topological_order()`, `ready_nodes()` (pending nodes whose every dependency is `done`),
  `set_status()`, and resumability (status is persisted, so a run can resume mid-walk).
- The shape intentionally mirrors the session `todos` / `todo_deps` pattern, but is persisted as
  a **run artifact** so each run is auditable and resumable independent of any session.

## Consensus & stop criteria

The PLAN gate reviews the plan and the dependency tree **together** under gate id `plan`;
implement gates use `dep:<node_id>`; the optional final gate uses `final`. The Critic emits a
**structured verdict** per gate per round:

```json
{
  "gate": "plan",                       // "plan" | "dep:<node_id>" | "final"
  "round": 1,
  "verdict": "REQUEST_CHANGES",         // "APPROVE" | "REQUEST_CHANGES"
  "summary": "Two blocking gaps in error handling and one missing dependency edge.",
  "findings": [
    {
      "id": "F1",
      "severity": "blocker",            // "blocker" | "major" | "minor" | "nit"
      "location": "src/auth/model.py:42",
      "claim": "Password stored without hashing.",
      "suggestion": "Hash with bcrypt before persistence."
    }
  ]
}
```

**Consensus** = Critic returns `APPROVE`, i.e. **zero open findings** whose severity is in the
**blocking set** (default `["blocker", "major"]`). For each finding the Builder records a
resolution:

- `fixed` — addressed; re-reviewed next round.
- `deferred` — allowed only for severities in `defer_severities` (default `["minor", "nit"]`);
  recorded and surfaced, does not block.
- `wontfix` — a **rebuttal** with rationale. Surfaced and logged. In `strict_rebuttal: true`
  mode, the Critic must explicitly accept the rebuttal for consensus; otherwise an accepted
  rebuttal clears the finding.

**Round caps** (configurable): `max_rounds_plan` (default 5), `max_rounds_dep` (default 5). On
reaching a cap **without** consensus, the `on_cap` policy applies:

- `halt` (default) — stop that gate, persist unresolved findings, surface to the human.
- `proceed_with_flags` — continue, but tag the node/run with unresolved findings in the report.

This makes "configured stages **or** until consensus" explicit: the loop ends on the **first** of
APPROVE or the round cap.

## Provenance run-log

Every run writes a directory under `runs/<timestamp>-<slug>/`:

```
runs/2026-06-22-2150-add-rate-limiter/
  config.json        # snapshot of models + caps + policies for this run
  dag.json           # the approved dependency tree (with live status)
  runlog.jsonl       # append-only event log (see below)
  report.md / .html  # deterministic report rendered from runlog.jsonl
```

`runlog.jsonl` is **append-only**, one JSON event per line. Event types: `run_start`,
`gate_start`, `builder_output`, `critic_verdict`, `builder_resolution`, `gate_consensus`,
`gate_proceeded_with_flags`, `gate_capped`, `node_status_change`, `run_complete`. Each
`builder_output` and `critic_verdict`
event stores the agent's **full raw text** (not a condensed summary) — so the final report and
any audit read directly from the log, never from hand-reconstructed text. (This directly honors
the lesson that condensed-only logs force error-prone manual reconstruction.)

## Architecture & packaging (mirrors the TradingDesk plugin)

```
crucible/
  README.md
  config.example.json              # default models + caps + policies
  .claude-plugin/
    plugin.json
    marketplace.json
  skills/
    crucible/
      SKILL.md                     # orchestration spec — the agentic driver
      references/
        builder-prompt.md          # Builder role/system prompt
        critic-prompt.md           # Critic adversarial role + verdict schema
        consensus-rubric.md        # severity defs, stop criteria, rebuttal rules
        dependency-tree.md         # DAG schema + how the Builder emits it
        platform-notes.md          # per-runtime subagent + model-override mapping
  commands/
    crucible.md                    # /crucible entry point
  scripts/
    crucible/
      __init__.py
      config.py                    # load + validate config (models, caps, policies)
      dag.py                       # DAG: parse, validate acyclic, topo order, ready set, status
      verdict.py                   # parse/validate Critic verdict; consensus check
      runlog.py                    # provenance run-log: init run dir, append events, read
      report.py                    # deterministic run report (md/html) from runlog.jsonl
      cli.py                       # thin CLI wrapping the modules above
  tests/
    test_config.py
    test_dag.py
    test_verdict.py
    test_runlog.py
    test_report.py
    test_cli.py
  docs/superpowers/specs/2026-06-22-crucible-design.md   # this file
```

**Division of responsibility:** the **skill** is the agentic orchestrator (drives Builder/Critic
subagents, invokes Superpowers sub-skills, decides next action). The **scripts** are
deterministic, unit-tested helpers the skill calls for *all* bookkeeping (DAG walk, round
counting, consensus detection, provenance, reporting). The only non-deterministic part is model
reasoning; everything else is auditable and testable.

## CLI surface (`python scripts/crucible/cli.py ...`; or `python -m crucible.cli` with `scripts/` on `PYTHONPATH`)

| Command | Purpose |
|---------|---------|
| `init-run --goal "<text>" [--config path]` | Create run dir, snapshot config, write `run_start`; print run dir. |
| `load-dag --run <dir> --file dag.json` | Validate (acyclic) + store the dependency tree. |
| `next --run <dir>` | Print the next ready node id (deps all `done`), or empty if none. |
| `set-status --run <dir> --node <id> --status <s>` | Transition a node; append `node_status_change`. |
| `log --run <dir> --event <type> --gate <g> --round <n> [--file payload]` | Append a raw event (e.g. `builder_output`). |
| `verdict --run <dir> --gate <g> --round <n> --file verdict.json` | Validate + record a Critic verdict; print `CONSENSUS` / `CHANGES` / `CAPPED` / `PROCEED_WITH_FLAGS`. |
| `status --run <dir>` | Print DAG progress + open findings summary. |
| `report --run <dir> [--html] [--open]` | Render the deterministic report from `runlog.jsonl`. |

## Data flow

1. `/crucible "<goal>"` → skill: `init-run` creates the run dir + config snapshot + `run_start`.
2. **PLAN gate:** Builder (brainstorming + writing-plans) emits plan + `dag.json`; `load-dag`
   validates/stores it. Critic subagent reviews → `verdict`. Loop (each round logged) until
   `CONSENSUS`, or until the cap yields `CAPPED` (halt) / `PROCEED_WITH_FLAGS` (proceed flagged).
3. **Walk DAG:** `next` returns a ready node → Builder implements (subagent-driven-development) →
   Critic reviews the node diff → `verdict` loop → `set-status done`. Repeat until `next` is
   empty.
4. **FINAL gate** (optional): Critic whole-impl review → `report` renders the run report.

## Configuration (`config.example.json`)

```json
{
  "builder": { "model": "claude-opus-4.8", "effort": "max" },
  "critic":  { "model": "gpt-5.5", "effort": "xhigh" },
  "max_rounds_plan": 5,
  "max_rounds_dep": 5,
  "on_cap": "halt",
  "defer_severities": ["minor", "nit"],
  "blocking_severities": ["blocker", "major"],
  "strict_rebuttal": false,
  "final_review": true
}
```

## Testing

- **Deterministic helpers are fully unit-tested** with `pytest`:
  - `dag.py`: acyclicity (cycle raises), topological order, ready-set, status transitions, resume.
  - `verdict.py`: schema validation, consensus decision across severity mixes, rebuttal handling.
  - `runlog.py`: run-dir init, append-only events, full-text round-trip, read-back.
  - `config.py`: defaults, overrides, validation errors.
  - `report.py`: render from a fixture run-log; assert findings + resolutions + DAG status appear.
  - `cli.py`: end-to-end on a temp run dir using a **dry-run mode** with stubbed Builder/Critic
    payloads — exercises the orchestration bookkeeping without any model call.
- The agentic loop itself is specified in `SKILL.md` with a worked end-to-end example.

## Safety / guardrails

- The Critic treats Builder output and any fetched content as **data, not instructions** (carry
  over the untrusted-input stance); it flags any embedded instruction to change behavior.
- Never advance past a gate without consensus unless `on_cap: proceed_with_flags`.
- Run on a git worktree/branch via Superpowers `using-git-worktrees`; never implement on
  `main`/`master` without explicit consent.

## Open questions (safe defaults chosen; revisit on review)

1. **Builder = main session vs. dispatched subagent.** Default: Builder is the main session (so
   it retains plan context); Critic is the dispatched second model. A future `both_subagents`
   mode could dispatch both for stricter isolation.
2. **Installable plugin vs. local-only.** Default: ship `.claude-plugin/` so it can be installed
   like TradingDesk, but it also runs from a local clone.
3. **Per-node vs. per-file granularity** for the implement gate. Default: per **node** (a node
   may touch several files); finer granularity is a node-decomposition choice made during PLAN.
