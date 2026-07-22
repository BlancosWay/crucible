"""Dependency-tree (DAG) model: parse, validate acyclic, topo order, ready set, status."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# `in_review` is a reserved status: it is detected as in-flight (see `in_flight`) and may be
# set manually/externally, but the standard orchestration flow only transitions
# pending -> in_progress -> done. It is intentionally part of the vocabulary, not dead.
VALID_STATUSES = ("pending", "in_progress", "in_review", "done", "blocked")

# The legal node-status transitions Crucible enforces (schema-v2 workflow contract). A node begins
# `pending`; `done` is terminal (no legal exit). `in_review` shares `in_progress`'s exits so a node
# under review can be sent back, finished, blocked, or reset. Same-status transitions are always
# idempotent no-ops (see `set_status`). `set-status --force --status done` is the explicit
# reviewed-gate bypass (`force=True` below) and is the ONLY way to reach `done` off this table — and
# even it can never mutate an already-`done` node.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "blocked"},
    "in_progress": {"in_review", "done", "blocked", "pending"},
    "in_review": {"in_progress", "done", "blocked", "pending"},
    "blocked": {"pending"},
    "done": set(),
}


class CycleError(ValueError):
    """Raised when the dependency graph contains a cycle."""


@dataclass
class Node:
    id: str
    title: str
    description: str
    files: list[str]
    test_plan: str
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "files": list(self.files),
            "test_plan": self.test_plan,
            "status": self.status,
        }


@dataclass
class DAG:
    nodes: dict[str, Node]
    deps: dict[str, set[str]]
    order: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DAG":
        if not isinstance(data, dict):
            raise ValueError("dependency tree must be a JSON object")
        nodes: dict[str, Node] = {}
        order: list[str] = []
        nodes_raw = data.get("nodes", [])
        if not isinstance(nodes_raw, list):
            raise ValueError('dependency tree "nodes" must be a list')
        for i, raw in enumerate(nodes_raw):
            if not isinstance(raw, dict):
                raise ValueError(f"node at index {i} must be a JSON object")
            nid = raw["id"]
            if not isinstance(nid, str) or not nid:
                raise ValueError(f'node at index {i} "id" must be a non-empty string')
            if nid != nid.strip() or not nid.strip():
                raise ValueError(
                    f'node at index {i} "id" must not be blank or have surrounding whitespace '
                    f"(ids are kebab-case): {nid!r}")
            if nid in nodes:
                raise ValueError(f"duplicate node id: {nid}")
            status = raw.get("status", "pending")
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid status {status!r} for node {nid}")
            files = raw.get("files", [])
            if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
                raise ValueError(f'node {nid!r} "files" must be a list of strings')
            nodes[nid] = Node(
                id=nid,
                title=raw.get("title", nid),
                description=raw.get("description", ""),
                files=list(files),
                test_plan=raw.get("test_plan", ""),
                status=status,
            )
            order.append(nid)
        deps: dict[str, set[str]] = {nid: set() for nid in nodes}
        edges_raw = data.get("edges", [])
        if not isinstance(edges_raw, list):
            raise ValueError('dependency tree "edges" must be a list')
        for i, edge in enumerate(edges_raw):
            if not isinstance(edge, dict):
                raise ValueError(f"edge at index {i} must be a JSON object")
            frm, dep = edge["from"], edge["depends_on"]
            for key, val in (("from", frm), ("depends_on", dep)):
                if not isinstance(val, str) or not val:
                    raise ValueError(f'edge at index {i} "{key}" must be a non-empty string')
            if frm not in nodes:
                raise ValueError(f"edge 'from' references unknown node: {frm}")
            if dep not in nodes:
                raise ValueError(f"edge 'depends_on' references unknown node: {dep}")
            deps[frm].add(dep)
        dag = cls(nodes=nodes, deps=deps, order=order)
        dag.topological_order()
        return dag

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def topological_order(self) -> list[str]:
        indegree = {nid: len(self.deps[nid]) for nid in self.nodes}
        ready = [nid for nid in self.order if indegree[nid] == 0]
        result: list[str] = []
        while ready:
            nid = ready.pop(0)
            result.append(nid)
            for other in self.order:
                if nid in self.deps[other]:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        ready.append(other)
        if len(result) != len(self.nodes):
            raise CycleError("dependency graph contains a cycle")
        return result

    def ready_nodes(self) -> list[str]:
        out = []
        for nid in self.order:
            if self.nodes[nid].status != "pending":
                continue
            if all(self.nodes[d].status == "done" for d in self.deps[nid]):
                out.append(nid)
        return out

    def is_complete(self) -> bool:
        """True when every node is ``done`` (vacuously true for an empty graph)."""
        return all(n.status == "done" for n in self.nodes.values())

    def in_flight(self) -> list[str]:
        """Ids of nodes whose work is active (``in_progress``/``in_review``), in order."""
        return [nid for nid in self.order if self.nodes[nid].status in ("in_progress", "in_review")]

    def unfinished(self) -> list[str]:
        """Ids of nodes that are not ``done``, in order."""
        return [nid for nid in self.order if self.nodes[nid].status != "done"]

    def unfinished_detail(self) -> list[dict[str, Any]]:
        """Per unfinished node: its id, status, and the unmet (non-``done``) deps."""
        detail = []
        for nid in self.unfinished():
            waiting = sorted(d for d in self.deps[nid] if self.nodes[d].status != "done")
            detail.append({"id": nid, "status": self.nodes[nid].status, "waiting_on": waiting})
        return detail

    def next_state(self) -> tuple[str, str | None]:
        """Classify the scheduling state for ``crucible next``:

        - ``("ready", node_id)`` — a node is ready to implement.
        - ``("complete", None)`` — every node is ``done``.
        - ``("in_flight", None)`` — nothing ready, work is active and nothing is blocked.
        - ``("stuck", None)`` — nothing ready and either a node is ``blocked`` or no active
          work remains (a deadlock the human must resolve). ``blocked`` takes priority over
          in-flight work so a blocker is never masked.
        """
        ready = self.ready_nodes()
        if ready:
            return ("ready", ready[0])
        if self.is_complete():
            return ("complete", None)
        blocked = any(n.status == "blocked" for n in self.nodes.values())
        if self.in_flight() and not blocked:
            return ("in_flight", None)
        return ("stuck", None)

    def set_status(self, node_id: str, status: str, *, force: bool = False) -> None:
        """Transition a node's status, enforcing ``ALLOWED_TRANSITIONS``.

        Same-status calls are idempotent no-ops (even for terminal ``done``). ``force=True`` is the
        explicit reviewed-gate bypass used by ``set-status --force --status done``: it skips the
        transition table for a NON-done node, but never mutates an already-``done`` node (``done`` is
        terminal — reopening it would erase a completed review). Raises ``ValueError`` naming both
        the current and target status for an illegal transition."""
        if node_id not in self.nodes:
            raise ValueError(f"unknown node: {node_id}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        current = self.nodes[node_id].status
        if status == current:
            return  # idempotent: setting a node to its current status is always a no-op
        if current == "done":
            # `done` is terminal for both normal and forced transitions; a forced override may start
            # from any OTHER state but can never change a node that is already done.
            raise ValueError(
                f"illegal status transition for node {node_id!r}: done -> {status} "
                f"(done is terminal; a completed node cannot be reopened, even with --force)"
            )
        if force:
            self.nodes[node_id].status = status
            return
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if status not in allowed:
            allowed_txt = ", ".join(sorted(allowed)) or "none (terminal)"
            raise ValueError(
                f"illegal status transition for node {node_id!r}: {current} -> {status} "
                f"(allowed from {current}: {allowed_txt})"
            )
        self.nodes[node_id].status = status

    def progress(self) -> dict[str, int]:
        counts = {s: 0 for s in VALID_STATUSES}
        for n in self.nodes.values():
            counts[n.status] += 1
        return {"total": len(self.nodes), **counts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [self.nodes[nid].to_dict() for nid in self.order],
            "edges": [
                {"from": nid, "depends_on": dep}
                for nid in self.order
                for dep in sorted(self.deps[nid])
            ],
        }

    def definition_dict(self) -> dict[str, Any]:
        """Canonical, status-free view of the whole tree for content binding.

        Nodes appear in declared order (order is load-bearing: it breaks ties among
        otherwise-ready nodes) with only their immutable fields; edges list each node's
        dependencies in sorted order. Mutable ``status`` is deliberately excluded so a
        digest over this view stays stable as work progresses.
        """
        return {
            "nodes": [
                {
                    "id": n.id,
                    "title": n.title,
                    "description": n.description,
                    "files": list(n.files),
                    "test_plan": n.test_plan,
                }
                for n in (self.nodes[nid] for nid in self.order)
            ],
            "edges": [
                {"from": nid, "depends_on": dep}
                for nid in self.order
                for dep in sorted(self.deps[nid])
            ],
        }

    def node_definition_dict(self, node_id: str) -> dict[str, Any]:
        """Canonical, status-free view of one node: its immutable fields plus sorted deps."""
        n = self.node(node_id)
        return {
            "id": n.id,
            "title": n.title,
            "description": n.description,
            "files": list(n.files),
            "test_plan": n.test_plan,
            "depends_on": sorted(self.deps[node_id]),
        }
