# Platform notes — realizing two models

The **Builder** is the main session. The **Critic** is dispatched as a subagent with a model
override at each gate. At **code-review gates** (IMPLEMENT and FINAL), the Critic is the
**`superpowers:code-reviewer`** agent run on the critic model; at the **PLAN** gate (plan + DAG
review, not code) the Critic is a plain subagent seeded with `critic-prompt.md`.

## Copilot CLI (primary)

- **Code-review gates (IMPLEMENT / FINAL):** dispatch the **`superpowers:code-reviewer`** agent
  via the `task` tool with `agent_type: "superpowers:code-reviewer"`, `model` = the critic model id
  (default `gpt-5.5`), and `reasoning_effort` = the critic effort (default `xhigh`). Give it the
  node's diff (or the whole implementation) plus the task/plan context, and require its findings as
  the `critic-prompt.md` verdict JSON.
- **PLAN gate:** dispatch a plain `task` subagent with the same `model`/`reasoning_effort`, seeded
  with `critic-prompt.md` + the plan + the DAG.

## Claude Code / Codex

Use the native subagent dispatch with a per-agent model set to the critic model — the
`superpowers:code-reviewer` agent for code-review gates, a plain subagent for the PLAN gate. If the
runtime rejects the configured model id, fall back to the most capable available model and note it
in the run-log.

## No-subagent fallback

If no subagent mechanism is available, run the Critic prompt as a separate, clearly delimited
pass in the same session (state "Acting as Critic now"), capture its JSON verdict, and feed it to
`crucible verdict`. Record the full text via `crucible log --event critic_output`.
