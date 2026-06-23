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

Consensus = Critic returns `APPROVE` (no open `blocker`/`major` findings). On a round cap without
consensus, Crucible **halts and surfaces** the unresolved findings (configurable).

## Defaults

| Setting | Default |
|---------|---------|
| Builder | `claude-opus-4.8` (effort `max`) |
| Critic | `gpt-5.5` (effort `xhigh`) |
| Rounds per gate | 5 |
| On cap | `halt` |

Override via a JSON config (see `config.example.json`).

## Usage

In an agent runtime with Superpowers installed, run the skill:

- Slash command: `/crucible <goal>`
- Or ask: "use crucible to add a rate limiter".

The skill drives the loop and calls the deterministic CLI for every decision:

```bash
RUN=$(python -m crucible init-run --goal "add a rate limiter")
python -m crucible load-dag --run "$RUN" --file dag.json
python -m crucible next --run "$RUN"
python -m crucible verdict --run "$RUN" --gate plan --round 1 --max-rounds 5 --file verdict.json
python -m crucible report --run "$RUN" --html
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

Engineering tool. Not affiliated with any model provider.
