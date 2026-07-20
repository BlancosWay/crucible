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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from crucible.integrity import artifact_sha256, dag_sha256, node_sha256
from crucible.symmetric import SYMMETRIC_WORKFLOWS, workflow_kind

if TYPE_CHECKING:  # annotation-only imports keep this module free of runtime import cycles
    from crucible.config import Config
    from crucible.dag import DAG
    from crucible.runlog import RunLog

# A gate concludes with exactly one terminal event; only these two ADVANCE past the gate. A
# ``gate_capped`` is a halt, never an acceptance.
_ADVANCE_TERMINALS = ("gate_consensus", "gate_proceeded_with_flags")
_TERMINAL_EVENTS = ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")


def _accepted_terminal_with_index(
    events: list[dict[str, Any]], gate: str
) -> tuple[dict[str, Any] | None, int | None]:
    """The gate's accepted terminal event AND its index in ``events`` (or ``(None, None)``).

    The single owner of the LAST-terminal-event rule: :func:`accepted_terminal` and the report's
    event-order checks both read a gate's outcome through here, so they agree on which terminal is
    authoritative. The index lets the report reason about *when a phase concluded* in log order —
    always a terminal event, never an interleaved builder/critic non-terminal event.
    """
    last: dict[str, Any] | None = None
    last_idx: int | None = None
    for i, e in enumerate(events):
        if e.get("gate") == gate and e.get("event") in _TERMINAL_EVENTS:
            last, last_idx = e, i
    if last is None or last.get("event") not in _ADVANCE_TERMINALS:
        return None, None
    return last, last_idx


def accepted_terminal(events: list[dict[str, Any]], gate: str) -> dict[str, Any] | None:
    """The gate's accepted terminal event, or ``None``.

    LAST-terminal-event semantics (matching ``report.py`` and the ``set-status done`` guard): the
    last terminal logged for the gate is authoritative, so an earlier consensus followed by a later
    ``gate_capped`` counts as capped. Returns the event only when that last terminal ADVANCES
    (``gate_consensus`` / ``gate_proceeded_with_flags``); a capped or undecided gate returns ``None``.
    """
    return _accepted_terminal_with_index(events, gate)[0]


def _latest_with_index(
    events: list[dict[str, Any]], event_name: str
) -> tuple[dict[str, Any] | None, int | None]:
    """The last event named ``event_name`` and its index in ``events`` (or ``(None, None)``)."""
    latest: dict[str, Any] | None = None
    latest_idx: int | None = None
    for i, e in enumerate(events):
        if e.get("event") == event_name:
            latest, latest_idx = e, i
    return latest, latest_idx


def _latest(events: list[dict[str, Any]], event_name: str) -> dict[str, Any] | None:
    return _latest_with_index(events, event_name)[0]


def _first_dependency_or_final_work_index(events: list[dict[str, Any]]) -> int | None:
    """Index of the EARLIEST event that constitutes dependency or FINAL work, or ``None`` if none.

    "Work" is any ``dep:<id>`` gate event (Builder output, Critic verdict, or a terminal), any
    ``node_status_change`` (starting, advancing, or completing a node), or any ``final`` gate event.
    Configured human approval must precede all of it, so the report compares the approval index
    against this. ``events`` is append-only and in log order, so the first match is the earliest.
    """
    for i, e in enumerate(events):
        gate = e.get("gate")
        if ((isinstance(gate, str) and gate.startswith("dep:"))
                or e.get("event") == "node_status_change"
                or gate == "final"):
            return i
    return None


def require_reproduce_ready(cfg: Config) -> None:
    """Guard REPRODUCE logging/bindings/verdict — the reproduce counterpart to the other gate
    readiness helpers, and the single owner of "may the REPRODUCE gate run at all".

    The REPRODUCE gate is Stage 0, but it is part of the configured workflow ONLY when
    ``reproduce_gate`` is enabled (design: "``reproduce`` is accepted only when ``reproduce_gate:
    true``"). When it is disabled the gate is a configured-forbidden phase, so this rejects it —
    the CLI's ``log``/``bindings``/``verdict`` handshake all delegate here before any log append, so
    a disabled REPRODUCE gate can never record an artifact/binding/verdict, let alone certify one.
    Takes only ``cfg`` because the decision is purely configuration; no run history is consulted.
    """
    if not cfg.reproduce_gate:
        raise SystemExit(
            "crucible: reproduce_gate is disabled; the REPRODUCE gate is not part of this run's "
            "configured workflow — enable reproduce_gate to run it, or start at the PLAN gate."
        )


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


