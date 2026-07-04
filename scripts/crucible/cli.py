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
from crucible.report import render_html, render_markdown
from crucible.runlog import RunLog, RunLogCorruptError, init_run
from crucible.verdict import VALID_RESOLUTIONS, Finding, Verdict, decide

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


def _print_approved_plan(run, dag, stream=None) -> None:
    """Render the approved plan artifact + a given (already-loaded) dependency tree to
    ``stream``. Shared by `show-plan` (stdout) and the automatic echo when the PLAN gate
    settles (stderr). The caller loads the DAG, so each caller owns its strict-vs-tolerant
    policy; this renderer never loads the DAG and never prints a masking placeholder."""
    stream = sys.stdout if stream is None else stream
    plans = [e for e in run.read_events()
             if e.get("gate") == "plan" and e.get("event") == "builder_output"]
    print("=== Approved plan ===", file=stream)
    print(plans[-1].get("payload", "(no plan artifact logged)") if plans else "(no plan artifact logged)",
          file=stream)
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
        dag = run.load_dag()
    except FileNotFoundError:
        raise ValueError(f"gate {gate!r} targets a dependency node, but this run has no "
                         f"dependency tree yet; run load-dag first")
    known = {n.get("id") for n in dag.get("nodes", [])}
    if node_id not in known:
        raise ValueError(f"gate {gate!r} targets unknown node {node_id!r}; known nodes: "
                         f"{sorted(i for i in known if isinstance(i, str))}")


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


def cmd_init_run(args) -> int:
    cfg = load_config(args.config) if args.config else Config.from_dict({})
    base = args.base_dir or os.environ.get("CRUCIBLE_RUNS_DIR") or str(Path.home() / ".crucible" / "runs")
    run = init_run(args.goal, cfg, base_dir=base)
    print(run.path)
    return 0


def cmd_load_dag(args) -> int:
    run = RunLog(args.run)
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
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=len(dag.nodes))
    print(f"loaded {len(dag.nodes)} nodes")
    print()
    _print_dependency_tree(dag)
    return 0


