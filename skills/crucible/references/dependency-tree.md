# Dependency tree (DAG) schema

The Builder emits the dependency tree as a single JSON object the `crucible` CLI can validate.

```json
{
  "nodes": [
    {
      "id": "auth-model",
      "title": "User auth model",
      "description": "Define User schema + password hashing in src/auth/model.py",
      "files": ["src/auth/model.py", "tests/auth/test_model.py"],
      "test_plan": "pytest tests/auth/test_model.py",
      "status": "pending"
    }
  ],
  "edges": [
    { "from": "auth-routes", "depends_on": "auth-model" }
  ]
}
```

- `id` — unique, kebab-case.
- `status` must be `pending` for every node in the emitted plan — `crucible load-dag` rejects a
  freshly imported tree with any non-`pending` node. The other lifecycle states
  (`in_progress | in_review | done | blocked`) are set only later, via `crucible set-status`.
- `edges[].from` depends on `edges[].depends_on`; both must be existing node ids.
- The graph must be **acyclic**. `crucible load-dag` rejects cycles and unknown ids.
- Implementation walks the graph in **topological** order; `crucible next` returns the next node
  whose dependencies are all `done`.

Keep nodes small: one clear responsibility, independently testable. Files that change together
belong to the same node.

**Each node owns its own documentation.** A node that adds or changes behavior includes the
documentation and `CHANGELOG` updates for *its own* deliverable in the same node — the docs live
with the code they describe (they are among "the files that node owns"), never deferred to a
separate catch-all node or a later step. Split documentation into its own node only when it is a
standalone deliverable (e.g. a tutorial or guide rewrite) not tied to a specific code change.