def workflow_issues(events: list[dict[str, Any]], dag: DAG, cfg: Config) -> list["WorkflowIssue"]:
    """Deterministic, structured list of configured-workflow problems for the report (Task 4).

    Pure over ``(events, dag, cfg)`` — no file reads, never raises. Each :class:`WorkflowIssue`
    carries a ``kind`` that drives the report's status precedence and a human-readable ``message``
    naming the offending phase/node:

    - ``"missing"`` — a configured REPRODUCE/approval/FINAL phase (or PLAN) that never happened, or
      a FINAL not yet reached though the tree is done; the run is merely *in progress*.
    - ``"invalid"`` — a binding that no longer matches the current artifact/tree, an unbound or
      tampered artifact, a stale approval, a phase recorded out of log order, or a
      configured-forbidden phase (a REPRODUCE terminal when ``reproduce_gate`` is disabled): an
      integrity violation recorded in the log.
    - ``"flagged"`` — a done node completed outside an accepted review gate (a forced override, or
      no gate at all): an audited/unaudited bypass, not a hard violation.

    Every accepted terminal (REPRODUCE / PLAN / ``dep:*`` / FINAL) is validated two ways: its
    ``artifact_sha256`` must bind a same-gate/same-round Builder output recorded BEFORE the terminal
    (:func:`_artifact_binding_issues`), and the configured phases must appear in log order (REPRODUCE
    before PLAN; approval after PLAN consensus but BEFORE any dependency/FINAL work; each dependency
    review before its non-forced ``done``; FINAL after every node's backing and ``done`` events).
    PLAN and FINAL terminals must additionally bind the CURRENT dependency tree (``dag_sha256``). An
    empty list means every configured phase is present, ordered, accepted, and currently bound.
    """
    issues: list[WorkflowIssue] = []

    reproduce_terminal, reproduce_idx = _accepted_terminal_with_index(events, "reproduce")
    if cfg.reproduce_gate:
        if reproduce_terminal is None:
            issues.append(WorkflowIssue(
                "missing", "configured REPRODUCE gate never reached consensus"))
        else:
            issues.extend(_artifact_binding_issues(events, "reproduce", reproduce_terminal,
                                                   reproduce_idx, "REPRODUCE"))
    elif any(e.get("gate") == "reproduce" and e.get("event") in _TERMINAL_EVENTS for e in events):
        # reproduce_gate is OFF, yet a REPRODUCE gate reached a terminal in the log. The REPRODUCE
        # phase is not part of this run's configured workflow (design: "reproduce is accepted only
        # when reproduce_gate: true"), so a recorded terminal — accepted OR capped — is a
        # configured-forbidden phase: an integrity violation, never CLEAN.
        issues.append(WorkflowIssue(
            "invalid",
            "REPRODUCE gate terminal is recorded though reproduce_gate is disabled "
            "(configured-forbidden phase)"))

    plan_terminal, plan_idx = _accepted_terminal_with_index(events, "plan")
    if plan_terminal is None:
        issues.append(WorkflowIssue("missing", "PLAN gate never reached an accepted terminal"))
        return issues  # nothing downstream can be trusted without an accepted plan

    issues.extend(_artifact_binding_issues(events, "plan", plan_terminal, plan_idx, "PLAN"))

    current_dag = dag_sha256(dag)
    if plan_terminal.get("dag_sha256") != current_dag:
        issues.append(WorkflowIssue(
            "invalid", "PLAN DAG binding no longer matches the current dependency tree"))

    # Configured phase ORDER, decided by terminal/event indices (never interleaved builder/critic
    # non-terminal events): REPRODUCE must conclude before PLAN.
    if (cfg.reproduce_gate and reproduce_idx is not None and plan_idx is not None
            and reproduce_idx > plan_idx):
        issues.append(WorkflowIssue(
            "invalid", "REPRODUCE gate terminal is recorded after the PLAN gate (out of order)"))

    if cfg.human_approval:
        approval, approval_idx = _latest_with_index(events, "plan_approved")
        if approval is None:
            issues.append(WorkflowIssue("missing", "configured human approval was never recorded"))
        else:
            if (approval.get("artifact_sha256") != plan_terminal.get("artifact_sha256")
                    or approval.get("dag_sha256") != plan_terminal.get("dag_sha256")):
                issues.append(WorkflowIssue(
                    "invalid", "recorded plan approval does not bind the accepted plan/DAG"))
            if plan_idx is not None and approval_idx is not None and approval_idx < plan_idx:
                issues.append(WorkflowIssue(
                    "invalid",
                    "plan approval is recorded before the PLAN gate reached consensus "
                    "(out of order)"))
            # Configured approval must GATE dependency work: it comes after PLAN consensus but before
            # any dependency or FINAL work begins (design phase order 3 before 4/5). Compare the
            # approval index against the earliest recorded dependency/FINAL work event; approval
            # recorded after that work means dependencies advanced without the required human OK — an
            # out-of-order integrity violation, never CLEAN.
            work_idx = _first_dependency_or_final_work_index(events)
            if approval_idx is not None and work_idx is not None and work_idx < approval_idx:
                issues.append(WorkflowIssue(
                    "invalid",
                    "plan approval is recorded after dependency or FINAL work began — configured "
                    "approval must gate dependency work (out of order)"))

    for nid in dag.order:
        if dag.nodes[nid].status == "done":
            issues.extend(_node_completion_issues(events, dag, nid))

    if cfg.final_review:
        issues.extend(_final_issues(events, dag))

    # Symmetric (deep-dive/pr-review) runs additionally persist a structured accepted finding set for
    # every dependency/FINAL gate that advances. A terminal without it — or an accepted set recorded
    # after the terminal — is incomplete/orphan history (see Task 2).
    if workflow_kind(events) in SYMMETRIC_WORKFLOWS:
        issues.extend(_symmetric_accepted_set_issues(events, dag))

    return issues


