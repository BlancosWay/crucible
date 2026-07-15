# Investigation thread graph (DAG) schema

The deep dive plans the work as a graph of **investigation threads** and hands it to the *unmodified*
`crucible` CLI as a single JSON object it validates. It is the **same DAG schema** the Crucible
Builder emits — reused verbatim — but each node is a **thread of investigation** rather than an
implementation task, and `test_plan` is the thread's **evidence / verification plan**.

```json
{
  "nodes": [
    {
      "id": "auth-token-lifetime",
      "title": "How is the auth token lifetime actually enforced?",
      "description": "Trace where the JWT exp is set and validated in src/auth/; answer whether refresh honors it.",
      "files": ["src/auth/token.py", "src/auth/middleware.py"],
      "test_plan": "rg -n 'exp|expires|ttl' src/auth/; python -c 'import auth; ...' — the exact commands/greps/queries either peer re-runs to reproduce the evidence.",
      "status": "pending"
    }
  ],
  "edges": [
    { "from": "refresh-flow", "depends_on": "auth-token-lifetime" }
  ]
}
```

- `id` — unique, kebab-case; names the question the thread answers.
- `title` / `description` — the investigative question and how to approach it (which code paths /
  data to interrogate).
- `files` — the primary sources this thread reads (code files, data locations). Not exhaustive, but
  where the evidence lives.
- `test_plan` — the **evidence/verification plan**: the concrete, **re-runnable** commands, greps, or
  data queries that ground this thread's findings, so **either peer can independently reproduce**
  them. This is the deep-dive analogue of a test command — evidence over assertion.
- `status` must be `pending` for every node in the emitted plan — `crucible load-dag` rejects a
  freshly imported tree with any non-`pending` node. The lifecycle states
  (`in_progress | in_review | done | blocked`) are set later via `crucible set-status`.
- `edges[].from` depends on `edges[].depends_on`; both must be existing node ids. Use an edge when a
  thread genuinely needs an earlier thread's **findings** before it can start.
- The graph must be **acyclic**. `crucible load-dag` rejects cycles and unknown ids.
- Investigation walks the graph in **topological** order; `crucible next` returns the next thread
  whose dependencies are all `done`.

Keep threads small and independently answerable: one clear question, its own evidence. Questions that
share the same evidence and must be answered together belong in the same thread.

**Each thread owns its evidence.** A thread records — in its `test_plan` and its findings — the exact
re-runnable evidence for its own conclusions; do not defer a thread's grounding to a later catch-all
step. The assembled findings report is the deliverable; it lives in the run dir and is surfaced to
the user (nothing is written into a target repo).
