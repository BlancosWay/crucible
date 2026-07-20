from crucible.config import Config, DEFAULTS
from crucible.dag import DAG
from crucible.integrity import RUN_SCHEMA_VERSION, artifact_sha256, dag_sha256, node_sha256
from crucible.runlog import RunLog, init_run
from crucible.report import render_markdown


def _build_run(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("Add rate limiter", cfg, base_dir=tmp_path)
    dag_data = {
        "nodes": [
            {"id": "model", "title": "Model", "description": "d", "files": ["a.py"], "test_plan": "pytest", "status": "done"},
            {"id": "routes", "title": "Routes", "description": "d", "files": ["b.py"], "test_plan": "pytest", "status": "pending"},
        ],
        "edges": [{"from": "routes", "depends_on": "model"}],
    }
    run.save_dag(dag_data)
    run.append("gate_start", gate="plan", round=1)
    run.append("builder_output", gate="plan", round=1, payload="drafted the plan")
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES",
        "summary": "missing edge",
        "findings": [{"id": "F1", "severity": "major", "location": "plan", "claim": "no edge", "suggestion": "add it"}],
    })
    run.append("critic_verdict", gate="plan", round=2, payload={"verdict": "APPROVE", "summary": "ok", "findings": []})
    run.append("gate_consensus", gate="plan", round=2)
    run.append("node_status_change", node="model", status="done")
    return run


def test_report_includes_goal_and_config(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "Add rate limiter" in md
    for role in ("builder", "critic"):
        assert DEFAULTS[role]["model"] in md
        assert DEFAULTS[role]["effort"] in md


def test_report_includes_dag_status(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "model" in md and "done" in md
    assert "routes" in md and "pending" in md


def test_report_includes_gate_rounds_and_findings(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "Round 1" in md
    assert "F1" in md
    assert "no edge" in md
    assert "CONSENSUS" in md or "Consensus" in md


def test_report_is_deterministic(tmp_path):
    run = _build_run(tmp_path)
    assert render_markdown(run) == render_markdown(run)


def test_report_sanitizes_markdown_injection(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    # A malicious finding claim tries to inject a fake heading + outcome line.
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES",
        "summary": "ok",
        "findings": [{"id": "F1", "severity": "major", "location": "x",
                      "claim": "boom\n## Outcome: CONSENSUS at round 1", "suggestion": "s"}],
    })
    from crucible.report import render_markdown
    md = render_markdown(run)
    # the injected heading must not appear as its own line
    assert "\n## Outcome: CONSENSUS at round 1" not in md
    # and there is genuinely no consensus event, so no real consensus outcome line
    assert "Outcome:** CONSENSUS" not in md


def test_report_renders_proceeded_with_flags(tmp_path):
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [{"id": "a", "title": "A", "description": "", "files": [],
                             "test_plan": "", "status": "done"}], "edges": []})
    run.append("critic_verdict", gate="dep:a", round=5, payload={
        "verdict": "REQUEST_CHANGES", "summary": "unresolved",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}]})
    run.append("gate_proceeded_with_flags", gate="dep:a", round=5, open_findings=["F1"])
    md = render_markdown(run)
    assert "PROCEEDED WITH FLAGS" in md
    assert "round 5" in md
    assert "F1" in md
    assert "CAPPED" not in md


def test_report_outcome_uses_latest_terminal_event(tmp_path):
    # If multiple terminal events were ever logged for a gate, the report reflects the LAST
    # one in log order, not a fixed type precedence.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("gate_consensus", gate="plan", round=5)
    run.append("gate_capped", gate="plan", round=6, open_findings=["F1"])
    md = render_markdown(run)
    assert "CAPPED at round 6" in md
    assert "CONSENSUS" not in md