def _symmetric_accepted_set_issues(
    events: list[dict[str, Any]], dag: DAG
) -> list[WorkflowIssue]:
    """Accepted-finding-set integrity for symmetric dependency/FINAL gates.

    Each dependency (``dep:<id>``) and FINAL gate carries a structured accepted finding set. When such
    a gate reaches an ADVANCING terminal (``gate_consensus``/``gate_proceeded_with_flags``), exactly
    one ``accepted_finding_set`` for it must be recorded BEFORE that terminal (the atomic-decision
    contract: symmetric_verdict -> accepted set -> terminal). Two integrity violations are reported as
    ``invalid``:

    - a terminal with no pre-terminal accepted set (the gate certified without persisting its result);
    - an accepted set recorded AFTER its gate's advancing terminal, or with no advancing terminal at
      all (orphan/post-terminal crash residue that must never count as accepted state).

    Binding-shape, malformed-payload, and FINAL-inclusion validation are Task 3; PLAN gates carry a
    text artifact and never an accepted finding set, so they are excluded.
    """
    issues: list[WorkflowIssue] = []
    finding_gates = [f"dep:{nid}" for nid in dag.order] + ["final"]
    for gate in finding_gates:
        terminal, terminal_idx = _accepted_terminal_with_index(events, gate)
        accepted_indices = [i for i, e in enumerate(events)
                            if e.get("event") == "accepted_finding_set" and e.get("gate") == gate]
        if terminal is not None:
            pre_terminal = [i for i in accepted_indices
                            if terminal_idx is None or i < terminal_idx]
            if not pre_terminal:
                issues.append(WorkflowIssue(
                    "invalid",
                    f"symmetric gate {gate!r} reached an accepted terminal without a persisted "
                    f"accepted finding set recorded before it"))
        for i in accepted_indices:
            if terminal is None or (terminal_idx is not None and i > terminal_idx):
                issues.append(WorkflowIssue(
                    "invalid",
                    f"symmetric gate {gate!r} records an accepted finding set after its terminal or "
                    f"without an accepted terminal (orphan/post-terminal, not accepted state)"))
    return issues


