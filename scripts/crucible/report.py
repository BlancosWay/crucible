"""Deterministic Markdown report rendered from a run-log."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from crucible.config import Config, resolved_config_shape_error
from crucible.dag import DAG
from crucible.integrity import RUN_SCHEMA_VERSION, run_schema_version
from crucible.runlog import RunLog
from crucible.symmetric import (
    PEER_SLOT_ROLES,
    SYMMETRIC_WORKFLOWS,
    CorruptWorkflowError,
    accepted_findings,
    require_complete_symmetric_run,
    review_result,
    workflow_kind,
)
from crucible.target import (
    target_event_issues,
    target_from_events,
    target_sha256,
    validate_source_materialization,
)
from crucible.workflow import WorkflowIssue, workflow_issues


# A fenced code block delimiter as emitted by ``_fenced`` (a column-0 run of >= 3 backticks).
_FENCE_LINE = re.compile(r"^`{3,}$")


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _san(value: Any) -> str:
    """Flatten untrusted text to a single line and neutralize Markdown table/heading, Markdown
    image/link, AND HTML breakers.

    Critic verdicts, goals, DAG fields, and PR-derived text are untrusted data: a newline or a stray
    ``|`` could inject a fake heading/row/outcome line; raw HTML (``<script>``, ``<img onerror=…>``)
    would execute when ``report.md`` is opened in an HTML-permitting Markdown renderer; and Markdown
    image/link syntax (``![alt](url)`` / ``[text](url)``) would render an **active** ``<img>`` request
    (a tracking pixel / content injection) with no HTML at all. So escape ``&``/``<``/``>`` (``&``
    first), ``|`` and backticks, AND the link/image brackets ``[``/``]`` (escaping the brackets breaks
    both ``![…](…)`` and ``[…](…)`` — the ``!`` alone is inert once the brackets are escaped). The
    fenced raw provenance is handled separately — kept verbatim in Markdown (code-fence content renders
    literally) and escaped by ``render_html``.

    Its output is safe in Markdown **running text**; it is NOT placed inside a backtick code span,
    where a backtick in the value would break out (code spans ignore backslash escapes) — untrusted
    ids are rendered as plain ``_san`` text, never ``` `…` ```.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("`", "\\`")
    text = text.replace("[", "\\[").replace("]", "\\]")
    return " ".join(text.split()).strip()


