# Platform notes — realizing two models

The **Builder** is the main session. The **Critic** is dispatched as a subagent with a model
override at each gate, realized as the matching **superpowers reviewer** on the critic model:
the **`superpowers:writing-plans` plan-document-reviewer** (+ **`superpowers:brainstorming`
spec-document-reviewer**) at the **PLAN** gate, and the **`superpowers:code-reviewer`** agent at
the **IMPLEMENT** and **FINAL** gates.

## Copilot CLI (primary)

- **PLAN gate:** dispatch a `task` subagent with `model` = the critic model id (default `gpt-5.5`)
  and `reasoning_effort` = the critic effort (default `xhigh`), seeded with the
  `superpowers:writing-plans` **plan-document-reviewer** prompt (and the
  `superpowers:brainstorming` **spec-document-reviewer** prompt for the design spec) plus the plan +
  DAG. Require its result mapped into the `critic-prompt.md` verdict JSON.
- **Code-review gates (IMPLEMENT / FINAL):** dispatch the **`superpowers:code-reviewer`** agent
  via the `task` tool with `agent_type: "superpowers:code-reviewer"`, the same `model` /
  `reasoning_effort`. Give it the node's diff (or the whole implementation) plus the task/plan
  context, and require its findings as the `critic-prompt.md` verdict JSON. If this build's
  Superpowers packaging does not expose that `agent_type`, fall back to the platform's built-in
  code-review agent on the critic model (or the **No-subagent fallback** below) and note the
  substitution in the run-log.

## Claude Code / Codex

Use the native subagent dispatch with a per-agent model set to the critic model — the
`superpowers:writing-plans` plan-document-reviewer (+ spec-document-reviewer) for the PLAN gate, and
the `superpowers:code-reviewer` agent for code-review gates. If the runtime rejects the configured
model id, fall back to the most capable available model and note it in the run-log.

## No-subagent fallback

If no subagent mechanism is available, run the Critic prompt as a separate, clearly delimited
pass in the same session (state "Acting as Critic now"), capture its JSON verdict, and feed it to
`crucible verdict`. Record the full text via
`crucible log --run "$RUN" --event critic_output --gate "$GATE" --round N --file "$RUN"/critic-output.txt`
(`--run`, `--gate`, `--round`, and `--file` are all required; without `--file` the payload is
empty and the raw Critic provenance is lost).