def _artifact_binding_issues(
    events: list[dict[str, Any]], gate: str, terminal: dict[str, Any],
    terminal_idx: int | None, label: str
) -> list[WorkflowIssue]:
    """Validate an accepted terminal's ``artifact_sha256`` against its Builder output — the single
    owner of this check for every gate (never a per-gate duplicated loop).

    The terminal's recorded ``artifact_sha256`` must equal the digest recomputed from the exact
    bytes of the LATEST same-gate/same-round ``builder_output`` payload recorded BEFORE the terminal
    (``terminal_idx``). ``runlog`` stores that payload as a strict UTF-8 decode of the original
    artifact bytes (via ``read_artifact``), so re-encoding it recovers those exact bytes — CRLF
    included — and the digest matches iff the accepted decision refers to the reviewed artifact.

    Only PRE-terminal Builder outputs count: an artifact logged after the terminal could not have
    been reviewed by the Critic for that decision, so a forged log that records ``gate_consensus``
    first and appends a matching ``builder_output`` later must not satisfy the binding, and a later
    post-terminal output can neither replace nor bypass the exact pre-terminal binding. A terminal
    with no recorded hash, no same-gate/same-round Builder output before it, or a hash that disagrees
    with that payload is an ``invalid`` binding (unreviewable or tampered), never allowed to reach
    ``CLEAN``.
    """
    recorded = terminal.get("artifact_sha256")
    round_index = terminal.get("round")
    outputs = [
        e
        for i, e in enumerate(events)
        if (terminal_idx is None or i < terminal_idx)
        and e.get("event") == "builder_output"
        and e.get("gate") == gate
        and e.get("round") == round_index
        and isinstance(e.get("payload"), str)
        and e.get("payload")
    ]
    if not isinstance(recorded, str) or not recorded or not outputs:
        return [WorkflowIssue(
            "invalid",
            f"{label} terminal has no reviewed Builder artifact bound for its gate/round")]
    if recorded != artifact_sha256(outputs[-1]["payload"].encode("utf-8")):
        return [WorkflowIssue(
            "invalid",
            f"{label} artifact binding does not match the reviewed Builder output")]
    return []


def _final_issues(events: list[dict[str, Any]], dag: DAG) -> list[WorkflowIssue]:
    """Configured FINAL phase problems: presence, artifact binding, DAG binding, and LOG ORDER.

    FINAL certifies the whole implementation, so its accepted terminal must (1) bind a same-gate
    Builder artifact recorded before it, (2) bind the CURRENT dependency tree (``dag_sha256``), and
    (3) be recorded AFTER every node's completion. A FINAL terminal whose ``dag_sha256`` is missing
    or no longer matches the current tree certifies a different implementation tree and is
    ``invalid``. A FINAL terminal logged before a node's backing dep/force event or its ``done``
    transition is out of order even if the tree later ends done — the recorded log order, not just
    the current DAG snapshot, decides. An absent FINAL over a done tree is merely ``missing`` (still
    in progress); an absent FINAL over an unfinished tree is not yet expected.
    """
    final_terminal, final_idx = _accepted_terminal_with_index(events, "final")
    if final_terminal is None:
        if dag.is_complete():
            return [WorkflowIssue("missing", "configured FINAL gate never reached consensus")]
        return []
    issues = _artifact_binding_issues(events, "final", final_terminal, final_idx, "FINAL")
    if final_terminal.get("dag_sha256") != dag_sha256(dag):
        issues.append(WorkflowIssue(
            "invalid", "FINAL DAG binding no longer matches the current dependency tree"))
    if not dag.is_complete():
        issues.append(WorkflowIssue(
            "invalid", "FINAL gate reached a terminal before every node was done"))
    elif final_idx is not None and _final_precedes_completion(events, dag, final_idx):
        issues.append(WorkflowIssue(
            "invalid",
            "FINAL gate terminal is recorded before a node's completion (out of order)"))
    return issues


def _final_precedes_completion(
    events: list[dict[str, Any]], dag: DAG, final_idx: int
) -> bool:
    """True when any done node's backing (dep terminal / forced ``done``) or ``done`` transition is
    logged after ``final_idx`` — i.e. FINAL was recorded before that node's completion."""
    for nid in dag.order:
        if dag.nodes[nid].status != "done":
            continue
        if any(idx > final_idx for idx in _node_completion_event_indices(events, nid)):
            return True
    return False


