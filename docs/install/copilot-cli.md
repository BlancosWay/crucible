# Crucible on GitHub Copilot CLI

Install, set up, and run Crucible with the **GitHub Copilot CLI** (`copilot`). Crucible is a
two-model adversarial planning + implementation workflow — an **engineering tool** built on
[Superpowers](https://github.com/obra/superpowers).

Follow the option that fits you: **[A — just run it](#option-a-just-run-it)** or
**[B — develop from a local clone](#option-b-develop-from-a-local-clone)**.

## Prerequisites

- **GitHub Copilot CLI** (`copilot`) — `copilot --version`.
- **Superpowers** installed — **v5.1.0+, last tested against v6.0.3** (Crucible dispatches its
  `writing-plans` / `brainstorming` reviewers and the `requesting-code-review` reviewer template).
  Verify Superpowers loads in your session.
- **Python 3.11+** for the deterministic `crucible` CLI invoked by the skill.

Crucible needs **no MCP servers and no API keys**.

---

## Option A: Just run it

**Install — from GitHub.** This repo doubles as a Copilot CLI plugin marketplace; add it straight
from GitHub and install by name (no clone needed):

```bash
copilot plugin marketplace add BlancosWay/crucible
copilot plugin install crucible@crucible-marketplace
```

Confirm it loaded with `copilot plugin list`. The workflow appears as the `crucible:crucible`
skill with `crucible:crucible` as a slash command.

**Updating.** `copilot plugin install` installs from the marketplace's **cached** catalog and never
re-fetches it, so re-running `install` alone reinstalls the same version. To move to a newly published
release, refresh the catalog from GitHub first, then reinstall:

```bash
copilot plugin marketplace update crucible-marketplace
copilot plugin install crucible@crucible-marketplace
```

> The same plugin also ships the independent **`deep-dive`** skill (`deep-dive:deep-dive`, slash
> command `/deep-dive <question>`) — a symmetric two-peer investigation of the actual code/data that
> reuses the same `crucible` CLI. See [`skills/deep-dive/SKILL.md`](../../skills/deep-dive/SKILL.md).
>
> It also ships the independent **`pr-review`** skill (`pr-review:pr-review`, slash command
> `/pr-review <pr-or-diff>`) — a symmetric two-peer review of a GitHub PR or a local diff that reuses
> the same `crucible` CLI. A PR-URL and a diff-file review are **static/CI-only** and never execute
> locally; running tests or builds is available only for a **trusted local checkout**, after explicit
> execution **consent** to the exact commands and an arbitrary-code warning (consent does not imply
> sandboxing, and is separate from posting consent). See
> [`skills/pr-review/SKILL.md`](../../skills/pr-review/SKILL.md).

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

Shipped values live in `config.defaults.json`; `init-run` records the resolved values (including
`--config` overrides) in `RUN/config.json`.

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
`copilot plugin install crucible@crucible-marketplace` to reload the cached plugin components. Note
this reloads the installed plugin from the marketplace's **cached** catalog; refreshing a registered
marketplace's catalog (local or GitHub) is a separate step — run
`copilot plugin marketplace update crucible-marketplace` first when you need the catalog itself
re-read (e.g. after a version bump).

**Test your changes.**

```bash
python3 scripts/check.py
```

`check.py` is the offline gate CI runs — structural + links + pytest + shellcheck. Keep it green.
See [`CONTRIBUTING.md`](../../CONTRIBUTING.md).

---

Next: [Usage](../../README.md#usage) · [Defaults](../../config.defaults.json) ·
[Layout](../../README.md#layout).
