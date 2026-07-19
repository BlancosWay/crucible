# Installing the Crucible skill on Codex

Codex has **no plugin marketplace**; it discovers **skills** from `~/.agents/skills/` and reads
`AGENTS.md`. Install by cloning and symlinking the `crucible` skill:

```bash
git clone https://github.com/BlancosWay/crucible.git ~/.codex/crucible
mkdir -p ~/.agents/skills
ln -s ~/.codex/crucible/skills/crucible ~/.agents/skills/crucible
```

Or, from a clone you already have:

```bash
mkdir -p ~/.agents/skills
ln -s /path/to/crucible/skills/crucible ~/.agents/skills/crucible
```

The repo also ships an independent second skill, **`deep-dive`** (a symmetric two-peer investigation
of the actual code/data, reusing the same `crucible` CLI). Symlink it the same way to enable
`/deep-dive`:

```bash
ln -s /path/to/crucible/skills/deep-dive ~/.agents/skills/deep-dive
```

The repo also ships an independent third skill, **`pr-review`** (a symmetric two-peer review of a
GitHub PR or a local diff against the real code, reusing the same `crucible` CLI). Symlink it the same
way to enable `/pr-review`:

```bash
ln -s /path/to/crucible/skills/pr-review ~/.agents/skills/pr-review
```

`pr-review` is **read-only** over the target and treats a reviewed change as untrusted: a PR-URL and
a diff-file review are **static/CI-only** and never execute locally; running tests or builds is
available only for a **trusted local checkout**, after explicit execution **consent** to the exact
commands and an arbitrary-code warning (consent does not imply sandboxing, and is separate from
posting consent).

Then load the instructions: copy `AGENTS.md` to `~/.codex/AGENTS.md` (or append its contents).

Windows (PowerShell, developer mode or admin):

```powershell
git clone https://github.com/BlancosWay/crucible.git $HOME\.codex\crucible
New-Item -ItemType Directory -Force -Path $HOME\.agents\skills | Out-Null
New-Item -ItemType SymbolicLink -Path $HOME\.agents\skills\crucible `
  -Target $HOME\.codex\crucible\skills\crucible
```

Restart Codex, then verify:

```bash
ls -l ~/.agents/skills/crucible
```

The clone is also your dev tree — edits to `~/.codex/crucible` are picked up directly, no
reinstall. Superpowers must be installed separately — **v5.1.0+, last tested against v6.0.3**
(Crucible dispatches its reviewers). Run the
test gate with `cd ~/.codex/crucible && python3 scripts/check.py`.
