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
- `status` is one of `pending | in_progress | in_review | done | blocked` (start `pending`).
- `edges[].from` depends on `edges[].depends_on`; both must be existing node ids.
- The graph must be **acyclic**. `crucible load-dag` rejects cycles and unknown ids.
- Implementation walks the graph in **topological** order; `crucible next` returns the next node
  whose dependencies are all `done`.

Keep nodes small: one clear responsibility, independently testable. Files that change together
belong to the same node.
