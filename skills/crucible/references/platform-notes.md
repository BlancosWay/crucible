# Platform notes — realizing two models

The **Builder** is the main session. The **Critic** is dispatched as a subagent with a model
override at each gate.

## Copilot CLI (primary)

Dispatch the Critic with the `task` tool, overriding the model and effort:

- `model`: the critic model id from config (default `gpt-5.5`).
- `reasoning_effort`: the critic effort from config (default `xhigh`).

Pass the Critic the contents of `critic-prompt.md` plus the Builder artifact under review.

## Claude Code / Codex

Use the native subagent dispatch with a per-agent model set to the critic model. If the runtime
rejects the configured model id, fall back to the most capable available model and note it in the
run-log.

## No-subagent fallback

If no subagent mechanism is available, run the Critic prompt as a separate, clearly delimited
pass in the same session (state "Acting as Critic now"), capture its JSON verdict, and feed it to
`crucible verdict`. Record the full text via `crucible log --event critic_output`.
