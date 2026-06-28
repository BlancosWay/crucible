"""Thin CLI wrapping config, dag, verdict, runlog, and report modules."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from crucible.config import Config, load_config
from crucible.dag import DAG
from crucible.report import render_html, render_markdown
from crucible.runlog import RunLog, RunLogCorruptError, init_run
from crucible.verdict import VALID_RESOLUTIONS, Verdict, decide

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
        if res is not None and res not in VALID_RESOLUTIONS:
            raise ValueError(f"invalid resolution {res!r} for {fid}; must be one of {VALID_RESOLUTIONS}")
        norm[fid] = res
    return norm, raw


def _max_rounds_for_gate(cfg: Config, gate: str) -> int:
    return cfg.max_rounds_plan if gate == "plan" else cfg.max_rounds_dep


def cmd_init_run(args) -> int:
    cfg = load_config(args.config) if args.config else Config.from_dict({})
    run = init_run(args.goal, cfg, base_dir=args.base_dir)
    print(run.path)
    return 0


def cmd_load_dag(args) -> int:
    run = RunLog(args.run)
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    dag = DAG.from_dict(data)
    if not dag.nodes:
        raise SystemExit("load-dag: dependency tree is empty (0 nodes); a plan must decompose "
                         "into at least one node")
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=len(dag.nodes))
    print(f"loaded {len(dag.nodes)} nodes")
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
    dag.set_status(args.node, args.status)
    run.save_dag(dag.to_dict())
    run.append("node_status_change", node=args.node, status=args.status)
    print(f"{args.node} -> {args.status}")
    return 0


def cmd_log(args) -> int:
    if args.event not in LOG_EVENTS:
        raise ValueError(f"log --event must be one of {LOG_EVENTS}; other events are written "
                         f"by their own commands (verdict, set-status, load-dag)")
    run = RunLog(args.run)
    run.append(args.event, gate=args.gate, round=args.round, payload=_read_payload(args.file))
    print("logged")
    return 0


def cmd_verdict(args) -> int:
    if args.round < 1:
        raise SystemExit("--round must be >= 1 (rounds are 1-based)")
    if args.max_rounds is not None and args.max_rounds < 1:
        raise SystemExit("--max-rounds must be >= 1")
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

    # H1: reject a Critic verdict whose APPROVE/REQUEST_CHANGES label contradicts its
    # findings under the run's blocking_severities, before logging or deciding.
    inconsistency = verdict.consistency_error(cfg)
    if inconsistency:
        raise SystemExit(inconsistency)

    # F1: round cap comes from config (by gate) unless explicitly overridden.
    max_rounds = args.max_rounds if args.max_rounds is not None else _max_rounds_for_gate(cfg, args.gate)

    # F2: optional Builder resolutions feed the deterministic decision.
    resolutions, resolutions_raw = _load_resolutions(args.resolutions)
    if resolutions_raw:
        run.append("builder_resolution", gate=args.gate, round=args.round, payload=resolutions_raw)

    decision = decide(verdict, cfg, round_index=args.round, max_rounds=max_rounds, resolutions=resolutions)

    # F6: persist both the parsed verdict and the Critic's full raw output.
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
    s.add_argument("--config"); s.add_argument("--base-dir", default="runs")
    s.set_defaults(func=cmd_init_run)

    s = sub.add_parser("load-dag"); s.add_argument("--run", required=True); s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_load_dag)

    s = sub.add_parser("next"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("set-status"); s.add_argument("--run", required=True)
    s.add_argument("--node", required=True); s.add_argument("--status", required=True)
    s.set_defaults(func=cmd_set_status)

    s = sub.add_parser("log"); s.add_argument("--run", required=True); s.add_argument("--event", required=True)
    s.add_argument("--gate", default=None); s.add_argument("--round", type=int, default=None); s.add_argument("--file")
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

    s = sub.add_parser("report"); s.add_argument("--run", required=True); s.add_argument("--html", action="store_true")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_report)

    return p


def main(argv=None) -> int:
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
