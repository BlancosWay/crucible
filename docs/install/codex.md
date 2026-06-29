# Crucible on OpenAI Codex

Install, set up, and run Crucible with **OpenAI Codex** (`codex`). Crucible is a two-model
adversarial planning + implementation workflow — an **engineering tool** built on
[Superpowers](https://github.com/obra/superpowers). See also [`AGENTS.md`](../../AGENTS.md).

Codex has **no plugin marketplace**: it discovers **skills** from `~/.agents/skills/`, reads
`AGENTS.md`, and runs the Critic **inline** (no native sub-agents). So everyone installs the same
way — by cloning and symlinking. The only difference between options is whether you also develop.
Canonical steps: [`.codex/INSTALL.md`](../../.codex/INSTALL.md).

## Prerequisites

- **OpenAI Codex** (`codex`) and **git** (the install is clone-based).
- **Superpowers** installed (Crucible dispatches its reviewers; on Codex they run inline).
- **Python 3.11+** for the deterministic `crucible` CLI.

Crucible needs no MCP servers and no API keys.

## Install (clone and symlink the skill)

```bash
git clone https://github.com/BlancosWay/crucible.git ~/.codex/crucible
mkdir -p ~/.agents/skills
ln -s ~/.codex/crucible/skills/crucible ~/.agents/skills/crucible
```

Then copy `AGENTS.md` to `~/.codex/AGENTS.md` (or append its contents). The Windows symlink form
is in [`.codex/INSTALL.md`](../../.codex/INSTALL.md). Restart Codex, then verify:

```bash
ls -l ~/.agents/skills/crucible
```

---

## Option A: Just run it

**Run — interactive.** Start `codex`, then ask:

```text
use crucible to add a rate limiter
```

**Run — headless (one-shot).**

```bash
codex exec "use crucible to add a rate limiter"
```

Codex realizes the Critic inline (a delimited "Acting as Critic now" pass) per the no-subagent
fallback in `references/platform-notes.md`; the loop, gates, and consensus rule are identical.

---

## Option B: Develop and test

The clone from install **is** your dev tree — edits to `~/.codex/crucible` are picked up directly,
no reinstall.

```bash
cd ~/.codex/crucible && python3 scripts/check.py
```

`scripts/check.py` is the offline gate CI runs (structural + links + pytest + shellcheck). See
[`CONTRIBUTING.md`](../../CONTRIBUTING.md).

---

Next: [Usage](../../README.md#usage) · [Defaults](../../README.md#defaults) ·
[Layout](../../README.md#layout).
