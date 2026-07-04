# Crucible

**Two-model adversarial planning and implementation, on top of [Superpowers](https://github.com/obra/superpowers).**

One model (**Builder**) plans and implements; a second model (**Critic**) adversarially reviews
the plan, the dependency tree, and every dependency as it is built — looping each gate until the
Critic signs off (**consensus**) or a configured round cap is hit.

## How it works

0. **REPRODUCE gate** *(bug fixes only; off by default — `reproduce_gate`)* — Builder first proves the
   bug with a **failing test**; the Critic confirms the test genuinely reproduces it before any plan.
1. **PLAN gate** — Builder uses Superpowers `writing-plans` to produce a plan **and** a dependency
   tree (DAG). Critic reviews both; loop until consensus or `max_rounds_plan`. Optionally pause here
   for your explicit OK after consensus (`human_approval`).
2. **IMPLEMENT gates** — for each dependency in topological order, Builder implements it
   (`subagent-driven-development`, TDD) and the Critic reviews that diff; loop until consensus or
   `max_rounds_dep`.
3. **FINAL gate** — optional whole-implementation review (`final_review`), then a deterministic run
   report.

Consensus = Critic returns `APPROVE` (no open findings whose severity is in the configured
`blocking_severities`, default `blocker`/`major`). On a round cap without
consensus, Crucible **halts and surfaces** the unresolved findings (configurable via `on_cap`).

## Configuration

Every setting has a default, so a config file is optional. Override any subset via a JSON file
(`--config`, see [`config.example.json`](config.example.json)); unset keys keep their defaults.

| Key | Default | Meaning |
|-----|---------|---------|
| `builder` | `{"model": "claude-opus-4.8", "effort": "max"}` | Model + effort that plans and implements. |
| `critic` | `{"model": "gpt-5.5", "effort": "xhigh"}` | Model + effort that adversarially reviews every gate. |
| `max_rounds_plan` | `5` | Round cap for the PLAN gate before `on_cap` applies. |
| `max_rounds_dep` | `5` | Round cap for **each** IMPLEMENT gate. |
| `on_cap` | `halt` | At a round cap without consensus: `halt`, or `proceed_with_flags` (advance and record the open findings). |
| `blocking_severities` | `["blocker", "major"]` | Severities that keep a gate from reaching consensus. |
| `defer_severities` | `["minor", "nit"]` | Severities the Builder may defer (acknowledge without fixing) without blocking. |
| `strict_rebuttal` | `false` | `false`: a Builder `wontfix` rebuttal clears the finding. `true`: it stays blocking until the Critic explicitly accepts it. |
| `final_review` | `true` | Run the Stage 3 FINAL gate (whole-diff review). |
| `human_approval` | `false` | Pause after PLAN consensus for your explicit OK before any implementation. |
| `reproduce_gate` | `false` | Run the Stage 0 REPRODUCE gate (failing-test-first) before PLAN — for bug fixes. |

## Install

Crucible is a Copilot CLI plugin (and runs on Claude Code / Codex). It needs **no MCP servers and
no API keys** — only [Superpowers](https://github.com/obra/superpowers) and Python 3.11+. Add this
repo as a marketplace and install by name (no clone needed):

```bash
copilot plugin marketplace add BlancosWay/crucible
copilot plugin install crucible@crucible-marketplace
```

Then `/crucible <goal>`. Full per-platform steps: **[Copilot CLI](docs/install/copilot-cli.md)** ·
**[Claude Code](docs/install/claude-code.md)** · **[Codex](docs/install/codex.md)**.

## Usage

In an agent runtime with Superpowers installed, run the skill:

- Slash command: `/crucible <goal>`
- Or ask: "use crucible to add a rate limiter".

The skill drives the loop and calls the deterministic CLI for every decision:

```bash
RUN=$(PYTHONPATH=scripts python3 -m crucible init-run --goal "add a rate limiter")
PYTHONPATH=scripts python3 -m crucible load-dag --run "$RUN" --file "$RUN"/dag.json
PYTHONPATH=scripts python3 -m crucible next --run "$RUN"
PYTHONPATH=scripts python3 -m crucible verdict --run "$RUN" --gate plan --round 1 --file "$RUN"/verdict.json
PYTHONPATH=scripts python3 -m crucible report --run "$RUN" --html
```

Scratch files (`dag.json`, `plan.md`, `verdict.json`, …) live under `"$RUN"/`, and runs default to
`~/.crucible/runs` (override `--base-dir`/`$CRUCIBLE_RUNS_DIR`), so nothing is written into the
target repo. Delete a finished run with `crucible clean --run "$RUN"` (refuses in-progress runs).

### Usage patterns

Same loop, different config — pass a JSON file with `--config` on `init-run`; unset keys keep their
defaults.

- **Add a feature** *(default)* — the flow above: `init-run` → `load-dag` → per-gate `next` /
  `verdict` → `report`.
- **Fix a bug** — turn on the REPRODUCE gate so a failing test comes first:
  ```bash
  echo '{"reproduce_gate": true}' > cfg.json
  RUN=$(PYTHONPATH=scripts python3 -m crucible init-run --goal "fix the crash" --config cfg.json)
  ```
- **Override defaults** — e.g. a lighter Critic, advance past a stuck gate, or skip FINAL:
  ```json
  {"critic": {"model": "gpt-5.5", "effort": "high"}, "on_cap": "proceed_with_flags", "final_review": false}
  ```
- **Inspect / clean up** — `crucible status --run "$RUN"` (JSON progress), `crucible show-plan --run
  "$RUN"` (approved plan + DAG), `crucible report --run "$RUN" --open` (render + open in a browser),
  `crucible clean --run "$RUN"` (delete a finished run).

Full command reference — all 13 subcommands with their arguments, grouped by phase:
**[docs/cli.md](docs/cli.md)**.

## Development

```bash
python -m pytest -q     # run the test suite (pytest.ini sets pythonpath=scripts)
```

## Layout

- `skills/crucible/` — orchestrator skill + role prompts and rubric (`references/`).
- `commands/crucible.md` — `/crucible` entry point.
- `scripts/crucible/` — deterministic helpers: `config`, `dag`, `verdict`, `runlog`, `report`, `cli`.
- `.claude-plugin/` — plugin + marketplace manifests.
- `docs/cli.md` — full CLI reference; `docs/install/` — per-platform install guides.

Engineering tool. Not affiliated with any model provider.