def _gate_policy_suffix(terminal_event: dict[str, Any]) -> str:
    """The effective per-gate policy recorded on a terminal event (F5) — the round cap actually used
    (a ``--max-rounds`` override or the config default) and ``on_cap`` that produced the decision —
    rendered as ``` (round cap N, on_cap X)```. A legacy terminal event that never recorded the policy
    yields ``''`` (no suffix), so the report stays readable for pre-F5 runs. Surfacing this lets the
    report reconstruct a ``--max-rounds``-driven decision that the run config alone cannot show."""
    mr = terminal_event.get("max_rounds")
    oc = terminal_event.get("on_cap")
    parts = []
    if mr is not None:
        parts.append(f"round cap {_san(mr)}")
    if oc is not None:
        parts.append(f"on_cap {_san(oc)}")
    return f" ({', '.join(parts)})" if parts else ""


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

    A schema-v2 run is only certifiable against a well-formed current dependency tree and a COMPLETE
    resolved config. If either artifact is PRESENT but cannot be used, this fails *closed*: it returns
    an ``invalid`` :class:`WorkflowIssue` naming the malformed artifact rather than silently returning
    ``[]``. That is deliberate — a malformed current tree still carries raw ``status: "done"`` nodes,
    so returning ``[]`` would let the event-based ``dag_done`` check certify the run ``CLEAN`` even
    though nothing about it can be recomputed or bound. The caller only invokes this for a *present*
    schema-v2 DAG (an ABSENT tree stays ``IN PROGRESS`` and never reaches here), so a parse failure
    here means "present but malformed", never "absent".

    The recorded ``config`` is validated against its COMPLETE resolved shape (exactly the
    ``Config.to_dict`` keys and nested role keys) BEFORE it is parsed — never with ``Config.from_dict``
    override semantics, which would silently fill an absent/partial config from defaults and certify a
    run whose configured phases are unknown. The validator itself is pure and total over well-formed
    inputs, and this never raises.
    """
    try:
        dag_obj = DAG.from_dict(dag)
    except (ValueError, KeyError, TypeError):
        return [WorkflowIssue(
            "invalid",
            "the current dependency tree (dag.json) is malformed and cannot be parsed, bound, or "
            "certified")]
    shape_error = resolved_config_shape_error(config)
    if shape_error is not None:
        return [WorkflowIssue(
            "invalid",
            f"the recorded run configuration {shape_error} and cannot be validated or certified")]
    try:
        cfg_obj = Config.from_dict(config)
    except (ValueError, KeyError, TypeError):
        return [WorkflowIssue(
            "invalid",
            "the recorded run configuration is malformed and cannot be parsed or validated for "
            "certification")]
    return workflow_issues(events, dag_obj, cfg_obj)


def _gate_tally(
    gates: dict[str, list[dict[str, Any]]]
) -> tuple[int, int, int, int, list[tuple[str, list[str]]]]:
    """Each gate's authoritative outcome is its LAST terminal event (or None => undecided).

    Returns ``(consensus, flagged, capped, undecided, findings)`` where ``findings`` is the list of
    ``(gate_id, open finding ids)`` for the flagged/capped gates. Shared by the run summary and the
    symmetric result section so both read gate outcomes identically.
    """
    consensus = flagged = capped = undecided = 0
    findings: list[tuple[str, list[str]]] = []
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
    return consensus, flagged, capped, undecided, findings


def _schema_v2_issues(
    events: list[dict[str, Any]],
    dag: dict[str, Any],
    config: dict[str, Any] | None,
    schema_version: int | None,
    dag_present: bool,
    dag_load_error: bool,
) -> list[WorkflowIssue]:
    """The schema-v2 configured-workflow issues, or ``[]`` for a legacy/absent-tree run.

    The single owner of the schema/config-aware validation the report consumes. A PRESENT current
    tree that cannot be loaded as a JSON object fails closed to an ``invalid`` issue; a present,
    loadable tree is validated by :func:`_config_aware_issues` (which itself fails closed on a
    malformed tree/config). A legacy run yields ``[]`` (never certifiable). An ABSENT tree yields only
    the fail-closed target-identity and source-materialization integrity issues — a
    duplicate/malformed/late ``target_loaded``, pr-review protocol work with no target, or a
    forged/duplicate/target-less ``source_materialized`` is INVALID even before a tree is loaded — and
    otherwise ``[]`` (still in progress). Shared by the run summary and the symmetric result section so
    both agree on INVALID/MISSING integrity.
    """
    schema_current = schema_version is not None and schema_version >= RUN_SCHEMA_VERSION
    if not schema_current:
        return []
    # Corrupt run metadata fails a schema-v2 run closed regardless of tree state: a present
    # null/non-string/unrecognized workflow value can only come from tampering. Detected here (via the
    # single `workflow_kind` read path) BEFORE the tree checks, so it renders INVALID even when no
    # tree is loaded and it never routes a corrupt run down the config-aware validator.
    try:
        workflow = workflow_kind(events)
    except CorruptWorkflowError as exc:
        return [WorkflowIssue(
            "invalid", f"the run's recorded workflow metadata is corrupt: {exc}")]
    if dag_load_error:
        return [WorkflowIssue(
            "invalid",
            "the current dependency tree (dag.json) is present but is not a well-formed JSON "
            "object and cannot be parsed, bound, or certified")]
    if dag_present:
        return _config_aware_issues(events, dag, config)
    # An ABSENT tree normally stays IN PROGRESS (nothing to certify yet). But target IDENTITY and the
    # source snapshot are BOTH authoritative over the event history alone — not the dependency tree —
    # so they are validated NOW, tree or no tree, and each renders the run INVALID rather than being
    # masked as merely in progress:
    #   - target-EVENT integrity (`target_event_issues`): a duplicate/malformed/late `target_loaded`,
    #     or pr-review DAG/PLAN/review work recorded with no target (an init-only pr-review run that
    #     has simply not loaded its target yet is *missing*, not invalid, and returns []);
    #   - source-MATERIALIZATION integrity (`validate_source_materialization`, F2/F3): a
    #     forged/duplicate/target-less/wrong-kind/-hash/-order `source_materialized` event.
    # This reuses the SAME central validators the present-tree path applies via
    # `workflow_issues`/`_target_issues`, so both paths agree, and the source issue is listed exactly
    # once (target-event issues name `target_loaded`; source issues name `source_materialized`).
    # Tree-dependent phases (binding, DAG/PLAN/FINAL ordering) still wait for the tree.
    issues = [WorkflowIssue("invalid", msg) for msg in target_event_issues(events, workflow)]
    issues.extend(WorkflowIssue("invalid", msg)
                  for msg in validate_source_materialization(events, workflow).issues)
    return issues


def _overall_status(
    schema_current: bool,
    issues: list[WorkflowIssue],
    tally: tuple[int, int, int, int],
    dag_done: bool,
) -> str:
    """The run's overall status label + rationale, by the precedence (most severe first):

        LEGACY / UNVERIFIED > INVALID > BLOCKED > FLAGGED > CLEAN > IN PROGRESS

    Derived purely from the schema flag, the workflow issues, the gate tally, and whether every node
    is ``done``. Owned in one place so the summary banner and the symmetric result section never
    disagree about whether a run is CLEAN (complete and valid).
    """
    consensus, flagged, capped, undecided = tally
    total = consensus + flagged + capped + undecided
    invalid = [i for i in issues if i.kind == "invalid"]
    missing = [i for i in issues if i.kind == "missing"]
    flagged_issues = [i for i in issues if i.kind == "flagged"]
    if not schema_current:
        return (f"LEGACY / UNVERIFIED — this run predates schema v{RUN_SCHEMA_VERSION}; its "
                f"provenance is unverified and can never be certified")
    if invalid:
        return (f"INVALID — {len(invalid)} workflow integrity violation(s) recorded in the run "
                f"log")
    if capped:
        return f"BLOCKED — {capped} of {total} gate(s) capped with unresolved findings"
    if flagged:
        return f"FLAGGED — {flagged} of {total} gate(s) proceeded with unresolved findings"
    if flagged_issues:
        return (f"FLAGGED — {len(flagged_issues)} node(s) completed outside an accepted review "
                f"gate")
    if not missing and total and consensus == total and dag_done:
        return f"CLEAN — all {total} gate(s) reached consensus"
    return "IN PROGRESS — the run has not settled every configured phase"


def _run_summary_lines(
    events: list[dict[str, Any]],
    dag: dict[str, Any],
    config: dict[str, Any] | None = None,
    schema_version: int | None = None,
    dag_present: bool = True,
    dag_load_error: bool = False,
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
    consensus, flagged, capped, undecided, findings = _gate_tally(gates)

    total = consensus + flagged + capped + undecided
    nodes = dag.get("nodes", [])
    dag_done = bool(nodes) and all(n.get("status") == "done" for n in nodes)

    # Schema/config-aware validation (schema-v2 only). Legacy runs skip this entirely and are never
    # CLEAN. For a schema-v2 run a `done` node with no current-bound review, and every
    # out-of-order/stale binding, surfaces here. A PRESENT current tree that cannot even be loaded as
    # a JSON object (`dag_load_error`) fails closed to an `invalid` issue directly; a present tree
    # that loads but is structurally malformed (or a partial/absent resolved config) fails closed
    # inside `_config_aware_issues`. Either way the raw `dag_done` CLEAN check below can never certify
    # an uncertifiable run. An ABSENT tree is not routed here at all and stays IN PROGRESS below.
    schema_current = schema_version is not None and schema_version >= RUN_SCHEMA_VERSION
    issues = _schema_v2_issues(events, dag, config, schema_version, dag_present, dag_load_error)

    status = _overall_status(schema_current, issues,
                             (consensus, flagged, capped, undecided), dag_done)

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


def _symmetric_run_is_complete(
    events: list[dict[str, Any]],
    dag_obj: DAG,
    issues: list[WorkflowIssue],
    cfg: Config,
) -> bool:
    """Whether a symmetric run is a Finish-time-COMPLETE result, by the SAME engine semantics as the
    ``review-result`` CLI command — never the ``## Summary`` string status.

    A ``gate_proceeded_with_flags`` run is ``FLAGGED`` (not ``CLEAN``) yet still complete: every node
    is ``done`` and backed by an accepted set, so a deterministic recommendation exists (derived from
    the unresolved blocking objections). Completeness therefore mirrors ``cmd_review_result`` exactly:
    no configured prerequisite is ``missing`` or ``invalid`` (the shared ``workflow_issues`` the caller
    already computed into ``issues`` — the same owner ``_reject_incomplete_result_history`` consults; the
    caller also bails on ``invalid`` before calling this) AND :func:`require_complete_symmetric_run`
    accepts the recorded history against the current tree (``require_final``/``final_enabled`` both mirror
    ``cfg.final_review``, as in the CLI). A capped/halted or in-progress run leaves a node unbacked or a
    prerequisite missing, so it is NOT complete (the caller renders partial findings, no recommendation).

    The ``ValueError`` catch is narrow and deliberate: that is exactly how the shared guard signals
    "incomplete symmetric workflow", the sole reason to treat the run as partial rather than certify a
    result — never a broad swallow (a well-formed no-invalid run's config/tree already parse).
    """
    if any(i.kind in ("invalid", "missing") for i in issues):
        return False
    try:
        require_complete_symmetric_run(
            events, dag_obj, require_final=cfg.final_review, final_enabled=cfg.final_review)
    except ValueError:
        return False
    return True


def _symmetric_result_lines(
    events: list[dict[str, Any]],
    dag: dict[str, Any],
    config: dict[str, Any],
    *,
    schema_version: int | None,
    dag_present: bool,
    dag_load_error: bool,
) -> list[str]:
    """The deterministic symmetric review deliverable section (deep-dive / pr-review only).

    Renders the accepted finding set (grouped by ``source_gate``) and — for a COMPLETE, valid
    pr-review run only — the separate, exact ``**Review recommendation:** APPROVE|COMMENT|
    REQUEST_CHANGES`` line. Deliberately SEPARATE from the workflow ``## Summary`` status: the
    recommendation is derived from the accepted findings and unresolved blocking objections, never from
    workflow integrity — so a CLEAN workflow can recommend REQUEST_CHANGES, and a FLAGGED
    (proceeded-with-flags) workflow is still a COMPLETE result that carries one. Peer objections are NOT
    part of the accepted set — they stay in the ``## Gates`` provenance, visually separate from the
    accepted findings.

    Fails closed rather than fabricate: a run that is not a certifiable schema-v2 run with a present,
    loadable tree, or whose accepted history records ANY integrity violation (the Summary renders it
    ``INVALID``), yields NO result section. COMPLETENESS is decided by the SAME engine semantics as the
    ``review-result`` CLI (:func:`_symmetric_run_is_complete`), never the ``## Summary`` string status:
    a recommendation is derived once every configured prerequisite is present, valid, and bound and
    :func:`require_complete_symmetric_run` accepts the history — which a ``gate_proceeded_with_flags``
    run satisfies. A truly incomplete or capped/halted run (a missing prerequisite or an unbacked node)
    shows the partial accepted findings so far with no recommendation. The projection helpers
    (``accepted_findings`` / ``review_result``) own the finding/recommendation logic; this only renders
    their output. Gating on "no invalid issue" also keeps those helpers off history that would raise (a
    malformed/forged accepted set is an ``invalid`` issue, so the partial ``accepted_findings`` union
    here is total).
    """
    try:
        workflow = workflow_kind(events)
    except CorruptWorkflowError:
        return []  # corrupt run metadata: never fabricate a symmetric result (Summary renders INVALID)
    if workflow not in SYMMETRIC_WORKFLOWS:
        return []
    schema_current = schema_version is not None and schema_version >= RUN_SCHEMA_VERSION
    if not schema_current or dag_load_error or not dag_present:
        return []
    issues = _schema_v2_issues(events, dag, config, schema_version, dag_present, dag_load_error)
    if any(i.kind == "invalid" for i in issues):
        return []

    # A present, loadable schema-v2 tree with no `invalid` issue (bailed above) means both artifacts
    # parsed inside `_config_aware_issues`, so `Config.from_dict`/`DAG.from_dict` never raise here.
    cfg = Config.from_dict(config)
    dag_obj = DAG.from_dict(dag)
    # Completeness follows the SAME engine semantics as the `review-result` CLI, NOT the `## Summary`
    # string status: a `gate_proceeded_with_flags` run is FLAGGED yet a complete result whose
    # recommendation is derived from unresolved blocking objections.
    complete = _symmetric_run_is_complete(events, dag_obj, issues, cfg)

    recommendation: str | None = None
    if complete:
        # Pass the current DAG so `review_result` scopes the accepted union AND the unresolved
        # objections to the configured workflow and FAILS CLOSED on any out-of-scope dependency/FINAL
        # gate — the same fail-closed projection the CLI result command uses. `review_result` promotes
        # the accepted FINAL set when FINAL is configured, else the dependency union.
        result = review_result(events, cfg, workflow, dag_obj)
        finding_dicts = result["findings"]
        recommendation = result.get("recommendation")
    else:
        # Partial in-progress helper: the accepted dependency union so far (no scope guard needed —
        # a valid, in-scope run has no out-of-scope/duplicate/malformed accepted sets).
        finding_dicts = [f.to_dict() for f in accepted_findings(events).findings]
        if not finding_dicts:
            return []  # nothing accepted yet; the Summary already shows the in-progress status

    heading = "Review result" if workflow == "pr-review" else "Investigation result"
    lines = [f"## {heading}", ""]
    if recommendation is not None:
        # A separate, exact line — derived from the accepted finding set, never the workflow status.
        lines.append(f"**Review recommendation:** {recommendation}")
        lines.append("")
    if not complete:
        lines.append("_Partial result — the review has not settled every configured gate; no "
                     "recommendation is derived until it is complete._")
        lines.append("")

    lines.append(f"**Accepted findings:** {len(finding_dicts)}")
    lines.append("")
    # Group by source_gate in first-appearance order. Every untrusted field is `_san`-sanitized —
    # the same non-injection convention as the gate-provenance findings.
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for finding in finding_dicts:
        gate = finding.get("source_gate")
        if gate not in groups:
            groups[gate] = []
            order.append(gate)
        groups[gate].append(finding)
    for gate in order:
        lines.append(f"- **{_san(gate)}**")
        for finding in groups[gate]:
            lines.append(
                f"  - {_san(finding.get('id'))} [{_san(finding.get('severity'))}] "
                f"{_san(finding.get('location'))}: {_san(finding.get('claim'))} -> "
                f"{_san(finding.get('suggestion'))}"
            )
    lines.append("")
    return lines


def _source_snapshot_line(events: list[dict[str, Any]], workflow: str | None) -> str | None:
    """Render the source-snapshot status ONLY from the centrally-VALIDATED ``source_materialized``
    event (:func:`crucible.target.validate_source_materialization`): a single, in-order snapshot of a
    valid revision-bound loaded target with a matching kind/hash and a well-formed archive digest. A
    forged diff-file snapshot, a duplicate, a malformed/wrong-kind/wrong-hash/out-of-order event, or a
    snapshot of any other target never renders here (the Summary renders such a run INVALID)."""
    event = validate_source_materialization(events, workflow).event
    if event is None:
        return None
    archive = str(event.get("archive_sha256", ""))
    return f"**Source snapshot:** materialized (archive `{_san(archive[:12])}…`)"


def _review_target_lines(events: list[dict[str, Any]], workflow: str | None) -> list[str]:
    """The ``## Review target`` section (pr-review only), rendered from the authoritative
    ``target_loaded`` event with sanitized labels/paths and full hashes.

    Fails closed rather than fabricate: a build/deep-dive run (no target), a pr-review run whose
    target is not yet loaded (in progress), or a duplicate/malformed/invalid target (the Summary
    renders it INVALID) yields NO section — the reader never sees a half-formed identity.
    """
    if workflow != "pr-review":
        return []
    try:
        target = target_from_events(events)
    except ValueError:
        return []  # duplicate/malformed/mismatched target: the Summary renders the run INVALID
    if target is None:
        return []  # not loaded yet: the Summary renders the run IN PROGRESS
    sha = target_sha256(target)
    lines = ["## Review target", ""]
    if target.kind == "github-pr":
        lines.append(f"**Pull request:** {_san(target.repository)}#{target.pr_number}")
        lines.append(f"**URL:** {_san(target.url)}")
        lines.append(f"**Base:** {_san(target.base.repository)}@{_san(target.base.ref)} "
                     f"`{_san(target.base.sha)}`")
        lines.append(f"**Head:** {_san(target.head.repository)}@{_san(target.head.ref)} "
                     f"`{_san(target.head.sha)}`")
        lines.append(f"**Merge base:** `{_san(target.merge_base_sha)}`")
        lines.append(
            f"**Scope:** {'cross-repository' if target.is_cross_repository else 'same-repository'}")
        lines.append("**Revision:** bound")
    elif target.kind == "local-range":
        lines.append(f"**Local range:** {_san(target.repository)}")
        lines.append(f"**Base:** {_san(target.base.ref)} `{_san(target.base.sha)}`")
        lines.append(f"**Head:** {_san(target.head.ref)} `{_san(target.head.sha)}`")
        lines.append(f"**Merge base:** `{_san(target.merge_base_sha)}`")
        lines.append("**Revision:** bound")
    else:  # diff-file
        lines.append("**Kind:** diff-file")
        lines.append("**Revision:** unbound (patch identity only)")
    lines.append(f"**Intent:** {_san(target.intent_title)} \u2014 {_san(target.intent_body)}")
    files = target.changed_files
    listed = ", ".join(_san(f) for f in files) if files else "(none)"
    lines.append(f"**Changed files ({len(files)}):** {listed}")
    # F2: a github-pr review derives its patch/changed-files SOLELY from the base/head archives, which
    # omit submodule pointer (gitlink) OIDs. A genuinely empty content diff for a github-pr therefore
    # cannot distinguish "nothing changed" from "only a submodule pointer / other non-content change" —
    # surface that blind spot explicitly rather than silently showing zero changes.
    if (target.kind == "github-pr" and not files
            and target.diff_sha256 == hashlib.sha256(b"").hexdigest()):
        lines.append(
            "**\u26a0 Empty content diff:** this pull request's derived patch is empty; any submodule "
            "pointer changes or other non-content changes are not captured by content diffing — "
            "verify them from the PR's file list on GitHub.")
    lines.append(f"**Patch hash:** `{_san(target.diff_sha256)}`")
    lines.append(f"**Target hash:** `{_san(sha)}`")
    snapshot = _source_snapshot_line(events, workflow)
    if snapshot is not None:
        lines.append(snapshot)
    lines.append("")
    return lines


def render_markdown(run: RunLog) -> str:
    events = run.read_events()
    start = next((e for e in events if e["event"] == "run_start"), {})
    goal = start.get("goal", "(unknown goal)")
    # The recorded resolved config. Coerce a missing/null/non-object value to `{}` so the header can
    # render (it degrades to `?`); the schema-aware Summary separately fails such a run closed
    # because `{}` is not a complete resolved config shape (see `_config_aware_issues`).
    config = start.get("config")
    if not isinstance(config, dict):
        config = {}
    # Read the workflow through the single validation path. Corrupt run metadata (a present
    # null/non-string/unrecognized value) must NOT crash the report or render a symmetric header —
    # degrade the header to the asymmetric Builder/Critic layout; the schema-aware Summary renders the
    # run INVALID with a clear workflow-metadata issue (see `_schema_v2_issues`).
    try:
        workflow = workflow_kind(events)
    except CorruptWorkflowError:
        workflow = None
    schema_version = run_schema_version(events)

    lines: list[str] = []
    lines.append("# Crucible Run Report")
    lines.append("")
    lines.append(f"**Goal:** {_san(goal)}")
    if workflow in SYMMETRIC_WORKFLOWS:
        # Symmetric workflows reuse the two configured role slots as two EQUAL peers (Peer A = the
        # builder slot, Peer B = the critic slot) — a provenance mapping, never a Builder/Critic
        # asymmetry. Nested slots are isinstance-guarded so a malformed config degrades to `?`
        # rather than raising (the schema-aware Summary fails such a run closed separately).
        peer_a = config.get(PEER_SLOT_ROLES["A"], {})
        peer_b = config.get(PEER_SLOT_ROLES["B"], {})
        peer_a = peer_a if isinstance(peer_a, dict) else {}
        peer_b = peer_b if isinstance(peer_b, dict) else {}
        lines.append(
            f"**Peer A:** {_san(peer_a.get('model', '?'))} ({_san(peer_a.get('effort', '?'))}) - "
            f"**Peer B:** {_san(peer_b.get('model', '?'))} ({_san(peer_b.get('effort', '?'))})"
        )
    else:
        builder = config.get("builder", {})
        critic = config.get("critic", {})
        lines.append(
            f"**Builder:** {_san(builder.get('model', '?'))} ({_san(builder.get('effort', '?'))}) - "
            f"**Critic:** {_san(critic.get('model', '?'))} ({_san(critic.get('effort', '?'))})"
        )
    lines.append("")

    # Load the current dependency tree, fail-closed. Three outcomes drive the Summary:
    #   - ABSENT (FileNotFoundError) -> IN PROGRESS (no tree to certify yet);
    #   - PRESENT but not a well-formed JSON object (a JSON syntax/decoding error, or valid JSON that
    #     is not an object) -> INVALID, with an empty tree table and NO traceback;
    #   - PRESENT and loadable -> validated by the schema-aware Summary (a structurally malformed but
    #     loadable tree still fails closed inside `_run_summary_lines`).
    dag_present = True
    dag_load_error = False
    try:
        dag = run.load_dag()
    except FileNotFoundError:
        dag = {"nodes": []}
        dag_present = False
    except (json.JSONDecodeError, UnicodeDecodeError):
        dag = {"nodes": []}
        dag_load_error = True
    else:
        if not isinstance(dag, dict):
            dag = {"nodes": []}
            dag_load_error = True

    # The immutable review target identity (pr-review only), rendered BEFORE the Summary so a reader
    # sees WHAT was reviewed before the status. Read from the authoritative ``target_loaded`` event.
    lines.extend(_review_target_lines(events, workflow))

    lines.extend(_run_summary_lines(
        events, dag, config=config, schema_version=schema_version,
        dag_present=dag_present, dag_load_error=dag_load_error,
    ))

    # The symmetric (deep-dive/pr-review) review deliverable: accepted findings grouped by source
    # gate and — only for a COMPLETE, valid pr-review run — the separate PR recommendation. Empty for
    # build runs and for any run whose recorded history is not a valid schema-v2 result (legacy,
    # absent/malformed tree, or a recorded integrity violation), so the workflow status and the review
    # recommendation stay strictly separate. Completeness follows the `review-result` CLI semantics,
    # not the `## Summary` status, so a FLAGGED proceed-with-flags run still carries its recommendation.
    # Rendered before the gate provenance so the peers' objections (in `## Gates`) remain visually
    # separate from the accepted findings.
    lines.extend(_symmetric_result_lines(
        events, dag, config, schema_version=schema_version,
        dag_present=dag_present, dag_load_error=dag_load_error,
    ))

    lines.append("## Dependency tree")
    lines.append("")
    lines.append("| Node | Title | Status |")
    lines.append("|------|-------|--------|")
    for n in dag.get("nodes", []):
        lines.append(f"| {_san(n.get('id', ''))} | {_san(n.get('title', ''))} | {_san(n.get('status', ''))} |")
    lines.append("")

    lines.append("## Gates")
    lines.append("")
    gates = _events_by_gate(events)
    for gate, gate_events in gates.items():
        lines.append(f"### Gate: {_san(gate)}")
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
                        f"  - {_san(f.get('id'))} [{_san(f.get('severity'))}] {_san(f.get('location'))}: "
                        f"{_san(f.get('claim'))} -> {_san(f.get('suggestion'))}"
                    )
                raw = e.get("raw")
                if raw:
                    lines.append("")
                    lines.append(f"**Critic verdict raw (round {rnd}):**")
                    lines.append("")
                    lines.extend(_fenced(_payload_text(raw)))
                    lines.append("")
            elif ev == "symmetric_verdict":
                # Symmetric gate provenance: both equal peers' per-round verdict + summary, then the
                # namespaced aggregate objections (A:<id> / B:<id>). The accepted finding SET and any
                # PR recommendation are rendered elsewhere (Task 3/4) — this shows only the peers'
                # objections to the candidate, never the accepted results.
                peers = e.get("peers") or {}
                att_a = (peers.get("A") or {}).get("attestation") or {}
                att_b = (peers.get("B") or {}).get("attestation") or {}
                lines.append(
                    f"- **Round {rnd} (symmetric):** "
                    f"Peer A {_san(att_a.get('verdict', '?'))} - {_san(att_a.get('summary', ''))}; "
                    f"Peer B {_san(att_b.get('verdict', '?'))} - {_san(att_b.get('summary', ''))}"
                )
                for o in e.get("objections", []):
                    if isinstance(o, dict):
                        lines.append(
                            f"  - {_san(o.get('id'))} [{_san(o.get('severity'))}] "
                            f"{_san(o.get('location'))}: {_san(o.get('claim'))} -> "
                            f"{_san(o.get('suggestion'))}"
                        )
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
                        lines.append(f"  - {_san(fid)} -> {_san(res)}{tail}")
        # A gate ends in exactly one terminal event; if several were ever logged, the LAST in
        # log order is authoritative. Each interpolated value is sanitized individually.
        terminal = [e for e in gate_events
                    if e["event"] in _TERMINAL_EVENTS]
        if terminal:
            last = terminal[-1]
            rnd = _san(last.get("round", "?"))
            policy = _gate_policy_suffix(last)
            if last["event"] == "gate_consensus":
                lines.append(f"- **Outcome:** CONSENSUS at round {rnd}{policy}")
            elif last["event"] == "gate_proceeded_with_flags":
                flags = last.get("open_findings", [])
                ids = ", ".join(_san(i) for i in flags)
                carried = f": {ids}" if ids else ""
                lines.append(f"- **Outcome:** PROCEEDED WITH FLAGS at round {rnd}{policy} — "
                             f"{len(flags)} unresolved finding(s) carried{carried}")
            else:  # gate_capped
                lines.append(f"- **Outcome:** CAPPED at round {rnd}{policy} (unresolved)")
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