def cmd_next(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    state, node = dag.next_state()
    if state == "ready":
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
    dag = DAG.from_dict(run.load_dag())
    if args.node not in dag.nodes:
        raise ValueError(f"unknown node: {args.node}")
    # C2: a node cannot begin or finish work (in_progress/in_review/done) while a dependency
    # is unfinished — that would let `next` schedule its dependents and skip the dependency's
    # work. `pending`/`blocked` are not work statuses and stay settable for recovery.
    if args.status in ("in_progress", "in_review", "done"):
        unmet = sorted(d for d in dag.deps[args.node] if dag.nodes[d].status != "done")
        if unmet:
            raise SystemExit(f"set-status: cannot set {args.node!r} to {args.status!r} while these "
                             f"dependencies are not done: {unmet}")
    # H2: a node may only be marked `done` once its OWN review gate (dep:<node>) reached a terminal
    # ADVANCE outcome — gate_consensus or gate_proceeded_with_flags. A gate_capped (halt) does NOT
    # qualify. Use LAST-terminal-event semantics (matching report.py) so a runlog where an earlier
    # consensus is followed by a later gate_capped is correctly treated as capped. Without this,
    # `next` would schedule dependents of a node whose review the CLI decided must halt (or that
    # was never reviewed), advancing past a gate without consensus. `--force` is the explicit,
    # logged human recovery override.
    if args.status == "done" and not args.force:
        node_gate = f"dep:{args.node}"
        terminal = [e.get("event") for e in run.read_events()
                    if e.get("gate") == node_gate
                    and e.get("event") in ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")]
        accepted = bool(terminal) and terminal[-1] in ("gate_consensus", "gate_proceeded_with_flags")
        if not accepted:
            raise SystemExit(
                f"set-status: refusing to mark {args.node!r} done — its review gate {node_gate!r} "
                f"has not reached consensus (or proceeded with flags). Run the gate to a terminal "
                f"advance first, or pass --force to override (recorded)."
            )
    dag.set_status(args.node, args.status)
    run.save_dag(dag.to_dict())
    run.append("node_status_change", node=args.node, status=args.status, forced=bool(args.force))
    print(f"{args.node} -> {args.status}")
    return 0


def cmd_log(args) -> int:
    if args.event not in LOG_EVENTS:
        raise ValueError(f"log --event must be one of {LOG_EVENTS}; other events are written "
                         f"by their own commands (verdict, set-status, load-dag)")
    _validate_gate(args.gate)
    run = RunLog(args.run)
    _require_dep_node_in_dag(run, args.gate)
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


def cmd_verdict(args) -> int:
    if args.round < 1:
        raise SystemExit("--round must be >= 1 (rounds are 1-based)")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds must be >= 1")
    _validate_gate(args.gate)
    run = RunLog(args.run)
    cfg = load_config(run.path / "config.json")
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
    if resolutions_raw:
        run.append("builder_resolution", gate=args.gate, round=args.round, payload=resolutions_raw)

    decision = decide(verdict, cfg, round_index=args.round, max_rounds=max_rounds,
                      resolutions=resolutions, always_halt=(args.gate == "reproduce"))

    # F6: persist both the parsed verdict and the Critic's full raw output. These are not
    # redundant: the report renders the parsed payload as a readable digest (verdict + summary
    # + per-finding bullets) and the `raw` text as a full-fidelity provenance block (exact
    # bytes/formatting/extra keys). Keeping both is intentional (digest for humans, raw for audit).
    run.append("critic_verdict", gate=args.gate, round=args.round, payload=data, raw=raw_text)
    if decision.outcome == "CONSENSUS":
        run.append("gate_consensus", gate=args.gate, round=args.round)
    elif decision.outcome == "PROCEED_WITH_FLAGS":
        run.append("gate_proceeded_with_flags", gate=args.gate, round=args.round,
                   open_findings=[f.id for f in decision.open_findings])
    elif decision.outcome == "CAPPED":
        run.append("gate_capped", gate=args.gate, round=args.round,
                   open_findings=[f.id for f in decision.open_findings])
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
        except FileNotFoundError:
            settled_dag = None
        if settled_dag is not None:
            _print_approved_plan(run, settled_dag, sys.stderr)
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


def cmd_show_plan(args) -> int:
    """Print the approved plan + dependency tree to the terminal after PLAN consensus.

    Refuses to run until the plan gate has concluded, so the operator always sees exactly
    what was approved before any implementation begins. Reads from the run-log (final plan
    artifact) and the loaded DAG.
    """
    run = RunLog(args.run)
    concluded = [e for e in run.read_events()
                 if e.get("gate") == "plan" and e.get("event") in _TERMINAL_EVENTS]
    if not concluded:
        raise SystemExit("show-plan: the plan gate has not reached consensus yet; nothing to show")
    dag = DAG.from_dict(run.load_dag())
    _print_approved_plan(run, dag, sys.stdout)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crucible", description="Two-model adversarial workflow helper")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-run"); s.add_argument("--goal", required=True)
    s.add_argument("--config"); s.add_argument("--base-dir", default=None,
                   help="run base dir; default $CRUCIBLE_RUNS_DIR or ~/.crucible/runs (keeps runs out of the target repo)")
    s.set_defaults(func=cmd_init_run)

    s = sub.add_parser("load-dag"); s.add_argument("--run", required=True); s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_load_dag)

    s = sub.add_parser("next"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("set-status"); s.add_argument("--run", required=True)
    s.add_argument("--node", required=True); s.add_argument("--status", required=True)
    s.add_argument("--force", action="store_true",
                   help="override the node-gate-consensus requirement when marking done (recovery; logged)")
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

    s = sub.add_parser("clean"); s.add_argument("--run", required=True)
    s.add_argument("--force", action="store_true", help="delete even if the run is still in progress")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("report"); s.add_argument("--run", required=True); s.add_argument("--html", action="store_true")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_report)

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