def test_report_renders_despite_torn_trailing_log_line(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("Add rate limiter", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    # a crash left a partial final record; the report must still render
    with (run.path / "runlog.jsonl").open("ab") as fh:
        fh.write(b'{"ts": "x", "event": "partial"')
    md = render_markdown(run)
    assert "Add rate limiter" in md
    assert "CONSENSUS at round 1" in md


def _provenance_run(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    return run


def test_report_renders_builder_output_in_full(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="line one\nline two of the plan")
    md = render_markdown(run)
    assert "Builder output" in md
    assert "line one" in md and "line two of the plan" in md


def test_report_renders_builder_output_dict_sorted(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload={"b": 2, "a": 1})
    md = render_markdown(run)
    assert md.index('"a": 1') < md.index('"b": 2')


def test_report_renders_builder_resolution(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_resolution", gate="plan", round=2,
               payload={"F1": {"resolution": "wontfix", "rationale": "intended design"}})
    md = render_markdown(run)
    assert "Builder resolution" in md
    assert "F1" in md and "wontfix" in md and "intended design" in md


def test_report_renders_builder_resolution_plain_string_value(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_resolution", gate="plan", round=2, payload={"F1": "deferred"})
    md = render_markdown(run)
    assert "F1" in md and "deferred" in md


def _summary_block(md):
    return md.split("## Summary", 1)[1].split("## Dependency tree", 1)[0]


def test_summary_lists_forced_node_rationale(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag({"nodes": [{"id": "auth-model", "title": "Auth", "description": "",
                             "files": [], "test_plan": "", "status": "done"}], "edges": []})
    run.append("node_status_change", node="auth-model", status="done", forced=True,
               rationale="manual recovery: CI outage")
    summary = _summary_block(render_markdown(run))
    assert "manual recovery: CI outage" in summary and "auth-model" in summary


def test_summary_lists_wontfix_rationale(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_resolution", gate="dep:a", round=1,
               payload={"F1": {"resolution": "wontfix", "rationale": "false positive"}})
    summary = _summary_block(render_markdown(run))
    assert "F1" in summary and "false positive" in summary


def test_summary_surfaces_earlier_round_override(tmp_path):
    # An override from round 1 must still surface even when a later round of the same gate
    # logs a different resolution payload (scan all builder_resolution events, not last-per-gate).
    run = _provenance_run(tmp_path)
    run.append("builder_resolution", gate="dep:a", round=1,
               payload={"F1": {"resolution": "wontfix", "rationale": "early rebuttal"}})
    run.append("builder_resolution", gate="dep:a", round=2,
               payload={"F2": {"resolution": "deferred", "rationale": "later defer"}})
    summary = _summary_block(render_markdown(run))
    assert "early rebuttal" in summary and "later defer" in summary


def test_summary_skips_bare_string_resolution_without_rationale(tmp_path):
    # A historical/hand-built bare-string payload carries no rationale; it must not crash the
    # scan and must not appear as an audited override in the Summary.
    run = _provenance_run(tmp_path)
    run.append("builder_resolution", gate="dep:a", round=1, payload={"F1": "deferred"})
    summary = _summary_block(render_markdown(run))
    assert "Overrides" not in summary


def test_report_renders_critic_verdict_raw(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("critic_verdict", gate="plan", round=1,
               payload={"verdict": "APPROVE", "summary": "ok", "findings": []},
               raw='{"verdict": "APPROVE", "marker": "RAW_PROVENANCE"}')
    md = render_markdown(run)
    assert "RAW_PROVENANCE" in md


def test_report_renders_critic_output(tmp_path):
    # N4: the no-subagent-fallback `critic_output` (full raw review text) must appear in the report.
    run = _provenance_run(tmp_path)
    run.append("critic_output", gate="dep:a", round=1, payload="the critic's full raw review NOTED")
    md = render_markdown(run)
    assert "Critic output" in md
    assert "the critic's full raw review NOTED" in md


def test_report_empty_builder_output_renders_placeholder(tmp_path):
    # O4: an empty payload renders a placeholder, not an empty fenced code block.
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="")
    md = render_markdown(run)
    assert "_(empty)_" in md
    assert "```\n```" not in md  # no empty fence pair


def test_report_escapes_backticks_in_untrusted_text(tmp_path):
    # N6: a backtick in untrusted Critic text must be escaped (not start an inline code span).
    run = _provenance_run(tmp_path)
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES", "summary": "s",
        "findings": [{"id": "F1", "severity": "major", "location": "x",
                      "claim": "use `git rebase`", "suggestion": "s"}]})
    md = render_markdown(run)
    assert "\\`" in md  # backticks escaped
    assert "use `git rebase`" not in md  # the raw (unescaped) code-span form is gone


def test_report_builder_output_fence_is_injection_safe(tmp_path):
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1,
               payload="```\n## Pwned Heading\n```")
    md = render_markdown(run)
    # content has a 3-backtick run, so the wrapping fence must be >= 4 backticks
    assert "````" in md
    assert "## Pwned Heading" in md


def test_report_html_escapes_builder_output(tmp_path):
    from crucible.report import render_html
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="<script>alert(1)</script>")
    html = render_html(run)
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


# --- RPT-001: untrusted inline fields are HTML-safe in the markdown report -----

def test_report_markdown_escapes_html_in_inline_fields(tmp_path):
    # RPT-001: a Critic finding (untrusted model output) with raw HTML must not render as
    # live HTML when report.md is viewed in an HTML-permitting Markdown renderer.
    from crucible.report import render_markdown
    run = init_run("pwn <img src=x onerror=alert(1)>", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES", "summary": "<script>alert(1)</script>",
        "findings": [{"id": "F1", "severity": "major", "location": "<b>x</b>",
                      "claim": "<img src=x onerror=alert(1)>", "suggestion": "s"}]})
    md = render_markdown(run)
    # raw tags from inline fields must be neutralized
    assert "<script>" not in md
    assert "<img" not in md
    assert "<b>x</b>" not in md
    # and present in escaped form
    assert "&lt;script&gt;" in md
    assert "&lt;img" in md


