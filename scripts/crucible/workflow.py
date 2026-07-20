"""Shared schema-v2 workflow prerequisites — the single owner of Crucible's stage/phase contract.

Both the CLI's mutating commands and (Task 4) the report call these helpers, so the rules for *which
gate may run when* and *which node completions are trusted* live in exactly one place — never
duplicated or eyeballed.

Every prerequisite validates an accepted gate decision against the CURRENT artifacts: an accepted
terminal event's recorded content bindings (``artifact_sha256`` / ``dag_sha256`` / ``node_sha256``,
owned by ``integrity.py``) must still match the plan/DAG/node as it stands now. So a plan, DAG, or
node substituted after a Critic accepted it yields different bindings and is rejected as stale — the
deterministic CLI, not model discipline, owns stage order and provenance.

Gate order (schema-v2):

1. REPRODUCE (only when ``reproduce_gate``) reaches consensus, then
2. PLAN reaches an accepted terminal with a valid artifact + DAG binding, then
3. human approval is recorded (only when ``human_approval``), then
4. dependency work proceeds in DAG order (each node ``in_progress`` -> reviewed -> ``done``), then
5. FINAL (only when ``final_review``) runs after every node is ``done``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from crucible.integrity import dag_sha256, node_sha256

if TYPE_CHECKING:  # annotation-only imports keep this module free of runtime import cycles
    from crucible.config import Config
    from crucible.dag import DAG
    from crucible.runlog import RunLog

# A gate concludes with exactly one terminal event; only these two ADVANCE past the gate. A
# ``gate_capped`` is a halt, never an acceptance.
_ADVANCE_TERMINALS = ("gate_consensus", "gate_proceeded_with_flags")
_TERMINAL_EVENTS = ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")


def accepted_terminal(events: list[dict[str, Any]], gate: str) -> dict[str, Any] | None:
    """The gate's accepted terminal event, or ``None``.

    LAST-terminal-event semantics (matching ``report.py`` and the ``set-status done`` guard): the
    last terminal logged for the gate is authoritative, so an earlier consensus followed by a later
    ``gate_capped`` counts as capped. Returns the event only when that last terminal ADVANCES
    (``gate_consensus`` / ``gate_proceeded_with_flags``); a capped or undecided gate returns ``None``.
    """
    terminal = [e for e in events
                if e.get("gate") == gate and e.get("event") in _TERMINAL_EVENTS]
    if not terminal:
        return None
    last = terminal[-1]
    return last if last.get("event") in _ADVANCE_TERMINALS else None


def _latest(events: list[dict[str, Any]], event_name: str) -> dict[str, Any] | None:
    matches = [e for e in events if e.get("event") == event_name]
    return matches[-1] if matches else None


def require_plan_verdict_ready(run: RunLog, cfg: Config) -> None:
    """Guard PLAN logging/bindings/verdict: when ``reproduce_gate`` is configured, the REPRODUCE
    gate must have reached an accepted terminal first. A no-op when ``reproduce_gate`` is off."""
    if not cfg.reproduce_gate:
        return
    if accepted_terminal(run.read_events(), "reproduce") is None:
        raise SystemExit(
            "crucible: reproduce_gate is enabled but the REPRODUCE gate has not reached consensus; "
            "run the REPRODUCE gate to a terminal advance before the PLAN gate."
        )


def require_plan_ready(run: RunLog, cfg: Config) -> dict[str, Any]:
    """Return the accepted PLAN terminal, or raise ``SystemExit``.

    Validates that PLAN reached an accepted terminal, that its recorded ``dag_sha256`` still matches
    the CURRENT dependency tree (rejecting a DAG swapped after consensus), and — when
    ``human_approval`` is configured — that a ``plan_approved`` event binds the exact accepted
    plan/DAG. Dependency and FINAL prerequisites build on this.
    """
    from crucible.dag import DAG  # local import: dag.py has no crucible deps, so this cannot cycle

    events = run.read_events()
    terminal = accepted_terminal(events, "plan")
    if terminal is None:
        raise SystemExit(
            "crucible: the PLAN gate has not reached consensus (or proceeded with flags); settle "
            "the PLAN gate with a bound artifact + DAG before dependency work."
        )
    dag = DAG.from_dict(run.load_dag())
    current_dag = dag_sha256(dag)
    accepted_dag = terminal.get("dag_sha256")
    if accepted_dag != current_dag:
        raise SystemExit(
            "crucible: the accepted plan's DAG binding is stale — its dag_sha256 no longer matches "
            "the current dependency tree; the plan/DAG was changed after PLAN consensus — start a "
            "fresh run."
        )
    if cfg.human_approval:
        approval = _latest(events, "plan_approved")
        if approval is None:
            raise SystemExit(
                "crucible: human_approval is enabled but the accepted plan has no recorded "
                "approval; run `crucible approve-plan` after the human approves the plan."
            )
        if (approval.get("artifact_sha256") != terminal.get("artifact_sha256")
                or approval.get("dag_sha256") != accepted_dag):
            raise SystemExit(
                "crucible: the recorded plan approval is stale — it does not bind the currently "
                "accepted plan/DAG (artifact/dag sha256 mismatch); re-approve the current plan or "
                "start a fresh run."
            )
    return terminal


def require_node_review_ready(run: RunLog, cfg: Config, dag: DAG, node_id: str) -> None:
    """Guard dependency (``dep:<id>``) logging/bindings/verdict.

    Requires an accepted, currently-bound PLAN (and configured approval), every dependency of the
    node ``done``, and the node itself ``in_progress`` or ``in_review`` — a node cannot be reviewed
    while it is still ``pending`` (its work has not started) or already ``done``/``blocked``.
    """
    require_plan_ready(run, cfg)
    if node_id not in dag.nodes:
        raise SystemExit(f"crucible: unknown node {node_id!r}; known nodes: {sorted(dag.nodes)}")
    unmet = sorted(d for d in dag.deps[node_id] if dag.nodes[d].status != "done")
    if unmet:
        raise SystemExit(
            f"crucible: cannot review node {node_id!r} while these dependencies are not done: "
            f"{unmet}."
        )
    status = dag.nodes[node_id].status
    if status not in ("in_progress", "in_review"):
        raise SystemExit(
            f"crucible: node {node_id!r} is {status!r}; a dependency can only be logged/reviewed "
            f"while it is in_progress or in_review — mark it in_progress before reviewing its work."
        )


def require_final_ready(run: RunLog, cfg: Config, dag: DAG) -> None:
    """Guard FINAL logging/bindings/verdict.

    Requires ``final_review`` enabled, an accepted/bound PLAN (and configured approval), every node
    ``done``, and every done node backed by either a currently-bound accepted dependency gate or a
    recorded forced override — FINAL certifies the whole implementation, so it can only run once the
    work it reviews is complete and accounted for.
    """
    if not cfg.final_review:
        raise SystemExit(
            "crucible: final_review is disabled; the FINAL gate is not part of this run's "
            "configured workflow."
        )
    require_plan_ready(run, cfg)
    unfinished = dag.unfinished()
    if unfinished:
        raise SystemExit(
            f"crucible: the FINAL gate requires every node done; still unfinished: {unfinished}."
        )
    events = run.read_events()
    unbacked = [nid for nid in dag.order if not _node_completion_backed(events, dag, nid)]
    if unbacked:
        raise SystemExit(
            f"crucible: the FINAL gate requires every done node to be backed by an accepted "
            f"dependency review or a recorded forced override; these are not: {unbacked}."
        )


def _node_completion_backed(events: list[dict[str, Any]], dag: DAG, node_id: str) -> bool:
    """Whether a node's ``done`` status is backed by a current binding.

    True when the node's own ``dep:<id>`` gate accepted with bindings that still match the current
    DAG/node, or when a forced ``node_status_change`` recorded current DAG/node hashes for it (an
    explicit, audited override). Otherwise the completion is unbound (or stale) and not trusted.
    """
    current_dag = dag_sha256(dag)
    current_node = node_sha256(dag, node_id)
    terminal = accepted_terminal(events, f"dep:{node_id}")
    if terminal is not None and (terminal.get("dag_sha256") == current_dag
                                 and terminal.get("node_sha256") == current_node):
        return True
    forced = [e for e in events
              if e.get("event") == "node_status_change" and e.get("node") == node_id
              and e.get("forced") and e.get("status") == "done"]
    if forced:
        last = forced[-1]
        return (last.get("dag_sha256") == current_dag
                and last.get("node_sha256") == current_node)
    return False


def workflow_issues(events: list[dict[str, Any]], dag: DAG, cfg: Config) -> list[str]:
    """Deterministic list of configured-workflow problems for the report (consumed by Task 4).

    Pure over ``(events, dag, cfg)`` — no file reads, never raises. Each string names a missing or
    invalid required phase: a configured REPRODUCE/approval/FINAL that is absent, a PLAN that is
    unaccepted or whose DAG binding no longer matches, a done node lacking a bound accepted review
    (or forced override), or a FINAL logged before completion. An empty list means every configured
    phase is present, ordered, accepted, and currently bound.
    """
    issues: list[str] = []

    if cfg.reproduce_gate and accepted_terminal(events, "reproduce") is None:
        issues.append("configured REPRODUCE gate never reached consensus")

    plan_terminal = accepted_terminal(events, "plan")
    if plan_terminal is None:
        issues.append("PLAN gate never reached an accepted terminal")
        return issues  # nothing downstream can be trusted without an accepted plan

    current_dag = dag_sha256(dag)
    if plan_terminal.get("dag_sha256") != current_dag:
        issues.append("PLAN DAG binding no longer matches the current dependency tree")

    if cfg.human_approval:
        approval = _latest(events, "plan_approved")
        if approval is None:
            issues.append("configured human approval was never recorded")
        elif (approval.get("artifact_sha256") != plan_terminal.get("artifact_sha256")
              or approval.get("dag_sha256") != plan_terminal.get("dag_sha256")):
            issues.append("recorded plan approval does not bind the accepted plan/DAG")

    for nid in dag.order:
        if dag.nodes[nid].status == "done" and not _node_completion_backed(events, dag, nid):
            issues.append(f"node {nid!r} is done without a bound accepted review or forced override")

    if cfg.final_review:
        if not dag.is_complete():
            issues.append("configured FINAL gate requires every node done first")
        elif accepted_terminal(events, "final") is None:
            issues.append("configured FINAL gate never reached consensus")

    return issues
