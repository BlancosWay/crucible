"""Deterministic Markdown report rendered from a run-log."""

from __future__ import annotations

from typing import Any

from crucible.runlog import RunLog


def _san(value: Any) -> str:
    """Flatten untrusted text to a single line and neutralize table/markdown breakers.

    Critic verdicts, goals, and DAG fields are untrusted data; a newline or a stray
    ``|`` could otherwise inject fake headings, rows, or outcome lines into the report.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|")
    return " ".join(text.split()).strip()


def _events_by_gate(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    gates: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        gate = e.get("gate")
        if gate is None:
            continue
        gates.setdefault(gate, []).append(e)
    return gates


def render_markdown(run: RunLog) -> str:
    events = run.read_events()
    start = next((e for e in events if e["event"] == "run_start"), {})
    goal = start.get("goal", "(unknown goal)")
    config = start.get("config", {})

    lines: list[str] = []
    lines.append("# Crucible Run Report")
    lines.append("")
    lines.append(f"**Goal:** {_san(goal)}")
    builder = config.get("builder", {})
    critic = config.get("critic", {})
    lines.append(
        f"**Builder:** {_san(builder.get('model', '?'))} ({_san(builder.get('effort', '?'))}) - "
        f"**Critic:** {_san(critic.get('model', '?'))} ({_san(critic.get('effort', '?'))})"
    )
    lines.append("")

    try:
        dag = run.load_dag()
    except FileNotFoundError:
        dag = {"nodes": []}
    lines.append("## Dependency tree")
    lines.append("")
    lines.append("| Node | Title | Status |")
    lines.append("|------|-------|--------|")
    for n in dag.get("nodes", []):
        lines.append(f"| `{_san(n.get('id', ''))}` | {_san(n.get('title', ''))} | {_san(n.get('status', ''))} |")
    lines.append("")

    lines.append("## Gates")
    lines.append("")
    gates = _events_by_gate(events)
    for gate, gate_events in gates.items():
        lines.append(f"### Gate: `{_san(gate)}`")
        lines.append("")
        consensus = [e for e in gate_events if e["event"] == "gate_consensus"]
        capped = [e for e in gate_events if e["event"] == "gate_capped"]
        for e in gate_events:
            if e["event"] != "critic_verdict":
                continue
            payload = e.get("payload", {})
            rnd = e.get("round", "?")
            lines.append(f"- **Round {_san(rnd)}:** {_san(payload.get('verdict', '?'))} - {_san(payload.get('summary', ''))}")
            for f in payload.get("findings", []):
                lines.append(
                    f"  - `{_san(f.get('id'))}` [{_san(f.get('severity'))}] {_san(f.get('location'))}: "
                    f"{_san(f.get('claim'))} -> {_san(f.get('suggestion'))}"
                )
        if consensus:
            lines.append(f"- **Outcome:** CONSENSUS at round {_san(consensus[-1].get('round', '?'))}")
        elif capped:
            lines.append(f"- **Outcome:** CAPPED at round {_san(capped[-1].get('round', '?'))} (unresolved)")
        lines.append("")

    return "\n".join(lines)


def render_html(run: RunLog) -> str:
    md = render_markdown(run)
    escaped = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Crucible Run Report</title></head><body><pre>"
        f"{escaped}"
        "</pre></body></html>"
    )
