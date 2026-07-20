"""Thin CLI wrapping config, dag, verdict, runlog, and report modules."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import webbrowser
from pathlib import Path

from crucible.config import Config, load_config
from crucible.dag import DAG
from crucible.integrity import (
    current_bindings,
    dag_sha256,
    node_sha256,
    read_artifact,
    require_current_schema,
)
from crucible.lenses import read_critic_lenses
from crucible.report import render_html, render_markdown
from crucible.runlog import RunLog, RunLogCorruptError, init_run
from crucible.symmetric import (
    SYMMETRIC_WORKFLOWS,
    VALID_WORKFLOWS,
    FindingSet,
    PeerAttestation,
    decide_symmetric,
    peer_slot_provenance,
    workflow_kind,
)
from crucible.verdict import VALID_RESOLUTIONS, Finding, Verdict, decide
from crucible.workflow import (
    accepted_terminal,
    require_final_ready,
    require_node_review_ready,
    require_plan_ready,
    require_plan_verdict_ready,
    require_reproduce_ready,
)

# Events that `crucible log` may append. All other events are emitted by their own
# commands (verdict, set-status, load-dag, init-run); allowing `log` to write them would
# let a caller forge decisions/verdicts the report renders as authoritative.
LOG_EVENTS = ("builder_output", "critic_output")


def _read_payload(path):
    # Store the file's raw text verbatim (full-fidelity provenance) — do NOT parse JSON,
    # which would drop exact whitespace/key order when the report re-serializes it.
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def _print_dependency_tree(dag, stream=None) -> None:
    """Print the dependency tree in build (topological) order: one line per node with its
    title and the ids it depends on. Shared by `load-dag` (echo at import) and `show-plan`.
    Uses `topological_order()` — not the raw insertion `order` — so the 'build order' label
    is honest even when the plan lists nodes out of order. ``stream`` defaults to stdout,
    resolved at call time so a redirected stdout (pytest capsys) is still captured."""
    stream = sys.stdout if stream is None else stream
    print("=== Dependency tree (build order) ===", file=stream)
    for nid in dag.topological_order():
        n = dag.nodes[nid]
        deps = ", ".join(sorted(dag.deps.get(nid, ()))) or "—"
        print(f"  {n.id}: {n.title}  [deps: {deps}]", file=stream)


def _plan_advance_terminal(events) -> dict | None:
    """The last PLAN gate advance-terminal event (``gate_consensus``/``gate_proceeded_with_flags``),
    or ``None`` if the plan gate has not advanced. ``gate_capped`` (a halt) is deliberately excluded:
    a capped plan is not an approval."""
    advance = [e for e in events if e.get("gate") == "plan"
               and e.get("event") in ("gate_consensus", "gate_proceeded_with_flags")]
    return advance[-1] if advance else None


def _bound_plan_payload(events) -> str | None:
    """The exact approved plan artifact: the payload of the pre-terminal ``builder_output`` whose
    ``artifact_sha256`` matches the PLAN advance-terminal's recorded binding.

    Selection is by *content identity*, never by round — a later, un-reviewed edit (even at a higher
    round) has a different hash and is never chosen. Returns ``None`` (rendered as the no-artifact
    placeholder) when the plan has not advanced or no logged artifact matches the accepted binding.
    """
    terminal = _plan_advance_terminal(events)
    if terminal is None:
        return None
    approved_sha = terminal.get("artifact_sha256")
    if not approved_sha:
        return None
    matches = [e for e in events if e.get("gate") == "plan"
               and e.get("event") == "builder_output"
               and e.get("artifact_sha256") == approved_sha]
    return matches[-1].get("payload") if matches else None


def _print_approved_plan(payload, dag, stream=None) -> None:
    """Render an already-resolved approved plan ``payload`` + a given (already-loaded) dependency
    tree to ``stream``. Shared by `show-plan` (stdout) and the automatic echo when the PLAN gate
    settles (stderr). The caller resolves the exact bound payload (see ``_bound_plan_payload``) and
    loads the DAG; ``None`` renders the ``(no plan artifact logged)`` placeholder rather than any
    post-consensus or unbound payload."""
    stream = sys.stdout if stream is None else stream
    print("=== Approved plan ===", file=stream)
    print(payload if payload is not None else "(no plan artifact logged)", file=stream)
    print(file=stream)
    _print_dependency_tree(dag, stream)


def _load_resolutions(path):
    """Return (normalized {id: resolution}, raw {id: {...}}) from a resolutions file."""
    if not path:
        return {}, {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("resolutions file must be a JSON object mapping finding ids to resolutions")
    norm: dict[str, str] = {}
    raw: dict[str, dict] = {}
    for fid, val in data.items():
        if isinstance(val, dict):
            res = val.get("resolution")
            raw[fid] = val
        else:
            res = val
            raw[fid] = {"resolution": val}
        if res not in VALID_RESOLUTIONS:
            raise ValueError(f"invalid resolution {res!r} for {fid}; must be one of "
                             f"{VALID_RESOLUTIONS} (omit the id to leave a finding unresolved)")
        # `wontfix` (a rebuttal) and `deferred` clear a blocking finding without a fix — the loop
        # advances past it — so each must carry a non-empty rationale for the audit trail. `fixed`
        # is re-reviewed next round, so it needs none.
        if res in ("wontfix", "deferred"):
            rationale = raw[fid].get("rationale")
            if not (isinstance(rationale, str) and rationale.strip()):
                raise ValueError(
                    f"resolution {res!r} for {fid} requires a non-empty 'rationale' "
                    f'(use the object form {{"resolution": {res!r}, "rationale": "…"}}); '
                    f"a bare {res!r} that clears a finding without a recorded reason is not allowed"
                )
        norm[fid] = res
    return norm, raw


def _finding_line(f: Finding, resolution: str | None = None) -> str:
    """One detailed terminal line for a finding: ``  id [severity] (resolution) location: claim``.

    ``resolution`` is tagged only when it is a non-``fixed`` value that failed to clear (in
    practice a ``wontfix`` under ``strict_rebuttal``); ``fixed`` is suppressed because it is
    redundant with the "will fix" header. ``location``/``claim`` are omitted when empty."""
    tag = f" ({resolution})" if resolution and resolution != "fixed" else ""
    location = f" {f.location}" if f.location else ""
    claim = f": {f.claim}" if f.claim else ""
    return f"  {f.id} [{f.severity}]{tag}{location}{claim}"


def _print_finding_lists(fixed, unresolved, resolutions, stream) -> None:
    """Print the two disjoint finding lists the Builder faces, each only when non-empty:
    the findings it committed to fix (resolution ``fixed``) and the still-open blocking
    findings it has not (from the deterministic decision). Informational, so writes to
    ``stream`` (stderr) — the outcome token stays alone on stdout."""
    if fixed:
        print(f"Findings the Builder will fix ({len(fixed)}):", file=stream)
        for f in fixed:
            print(_finding_line(f), file=stream)
    if unresolved:
        print(f"Unresolved blocking findings ({len(unresolved)}):", file=stream)
        for f in unresolved:
            print(_finding_line(f, resolutions.get(f.id)), file=stream)


def _validate_gate(gate: str) -> None:
    """A gate is ``plan``, ``reproduce``, ``final``, or ``dep:<node-id>``. Reject anything else
    (e.g. a typo like ``finale``) so a verdict/log is never recorded under a bogus, off-convention
    gate — which would otherwise silently use the dependency round cap and render as a
    spurious report section. The ``dep:`` id must be non-empty and free of surrounding
    whitespace (``dep:`` / ``dep:  `` are blank ids, not real nodes)."""
    if gate in ("plan", "final", "reproduce"):
        return
    if gate.startswith("dep:"):
        node_id = gate[len("dep:"):]
        if node_id and node_id == node_id.strip():
            return
    raise ValueError(f"invalid --gate {gate!r}; must be 'plan', 'reproduce', 'final', or 'dep:<node-id>'")


# A gate concludes with exactly one of these terminal events; a second one would silently
# rewrite the gate's apparent outcome in the report (C3).
_TERMINAL_EVENTS = ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")


def _require_dep_node_in_dag(run: RunLog, gate: str) -> None:
    """For a ``dep:<id>`` gate, require ``<id>`` to be a real node in the run's DAG (C1). A
    typo'd/ghost dependency would otherwise record a verdict/log — and a terminal outcome —
    under a node that does not exist. ``plan``/``final`` gates are unaffected. Assumes the
    gate has already passed ``_validate_gate`` (so the id is non-empty)."""
    if not gate.startswith("dep:"):
        return
    node_id = gate[len("dep:"):]
    try:
        dag = DAG.from_dict(run.load_dag())
    except FileNotFoundError:
        raise ValueError(f"gate {gate!r} targets a dependency node, but this run has no "
                         f"dependency tree yet; run load-dag first")
    # DAG.from_dict validates the shape, so a malformed dag.json surfaces as a clean `crucible:`
    # error (a ValueError, or an "invalid JSON"/"missing required field" message) — never an
    # AttributeError traceback.
    if node_id not in dag.nodes:
        raise ValueError(f"gate {gate!r} targets unknown node {node_id!r}; known nodes: "
                         f"{sorted(dag.nodes)}")


def _require_gate_not_terminal(run: RunLog, gate: str) -> None:
    """Reject a verdict for a gate that already logged a terminal outcome (C3), so an
    accidental rerun cannot silently overwrite a concluded gate's decision. The gate loop's
    non-terminal ``CHANGES`` rounds log no terminal event, so re-deciding within the loop
    stays allowed."""
    prior = [e for e in run.read_events()
             if e.get("gate") == gate and e.get("event") in _TERMINAL_EVENTS]
    if prior:
        last = prior[-1]
        raise SystemExit(f"gate {gate!r} already concluded ({last['event']} at round "
                         f"{last.get('round', '?')}); refusing to re-decide a terminal gate")


def _max_rounds_for_gate(cfg: Config, gate: str) -> int:
    return cfg.max_rounds_plan if gate in ("plan", "reproduce") else cfg.max_rounds_dep


def _expected_round(run: RunLog, gate: str) -> int:
    """The next review round for a gate, DERIVED from run history: one past the number of prior
    per-round verdict events already logged for that gate. This makes round counting CLI-owned (F1b)
    so a caller cannot skip to the cap or repeat a round to dodge it.

    The verdict event is workflow-specific: the asymmetric Builder/Critic flow logs one
    ``critic_verdict`` per round, while a symmetric ``deep-dive``/``pr-review`` run logs one
    ``symmetric_verdict`` (two peer attestations) per round. Counting the mode's own marker keeps the
    shared ``log``/``bindings`` handshake and the per-mode decision command in lock-step.
    """
    events = run.read_events()
    marker = ("symmetric_verdict" if workflow_kind(events) in SYMMETRIC_WORKFLOWS
              else "critic_verdict")
    prior = sum(1 for e in events
                if e.get("gate") == gate and e.get("event") == marker)
    return prior + 1


def _require_gate_stage_ready(run: RunLog, cfg: Config, gate: str) -> None:
    """Enforce the configured STAGE/PHASE prerequisites for a gate's ``log``/``bindings``/``verdict``
    (Task 3). Shared by all three so a Builder artifact, its bindings, and the Critic verdict are
    only ever recorded/emitted once the gate is legitimately reachable:

    - ``reproduce`` — Stage 0, but part of the configured workflow ONLY when ``reproduce_gate`` is
      enabled (rejected otherwise, so a disabled REPRODUCE gate never records or certifies);
    - ``plan`` — REPRODUCE consensus when ``reproduce_gate`` is configured;
    - ``dep:<id>`` — accepted+bound PLAN, configured approval, deps done, node ``in_progress``/``in_review``;
    - ``final`` — ``final_review`` enabled, bound PLAN/approval, every node done and backed.

    Delegates to ``crucible.workflow`` so stage logic is never duplicated. Assumes ``gate`` already
    passed ``_validate_gate`` and (for ``dep:``) ``_require_dep_node_in_dag``.
    """
    if gate == "reproduce":
        require_reproduce_ready(cfg)
        return
    if gate == "plan":
        require_plan_verdict_ready(run, cfg)
        return
    dag = DAG.from_dict(run.load_dag())
    if gate == "final":
        require_final_ready(run, cfg, dag)
    elif gate.startswith("dep:"):
        require_node_review_ready(run, cfg, dag, gate[len("dep:"):])


def cmd_init_run(args) -> int:
    cfg = load_config(args.config) if args.config else Config.from_dict({})
    base = args.base_dir or os.environ.get("CRUCIBLE_RUNS_DIR") or str(Path.home() / ".crucible" / "runs")
    run = init_run(args.goal, cfg, base_dir=base, workflow=args.workflow)
    print(run.path)
    return 0


def cmd_load_dag(args) -> int:
    run = RunLog(args.run)
    require_current_schema(run)
    # Artifact immutability: once the PLAN gate concludes (any terminal outcome), the accepted
    # dependency tree is frozen. Refuse to replace it — even with --force — because a Critic already
    # reviewed and the run bound its decision to this exact tree. A changed plan/DAG needs a fresh
    # run. Checked before --force so the override cannot reach it.
    plan_terminal = [e for e in run.read_events()
                     if e.get("gate") == "plan" and e.get("event") in _TERMINAL_EVENTS]
    if plan_terminal:
        last = plan_terminal[-1]
        raise SystemExit(
            f"load-dag: the plan gate has already concluded ({last['event']} at round "
            f"{last.get('round', '?')}); refusing to replace the accepted dependency tree — a "
            f"changed plan requires a fresh run (--force cannot override an accepted plan)."
        )
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    dag = DAG.from_dict(data)
    if not dag.nodes:
        raise SystemExit("load-dag: dependency tree is empty (0 nodes); a plan must decompose "
                         "into at least one node")
    # G2: load-dag imports a *fresh* plan, so every node must start `pending`. A node baked
    # as `done`/`in_progress`/`blocked` would let `next` schedule its dependents and silently
    # skip its work. Statuses change only through set-status, never via the imported plan.
    non_pending = [nid for nid in dag.order if dag.nodes[nid].status != "pending"]
    if non_pending:
        raise SystemExit(f"load-dag: a freshly imported plan must have every node 'pending'; "
                         f"these are not: {non_pending}. Node statuses change only via set-status.")
    # G2b: refuse to clobber an existing run that already has progress. The incoming file is
    # all-`pending` (checked above), but blindly overwriting would reset a run mid-implementation
    # (done/in_progress -> pending). The PLAN loop re-runs load-dag while everything is still
    # pending, so that path is unaffected; only a real in-flight run is protected. --force overrides.
    if not args.force:
        try:
            existing = DAG.from_dict(run.load_dag())
        except FileNotFoundError:
            existing = None
        if existing is not None:
            progressed = [nid for nid in existing.order if existing.nodes[nid].status != "pending"]
            if progressed:
                raise SystemExit(
                    f"load-dag: this run already has a dependency tree with progress (non-pending "
                    f"nodes: {progressed}); refusing to overwrite and reset it. Pass --force to "
                    f"replace it (discards current node statuses)."
                )
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=len(dag.nodes), forced=bool(args.force),
               dag_sha256=dag_sha256(dag))
    print(f"loaded {len(dag.nodes)} nodes")
    print()
    _print_dependency_tree(dag)
    return 0


def cmd_next(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    state, node = dag.next_state()
    if state == "ready":
        # A ready node is about to be scheduled for implementation, so the PLAN gate (and configured
        # approval) must be accepted and still bound to the current tree first — never begin node
        # work before the plan the Critic reviewed is settled. Reporting states (complete/in_flight/
        # stuck) schedule nothing, so they are not gated here.
        cfg = load_config(run.path / "config.json")
        require_plan_ready(run, cfg)
        print(node)
        return 0
    if state == "complete":
        print("")
        return 0
    # No node can be scheduled and the run is not done: surface every unfinished node
    # (with its status and unmet deps) to STDERR and exit non-zero so the orchestrator
    # halts instead of mistaking "stuck" for "done". stdout stays empty.
    detail = "; ".join(
        f"{d['id']}[{d['status']}]" + (f" waiting on {d['waiting_on']}" if d["waiting_on"] else "")
        for d in dag.unfinished_detail()
    )
    if state == "in_flight":
        print(f"crucible next: no ready node; work is in flight — finish or reset it before "
              f"scheduling more. Unfinished: {detail}", file=sys.stderr)
        return 4
    print(f"crucible next: run is STUCK — no node can proceed and none is in flight. "
          f"Unfinished: {detail}", file=sys.stderr)
    return 3


def cmd_set_status(args) -> int:
    run = RunLog(args.run)
    # set-status is a mutating command: a legacy run's provenance is unverified, so refuse to
    # transition (or forcibly complete) its nodes — start a fresh run instead.
    require_current_schema(run)
    dag = DAG.from_dict(run.load_dag())
    if args.node not in dag.nodes:
        raise ValueError(f"unknown node: {args.node}")
    # --force is the reviewed-gate bypass for a COMPLETION only: its whole purpose is to force a node
    # to `done` within an accepted plan (recording current DAG/node hashes + rationale). Forcing any
    # other target (in_progress/in_review/pending/blocked) is not a supported operation — reject it up
    # front so `--force` can never skip the legal transition table for a non-done node or start work
    # outside the plan/approval gate.
    if args.force and args.status != "done":
        raise SystemExit(
            f"set-status: --force is only valid with --status done — it is the reviewed-gate "
            f"completion bypass (records the override + current hashes); refusing to force "
            f"{args.status!r}."
        )
    # C2: a node cannot begin or finish work (in_progress/in_review/done) while a dependency
    # is unfinished — that would let `next` schedule its dependents and skip the dependency's
    # work. `pending`/`blocked` are not work statuses and stay settable for recovery. Checked even
    # under --force (a forced completion must not bypass real dependency ordering).
    if args.status in ("in_progress", "in_review", "done"):
        unmet = sorted(d for d in dag.deps[args.node] if dag.nodes[d].status != "done")
        if unmet:
            raise SystemExit(f"set-status: cannot set {args.node!r} to {args.status!r} while these "
                             f"dependencies are not done: {unmet}")
    # A --force is an explicit human override, so it must carry a non-empty rationale — recorded in
    # provenance and surfaced in the report. Checked AFTER the C2 dependency guard (so a forced done
    # blocked by unfinished deps still reports the dependency problem) but BEFORE the PLAN/approval
    # and review-gate prerequisites (so a missing rationale is reported as such).
    rationale = (args.rationale or "").strip()
    if args.force and not rationale:
        raise SystemExit(
            "set-status: --force requires --rationale explaining the override "
            "(it bypasses the node's review gate; the reason is recorded in the run log)."
        )
    forced_bindings: dict[str, str] = {}
    # Starting or advancing node work (in_progress/in_review) requires the accepted+bound PLAN the
    # Critic reviewed — and, when human_approval is configured, the recorded approval — to be in
    # place first: never begin implementation before the plan/approval gate. Dependencies were
    # already enforced above (C2, so an unmet dep still reports the dependency problem). Recovery
    # statuses (`pending`/`blocked`) are intentionally left ungated so a run can always be reset or
    # unblocked.
    if args.status in ("in_progress", "in_review"):
        require_plan_ready(run, load_config(run.path / "config.json"))
    elif args.status == "done":
        cfg = load_config(run.path / "config.json")
        if args.force:
            # A forced completion is the explicit reviewed-gate bypass, but it still happens WITHIN
            # an accepted, currently-bound plan (and configured approval) — a run cannot force node
            # completion before its plan is settled. Record the current DAG/node hashes so the report
            # can prove the override targeted the current tree, and flag it.
            require_plan_ready(run, cfg)
            forced_bindings = {"dag_sha256": dag_sha256(dag),
                               "node_sha256": node_sha256(dag, args.node)}
        else:
            # Normal completion requires an accepted+bound PLAN (and configured approval), the node's
            # OWN review gate to have advanced (gate_consensus/gate_proceeded_with_flags — a
            # gate_capped halt does NOT qualify), and that gate's recorded DAG/node bindings to still
            # match the current tree/node (Task 2 stale-consensus guard). The legal transition
            # (in_progress|in_review -> done) is then enforced by DAG.set_status.
            require_plan_ready(run, cfg)
            node_gate = f"dep:{args.node}"
            terminal = accepted_terminal(run.read_events(), node_gate)
            if terminal is None:
                raise SystemExit(
                    f"set-status: refusing to mark {args.node!r} done — its review gate {node_gate!r} "
                    f"has not reached consensus (or proceeded with flags). Run the gate to a terminal "
                    f"advance first, or pass --force to override (recorded)."
                )
            if (terminal.get("dag_sha256") != dag_sha256(dag)
                    or terminal.get("node_sha256") != node_sha256(dag, args.node)):
                raise SystemExit(
                    f"set-status: refusing to mark {args.node!r} done — the reviewed dependency "
                    f"binding no longer matches the current tree/node (dag/node sha256 changed since "
                    f"gate {node_gate!r} was accepted). The plan/DAG/node was changed after review; "
                    f"re-review the current node or start a fresh run."
                )
    # `--force` only ever bypasses the transition table for a `done` completion (guarded above so a
    # non-done target already exited); every other status must follow the legal ALLOWED_TRANSITIONS
    # table. Pass the conjunction so DAG.set_status can never skip the table for a non-done node.
    dag.set_status(args.node, args.status, force=args.force and args.status == "done")
    run.save_dag(dag.to_dict())
    run.append("node_status_change", node=args.node, status=args.status,
               forced=bool(args.force), rationale=rationale, **forced_bindings)
    print(f"{args.node} -> {args.status}")
    return 0


def cmd_log(args) -> int:
    if args.event not in LOG_EVENTS:
        raise ValueError(f"log --event must be one of {LOG_EVENTS}; other events are written "
                         f"by their own commands (verdict, set-status, load-dag)")
    _validate_gate(args.gate)
    if args.round < 1:
        raise SystemExit("log --round must be >= 1 (rounds are 1-based)")
    run = RunLog(args.run)
    require_current_schema(run)
    _require_dep_node_in_dag(run, args.gate)
    # Stage/phase prerequisites (Task 3): a Builder artifact may only be logged when the gate is
    # legitimately reachable — REPRODUCE consensus before PLAN when configured; an accepted+bound
    # PLAN, configured approval, done deps and an in_progress/in_review node before a dep review; a
    # complete, backed implementation before FINAL. Checked before the terminal-gate guard so a
    # not-yet-reachable gate is rejected for the more fundamental reason.
    _require_gate_stage_ready(run, load_config(run.path / "config.json"), args.gate)
    # Artifact immutability: never log Builder/Critic output for a gate that already concluded — a
    # post-terminal artifact would masquerade as reviewed content (the reported same-round bug).
    terminal = [e for e in run.read_events()
                if e.get("gate") == args.gate and e.get("event") in _TERMINAL_EVENTS]
    if terminal:
        last = terminal[-1]
        raise SystemExit(
            f"log: gate {args.gate!r} already concluded ({last['event']} at round "
            f"{last.get('round', '?')}); refusing to log a new artifact to a terminal gate — a "
            f"changed artifact requires a fresh run."
        )
    if args.event == "builder_output":
        return _log_builder_output(run, args)
    # critic_output: raw provenance text, no binding — payload optional (empty allowed).
    payload = _read_payload(args.file)
    run.append(args.event, gate=args.gate, round=args.round, payload=payload)
    if payload is None:
        print(f"logged {args.event} (gate {args.gate}, round {args.round}); "
              f"no --file, empty payload")
    else:
        print(f"logged {args.event} (gate {args.gate}, round {args.round}, "
              f"{len(payload)} chars):")
        sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")
    return 0


def _log_builder_output(run: RunLog, args) -> int:
    """Log a Builder artifact under the binding handshake: require ``--file`` with a non-empty
    payload, require the CLI-derived current round (so an artifact can't be back/forward-dated to a
    concluded or future round), read the *exact bytes* (CRLF-preserving), and record the
    ``artifact_sha256`` the later `bindings`/verdict handshake binds the gate decision to."""
    if not args.file:
        raise SystemExit(
            "log: builder_output requires --file (the Builder artifact whose exact bytes bind the "
            "gate decision); an empty payload cannot be reviewed or bound."
        )
    expected = _expected_round(run, args.gate)
    if args.round != expected:
        raise SystemExit(
            f"log --round {args.round} does not match the expected round {expected} for gate "
            f"{args.gate!r} ({expected - 1} prior review round(s) logged); a Builder artifact is "
            f"logged for the current round only."
        )
    text, digest = read_artifact(args.file)
    if not text:
        raise SystemExit(
            "log: builder_output artifact is empty; a Builder artifact must be non-empty to be "
            "reviewable and bindable."
        )
    run.append("builder_output", gate=args.gate, round=args.round, payload=text,
               artifact_sha256=digest)
    print(f"logged builder_output (gate {args.gate}, round {args.round}, {len(text)} chars):")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def _require_matching_bindings(verdict: Verdict, expected: dict[str, str], gate: str, round_index: int) -> None:
    """Reject a schema-2 verdict whose echoed bindings do not exactly match ``expected`` (the
    CLI-selected ``crucible bindings`` output for this gate/round).

    An exact match is required: every expected field present and equal, and no extra binding field
    the gate never carries (e.g. a ``node_sha256`` on a plan gate). Raises ``SystemExit`` — with
    "binding" in the message — so the caller rejects before any log append."""
    provided = {key: getattr(verdict, key)
                for key in ("artifact_sha256", "dag_sha256", "node_sha256")}
    problems = [f"{key}: expected {want}, got {provided.get(key)!r}"
                for key, want in expected.items() if provided.get(key) != want]
    problems += [f"unexpected {key} for this gate" for key in provided
                 if provided[key] is not None and key not in expected]
    if problems:
        raise SystemExit(
            f"verdict bindings do not match the CLI-selected artifact/DAG/node for gate {gate!r} "
            f"round {round_index} ({'; '.join(problems)}); the Critic verdict must echo the exact "
            f"`crucible bindings` output — refusing to record a decision bound to a different "
            f"artifact."
        )


def cmd_verdict(args) -> int:
    if args.round < 1:
        raise SystemExit("--round must be >= 1 (rounds are 1-based)")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds must be >= 1")
    _validate_gate(args.gate)
    run = RunLog(args.run)
    # Schema guard: a legacy run's provenance is unverified, so a verdict must never append to it —
    # checked before any read/mutation so `verdict` can never certify unbindable history.
    require_current_schema(run)
    cfg = load_config(run.path / "config.json")
    # Routing guard: `verdict` records ONE asymmetric Builder/Critic sign-off. A symmetric
    # (deep-dive/pr-review) run requires two separately produced peer attestations, so it must never
    # be certified by a single `verdict` — reject before reading/parsing the verdict file and direct
    # the orchestrator to `symmetric-verdict`.
    if workflow_kind(run.read_events()) in SYMMETRIC_WORKFLOWS:
        raise SystemExit(
            "crucible: verdict is the asymmetric Builder/Critic command, but this is a symmetric "
            "run — use `symmetric-verdict` with separate --peer-a/--peer-b attestation files."
        )
    raw_text = Path(args.file).read_text(encoding="utf-8")
    data = json.loads(raw_text)
    verdict = Verdict.from_dict(data)

    # F5: the verdict's own gate/round must match what the caller asserts.
    if verdict.gate != args.gate:
        raise SystemExit(f"verdict gate {verdict.gate!r} does not match --gate {args.gate!r}")
    if verdict.round != args.round:
        raise SystemExit(f"verdict round {verdict.round} does not match --round {args.round}")

    # C3/C1: refuse to re-decide a concluded gate, and require a dep:<id> gate to name a real
    # node — both before anything is logged or decided.
    _require_gate_not_terminal(run, args.gate)
    _require_dep_node_in_dag(run, args.gate)

    # Stage/phase prerequisites (Task 3): the gate must be legitimately reachable before a verdict
    # can settle it — REPRODUCE before PLAN when configured; accepted+bound PLAN, configured
    # approval, done deps and an in_progress/in_review node before a dep review; a complete, backed
    # implementation before FINAL. Enforced here (after the concluded/ghost guards, before round
    # bookkeeping and the binding handshake) so an out-of-order gate never records a decision.
    _require_gate_stage_ready(run, cfg, args.gate)

    # F1b: the round is DERIVED from run history, not trusted from the caller. It must be exactly
    # one past the number of prior review rounds for this gate — closing the bypass where a caller
    # asserts round=max (immediate cap) or repeats a round to never cap. Checked after C3/C1 so a
    # concluded/ghost gate is still rejected for its own reason first.
    expected = _expected_round(run, args.gate)
    if args.round != expected:
        raise SystemExit(
            f"verdict --round {args.round} does not match the expected round {expected} for gate "
            f"{args.gate!r} ({expected - 1} prior review round(s) logged); rounds are derived from "
            f"run history and must be consecutive starting at 1."
        )

    # H1: reject a Critic verdict whose APPROVE/REQUEST_CHANGES label contradicts its
    # findings under the run's blocking_severities, before logging or deciding.
    inconsistency = verdict.consistency_error(cfg)
    if inconsistency:
        raise SystemExit(inconsistency)

    # F1: round cap comes from config (by gate) unless explicitly overridden.
    max_rounds = args.max_rounds if args.max_rounds is not None else _max_rounds_for_gate(cfg, args.gate)

    # F2: optional Builder resolutions feed the deterministic decision.
    resolutions, resolutions_raw = _load_resolutions(args.resolutions)
    # O1: a resolution must target a real finding; an unknown id (e.g. a typo) would be
    # silently ignored, so reject it before logging or deciding.
    finding_ids = {f.id for f in verdict.findings}
    unknown = set(resolutions) - finding_ids
    if unknown:
        raise ValueError(f"resolutions reference unknown finding id(s): {sorted(unknown)}; "
                         f"valid ids: {sorted(finding_ids)}")
    # O5-B: `deferred` only clears a finding whose severity is in defer_severities; deferring a
    # blocking finding has no effect, so reject it (a typo/misuse) instead of logging a
    # misleading no-op resolution.
    severity_by_id = {f.id: f.severity for f in verdict.findings}
    bad_defer = sorted(fid for fid, res in resolutions.items()
                       if res == "deferred" and severity_by_id.get(fid) not in cfg.defer_severities)
    if bad_defer:
        raise ValueError(f"cannot defer finding(s) {bad_defer}: 'deferred' is only allowed for "
                         f"severities in defer_severities {cfg.defer_severities}")

    # Binding handshake: the verdict must echo the EXACT bindings the CLI selects for this
    # gate/round — the artifact the Critic reviewed plus the relevant DAG/node identity. Recompute
    # them from run history + the current tree and require an exact match (every expected field
    # present and equal; no extra field the gate never carries) BEFORE any log append. This proves
    # the decision refers to the same content the CLI selected — a substituted artifact/DAG/node or
    # a stale echo is rejected without mutating the run. Placed after resolution validation so a
    # malformed-resolution verdict still fails for its own reason first.
    bindings = current_bindings(run, args.gate, args.round).to_dict()
    _require_matching_bindings(verdict, bindings, args.gate, args.round)

    if resolutions_raw:
        run.append("builder_resolution", gate=args.gate, round=args.round, payload=resolutions_raw)

    decision = decide(verdict, cfg, round_index=args.round, max_rounds=max_rounds,
                      resolutions=resolutions, always_halt=(args.gate == "reproduce"))

    # F6: persist both the parsed verdict and the Critic's full raw output. These are not
    # redundant: the report renders the parsed payload as a readable digest (verdict + summary
    # + per-finding bullets) and the `raw` text as a full-fidelity provenance block (exact
    # bytes/formatting/extra keys). Keeping both is intentional (digest for humans, raw for audit).
    # The validated bindings ride on the critic_verdict and every terminal event so provenance
    # records exactly which artifact/DAG/node this decision was bound to.
    run.append("critic_verdict", gate=args.gate, round=args.round, payload=data, raw=raw_text,
               **bindings)
    if decision.outcome == "CONSENSUS":
        run.append("gate_consensus", gate=args.gate, round=args.round, **bindings)
    elif decision.outcome == "PROCEED_WITH_FLAGS":
        run.append("gate_proceeded_with_flags", gate=args.gate, round=args.round,
                   open_findings=[f.id for f in decision.open_findings], **bindings)
    elif decision.outcome == "CAPPED":
        run.append("gate_capped", gate=args.gate, round=args.round,
                   open_findings=[f.id for f in decision.open_findings], **bindings)
    elif decision.outcome != "CHANGES":  # CHANGES intentionally logs no terminal event
        raise SystemExit(f"unexpected decision outcome: {decision.outcome!r}")
    print(decision.outcome)
    # Surface the two finding lists the Builder faces (fix vs. still-open blockers) to stderr,
    # leaving the machine-readable outcome token alone on stdout. Disjoint by construction:
    # `fixed` are the findings resolved `fixed`; `unresolved` are the decision's still-open
    # blockers not marked `fixed`.
    fixed = [f for f in verdict.findings if resolutions.get(f.id) == "fixed"]
    unresolved = [f for f in decision.open_findings if resolutions.get(f.id) != "fixed"]
    _print_finding_lists(fixed, unresolved, resolutions, sys.stderr)
    # When the PLAN gate settles, deterministically echo the approved plan + dependency tree to
    # stderr so the final plan and DAG are always visible before implementation — not dependent on
    # the orchestrator remembering to run `show-plan` (the reported bug). `verdict` is decoupled
    # from the DAG (it can settle a plan gate without one), so this is best-effort: skip the echo
    # (never crash, never print a masking placeholder) if no DAG is loaded. Only the two
    # "advance past PLAN" outcomes echo; CHANGES/CAPPED do not.
    if args.gate == "plan" and decision.outcome in ("CONSENSUS", "PROCEED_WITH_FLAGS"):
        try:
            settled_dag = DAG.from_dict(run.load_dag())
        except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError, OSError):
            # Best-effort echo: a missing OR corrupt/invalid/malformed dag.json must never turn a
            # concluded gate into a failure. The outcome token + gate_consensus are already emitted;
            # skip the echo. (CycleError is a ValueError; a node/edge missing a required key such as
            # "id" raises KeyError.) In practice the binding handshake above already required a valid
            # DAG, so this guard is defensive.
            settled_dag = None
        if settled_dag is not None:
            _print_approved_plan(_bound_plan_payload(run.read_events()), settled_dag, sys.stderr)
    return 0


def _require_matching_peer_bindings(peer: PeerAttestation, expected: dict[str, str], slot: str,
                                    gate: str, round_index: int) -> None:
    """Reject a peer attestation whose echoed bindings do not exactly match ``expected`` (the
    CLI-selected ``crucible bindings`` output for this gate/round) — the symmetric counterpart of
    ``_require_matching_bindings``.

    An exact match is required: every expected field present and equal, and no extra binding field
    the gate never carries. Raises ``SystemExit`` (with "binding" in the message) so the caller
    rejects before any append — a peer that reviewed a different artifact/DAG/node can never attest.
    """
    provided = {key: getattr(peer, key)
                for key in ("artifact_sha256", "dag_sha256", "node_sha256")}
    problems = [f"{key}: expected {want}, got {provided.get(key)!r}"
                for key, want in expected.items() if provided.get(key) != want]
    problems += [f"unexpected {key} for this gate" for key in provided
                 if provided[key] is not None and key not in expected]
    if problems:
        raise SystemExit(
            f"symmetric-verdict: peer {slot} bindings do not match the CLI-selected artifact/DAG/node "
            f"for gate {gate!r} round {round_index} ({'; '.join(problems)}); each peer attestation "
            f"must echo the exact `crucible bindings` output — refusing to record a decision bound to "
            f"a different artifact."
        )


def _bound_candidate_finding_set(run: RunLog, gate: str, round_index: int) -> FindingSet:
    """Parse the current bound Builder artifact for a dependency/FINAL gate as a ``FindingSet``.

    For symmetric dependency/FINAL gates the logged Builder artifact IS the candidate finding set
    (structured JSON), so the same latest non-empty ``builder_output`` payload that ``current_bindings``
    binds by hash is parsed here. PLAN artifacts are plain text and are never parsed this way, so this
    is only called for ``dep:``/``final`` gates. Assumes ``current_bindings`` already validated the
    artifact exists and its recorded hash matches the payload bytes.
    """
    outputs = [e for e in run.read_events()
               if e.get("event") == "builder_output" and e.get("gate") == gate
               and e.get("round") == round_index
               and isinstance(e.get("payload"), str) and e.get("payload")]
    if not outputs:  # pragma: no cover - current_bindings already required a bound artifact
        raise SystemExit(
            f"symmetric-verdict: no candidate finding set logged for gate {gate!r} round "
            f"{round_index}; log the Builder finding set before deciding."
        )
    return FindingSet.from_dict(json.loads(outputs[-1]["payload"]))


def cmd_symmetric_verdict(args) -> int:
    """Atomically decide a symmetric (deep-dive/pr-review) gate from TWO separately produced peer
    attestation files. Validates the complete candidate/peers/decision before any append, then logs
    one ``symmetric_verdict`` event (both raw+parsed slots, configured model/effort, namespaced
    aggregate objections, candidate + bindings), an ``accepted_finding_set`` for an advancing
    dependency/FINAL gate, and the standard terminal event LAST. No partial event is written if any
    file is invalid (design: atomic symmetric decision).
    """
    if args.round < 1:
        raise SystemExit("--round must be >= 1 (rounds are 1-based)")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds must be >= 1")
    _validate_gate(args.gate)
    run = RunLog(args.run)
    # Schema guard first: a legacy run's provenance is unverified, so never certify it.
    require_current_schema(run)
    cfg = load_config(run.path / "config.json")
    # Routing guard: symmetric-verdict is only for the two-peer workflows. A build run uses the
    # asymmetric `verdict` command — reject before any read/append.
    workflow = workflow_kind(run.read_events())
    if workflow not in SYMMETRIC_WORKFLOWS:
        raise SystemExit(
            f"crucible: symmetric-verdict is for deep-dive/pr-review runs, but this is a {workflow!r} "
            f"run — use `verdict` for the asymmetric Builder/Critic workflow."
        )

    # C3/C1: refuse to re-decide a concluded gate, and require a dep:<id> gate to name a real node.
    _require_gate_not_terminal(run, args.gate)
    _require_dep_node_in_dag(run, args.gate)
    # Stage/phase prerequisites: the gate must be legitimately reachable before a decision settles it
    # (accepted+bound PLAN, done deps, an in_progress/in_review node before a dep review, etc.).
    _require_gate_stage_ready(run, cfg, args.gate)

    # F1b: the round is DERIVED from prior symmetric_verdict events for this gate, not trusted from
    # the caller — it must be exactly one past the number logged, so a caller cannot skip to the cap
    # or repeat a round.
    expected = _expected_round(run, args.gate)
    if args.round != expected:
        raise SystemExit(
            f"symmetric-verdict --round {args.round} does not match the expected round {expected} "
            f"for gate {args.gate!r} ({expected - 1} prior symmetric round(s) logged); rounds are "
            f"derived from run history and must be consecutive starting at 1."
        )

    max_rounds = (args.max_rounds if args.max_rounds is not None
                  else _max_rounds_for_gate(cfg, args.gate))

    # The exact bindings BOTH peers must echo (recomputed from run history + the current tree).
    bindings = current_bindings(run, args.gate, args.round).to_dict()

    # Read and parse BOTH peer files completely before any validation append. Raw text is kept for
    # full-fidelity provenance; the parsed attestation is validated against the CLI-selected bindings.
    raw_a = Path(args.peer_a).read_text(encoding="utf-8")
    raw_b = Path(args.peer_b).read_text(encoding="utf-8")
    peer_a = PeerAttestation.from_dict(json.loads(raw_a))
    peer_b = PeerAttestation.from_dict(json.loads(raw_b))

    # Exactly one peer 'A' file via --peer-a and one peer 'B' file via --peer-b — no duplicates or
    # swapped labels. PeerAttestation.from_dict already restricts each `peer` to A/B, so this only has
    # to pin the slots to their flags.
    if peer_a.peer != "A" or peer_b.peer != "B":
        raise SystemExit(
            f"symmetric-verdict: expected one peer 'A' file via --peer-a and one peer 'B' file via "
            f"--peer-b; got --peer-a peer={peer_a.peer!r}, --peer-b peer={peer_b.peer!r}. Two equal "
            f"peers must supply exactly one A and one B attestation — no duplicate or swapped labels."
        )

    # Per-peer: exact gate/round, exact bindings, and internal APPROVE/REQUEST_CHANGES consistency.
    for slot, peer in (("A", peer_a), ("B", peer_b)):
        if peer.gate != args.gate:
            raise SystemExit(
                f"symmetric-verdict: peer {slot} gate {peer.gate!r} does not match --gate "
                f"{args.gate!r}")
        if peer.round != args.round:
            raise SystemExit(
                f"symmetric-verdict: peer {slot} round {peer.round} does not match --round "
                f"{args.round}")
        _require_matching_peer_bindings(peer, bindings, slot, args.gate, args.round)
        inconsistency = peer.consistency_error(cfg)
        if inconsistency:
            raise SystemExit(inconsistency)

    # Structured candidate finding set for dependency/FINAL gates (PLAN stays plain text). A
    # dependency candidate must attribute every finding to this exact gate; FINAL inclusion validation
    # against prior accepted findings is Task 3 (parsed here only so the event/accepted set can record
    # it).
    candidate: FindingSet | None = None
    if args.gate.startswith("dep:") or args.gate == "final":
        candidate = _bound_candidate_finding_set(run, args.gate, args.round)
        if args.gate.startswith("dep:"):
            candidate.validate_for_gate(args.gate)

    # Gate progress is decided ONLY from the union of peer objections, never from accepted-finding
    # severity — so a candidate that accepts a blocker still reaches consensus when both peers attest.
    decision = decide_symmetric(peer_a, peer_b, cfg, round_index=args.round, max_rounds=max_rounds)

    # ---- every file/candidate/decision is valid; append atomically ----
    provenance = peer_slot_provenance(cfg)
    objections = [
        {"id": o.id, "severity": o.severity, "location": o.location,
         "claim": o.claim, "suggestion": o.suggestion}
        for o in decision.open_objections
    ]
    sym_fields: dict = {
        "gate": args.gate, "round": args.round, "outcome": decision.outcome,
        "peers": {
            "A": {**provenance["A"], "raw": raw_a, "attestation": peer_a.to_dict()},
            "B": {**provenance["B"], "raw": raw_b, "attestation": peer_b.to_dict()},
        },
        "objections": objections,
        **bindings,
    }
    if candidate is not None:
        sym_fields["candidate"] = candidate.to_dict()
    run.append("symmetric_verdict", **sym_fields)

    # For an ADVANCING dependency/FINAL outcome, persist the accepted finding set AFTER the
    # symmetric_verdict and BEFORE the terminal, so a crash between them leaves incomplete history
    # (never a terminal without its accepted set). CHANGES/CAPPED never accept a set.
    advancing = decision.outcome in ("CONSENSUS", "PROCEED_WITH_FLAGS")
    if candidate is not None and advancing:
        accepted_fields: dict = {"gate": args.gate, "round": args.round,
                                 "payload": candidate.to_dict(), **bindings}
        if decision.outcome == "PROCEED_WITH_FLAGS":
            accepted_fields["accepted_with_flags"] = True
            accepted_fields["open_objections"] = [o.id for o in decision.open_objections]
        run.append("accepted_finding_set", **accepted_fields)

    # The standard terminal event is appended LAST with bindings (CHANGES logs none, staying in loop).
    if decision.outcome == "CONSENSUS":
        run.append("gate_consensus", gate=args.gate, round=args.round, **bindings)
    elif decision.outcome == "PROCEED_WITH_FLAGS":
        run.append("gate_proceeded_with_flags", gate=args.gate, round=args.round,
                   open_findings=[o.id for o in decision.open_objections], **bindings)
    elif decision.outcome == "CAPPED":
        run.append("gate_capped", gate=args.gate, round=args.round,
                   open_findings=[o.id for o in decision.open_objections], **bindings)
    elif decision.outcome != "CHANGES":  # CHANGES intentionally logs no terminal event
        raise SystemExit(f"unexpected decision outcome: {decision.outcome!r}")

    print(decision.outcome)
    return 0


def cmd_bindings(args) -> int:
    """Emit the deterministic content bindings for a gate/round as machine-readable JSON.

    The orchestrator appends this JSON to the Critic seed as trusted CLI metadata; the Critic echoes
    it in its verdict, and `verdict` requires an exact match (the binding handshake). Fields vary by
    gate: ``reproduce`` → artifact; ``plan``/``final`` → artifact + DAG; ``dep:<id>`` → artifact +
    DAG + node. Requires the current schema (a legacy run has no verifiable bindings)."""
    if args.round < 1:
        raise SystemExit("--round must be >= 1 (rounds are 1-based)")
    _validate_gate(args.gate)
    run = RunLog(args.run)
    require_current_schema(run)
    _require_dep_node_in_dag(run, args.gate)
    # Bindings are only meaningful for a legitimately reachable gate (the same stage/phase order the
    # `log`/`verdict` handshake enforces), so validate the stage prerequisites before emitting them.
    _require_gate_stage_ready(run, load_config(run.path / "config.json"), args.gate)
    print(json.dumps(current_bindings(run, args.gate, args.round).to_dict()))
    return 0


def cmd_status(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    print(json.dumps(dag.progress()))
    return 0


def cmd_should_final(args) -> int:
    """Deterministically answer whether the FINAL gate should run for this run.

    Prints ``yes``/``no`` and exits 0/1 so the orchestrator gates Stage 3 on the config
    flag instead of eyeballing it.
    """
    cfg = load_config(RunLog(args.run).path / "config.json")
    print("yes" if cfg.final_review else "no")
    return 0 if cfg.final_review else 1


def cmd_should_approve(args) -> int:
    """Deterministically answer whether to pause for human approval after PLAN consensus.

    Prints ``yes``/``no`` and exits 0/1 so the orchestrator gates the optional approval
    pause on the config flag instead of eyeballing it. Default is ``no`` (no pause).
    """
    cfg = load_config(RunLog(args.run).path / "config.json")
    print("yes" if cfg.human_approval else "no")
    return 0 if cfg.human_approval else 1


def cmd_should_reproduce(args) -> int:
    """Deterministically answer whether to run the REPRODUCE gate (Stage 0) before PLAN.

    Prints ``yes``/``no`` and exits 0/1 so the orchestrator gates bug-fix reproduction on
    the config flag instead of eyeballing it. Default is ``no`` (skip; no behavior change).
    """
    cfg = load_config(RunLog(args.run).path / "config.json")
    print("yes" if cfg.reproduce_gate else "no")
    return 0 if cfg.reproduce_gate else 1


def cmd_approve_plan(args) -> int:
    """Record human approval of the accepted plan (schema-v2 ``plan_approved`` event).

    A mutating command: it requires the current schema, and it is meaningful only when
    ``human_approval`` is configured and the PLAN gate has already reached an accepted terminal whose
    DAG binding still matches the current tree. It records the accepted plan/DAG hashes so downstream
    prerequisites can prove the human approved *this* exact plan, and rejects a duplicate or stale
    approval. Skills call it only after the human explicitly approves.
    """
    run = RunLog(args.run)
    require_current_schema(run)
    cfg = load_config(run.path / "config.json")
    if not cfg.human_approval:
        raise SystemExit(
            "approve-plan: human_approval is disabled for this run; recording an approval would add "
            "meaningless provenance — enable human_approval to gate on approval."
        )
    events = run.read_events()
    terminal = accepted_terminal(events, "plan")
    if terminal is None:
        raise SystemExit(
            "approve-plan: the PLAN gate has not reached consensus (or proceeded with flags) yet; "
            "there is no accepted plan to approve."
        )
    dag = DAG.from_dict(run.load_dag())
    approved_artifact = terminal.get("artifact_sha256")
    approved_dag = terminal.get("dag_sha256")
    # The accepted plan must still bind the current tree — a DAG changed after consensus (only
    # possible by editing dag.json directly, since load-dag freezes a terminal plan) is a stale
    # approval target.
    if approved_dag != dag_sha256(dag):
        raise SystemExit(
            "approve-plan: the accepted plan's DAG binding no longer matches the current dependency "
            "tree; the plan/DAG was changed after consensus — start a fresh run."
        )
    # An accepted plan is immutable within a run, so any prior approval is a duplicate — refuse
    # rather than record redundant (or conflicting) provenance.
    if any(e.get("event") == "plan_approved" for e in events):
        raise SystemExit(
            "approve-plan: the plan is already approved (duplicate approval); an accepted plan is "
            "approved exactly once — nothing to record."
        )
    run.append("plan_approved", gate="plan",
               artifact_sha256=approved_artifact, dag_sha256=approved_dag)
    print(f"approved plan (artifact {str(approved_artifact)[:12]}…, dag {str(approved_dag)[:12]}…)")
    return 0


def cmd_show_plan(args) -> int:
    """Print the exact approved plan + dependency tree to the terminal after PLAN consensus.

    Refuses to run until the plan gate has advanced (consensus/proceed-with-flags), and binds what it
    shows to what was accepted: it verifies the current ``dag_sha256`` still matches the accepted
    plan terminal and selects the pre-terminal Builder artifact whose ``artifact_sha256`` matches the
    terminal binding — never an artifact chosen merely by round. A plan/DAG changed after approval is
    refused, so the operator can only ever see exactly what was reviewed.
    """
    run = RunLog(args.run)
    require_current_schema(run)
    events = run.read_events()
    terminal = _plan_advance_terminal(events)
    if terminal is None:
        raise SystemExit("show-plan: the plan gate has not reached consensus (or proceeded with "
                         "flags) yet; nothing to show")
    dag = DAG.from_dict(run.load_dag())
    if dag_sha256(dag) != terminal.get("dag_sha256"):
        raise SystemExit(
            "show-plan: the current dependency tree no longer matches the accepted plan binding "
            "(dag_sha256); the plan/DAG was changed after approval — start a fresh run."
        )
    _print_approved_plan(_bound_plan_payload(events), dag, sys.stdout)
    return 0


def cmd_clean(args) -> int:
    """Delete a finished run's directory (logs, report, and all scratch).

    Refuses any path that is not a Crucible run dir — it must exist and contain a
    ``runlog.jsonl`` — so a typo/wrong path can never remove an unrelated directory. Also
    refuses a run still in progress (any node not ``done``) unless ``--force`` is given.
    """
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        raise SystemExit(f"crucible: not a run directory: {run_dir}")
    if not (run_dir / "runlog.jsonl").exists():
        raise SystemExit(f"crucible: refusing to delete {run_dir} — no runlog.jsonl (not a run dir)")
    if not args.force:
        try:
            prog = DAG.from_dict(RunLog(args.run).load_dag()).progress()
        except FileNotFoundError:
            prog = None  # no DAG loaded yet — nothing in progress, safe to remove
        if prog and prog["total"] and prog.get("done", 0) < prog["total"]:
            raise SystemExit(f"crucible: refusing to delete {run_dir} — run is in progress "
                             f"({prog.get('done', 0)}/{prog['total']} nodes done); pass --force to override")
    shutil.rmtree(run_dir)
    print(f"removed {run_dir}")
    return 0


def cmd_report(args) -> int:
    run = RunLog(args.run)
    if args.html:
        out = render_html(run)
        target = run.path / "report.html"
    else:
        out = render_markdown(run)
        target = run.path / "report.md"
    target.write_text(out, encoding="utf-8")
    print(out)
    if args.open:
        try:
            webbrowser.open(target.resolve().as_uri())
        except Exception:  # pragma: no cover - opening is best-effort
            pass
    return 0


def cmd_critic_lenses(args) -> int:
    """Print the operator's configured Critic lenses (``critic_checklists``) as one fenced block.

    Reads the resolved run config and emits each lens file's contents (labelled with its size + a
    short sha256) for the orchestrator to append to the Critic seed as additive DATA. Fail-closed:
    a missing / relative / symlink / oversized lens raises ``LensError`` (a ``ValueError``), which
    ``main`` renders as ``crucible: ...`` on stderr with a non-zero exit — halting the dispatch.
    An empty ``critic_checklists`` prints nothing.
    """
    cfg = load_config(RunLog(args.run).path / "config.json")
    sys.stdout.write(read_critic_lenses(cfg.critic_checklists))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crucible", description="Two-model adversarial workflow helper")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-run"); s.add_argument("--goal", required=True)
    s.add_argument("--config"); s.add_argument("--base-dir", default=None,
                   help="run base dir; default $CRUCIBLE_RUNS_DIR or ~/.crucible/runs (keeps runs out of the target repo)")
    s.add_argument("--workflow", choices=VALID_WORKFLOWS, default="build",
                   help="immutable workflow kind recorded on run_start; default build (asymmetric "
                        "Builder/Critic). deep-dive/pr-review select the symmetric two-peer flow.")
    s.set_defaults(func=cmd_init_run)

    s = sub.add_parser("load-dag"); s.add_argument("--run", required=True); s.add_argument("--file", required=True)
    s.add_argument("--force", action="store_true",
                   help="overwrite an existing run's DAG even if it has progress (resets node statuses)")
    s.set_defaults(func=cmd_load_dag)

    s = sub.add_parser("next"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("set-status"); s.add_argument("--run", required=True)
    s.add_argument("--node", required=True); s.add_argument("--status", required=True)
    s.add_argument("--force", action="store_true",
                   help="override the node-gate-consensus requirement when marking done (recovery; logged)")
    s.add_argument("--rationale", default="",
                   help="reason for --force; required with --force and recorded in run-log provenance")
    s.set_defaults(func=cmd_set_status)

    s = sub.add_parser("log"); s.add_argument("--run", required=True); s.add_argument("--event", required=True)
    s.add_argument("--gate", required=True); s.add_argument("--round", type=int, required=True); s.add_argument("--file")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("verdict"); s.add_argument("--run", required=True); s.add_argument("--gate", required=True)
    s.add_argument("--round", type=int, required=True)
    s.add_argument("--max-rounds", type=int, default=None,
                   help="override the round cap; defaults to config max_rounds_plan/max_rounds_dep by gate")
    s.add_argument("--resolutions", help="JSON file of Builder per-finding resolutions (id -> fixed|deferred|wontfix)")
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_verdict)

    s = sub.add_parser("symmetric-verdict")
    s.add_argument("--run", required=True); s.add_argument("--gate", required=True)
    s.add_argument("--round", type=int, required=True)
    s.add_argument("--peer-a", required=True,
                   help="Peer A attestation JSON (echoes the current bindings; verdict/objections)")
    s.add_argument("--peer-b", required=True,
                   help="Peer B attestation JSON (echoes the current bindings; verdict/objections)")
    s.add_argument("--max-rounds", type=int, default=None,
                   help="override the round cap; defaults to config max_rounds_plan/max_rounds_dep by gate")
    s.set_defaults(func=cmd_symmetric_verdict)

    s = sub.add_parser("bindings"); s.add_argument("--run", required=True); s.add_argument("--gate", required=True)
    s.add_argument("--round", type=int, required=True)
    s.set_defaults(func=cmd_bindings)

    s = sub.add_parser("status"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("should-final"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_should_final)

    s = sub.add_parser("should-approve"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_should_approve)

    s = sub.add_parser("should-reproduce"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_should_reproduce)

    s = sub.add_parser("show-plan"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_show_plan)

    s = sub.add_parser("approve-plan"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_approve_plan)

    s = sub.add_parser("clean"); s.add_argument("--run", required=True)
    s.add_argument("--force", action="store_true", help="delete even if the run is still in progress")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("report"); s.add_argument("--run", required=True); s.add_argument("--html", action="store_true")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("critic-lenses"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_critic_lenses)

    return p


def main(argv=None) -> int:
    # Make console output resilient to characters the terminal encoding cannot represent
    # (e.g. a non-ASCII plan title or payload under an ASCII/C locale): escape them instead
    # of aborting with UnicodeEncodeError. No-op under a UTF-8 locale. File provenance
    # (runlog.jsonl, dag.json, report.*) is always written UTF-8 and is unaffected.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    # Surface expected input/IO failures as a clean message instead of a raw traceback.
    # SystemExit (the explicit gate/round/consistency/empty-DAG exits) is a BaseException and
    # passes straight through. TypeError/AttributeError are NOT caught — they indicate a bug
    # (or a malformed input shape that the per-command validators should reject explicitly).
    try:
        return args.func(args)
    except json.JSONDecodeError as e:
        msg = f"invalid JSON: {e}"
    except KeyError as e:
        msg = f"missing required field: {e}"
    except (ValueError, RunLogCorruptError, OSError) as e:
        msg = str(e)
    print(f"crucible: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
