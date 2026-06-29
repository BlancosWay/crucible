# Crucible on GitHub Copilot CLI

Install, set up, and run Crucible with the **GitHub Copilot CLI** (`copilot`). Crucible is a
two-model adversarial planning + implementation workflow — an **engineering tool** built on
[Superpowers](https://github.com/obra/superpowers).

Follow the option that fits you: **[A — just run it](#option-a-just-run-it)** or
**[B — develop from a local clone](#option-b-develop-from-a-local-clone)**.

## Prerequisites

- **GitHub Copilot CLI** (`copilot`) — `copilot --version`.
- **Superpowers** installed (Crucible dispatches its `writing-plans` / `brainstorming` reviewers
  and the `code-reviewer` agent). Verify Superpowers loads in your session.
- **Python 3.11+** for the deterministic `crucible` CLI invoked by the skill.

Crucible needs **no MCP servers and no API keys**.

---

## Option A: Just run it

**Install — local marketplace.** This repo doubles as a Copilot CLI plugin marketplace; add the
clone as a marketplace and install by name:

```bash
copilot plugin marketplace add ~/personal/crucible
copilot plugin install crucible@crucible-marketplace
```

Confirm it loaded with `copilot plugin list`. The workflow appears as the `crucible:crucible`
skill with `crucible:crucible` as a slash command.

**Run — interactive.** Start a session, then use the slash command (or natural language):

```bash
copilot
```

```text
/crucible add a Redis-backed rate limiter to the API
```

**Run — headless (one-shot).** Pass the goal with `-p`:

```bash
copilot --allow-all-tools --allow-all-paths -p "/crucible add a rate limiter"
```

Defaults: Builder = Opus 4.8 (max), Critic = GPT-5.5 (xhigh), 5 rounds per gate, `on_cap: halt`.

---

## Option B: Develop from a local clone

**Extra dependencies.** **Python 3.11+** and **git**. Set up the test venv:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install pytest
```

**Install — from your clone.** Re-run install to pick up edits (cached):

```bash
git clone https://github.com/BlancosWay/crucible.git
cd crucible
copilot plugin marketplace add .
copilot plugin install crucible@crucible-marketplace
```

After editing the skill (`skills/crucible/`), the command, or a script, **re-run**
`copilot plugin install crucible@crucible-marketplace` to reload the cached components.

**Test your changes.**

```bash
python3 scripts/check.py
```

`check.py` is the offline gate CI runs — structural + links + pytest + shellcheck. Keep it green.
See [`CONTRIBUTING.md`](../../CONTRIBUTING.md).

---

Next: [Usage](../../README.md#usage) · [Defaults](../../README.md#defaults) ·
[Layout](../../README.md#layout).