def test_report_html_escapes_inline_html_fields_without_double_escaping(tmp_path):
    from crucible.report import render_html
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES", "summary": "ok",
        "findings": [{"id": "F1", "severity": "major", "location": "loc",
                      "claim": "<script>alert(1)</script>", "suggestion": "s"}]})
    html = render_html(run)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # the inline field was escaped exactly once (no &amp;lt; double-escaping)
    assert "&amp;lt;" not in html


def test_report_markdown_keeps_provenance_fence_raw(tmp_path):
    # Fidelity: the fenced raw provenance (Builder/Critic output) stays verbatim in the
    # markdown — code-fence content is rendered literally by Markdown processors, so raw
    # HTML there is safe without escaping (and the HTML path still escapes it).
    from crucible.report import render_markdown, render_html
    run = _provenance_run(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="raw <tag> kept <as-is>")
    md = render_markdown(run)
    assert "raw <tag> kept <as-is>" in md          # markdown fence: full fidelity
    assert "raw &lt;tag&gt;" in render_html(run)    # html: escaped


# --- run-level Summary banner ------------------------------------------------

def _summary_block(md: str) -> str:
    """The text of the `## Summary` section (up to the next `## ` heading)."""
    assert "## Summary" in md, "no Summary section rendered"
    return md.split("## Summary", 1)[1].split("\n## ", 1)[0]


def _node(nid, status="done"):
    return {"id": nid, "title": nid.upper(), "description": "", "files": [],
            "test_plan": "", "status": status}


# Schema-v2 binding handshake helpers. For a schema-v2 run the Summary status is derived from the
# run's RESOLVED CONFIGURATION and the CURRENT artifact bindings (via crucible.workflow), not merely
# from which events happened; precedence (most severe first) is:
#   LEGACY / UNVERIFIED > INVALID > BLOCKED > FLAGGED > CLEAN > IN PROGRESS
# These record the same artifact/DAG/node bindings the CLI would, so a bound PLAN / dependency
# terminal here is indistinguishable from a real run's — the config-aware validator then sees valid
# bindings, and only the phase/ordering behavior under test drives the status.

def _bound_dag(*specs):
    """A DAG whose nodes carry non-empty immutable fields so their digests are meaningful. Each
    spec is ``(id, status)`` or just ``id`` (defaults to ``done``)."""
    nodes = []
    for spec in specs:
        nid, status = spec if isinstance(spec, tuple) else (spec, "done")
        nodes.append({"id": nid, "title": nid.upper(), "description": f"do {nid}",
                      "files": [f"{nid}.py"], "test_plan": "pytest", "status": status})
    return DAG.from_dict({"nodes": nodes, "edges": []})


def _bind_plan(run, dag, *, artifact=b"reviewed plan"):
    """Record a schema-v2 bound PLAN gate and save the tree; return ``(artifact_sha256, dag_sha256)``."""
    run.save_dag(dag.to_dict())
    a, d = artifact_sha256(artifact), dag_sha256(dag)
    run.append("builder_output", gate="plan", round=1, payload=artifact.decode("utf-8"),
               artifact_sha256=a)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=d)
    return a, d


def _bind_dep(run, dag, nid):
    """Record a schema-v2 bound dependency terminal for ``nid`` against the current tree."""
    a = artifact_sha256(f"impl {nid}".encode("utf-8"))
    run.append("builder_output", gate=f"dep:{nid}", round=1, payload=f"impl {nid}",
               artifact_sha256=a)
    run.append("gate_consensus", gate=f"dep:{nid}", round=1, artifact_sha256=a,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, nid))


def _bind_gate(run, dag, gate, *, artifact=b"artifact"):
    """Record a bound REPRODUCE/FINAL terminal (artifact + DAG for FINAL, artifact only otherwise)."""
    a = artifact_sha256(artifact)
    run.append("builder_output", gate=gate, round=1, payload=artifact.decode("utf-8"),
               artifact_sha256=a)
    fields = {"artifact_sha256": a}
    if gate in ("plan", "final"):
        fields["dag_sha256"] = dag_sha256(dag)
    run.append("gate_consensus", gate=gate, round=1, **fields)


