from crucible.config import Config
from crucible.runlog import init_run
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
    assert "claude-opus-4.8" in md
    assert "gpt-5.5" in md


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


def test_summary_before_dependency_tree(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "## Summary" in md and "## Dependency tree" in md
    assert md.index("## Summary") < md.index("## Dependency tree")


def test_summary_clean_when_all_gates_consensus_and_dag_done(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block
    assert "2 total" in block and "2 consensus" in block
    assert "Unresolved blocking findings" not in block  # none, so line omitted


def test_summary_flagged_when_done_node_lacks_review_gate(tmp_path):
    # A node marked done without an accepted dep gate (e.g. --force) must NOT render CLEAN.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a", status="done"), _node("b", status="done")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)          # a reviewed
    # b is done but has NO dep:b advance gate (forced / un-gated)
    block = _summary_block(render_markdown(run))
    assert "Status:** FLAGGED" in block
    assert "without an accepted review gate" in block and "b" in block
    assert "CLEAN" not in block


def test_summary_clean_when_every_done_node_reviewed(tmp_path):
    # Regression: every done node HAS an accepted dep gate -> still CLEAN.
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a", status="done"), _node("b", status="done")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    run.append("gate_consensus", gate="dep:b", round=1)
    block = _summary_block(render_markdown(run))
    assert "Status:** CLEAN" in block


def test_summary_blocked_when_a_gate_capped(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a", status="done"), _node("b", status="blocked")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
    run.append("gate_capped", gate="dep:b", round=5, open_findings=["F1", "F3"])
    block = _summary_block(render_markdown(run))
    assert "Status:** BLOCKED" in block
    assert "1 capped" in block
    assert "Unresolved blocking findings:** 2" in block
    assert "F1" in block and "F3" in block
    assert "dep:b" in block


def test_summary_flagged_when_proceeded_with_flags_no_capped(tmp_path):
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_proceeded_with_flags", gate="dep:a", round=5, open_findings=["F2"])
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
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [_node("a", status="done"), _node("b", status="in_review")], "edges": []})
    run.append("gate_consensus", gate="plan", round=1)
    run.append("gate_consensus", gate="dep:a", round=1)
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
