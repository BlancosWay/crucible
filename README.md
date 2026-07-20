# Crucible

**Two-model adversarial planning and implementation, on top of [Superpowers](https://github.com/obra/superpowers).**

One model (**Builder**) plans and implements; a second model (**Critic**) adversarially reviews
the plan, the dependency tree, and every dependency as it is built — looping each gate until the
Critic signs off (**consensus**) or a configured round cap is hit.

The Critic runs on a **separate model** and is realized as the matching **Superpowers reviewer**:
the `writing-plans` **plan-document-reviewer** at the PLAN gate, and the `requesting-code-review`
**code-reviewer** at the IMPLEMENT and FINAL gates.

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
A node advances (its dependents unblock) only once its own gate reaches consensus or
proceeds-with-flags; `crucible set-status --force` is an explicit human recovery override that
marks a node `done` without that — recorded only in run-log provenance, not normal gate advancement.

**Every gate decision is bound to the exact reviewed artifact.** New runs are **schema v2**: the CLI
hashes the Builder artifact and the DAG/node definition into canonical SHA-256 **content bindings**,
the Critic verdict must **echo** those `artifact_sha256`/`dag_sha256`/`node_sha256` values, and the
configured phase order (REPRODUCE → PLAN → optional approval → dependencies → optional FINAL) and
legal node transitions are enforced. The accepted plan/DAG and each reviewed node cannot change after
acceptance, so a `CLEAN` report means every configured phase was present, ordered, accepted, and still
bound to the current artifacts; human approval, when enabled, is recorded with `crucible approve-plan`.
A pre-schema-2 **legacy** run stays readable but reports `LEGACY / UNVERIFIED` and cannot be mutated
(start a fresh run). See [SECURITY.md](SECURITY.md) and [docs/cli.md](docs/cli.md).

## Configuration

Every setting has a shipped default in [`config.defaults.json`](config.defaults.json). Override any
subset via `--config`; `init-run` writes the fully resolved values to `RUN/config.json`.
The defaults file is also a complete valid `--config` template.

| Key | Meaning |
|-----|---------|
| `builder` | Model + effort that plans and implements. Only `model`/`effort` are accepted; an unknown nested key is rejected. |
| `critic` | Model + effort that adversarially reviews every gate. Only `model`/`effort` are accepted; an unknown nested key is rejected. |
| `max_rounds_plan` | Round cap for the PLAN gate before `on_cap` applies. |
| `max_rounds_dep` | Round cap for each IMPLEMENT gate. |
| `on_cap` | At a round cap without consensus: `halt`, or `proceed_with_flags` to advance and record open findings. |
| `blocking_severities` | Severities that keep a gate from reaching consensus. |
| `defer_severities` | Severities the Builder may defer without blocking. |
| `strict_rebuttal` | Whether a Builder `wontfix` rebuttal stays blocking until the Critic accepts it. |
| `final_review` | Whether to run the Stage 3 FINAL gate. |
| `human_approval` | Whether to pause after PLAN consensus before implementation. |
| `reproduce_gate` | Whether bug-fix runs start with the Stage 0 REPRODUCE gate. |
| `critic_checklists` | Optional list of **absolute** paths to operator-provided checklist files appended to the Critic prompt at each gate as additive "lenses" (empty by default). |

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

> **Superpowers compatibility.** Crucible needs **Superpowers v5.1.0+** and is **last tested
> against v6.0.3**. It drives Superpowers reviewer *templates* (e.g. `requesting-code-review`'s
> `code-reviewer.md`) rather than the `superpowers:code-reviewer` named agent that Superpowers
> removed in v5.1.0 — dispatched as general-purpose subagents on subagent-capable runtimes, and run
> inline on Codex. Re-validate Crucible after a major Superpowers upgrade.

## Usage

In an agent runtime with Superpowers installed, run the skill:

- Slash command: `/crucible <goal>`
- Or ask: "use crucible to add a rate limiter".

The skill drives the loop and calls the deterministic CLI for every decision:

```bash
RUN=$(PYTHONPATH=scripts python3 -m crucible init-run --goal "add a rate limiter")
PYTHONPATH=scripts python3 -m crucible load-dag --run "$RUN" --file "$RUN"/dag.json
PYTHONPATH=scripts python3 -m crucible log --run "$RUN" --event builder_output --gate plan --round 1 --file "$RUN"/plan.md
BINDINGS=$(PYTHONPATH=scripts python3 -m crucible bindings --run "$RUN" --gate plan --round 1)   # Critic echoes these
PYTHONPATH=scripts python3 -m crucible verdict --run "$RUN" --gate plan --round 1 --file "$RUN"/verdict.json
PYTHONPATH=scripts python3 -m crucible next --run "$RUN"        # first ready node — only after PLAN is accepted
PYTHONPATH=scripts python3 -m crucible report --run "$RUN" --html
```

Scratch files (`dag.json`, `plan.md`, `verdict.json`, …) live under `"$RUN"/`, and runs default to
`~/.crucible/runs` (override `--base-dir`/`$CRUCIBLE_RUNS_DIR`), so nothing is written into the
target repo. Delete a finished run with `crucible clean --run "$RUN"` (refuses in-progress runs).

### Companion skill: `deep-dive`

The repo also ships an independent second skill, **`deep-dive`** (`/deep-dive <question>`) — a
two-model **symmetric** adversarial *investigation* rather than construction. Two **equal peers**
(no Builder/Critic asymmetry) each interrogate the actual code or data independently, cross-examine
each other, and converge on an **evidence-grounded consensus finding set** (each finding cites a
re-verifiable `file:line` / data locator; disputes are settled by returning to the source, never by a
vote or an average). It **reuses the same deterministic `crucible` CLI** with no config-schema change
— each round both peers review the merged set and the recorded verdict is the union of their
findings. See [`skills/deep-dive/SKILL.md`](skills/deep-dive/SKILL.md) and its design in
[`docs/superpowers/specs/2026-07-15-deep-dive-skill-design.md`](docs/superpowers/specs/2026-07-15-deep-dive-skill-design.md).

### Companion skill: `pr-review`

The repo also ships an independent third skill, **`pr-review`** (`/pr-review <pr-or-diff>`) — a
two-model **symmetric** adversarial *review* of a pull request. Two **equal peers** (no Builder/Critic
asymmetry) review a **GitHub PR** (via `gh`) or a **local diff** independently against the real code,
cross-examine, and converge on an **evidence-grounded consensus finding set** plus a **derived**
Approve/Comment/Request-changes recommendation. It **reuses the same deterministic `crucible` CLI**
with no config-schema change, and is **read-only** over the target by default (posting the review to
the PR happens only for a GitHub PR, only after consensus, and only with your explicit OK).
**Execution safety:** reviewed code is untrusted, so a PR-URL and a diff-file review are
**static/CI-only** and never execute locally; running tests or builds is available only for a
**trusted local checkout**, after explicit execution **consent** to the exact commands (with an
arbitrary-code warning). Consent does not imply sandboxing and is separate from posting consent. See
[`skills/pr-review/SKILL.md`](skills/pr-review/SKILL.md) and its design in
[`docs/superpowers/specs/2026-07-17-pr-review-skill-design.md`](docs/superpowers/specs/2026-07-17-pr-review-skill-design.md)
(execution trust boundary:
[`docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md`](docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md)).

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
  {"critic": {"effort": "high"}, "on_cap": "proceed_with_flags", "final_review": false}
  ```
- **Inspect / clean up** — `crucible status --run "$RUN"` (JSON progress), `crucible show-plan --run
  "$RUN"` (approved plan + DAG), `crucible report --run "$RUN" --open` (render + open in a browser),
  `crucible clean --run "$RUN"` (delete a finished run).

Full command reference — every subcommand with its arguments, grouped by phase:
**[docs/cli.md](docs/cli.md)**.

## Safety

Crucible is built to fail safe. The posture (full policy in [SECURITY.md](SECURITY.md)):

- **Critic output is untrusted data, not instructions.** The Critic — and any file contents or
  fetched text it echoes — is treated as data; it can never make the Builder change behavior, skip
  review, or approve its own work.
- **No consensus, no advance.** A gate advances only on `CONSENSUS` (or an explicit
  `on_cap: proceed_with_flags`); the `crucible` CLI decides consensus deterministically — it is
  never eyeballed.
- **Never implement on `main`/`master` without consent.** Work is isolated in a git worktree
  (`superpowers:using-git-worktrees`), so a protected branch is never touched unasked.
- **Nothing lands in the target repo.** Runs and all scratch default to `~/.crucible/runs`
  (override with `--base-dir` or `$CRUCIBLE_RUNS_DIR`), so running Crucible over someone else's
  project writes nothing into their tree.

Report a vulnerability privately via GitHub Security Advisories — see **[SECURITY.md](SECURITY.md)**.

## Development

```bash
python -m pytest -q          # the test suite (pytest.ini sets pythonpath=scripts)
python3 scripts/check.py     # the full local governance suite
```

`scripts/check.py` runs the **local** deterministic checks: structural validation, internal
Markdown links, the pytest suite, and ShellCheck (skipped with a note when it isn't installed). CI
(the `Validate` workflow) runs those **and more** — a **Minimum Python** job on the supported
**3.11** floor (the newest `3.x` is covered by the **Unit tests** job), plus the changelog and
release-dry-run gates — so a green `check.py` is necessary but not the whole CI story.

## Layout

- `skills/crucible/` — orchestrator skill + role prompts and rubric (`references/`).
- `skills/deep-dive/` — companion symmetric two-peer investigation skill + peer prompt and rubric (`references/`).
- `skills/pr-review/` — companion symmetric two-peer PR-review skill + peer prompt and rubric (`references/`).
- `commands/crucible.md` — `/crucible` entry point; `commands/deep-dive.md` — `/deep-dive` entry point; `commands/pr-review.md` — `/pr-review` entry point.
- `scripts/crucible/` — deterministic helpers: `config`, `dag`, `verdict`, `runlog`, `report`, `cli`.
- `.claude-plugin/` — plugin + marketplace manifests.
- `docs/cli.md` — full CLI reference; `docs/install/` — per-platform install guides.

Engineering tool. Not affiliated with any model provider.
