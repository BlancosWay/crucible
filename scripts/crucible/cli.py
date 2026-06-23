"""Thin CLI wrapping config, dag, verdict, runlog, and report modules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from crucible.config import Config, load_config
from crucible.dag import DAG
from crucible.report import render_html, render_markdown
from crucible.runlog import RunLog, init_run
from crucible.verdict import Verdict, decide


def _read_payload(path):
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def cmd_init_run(args) -> int:
    cfg = load_config(args.config) if args.config else Config.from_dict({})
    run = init_run(args.goal, cfg, base_dir=args.base_dir)
    print(run.path)
    return 0


def cmd_load_dag(args) -> int:
    run = RunLog(args.run)
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    dag = DAG.from_dict(data)
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=len(dag.nodes))
    print(f"loaded {len(dag.nodes)} nodes")
    return 0


def cmd_next(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    ready = dag.ready_nodes()
    print(ready[0] if ready else "")
    return 0


def cmd_set_status(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    dag.set_status(args.node, args.status)
    run.save_dag(dag.to_dict())
    run.append("node_status_change", node=args.node, status=args.status)
    print(f"{args.node} -> {args.status}")
    return 0


def cmd_log(args) -> int:
    run = RunLog(args.run)
    run.append(args.event, gate=args.gate, round=args.round, payload=_read_payload(args.file))
    print("logged")
    return 0


def cmd_verdict(args) -> int:
    run = RunLog(args.run)
    cfg = load_config(run.path / "config.json")
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    verdict = Verdict.from_dict(data)
    decision = decide(verdict, cfg, round_index=args.round, max_rounds=args.max_rounds)
    run.append("critic_verdict", gate=args.gate, round=args.round, payload=data)
    if decision.outcome == "CONSENSUS":
        run.append("gate_consensus", gate=args.gate, round=args.round)
    elif decision.outcome == "CAPPED":
        run.append("gate_capped", gate=args.gate, round=args.round,
                   open_findings=[f.id for f in decision.open_findings])
    print(decision.outcome)
    return 0


def cmd_status(args) -> int:
    run = RunLog(args.run)
    dag = DAG.from_dict(run.load_dag())
    print(json.dumps(dag.progress()))
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
    s.add_argument("--round", type=int, required=True); s.add_argument("--max-rounds", type=int, required=True)
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_verdict)

    s = sub.add_parser("status"); s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("report"); s.add_argument("--run", required=True); s.add_argument("--html", action="store_true")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_report)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
