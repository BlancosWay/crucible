"""Deterministic Markdown report rendered from a run-log."""

from __future__ import annotations

import json
import re
from typing import Any

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import RUN_SCHEMA_VERSION, run_schema_version
from crucible.runlog import RunLog
from crucible.workflow import WorkflowIssue, workflow_issues


# A fenced code block delimiter as emitted by ``_fenced`` (a column-0 run of >= 3 backticks).
_FENCE_LINE = re.compile(r"^`{3,}$")


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _san(value: Any) -> str:
    """Flatten untrusted text to a single line and neutralize Markdown table/heading AND
    HTML breakers.

    Critic verdicts, goals, and DAG fields are untrusted data: a newline or a stray ``|``
    could inject a fake heading/row/outcome line, and raw HTML (``<script>``,
    ``<img onerror=…>``) would execute when ``report.md`` is opened in an HTML-permitting
    Markdown renderer. So escape ``&``/``<``/``>`` (``&`` first) in addition to ``|`` and
    backticks. The fenced raw provenance is handled separately — kept verbatim in Markdown
    (code-fence content renders literally) and escaped by ``render_html``.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("`", "\\`")
    return " ".join(text.split()).strip()


def _payload_text(payload: Any) -> str:
    """Full-fidelity text for a raw provenance payload (Builder output / Critic raw)."""
    if isinstance(payload, str):
        return payload
    if payload is None:
        return ""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _fenced(text: str) -> list[str]:
    """Render ``text`` as a Markdown code block whose fence is longer than any backtick
    run inside it, so untrusted content can never close the fence early. Returned as
    column-0 lines (the fence must not be indented under a list item)."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return [fence, *text.splitlines(), fence]


def _provenance_block(label: str, payload: Any) -> list[str]:
    """A labelled raw-text provenance block (Builder/Critic output): a fenced code block,
    or an ``_(empty)_`` placeholder when there is nothing to show (avoids an empty fence)."""
    text = _payload_text(payload)
    body = _fenced(text) if text else ["_(empty)_"]
    return [label, "", *body, ""]


