# Crucible on Claude Code

Install, set up, and run Crucible with **Claude Code** (`claude`). Crucible is a two-model
adversarial planning + implementation workflow — an **engineering tool** built on
[Superpowers](https://github.com/obra/superpowers). See also [`CLAUDE.md`](../../CLAUDE.md).

Follow the option that fits you: **[A — just run it](#option-a-just-run-it)** or
**[B — develop from a local clone](#option-b-develop-from-a-local-clone)**.

## Prerequisites

- **Claude Code** (`claude`).
- **Superpowers** installed — **v5.1.0+, last tested against v6.0.3** (Crucible dispatches its
  `writing-plans` / `brainstorming` reviewers and the `requesting-code-review` reviewer template).
- **Python 3.11+** for the deterministic `crucible` CLI.

Crucible needs **no MCP servers and no API keys**.

---

## Option A: Just run it

**Install — from GitHub (no clone needed).** Add this repo as a marketplace and install by name:

```text
/plugin marketplace add BlancosWay/crucible
/plugin install crucible
```

The workflow loads as the `crucible:crucible` skill with `crucible:crucible` as a slash command.

> The same plugin also ships the independent **`deep-dive`** skill (`deep-dive:deep-dive`, slash
> command `/deep-dive <question>`) — a symmetric two-peer investigation of the actual code/data that
> reuses the same `crucible` CLI. See [`skills/deep-dive/SKILL.md`](../../skills/deep-dive/SKILL.md).
>
> It also ships the independent **`pr-review`** skill (`pr-review:pr-review`, slash command
> `/pr-review <pr-or-diff>`) — a symmetric two-peer review of a GitHub PR or a local diff that reuses
> the same `crucible` CLI. See [`skills/pr-review/SKILL.md`](../../skills/pr-review/SKILL.md).

…or read a local clone directly with `--plugin-dir` — edits apply immediately, with no marketplace
cache to refresh:

```bash
git clone https://github.com/BlancosWay/crucible.git
claude --plugin-dir /path/to/crucible
```

**Run — interactive.**

```text
/crucible add a Redis-backed rate limiter     # or: "use crucible to add a rate limiter"
```

**Model fidelity.** The judges are tuned for a top-tier model; for full fidelity run on Opus —
`claude --model opus`. On Claude Code the gate roles run on the session model unless per-gate
overrides are accepted. See [`CLAUDE.md`](../../CLAUDE.md).

---

## Option B: Develop from a local clone

**Extra dependencies.** **Python 3.11+** and **git**:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install pytest
```

**Run.** Pass `--plugin-dir` so the run uses your clone's edits:

```bash
claude --plugin-dir /path/to/crucible        # then /crucible <goal>
```

**Test your changes.**

```bash
cd /path/to/crucible && python3 scripts/check.py
```

`scripts/check.py` is the offline gate CI runs (structural + links + pytest + shellcheck) — keep
it green. See [`CONTRIBUTING.md`](../../CONTRIBUTING.md).

---

Next: [Usage](../../README.md#usage) · [Defaults](../../config.defaults.json) ·
[Layout](../../README.md#layout).
