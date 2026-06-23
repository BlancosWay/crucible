"""Dependency-tree (DAG) model: parse, validate acyclic, topo order, ready set, status."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_STATUSES = ("pending", "in_progress", "in_review", "done", "blocked")


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
        nodes: dict[str, Node] = {}
        order: list[str] = []
        for raw in data.get("nodes", []):
            nid = raw["id"]
            if nid in nodes:
                raise ValueError(f"duplicate node id: {nid}")
            status = raw.get("status", "pending")
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid status {status!r} for node {nid}")
            nodes[nid] = Node(
                id=nid,
                title=raw.get("title", nid),
                description=raw.get("description", ""),
                files=list(raw.get("files", [])),
                test_plan=raw.get("test_plan", ""),
                status=status,
            )
            order.append(nid)
        deps: dict[str, set[str]] = {nid: set() for nid in nodes}
        for edge in data.get("edges", []):
            frm, dep = edge["from"], edge["depends_on"]
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

    def set_status(self, node_id: str, status: str) -> None:
        if node_id not in self.nodes:
            raise KeyError(node_id)
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
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
