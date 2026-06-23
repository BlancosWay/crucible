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
