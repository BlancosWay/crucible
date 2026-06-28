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