def test_summary_before_dependency_tree(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "## Summary" in md and "## Dependency tree" in md
    assert md.index("## Summary") < md.index("## Dependency tree")


def test_summary_clean_when_all_gates_consensus_and_dag_done(tmp_path):
    # final_review off (this test is not about FINAL): a bound PLAN + bound dep:a over a done tree
    # is a complete configured workflow -> CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block
    assert "2 total" in block and "2 consensus" in block
    assert "Unresolved blocking findings" not in block  # none, so line omitted


def test_summary_flagged_when_done_node_lacks_review_gate(tmp_path):
    # A node marked done without an accepted dep gate (e.g. --force) must NOT render CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"), ("b", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")          # a reviewed
    # b is done but has NO dep:b advance gate (forced / un-gated)
    block = _summary_block(render_markdown(run))
    assert "Status:** FLAGGED" in block
    assert "without an accepted review gate" in block and "b" in block
    assert "CLEAN" not in block


def test_summary_clean_when_every_done_node_reviewed(tmp_path):
    # Regression: every done node HAS an accepted dep gate -> still CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"), ("b", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    _bind_dep(run, dag, "b")
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block


def test_summary_blocked_when_a_gate_capped(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"), ("b", "blocked"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    run.append("gate_capped", gate="dep:b", round=5, open_findings=["F1", "F3"])
    block = _summary_block(render_markdown(run))
    assert "Status:** BLOCKED" in block
    assert "1 capped" in block
    assert "Unresolved blocking findings:** 2" in block
    assert "F1" in block and "F3" in block
    assert "dep:b" in block


def test_summary_flagged_when_proceeded_with_flags_no_capped(tmp_path):
    cfg = Config.from_dict({"on_cap": "proceed_with_flags", "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    # dep:a reached an advance terminal (proceeded with flags) bound to the current node, so the
    # done node is properly review-gated (not INVALID); the flags themselves drive FLAGGED.
    a = artifact_sha256(b"impl a")
    run.append("builder_output", gate="dep:a", round=5, payload="impl a", artifact_sha256=a)
    run.append("gate_proceeded_with_flags", gate="dep:a", round=5, open_findings=["F2"],
               artifact_sha256=a, dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    block = _summary_block(render_markdown(run))
    assert "Status:** FLAGGED" in block
    assert "1 flagged" in block
    assert "Unresolved blocking findings:** 1" in block and "F2" in block


def test_summary_capped_beats_flagged_precedence(tmp_path):
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a"), _node("b")], "edges": []})
    run.append("gate_proceeded_with_flags", gate="dep:a", round=5, open_findings=["F1"])
    run.append("gate_capped", gate="dep:b", round=5, open_findings=["F2"])
    block = _summary_block(render_markdown(run))
    assert "Status:** BLOCKED" in block  # capped outranks flagged
    assert "1 flagged" in block and "1 capped" in block
    assert "Unresolved blocking findings:** 2" in block


def test_summary_in_progress_when_gate_undecided(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a", status="pending")], "edges": []})
    # a critic_verdict but NO terminal event => undecided gate
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES", "summary": "s",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}]})
    block = _summary_block(render_markdown(run))
    assert "Status:** IN PROGRESS" in block


def test_summary_in_progress_when_consensus_but_no_dag(tmp_path):
    # F1: a consensus gate with no loaded DAG (fallback {"nodes": []}) must NOT read CLEAN.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)  # deliberately NO save_dag
    run.append("gate_consensus", gate="plan", round=1)
    block = _summary_block(render_markdown(run))
    assert "Status:** IN PROGRESS" in block
    assert "CLEAN" not in block


def test_summary_in_progress_when_node_not_done(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"), ("b", "in_review"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** IN PROGRESS" in block  # a node still in_review


def test_summary_excludes_critic_prose(tmp_path):
    # F2: the banner must never interpolate untrusted Critic summary/claim text.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("critic_verdict", gate="dep:a", round=5, payload={
        "verdict": "REQUEST_CHANGES", "summary": "MALICIOUSSUMMARYXYZZY",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x",
                      "claim": "MALICIOUSCLAIMPLUGH", "suggestion": "s"}]})
    run.append("gate_capped", gate="dep:a", round=5, open_findings=["F1"])
    block = _summary_block(render_markdown(run))
    assert "MALICIOUSSUMMARYXYZZY" not in block
    assert "MALICIOUSCLAIMPLUGH" not in block
    assert "F1" in block  # the id (sanitized) is fine; the prose is not


def test_summary_sanitizes_finding_ids(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_capped", gate="dep:a", round=5, open_findings=["F1|x<b>"])
    block = _summary_block(render_markdown(run))
    assert "Status:** BLOCKED" in block
    # raw markdown/HTML breakers from the untrusted id must be neutralized
    assert "|x<b>" not in block
    assert "<b>" not in block


def test_summary_ids_not_wrapped_in_code_spans(tmp_path):
    # F1: untrusted gate/finding ids must be plain _san text, never wrapped in a backtick code
    # span (a backtick in an id would otherwise break out — Markdown code spans ignore backslash
    # escapes). This asserts the wrappers are absent while the ids still render.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_capped", gate="dep:a", round=5, open_findings=["Fx"])
    block = _summary_block(render_markdown(run))
    assert "`dep:a`" not in block  # gate id not code-span-wrapped
    assert "`Fx`" not in block     # finding id not code-span-wrapped
    assert "dep:a" in block and "Fx" in block  # but present as plain text


def test_summary_empty_run_is_in_progress(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)  # no dag, no gates
    block = _summary_block(render_markdown(run))
    assert "Status:** IN PROGRESS" in block


def test_summary_unresolved_by_severity_breakdown(tmp_path):
    # #6: the banner breaks the unresolved findings down by severity, derived from each gate's
    # last critic_verdict payload (id -> severity), deterministic from the run-log.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("critic_verdict", gate="dep:a", round=5, payload={
        "verdict": "REQUEST_CHANGES", "summary": "s",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
                     {"id": "F2", "severity": "major", "location": "y", "claim": "c", "suggestion": "s"}]})
    run.append("gate_capped", gate="dep:a", round=5, open_findings=["F1", "F2"])
    block = _summary_block(render_markdown(run))
    assert "Unresolved by severity:" in block
    sev_line = [l for l in block.splitlines() if "Unresolved by severity:" in l][0]
    assert "1 blocker" in sev_line and "1 major" in sev_line
    assert sev_line.index("blocker") < sev_line.index("major")  # canonical order


def test_summary_by_severity_omits_zero_counts(tmp_path):
    # two blockers, no majors -> "2 blocker", no "0 major" noise.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("critic_verdict", gate="dep:a", round=3, payload={
        "verdict": "REQUEST_CHANGES", "summary": "s",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
                     {"id": "F2", "severity": "blocker", "location": "y", "claim": "c", "suggestion": "s"}]})
    run.append("gate_capped", gate="dep:a", round=3, open_findings=["F1", "F2"])
    block = _summary_block(render_markdown(run))
    sev_line = [l for l in block.splitlines() if "Unresolved by severity:" in l][0]
    assert "2 blocker" in sev_line
    assert "major" not in sev_line and "0 " not in sev_line


