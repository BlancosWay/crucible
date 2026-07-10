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