def _node_completion_event_indices(
    events: list[dict[str, Any]], node_id: str
) -> list[int]:
    """Indices of the events that back and record ``node_id``'s completion: its accepted dependency
    terminal (if any) plus every ``node_status_change`` to ``done`` (forced or not)."""
    indices: list[int] = []
    _, terminal_idx = _accepted_terminal_with_index(events, f"dep:{node_id}")
    if terminal_idx is not None:
        indices.append(terminal_idx)
    for i, e in enumerate(events):
        if (e.get("event") == "node_status_change" and e.get("node") == node_id
                and e.get("status") == "done"):
            indices.append(i)
    return indices


def _nonforced_done_index(events: list[dict[str, Any]], node_id: str) -> int | None:
    """Index of the LAST non-forced ``done`` transition recorded for ``node_id`` (or ``None``)."""
    idx: int | None = None
    for i, e in enumerate(events):
        if (e.get("event") == "node_status_change" and e.get("node") == node_id
                and e.get("status") == "done" and not e.get("forced")):
            idx = i
    return idx


@dataclass(frozen=True)
class WorkflowIssue:
    """One configured-workflow problem, classified for the report's status derivation.

    ``kind`` is exactly one of ``"missing"`` / ``"invalid"`` / ``"flagged"`` and orders the report
    status (``invalid`` > ``flagged`` > ``missing``, i.e. INVALID > FLAGGED > IN PROGRESS); the CLEAN
    status requires *no* issues at all. ``message`` names the phase/node and is rendered (sanitized)
    in the report Summary.
    """

    kind: str  # "missing" | "invalid" | "flagged"
    message: str


def _node_completion_issues(
    events: list[dict[str, Any]], dag: DAG, node_id: str
) -> list[WorkflowIssue]:
    """Classify how a ``done`` node's completion is (or is not) backed by a current binding.

    Mirrors :func:`_node_completion_backed` (the CLI's FINAL prerequisite), but distinguishes the
    *reason* for the report:

    - a current-bound accepted ``dep:<id>`` terminal -> no issue *iff* its artifact binds a
      same-gate/same-round Builder output and it is recorded before the node's non-forced ``done``;
    - a current-bound forced override -> ``flagged`` (an audited reviewed-gate bypass);
    - a ``dep:<id>`` terminal or forced override whose recorded hashes are stale -> ``invalid``;
    - a current-bound review with an unbound/tampered artifact, or recorded after the ``done``
      transition -> ``invalid``;
    - neither -> ``flagged`` (done without an accepted review gate).
    """
    current_dag = dag_sha256(dag)
    current_node = node_sha256(dag, node_id)

    terminal, terminal_idx = _accepted_terminal_with_index(events, f"dep:{node_id}")
    if terminal is not None:
        if (terminal.get("dag_sha256") != current_dag
                or terminal.get("node_sha256") != current_node):
            return [WorkflowIssue(
                "invalid",
                f"node {node_id!r} completion is bound to a stale review — its dag/node sha256 no "
                f"longer match the current tree")]
        issues = _artifact_binding_issues(events, f"dep:{node_id}", terminal, terminal_idx,
                                          f"node {node_id!r}")
        # The accepted review must be recorded BEFORE the node's non-forced ``done`` transition; a
        # dependency terminal appended after ``done`` marked the node complete before its review.
        done_idx = _nonforced_done_index(events, node_id)
        if done_idx is not None and terminal_idx is not None and terminal_idx > done_idx:
            issues.append(WorkflowIssue(
                "invalid",
                f"node {node_id!r} was marked done before its dependency review reached consensus "
                f"(out of order)"))
        return issues

    forced = [e for e in events
              if e.get("event") == "node_status_change" and e.get("node") == node_id
              and e.get("forced") and e.get("status") == "done"]
    if forced:
        last = forced[-1]
        if (last.get("dag_sha256") == current_dag
                and last.get("node_sha256") == current_node):
            return [WorkflowIssue(
                "flagged",
                f"node {node_id!r} was completed by a forced override (reviewed-gate bypass)")]
        return [WorkflowIssue(
            "invalid",
            f"node {node_id!r} forced completion records stale dag/node sha256")]

    return [WorkflowIssue(
        "flagged",
        f"node {node_id!r} is done without an accepted review gate or forced override")]