def test_summary_by_severity_unknown_bucket_when_id_absent(tmp_path):
    # An open id not present in the gate's critic_verdict is counted defensively as "unknown".
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_capped", gate="dep:a", round=5, open_findings=["Fx"])  # no critic_verdict
    block = _summary_block(render_markdown(run))
    sev_line = [l for l in block.splitlines() if "Unresolved by severity:" in l][0]
    assert "1 unknown" in sev_line


def test_summary_no_by_severity_line_when_clean(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_consensus", gate="dep:a", round=1)
    block = _summary_block(render_markdown(run))
    assert "Unresolved by severity" not in block


# --- Task 4: binding- and configuration-aware report status --------------------
#
# The schema-v2 binding handshake is recorded by the `_bind_*` helpers defined near the top of this
# file (alongside `_node`/`_summary_block`); the tests below drive each status in the precedence
#   LEGACY / UNVERIFIED > INVALID > BLOCKED > FLAGGED > CLEAN > IN PROGRESS


def test_legacy_report_is_unverified_never_clean(tmp_path):
    # A run whose run_start records no schema_version is legacy: readable, but LEGACY / UNVERIFIED
    # and never CLEAN, even when every gate reached consensus and the tree is done.
    run_dir = tmp_path / "legacy-run"
    run_dir.mkdir()
    run = RunLog(run_dir)
    run.append("run_start", goal="legacy", config=Config.from_dict({}).to_dict())  # NO schema_version
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    block = _summary_block(render_markdown(run))
    assert "Status:** LEGACY / UNVERIFIED" in block
    assert "CLEAN" not in block


def test_missing_configured_reproduce_approval_final_is_in_progress(tmp_path):
    # All three optional phases are enabled and the PLAN + dependency are validly bound, but
    # REPRODUCE, approval, and FINAL are omitted -> IN PROGRESS, naming all three phases.
    cfg = Config.from_dict({"reproduce_gate": True, "human_approval": True, "final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** IN PROGRESS" in block
    assert "reproduce" in block.lower()
    assert "approval" in block.lower()
    assert "final" in block.lower()


def test_binding_mismatch_is_invalid(tmp_path):
    # A fully bound run whose current dag.json is then semantically changed: the PLAN's recorded
    # dag_sha256 no longer matches the tree -> INVALID, naming the DAG binding.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    # Replace the tree with a semantic change (different files) after PLAN consensus.
    run.save_dag(_bound_dag(("a", "done")).to_dict() | {
        "nodes": [{"id": "a", "title": "A", "description": "do a", "files": ["changed.py"],
                   "test_plan": "pytest", "status": "done"}]})
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "DAG binding" in block


def test_out_of_order_final_is_invalid(tmp_path):
    # A FINAL terminal recorded BEFORE the dependency completes is out of order even though the tree
    # later ends done: log FINAL first, then append the dependency terminal and the node's done
    # transition and save the completed tree. The status is derived from the RECORDED log order, not
    # merely the final DAG snapshot -> INVALID, naming FINAL.
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_gate(run, dag, "final")   # FINAL terminal logged now, before the dependency completes
    # Later in log order: the dependency is reviewed and the node is marked done; tree ends done.
    _bind_dep(run, dag, "a")
    run.append("node_status_change", node="a", status="done",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "FINAL" in block


def test_missing_builder_output_artifact_is_invalid(tmp_path):
    # A PLAN terminal that carries an artifact_sha256 but has NO same-gate/same-round builder_output
    # to bind it to is an unbound (unreviewable) artifact -> INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    run.save_dag(dag.to_dict())
    a = artifact_sha256(b"reviewed plan")
    # PLAN consensus with an artifact hash, but no builder_output logged for plan/round 1.
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=dag_sha256(dag))
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_wrong_artifact_sha256_is_invalid(tmp_path):
    # The PLAN terminal's artifact_sha256 disagrees with the bytes of its same-gate/same-round
    # builder_output payload (a tampered/stale binding) -> INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    run.save_dag(dag.to_dict())
    run.append("builder_output", gate="plan", round=1, payload="reviewed plan",
               artifact_sha256=artifact_sha256(b"reviewed plan"))
    # The terminal records a DIFFERENT artifact hash than the payload bytes hash to.
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256="0" * 64,
               dag_sha256=dag_sha256(dag))
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_wrong_round_artifact_is_invalid(tmp_path):
    # The only PLAN builder_output is logged under a different round than the accepted terminal, so
    # the terminal has no same-gate/same-round artifact to bind -> INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    run.save_dag(dag.to_dict())
    a = artifact_sha256(b"reviewed plan")
    run.append("builder_output", gate="plan", round=2, payload="reviewed plan", artifact_sha256=a)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=dag_sha256(dag))
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_dep_terminal_unbound_artifact_is_invalid(tmp_path):
    # A dependency terminal that is DAG/node-bound but whose artifact_sha256 has no backing
    # builder_output is an unbound completion -> INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256="0" * 64,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_malformed_current_dag_missing_node_id_is_invalid_never_clean(tmp_path):
    # F5: after an otherwise consensus-like history, the CURRENT dag.json is malformed — a node with
    # no `id`. Such a tree cannot be parsed, bound, or certified, so it must NEVER read CLEAN even
    # though its raw node status is "done" (the certification path must outrank raw dag_done).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    # A present-but-malformed current tree: the sole node has status "done" but no `id`.
    run.save_dag({"nodes": [{"status": "done"}], "edges": []})
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "dependency tree" in block  # the issue names the malformed tree
    assert "CLEAN" not in block


def test_malformed_current_dag_edge_is_invalid_never_clean(tmp_path):
    # F5: after an otherwise consensus-like history, the CURRENT dag.json has a malformed edge
    # (missing `depends_on`). An unparseable tree cannot be certified: INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    run.save_dag({
        "nodes": [{"id": "a", "title": "A", "description": "do a", "files": ["a.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [{"from": "a"}],  # malformed: missing `depends_on`
    })
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "dependency tree" in block
    assert "CLEAN" not in block


def test_malformed_run_config_is_invalid_never_clean(tmp_path):
    # F5 (fail closed): a schema-v2 run whose recorded resolved config cannot be parsed cannot be
    # validated against, so — even with a valid current tree and a consensus-like history — it must
    # never certify: INVALID, never CLEAN.
    run_dir = tmp_path / "bad-config-run"
    run_dir.mkdir()
    run = RunLog(run_dir)
    run.append("run_start", schema_version=RUN_SCHEMA_VERSION, goal="g",
               config={"unknown_key": 1})  # not a parseable resolved run config
    dag = _bound_dag(("a", "done"))
    run.save_dag(dag.to_dict())
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_complete_bound_configured_workflow_is_clean(tmp_path):
    # REPRODUCE -> PLAN -> approval -> dependency done -> FINAL, all with matching hashes and in
    # order -> CLEAN.
    cfg = Config.from_dict({"reproduce_gate": True, "human_approval": True, "final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_gate(run, dag, "reproduce")
    artifact, dag_hash = _bind_plan(run, dag)
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    _bind_dep(run, dag, "a")
    _bind_gate(run, dag, "final")
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block


def test_disabled_reproduce_terminal_is_invalid(tmp_path):
    # F1: reproduce_gate is disabled, yet the run log records a REPRODUCE gate terminal. That gate is
    # not part of the configured workflow (a configured-forbidden phase), so the run must render
    # INVALID and never CLEAN even though the PLAN + dependency are validly bound.
    cfg = Config.from_dict({"reproduce_gate": False, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_gate(run, dag, "reproduce")  # a bound, accepted REPRODUCE terminal though it is disabled
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "reproduce" in block.lower()
    assert "CLEAN" not in block


def test_late_approval_after_dependency_is_invalid(tmp_path):
    # F2: human_approval is configured; PLAN reaches consensus, the dependency is reviewed and the
    # node marked done, and only THEN is plan_approved appended. Configured approval must gate
    # dependency work, so an approval recorded after that work is out of order -> INVALID, never CLEAN
    # (mirrors the finding's report probe that wrongly rendered CLEAN).
    cfg = Config.from_dict({"human_approval": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    artifact, dag_hash = _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    run.append("node_status_change", node="a", status="done",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "approval" in block.lower()
    assert "CLEAN" not in block


def test_final_missing_dag_binding_is_invalid(tmp_path):
    # F3: an otherwise-complete, otherwise-valid run whose FINAL terminal carries a valid artifact
    # but NO dag_sha256 binding must not certify — FINAL's DAG binding is required, so a missing one
    # is INVALID, never CLEAN (a final review over an unspecified tree cannot certify the current run).
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    fa = artifact_sha256(b"final artifact")
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa)  # NO dag_sha256
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block
    assert "FINAL" in block and "DAG binding" in block


def test_final_wrong_dag_binding_is_invalid(tmp_path):
    # F3: an otherwise-complete, otherwise-valid run whose FINAL terminal binds a WRONG dag_sha256 (a
    # final review recorded over a different implementation tree) must not certify -> INVALID, never
    # CLEAN.
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    fa = artifact_sha256(b"final artifact")
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa, dag_sha256="0" * 64)
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block
    assert "FINAL" in block and "DAG binding" in block


def test_plan_artifact_after_terminal_is_invalid(tmp_path):
    # F4: a PLAN terminal whose only matching same-gate/same-round builder_output is appended AFTER
    # the terminal could not have been the artifact the Critic reviewed for that decision -> INVALID,
    # never CLEAN (a forged log records consensus first, then a matching artifact later).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    run.save_dag(dag.to_dict())
    a = artifact_sha256(b"reviewed plan")
    # Terminal FIRST, then a matching builder_output for the same gate/round appended after it.
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=dag_sha256(dag))
    run.append("builder_output", gate="plan", round=1, payload="reviewed plan", artifact_sha256=a)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_dep_artifact_after_terminal_is_invalid(tmp_path):
    # F4 (downstream gate): a dependency terminal whose only matching builder_output is appended AFTER
    # the terminal is an unreviewable binding -> INVALID, never CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    da = artifact_sha256(b"impl a")
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256=da,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    run.append("builder_output", gate="dep:a", round=1, payload="impl a", artifact_sha256=da)
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_final_artifact_after_terminal_is_invalid(tmp_path):
    # F4 (downstream gate): a FINAL terminal whose only matching builder_output is appended AFTER the
    # terminal is an unreviewable binding -> INVALID, never CLEAN (even with a correct DAG binding).
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    fa = artifact_sha256(b"final artifact")
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa, dag_sha256=dag_sha256(dag))
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_valid_artifact_survives_later_post_terminal_output(tmp_path):
    # F4 (pre-terminal-only guard): validation considers pre-terminal Builder outputs only. A valid
    # pre-terminal PLAN binding is neither replaced nor bypassed by a later same-gate/same-round
    # builder_output appended after the terminal — the exact terminal binding stands and the run
    # stays CLEAN (the post-terminal artifact is ignored, not used to flip the outcome either way).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)  # valid pre-terminal PLAN artifact ("reviewed plan") binds the terminal
    _bind_dep(run, dag, "a")
    run.append("builder_output", gate="plan", round=1, payload="tampered later plan",
               artifact_sha256=artifact_sha256(b"tampered later plan"))
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block


def test_forced_current_node_remains_flagged(tmp_path):
    # A node completed via a forced override that records current DAG/node hashes and a rationale
    # is backed but not review-gated -> FLAGGED, and the rationale is surfaced.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    run.append("node_status_change", node="a", status="done", forced=True,
               rationale="manual recovery: CI outage",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    block = _summary_block(render_markdown(run))
    assert "Status:** FLAGGED" in block
    assert "manual recovery: CI outage" in block


# F6: the resolved run_start config must be certified against its COMPLETE recorded shape (exactly
# the Config.to_dict keys and nested builder/critic role keys), never re-derived from defaults via
# Config.from_dict override semantics. A schema-v2 run whose run_start omits the config, records only
# a partial override, or drops a nested role key cannot know which phases were configured, so it must
# fail closed to INVALID and can never certify CLEAN.

def test_missing_run_start_config_is_invalid_never_clean(tmp_path):
    # A schema-v2 run_start with NO `config` at all: even with a fully bound PLAN + dependency +
    # FINAL over a done tree (which would otherwise be CLEAN under the shipped defaults), the absent
    # resolved config cannot be certified -> INVALID, never CLEAN, naming the run configuration.
    run_dir = tmp_path / "no-config-run"
    run_dir.mkdir()
    run = RunLog(run_dir)
    run.append("run_start", schema_version=RUN_SCHEMA_VERSION, goal="g")  # NO config recorded
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    _bind_gate(run, dag, "final")  # shipped defaults enable final_review
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block
    assert "run configuration" in block.lower()


def test_partial_run_start_config_is_invalid_never_clean(tmp_path):
    # A schema-v2 run_start whose config is a from_dict OVERRIDE (only `final_review`), not a full
    # resolved config. Under override semantics it would parse (filling every other key from
    # defaults) and certify CLEAN over a bound PLAN + dependency; the resolved-shape check rejects it
    # -> INVALID, never CLEAN.
    run_dir = tmp_path / "partial-config-run"
    run_dir.mkdir()
    run = RunLog(run_dir)
    run.append("run_start", schema_version=RUN_SCHEMA_VERSION, goal="g",
               config={"final_review": False})  # a partial override, not a resolved config
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block
    assert "run configuration" in block.lower()


def test_incomplete_role_run_start_config_is_invalid_never_clean(tmp_path):
    # A schema-v2 run_start config with every top-level key but a builder role missing `effort`.
    # Config.from_dict would fill the missing role key from defaults (override semantics) and certify
    # CLEAN; the resolved-shape check requires exactly the nested role keys -> INVALID, never CLEAN.
    run_dir = tmp_path / "partial-role-run"
    run_dir.mkdir()
    run = RunLog(run_dir)
    resolved = Config.from_dict({"final_review": False}).to_dict()
    resolved["builder"] = {"model": resolved["builder"]["model"]}  # drop nested `effort`
    run.append("run_start", schema_version=RUN_SCHEMA_VERSION, goal="g", config=resolved)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block
    assert "run configuration" in block.lower()


def test_resolved_run_start_config_still_certifies_clean(tmp_path):
    # Regression for F6: a run_start carrying the EXACT resolved config shape (Config.to_dict, as
    # init_run records) is unchanged — it still certifies CLEAN when every configured phase is bound.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _bound_dag(("a", "done"))
    _bind_plan(run, dag)
    _bind_dep(run, dag, "a")
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block


# F7: a PRESENT but unparseable dag.json (syntactically invalid JSON, or valid JSON that is not an
# object) must fail closed to INVALID with no traceback; an ABSENT dag.json stays IN PROGRESS; a
# legacy run stays LEGACY / UNVERIFIED regardless of a malformed present tree.

def test_syntactically_invalid_present_dag_is_invalid_without_traceback(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    (run.path / "dag.json").write_text("{not-json")  # present, but not valid JSON
    block = _summary_block(render_markdown(run))  # must not raise a JSONDecodeError traceback
    assert "Status:** INVALID" in block
    assert "dependency tree" in block
    assert "CLEAN" not in block


def test_present_non_object_dag_is_invalid_without_traceback(tmp_path):
    # A present dag.json that is valid JSON but not an object (a list) is still an uncertifiable tree
    # -> INVALID, no `.get`/parse traceback.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="plan", round=1)
    (run.path / "dag.json").write_text("[]")
    block = _summary_block(render_markdown(run))
    assert "Status:** INVALID" in block
    assert "CLEAN" not in block


def test_legacy_present_invalid_dag_is_unverified_without_traceback(tmp_path):
    # Precedence: a legacy run (no schema_version) with a present but non-JSON dag.json stays
    # LEGACY / UNVERIFIED, never INVALID or a traceback.
    run_dir = tmp_path / "legacy-bad-dag"
    run_dir.mkdir()
    run = RunLog(run_dir)
    run.append("run_start", goal="legacy", config=Config.from_dict({}).to_dict())  # NO schema_version
    run.append("gate_consensus", gate="plan", round=1)
    (run.path / "dag.json").write_text("{not-json")
    block = _summary_block(render_markdown(run))
    assert "Status:** LEGACY / UNVERIFIED" in block
    assert "CLEAN" not in block