def _events_by_gate(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    gates: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        gate = e.get("gate")
        if gate is None:
            continue
        gates.setdefault(gate, []).append(e)
    return gates


# The terminal events that conclude a gate, in log order of severity irrelevance — the LAST one
# logged for a gate is authoritative (a gate concludes exactly once; see render_markdown).
_TERMINAL_EVENTS = ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")


def _config_aware_issues(
    events: list[dict[str, Any]], dag: dict[str, Any], config: dict[str, Any]
) -> list[WorkflowIssue]:
    """Parse the current DAG + resolved run config and delegate to the shared workflow validator
    (``crucible.workflow.workflow_issues``), the single owner of the stage/phase contract.

    Returns ``[]`` if the tree or config cannot be parsed: a malformed artifact is not *certified*
    here (the event-based status still applies) and the report never raises. The validator itself
    is pure and total over well-formed inputs.
    """
    try:
        dag_obj = DAG.from_dict(dag)
        cfg_obj = Config.from_dict(config)
    except (ValueError, KeyError, TypeError):
        return []
    return workflow_issues(events, dag_obj, cfg_obj)


def _run_summary_lines(
    events: list[dict[str, Any]],
    dag: dict[str, Any],
    config: dict[str, Any] | None = None,
    schema_version: int | None = None,
    dag_present: bool = True,
) -> list[str]:
    """A deterministic run-level ``## Summary`` banner.

    For a schema-v2 run the overall status is derived from the run's RESOLVED CONFIGURATION and the
    CURRENT artifact bindings (via ``crucible.workflow.workflow_issues``) — not merely from which
    events happened — with this precedence (most severe first):

        LEGACY / UNVERIFIED > INVALID > BLOCKED > FLAGGED > CLEAN > IN PROGRESS

    - ``LEGACY / UNVERIFIED`` — the run predates schema v2; its provenance cannot be certified (so
      it is never ``CLEAN``, even when every gate reached consensus);
    - ``INVALID`` — a recorded binding no longer matches the current artifact/tree, an approval is
      stale, or a configured phase is out of order (an integrity violation in the log);
    - ``BLOCKED`` — a gate capped with unresolved findings;
    - ``FLAGGED`` — a gate proceeded with flags, or a node completed outside an accepted review
      gate (a forced override or none at all);
    - ``CLEAN`` — every configured phase is present, ordered, accepted, and currently bound, and
      every node is ``done``;
    - ``IN PROGRESS`` — a required configured phase or node is still incomplete.

    The banner is still derived purely from the append-only run log plus the current DAG/config; it
    does not claim tamper resistance. Every interpolated value is a literal label, an int count, or
    a ``_san``-sanitized id/message, so it adds no injection surface even though issue messages name
    untrusted DAG node ids.
    """
    gates = _events_by_gate(events)
    # Each gate's authoritative outcome is its LAST terminal event (or None => undecided).
    consensus = flagged = capped = undecided = 0
    findings: list[tuple[str, list[str]]] = []  # (gate_id, open finding ids) for flagged/capped
    for gate, gate_events in gates.items():
        terminal = [e for e in gate_events if e["event"] in _TERMINAL_EVENTS]
        if not terminal:
            undecided += 1
            continue
        last = terminal[-1]
        if last["event"] == "gate_consensus":
            consensus += 1
        elif last["event"] == "gate_proceeded_with_flags":
            flagged += 1
            findings.append((gate, list(last.get("open_findings", []))))
        else:  # gate_capped
            capped += 1
            findings.append((gate, list(last.get("open_findings", []))))

    total = consensus + flagged + capped + undecided
    nodes = dag.get("nodes", [])
    dag_done = bool(nodes) and all(n.get("status") == "done" for n in nodes)

    # Schema/config-aware validation (schema-v2 only, and only when a current DAG + resolved config
    # are available to validate against). Legacy runs skip this entirely and are never CLEAN. A
    # `done` node with no current-bound review, and every out-of-order/stale binding, surfaces here.
    schema_current = schema_version is not None and schema_version >= RUN_SCHEMA_VERSION
    issues: list[WorkflowIssue] = []
    if schema_current and dag_present and config is not None:
        issues = _config_aware_issues(events, dag, config)
    invalid = [i for i in issues if i.kind == "invalid"]
    missing = [i for i in issues if i.kind == "missing"]
    flagged_issues = [i for i in issues if i.kind == "flagged"]

    if not schema_current:
        status = (f"LEGACY / UNVERIFIED — this run predates schema v{RUN_SCHEMA_VERSION}; its "
                  f"provenance is unverified and can never be certified")
    elif invalid:
        status = (f"INVALID — {len(invalid)} workflow integrity violation(s) recorded in the run "
                  f"log")
    elif capped:
        status = f"BLOCKED — {capped} of {total} gate(s) capped with unresolved findings"
    elif flagged:
        status = f"FLAGGED — {flagged} of {total} gate(s) proceeded with unresolved findings"
    elif flagged_issues:
        status = (f"FLAGGED — {len(flagged_issues)} node(s) completed outside an accepted review "
                  f"gate")
    elif not missing and total and consensus == total and dag_done:
        status = f"CLEAN — all {total} gate(s) reached consensus"
    else:
        status = "IN PROGRESS — the run has not settled every configured phase"

    counts = f"{total} total \u00b7 {consensus} consensus \u00b7 {flagged} flagged \u00b7 {capped} capped"
    if undecided:
        counts += f" \u00b7 {undecided} undecided"

    lines = ["## Summary", "", f"**Status:** {status}", "", f"**Gates:** {counts}"]

    # List every configured-workflow problem explicitly (design: "the summary lists missing/invalid
    # required phases"). Messages name untrusted DAG node ids, so each is `_san`-sanitized (flattens
    # newlines, escapes &<>| and backticks) — same non-injection convention as the findings block.
    if issues:
        lines.append(f"**Workflow issues:** {len(issues)}")
        lines.extend(f"- [{issue.kind}] {_san(issue.message)}" for issue in issues)

    total_open = sum(len(ids) for _, ids in findings)
    if total_open:
        # Untrusted gate/finding ids are rendered as plain _san text — never wrapped in a
        # backtick code span. _san escapes &<>| and backticks and flattens newlines, which fully
        # neutralizes ids in running text; wrapping them in backticks would instead create an
        # inline code span that a backtick in the id could break out of (Markdown code spans do
        # not honor backslash escapes).
        breakdown = "; ".join(
            f"{_san(g)}: {', '.join(_san(i) for i in ids)}"
            for g, ids in findings if ids
        )
        line = f"**Unresolved blocking findings:** {total_open}"
        if breakdown:
            line += f" ({breakdown})"
        lines.append(line)

        # By-severity breakdown of the same unresolved findings, derived from each gate's LAST
        # critic_verdict payload (id -> severity) — deterministic, from the run-log only, never
        # Critic prose. Open ids are blocking-severity by construction; an id whose severity can't
        # be found is counted defensively under "unknown". Values are literal severity labels
        # (from the fixed order) or _san-sanitized, plus int counts, so no injection surface.
        sev_by_gate_id: dict[tuple[str, Any], Any] = {}
        for gate, gate_events in gates.items():
            critic_verdicts = [e for e in gate_events if e["event"] == "critic_verdict"]
            if not critic_verdicts:
                continue
            payload = critic_verdicts[-1].get("payload") or {}
            for f in payload.get("findings", []):
                if isinstance(f, dict):
                    sev_by_gate_id[(gate, f.get("id"))] = f.get("severity")
        sev_counts: dict[str, int] = {}
        for gate, ids in findings:
            for fid in ids:
                sev = sev_by_gate_id.get((gate, fid)) or "unknown"
                sev_counts[sev] = sev_counts.get(sev, 0) + 1
        order = ["blocker", "major", "minor", "nit", "unknown"]
        parts = [f"{sev_counts[s]} {s}" for s in order if sev_counts.get(s)]
        # any unexpected severity label (defensive) rendered sanitized, in deterministic order
        parts += [f"{sev_counts[s]} {_san(s)}" for s in sorted(sev_counts) if s not in order]
        if parts:
            # Join outside the f-string expression: a backslash (here the \u00b7
            # escape) inside an f-string '{...}' is a SyntaxError before Python 3.12.
            detail = " \u00b7 ".join(parts)
            lines.append(f"**Unresolved by severity:** {detail}")

    # Audited overrides — every point where the adversarial loop was bypassed carries a recorded
    # reason: a `set-status --force` node advance, or a `wontfix`/`deferred` rebuttal that cleared a
    # finding. Surface them here (derived purely from the run log, `_san`-sanitized, no backticks —
    # same non-injection convention as the findings block above) so a low-effort rationale is visible.
    overrides: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for e in events:
        ev = e.get("event")
        if ev == "node_status_change" and e.get("forced"):
            reason = e.get("rationale")
            if isinstance(reason, str) and reason.strip():
                key = ("force", str(e.get("node")), "", reason)
                if key not in seen:
                    seen.add(key)
                    overrides.append(f"forced node {_san(e.get('node'))} done \u2014 {_san(reason)}")
        elif ev == "builder_resolution":
            payload = e.get("payload")
            if isinstance(payload, dict):
                gate = e.get("gate")
                for fid, info in payload.items():
                    if not isinstance(info, dict):
                        continue  # bare-string payloads carry no rationale — nothing to audit
                    res = info.get("resolution")
                    reason = info.get("rationale")
                    if res in ("wontfix", "deferred") and isinstance(reason, str) and reason.strip():
                        key = (str(gate), str(fid), str(res), reason)
                        if key not in seen:
                            seen.add(key)
                            overrides.append(
                                f"{_san(res)} {_san(fid)} ({_san(gate)}) \u2014 {_san(reason)}"
                            )
    if overrides:
        lines.append(f"**Overrides:** {len(overrides)} (" + "; ".join(overrides) + ")")
    lines.append("")
    return lines



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

    dag_present = True
    try:
        dag = run.load_dag()
    except FileNotFoundError:
        dag = {"nodes": []}
        dag_present = False

    lines.extend(_run_summary_lines(
        events, dag, config=config, schema_version=run_schema_version(events),
        dag_present=dag_present,
    ))

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
        for e in gate_events:
            ev = e.get("event")
            rnd = _san(e.get("round", "?"))
            if ev == "builder_output":
                lines.extend(_provenance_block(f"**Builder output (round {rnd}):**", e.get("payload")))
            elif ev == "critic_output":
                lines.extend(_provenance_block(f"**Critic output (round {rnd}):**", e.get("payload")))
            elif ev == "critic_verdict":
                payload = e.get("payload", {})
                lines.append(f"- **Round {rnd}:** {_san(payload.get('verdict', '?'))} - {_san(payload.get('summary', ''))}")
                for f in payload.get("findings", []):
                    lines.append(
                        f"  - `{_san(f.get('id'))}` [{_san(f.get('severity'))}] {_san(f.get('location'))}: "
                        f"{_san(f.get('claim'))} -> {_san(f.get('suggestion'))}"
                    )
                raw = e.get("raw")
                if raw:
                    lines.append("")
                    lines.append(f"**Critic verdict raw (round {rnd}):**")
                    lines.append("")
                    lines.extend(_fenced(_payload_text(raw)))
                    lines.append("")
            elif ev == "builder_resolution":
                payload = e.get("payload") or {}
                if isinstance(payload, dict) and payload:
                    lines.append(f"- **Builder resolutions (round {rnd}):**")
                    for fid, info in payload.items():
                        if isinstance(info, dict):
                            res, rationale = info.get("resolution", "?"), info.get("rationale", "")
                        else:
                            res, rationale = info, ""
                        tail = f" — {_san(rationale)}" if rationale else ""
                        lines.append(f"  - `{_san(fid)}` -> {_san(res)}{tail}")
        # A gate ends in exactly one terminal event; if several were ever logged, the LAST in
        # log order is authoritative. Each interpolated value is sanitized individually.
        terminal = [e for e in gate_events
                    if e["event"] in _TERMINAL_EVENTS]
        if terminal:
            last = terminal[-1]
            rnd = _san(last.get("round", "?"))
            if last["event"] == "gate_consensus":
                lines.append(f"- **Outcome:** CONSENSUS at round {rnd}")
            elif last["event"] == "gate_proceeded_with_flags":
                flags = last.get("open_findings", [])
                ids = ", ".join(_san(i) for i in flags)
                carried = f": {ids}" if ids else ""
                lines.append(f"- **Outcome:** PROCEEDED WITH FLAGS at round {rnd} — "
                             f"{len(flags)} unresolved finding(s) carried{carried}")
            else:  # gate_capped
                lines.append(f"- **Outcome:** CAPPED at round {rnd} (unresolved)")
        lines.append("")

    return "\n".join(lines)


def render_html(run: RunLog) -> str:
    # Inline fields are already HTML-escaped by `_san`; only the raw provenance inside code
    # fences remains unescaped. Escape exactly those fence bodies — escaping the whole
    # document would double-escape the inline fields (`&lt;` -> `&amp;lt;`). Fence delimiter
    # lines are pure backticks (no `&<>`), so they pass through unchanged.
    md = render_markdown(run)
    out: list[str] = []
    fence: str | None = None
    for line in md.split("\n"):
        if fence is None and _FENCE_LINE.match(line):
            fence = line
            out.append(line)
        elif fence is not None and line == fence:
            fence = None
            out.append(line)
        elif fence is not None:
            out.append(_html_escape(line))
        else:
            out.append(line)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Crucible Run Report</title></head><body><pre>"
        f"{chr(10).join(out)}"
        "</pre></body></html>"
    )
