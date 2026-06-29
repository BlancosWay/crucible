# Crucible

**Two-model adversarial planning and implementation, on top of [Superpowers](https://github.com/obra/superpowers).**

One model (**Builder**) plans and implements; a second model (**Critic**) adversarially reviews
the plan, the dependency tree, and every dependency as it is built — looping each gate until the
Critic signs off (**consensus**) or a configured round cap is hit.

## How it works

1. **PLAN gate** — Builder uses Superpowers `writing-plans` to produce a plan **and** a dependency
   tree (DAG). Critic reviews both; loop until consensus or `max_rounds_plan`.
2. **IMPLEMENT gates** — for each dependency in topological order, Builder implements it
   (`subagent-driven-development`, TDD) and the Critic reviews that diff; loop until consensus or
   `max_rounds_dep`.
3. **FINAL gate** — optional whole-implementation review, then a deterministic run report.

Consensus = Critic returns `APPROVE` (no open findings whose severity is in the configured
`blocking_severities`, default `blocker`/`major`). On a round cap without
consensus, Crucible **halts and surfaces** the unresolved findings (configurable).

## Defaults

| Setting | Default |
|---------|---------|
| Builder | `claude-opus-4.8` (effort `max`) |
| Critic | `gpt-5.5` (effort `xhigh`) |
| Rounds per gate | 5 |
| On cap | `halt` |

Override via a JSON config (see `config.example.json`).

## Install

Crucible is a Copilot CLI plugin (and runs on Claude Code / Codex). It needs **no MCP servers and
no API keys** — only [Superpowers](https://github.com/obra/superpowers) and Python 3.11+. Install
**locally** from this repo:

```bash
copilot plugin marketplace add ~/personal/crucible
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
PYTHONPATH=scripts python3 -m crucible load-dag --run "$RUN" --file dag.json
PYTHONPATH=scripts python3 -m crucible next --run "$RUN"
PYTHONPATH=scripts python3 -m crucible verdict --run "$RUN" --gate plan --round 1 --file verdict.json
PYTHONPATH=scripts python3 -m crucible report --run "$RUN" --html
```

## Development

```bash
python -m pytest -q     # run the test suite (pytest.ini sets pythonpath=scripts)
```

## Layout

- `skills/crucible/` — orchestrator skill + role prompts and rubric (`references/`).
- `commands/crucible.md` — `/crucible` entry point.
- `scripts/crucible/` — deterministic helpers: `config`, `dag`, `verdict`, `runlog`, `report`, `cli`.
- `.claude-plugin/` — plugin + marketplace manifests.
- `docs/install/` — per-platform install guides (Copilot CLI, Claude Code, Codex).

Engineering tool. Not affiliated with any model provider.
