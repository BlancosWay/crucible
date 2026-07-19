# Review thread graph (DAG) schema

The PR review plans the work as a graph of **review threads** and hands it to the *unmodified*
`crucible` CLI as a single JSON object it validates. It is the **same DAG schema** the Crucible
Builder emits — reused verbatim — but each node is a **slice of the PR to review** rather than an
implementation task, and `test_plan` is the thread's **evidence / verification plan**, split into
**static evidence** (reads/greps, always allowed) and **execution candidates (consent required)**
(test/build commands that run only after the Execution Safety Gate authorizes them).

```json
{
  "nodes": [
    {
      "id": "auth-logic",
      "title": "Review the auth/token changes",
      "description": "Review the login + token changes in src/auth/ against callers; check expiry, refresh, and authz edge cases.",
      "files": ["src/auth/login.py", "src/auth/tokens.py"],
      "test_plan": "static evidence (always allowed): rg -n 'exp|refresh|authorize' src/auth/ tests/auth/ — the greps/reads either peer reproduces to confirm a claimed test exists. execution candidates (consent required): pytest tests/auth/ -q — a candidate only; it may execute solely for a trusted local checkout after Execution-Safety-Gate consent, and never for a GitHub PR or diff-file target.",
      "status": "pending"
    },
    {
      "id": "api-surface",
      "title": "Review the API route changes",
      "description": "Review the new/changed routes in src/api/ against the auth contract the auth-logic thread established.",
      "files": ["src/api/routes.py"],
      "test_plan": "static evidence (always allowed): rg -n 'route|@app|def ' src/api/routes.py. execution candidates (consent required): pytest tests/api/ -q — never executed for a GitHub PR or diff-file target, and for a trusted local checkout only after exact-command consent.",
      "status": "pending"
    }
  ],
  "edges": [
    { "from": "api-surface", "depends_on": "auth-logic" }
  ]
}
```

- `id` — unique, kebab-case; names the concern the thread reviews.
- `title` / `description` — the slice under review and how to approach it (which changed files and
  surrounding code to interrogate).
- `files` — the primary changed files this thread reviews (plus the nearby code the evidence lives
  in). Not exhaustive.
- `test_plan` — the **evidence/verification plan**, written in two parts: **static evidence** (the
  concrete, **re-runnable** reads/greps that ground this thread's findings, so **either peer can
  independently reproduce** them) and **execution candidates (consent required)** (focused test/build
  commands that are *candidates* only). Evidence over assertion — and the place to confirm a claimed
  test actually **exists** statically. An execution candidate is never executed on its own: a GitHub
  PR or diff-file target **never executes locally**, a trusted local checkout runs only the exact
  commands approved at the Execution Safety Gate, and a **new command** or changed command requires
  **fresh consent**.
- `status` must be `pending` for every node in the emitted plan — `crucible load-dag` rejects a
  freshly imported tree with any non-`pending` node. The lifecycle states
  (`in_progress | in_review | done | blocked`) are set later via `crucible set-status`.
- `edges[].from` depends on `edges[].depends_on`; both must be existing node ids. Use an edge when one
  thread's review genuinely needs an earlier thread's **conclusions** first (e.g. the API-surface
  review depends on what the auth-logic review concluded about the contract).
- The graph must be **acyclic**. `crucible load-dag` rejects cycles and unknown ids.
- Review walks the graph in **topological** order; `crucible next` returns the next thread whose
  dependencies are all `done`.

## Sizing the graph to the PR (adaptive)

Size the decomposition to the change — the split itself is agreed at the PLAN gate and must reach
consensus:

- **Small PR** (a few files, one concern) → a **single node** covering the whole diff. No ceremony.
- **Large / multi-area PR** → **one thread per concern** (grouped by responsibility, not blindly
  per-file — one concern may span several files, and one file may mix concerns), with edges where one
  thread's conclusions inform another.

Keep threads small and independently reviewable: one clear concern, its own evidence. Changes that
must be judged together (they share the same evidence) belong in the same thread.

**Each thread owns its evidence.** A thread records — in its `test_plan` and its findings — the exact
re-runnable evidence for its own conclusions; do not defer a thread's grounding to a later catch-all
step. The assembled findings report is the deliverable; it lives in the run dir and is surfaced to the
user (nothing is written into the target repo unless the human consents to posting — see
`platform-notes.md`).
