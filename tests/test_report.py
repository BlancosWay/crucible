import json

from crucible.config import Config, DEFAULTS
from crucible.dag import DAG
from crucible.integrity import RUN_SCHEMA_VERSION, artifact_sha256, dag_sha256, node_sha256
from crucible.runlog import RunLog, init_run
from crucible.report import render_markdown
from crucible.target import ReviewTarget, target_sha256


# A minimal revision-unbound diff-file target — the smallest valid pr-review target, so a pr-review
# report fixture binds its gates to a real loaded target identity.
_TARGET_MANIFEST = {
    "version": 1, "kind": "diff-file", "revision_bound": False, "repository": None,
    "diff_sha256": "0" * 64, "changed_files": ["a.py"], "intent": {"title": "t", "body": "b"},
}
_TGT = target_sha256(ReviewTarget.from_dict(_TARGET_MANIFEST))


def _load_target(run):
    """Append the one immutable ``target_loaded`` event; return its authoritative hash."""
    run.append("target_loaded", target=ReviewTarget.from_dict(_TARGET_MANIFEST).to_dict(),
               target_sha256=_TGT)
    return _TGT


def _append_plan_consensus(run, dsha):
    """Append a target-bound PLAN consensus trio for a pr-review report fixture."""
    plan_art = artifact_sha256(b"plan")
    plan_bind = {"artifact_sha256": plan_art, "dag_sha256": dsha, "target_sha256": _TGT}
    run.append("builder_output", gate="plan", round=1, payload="plan", artifact_sha256=plan_art)
    run.append("symmetric_verdict", gate="plan", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("plan", 1, plan_bind), **plan_bind)
    run.append("gate_consensus", gate="plan", round=1, **plan_bind)


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


def test_report_shows_effective_gate_policy(tmp_path):
    # F5: the report surfaces the effective round cap + on_cap recorded on each gate outcome line, so
    # a --max-rounds-driven decision is reconstructable from the report itself.
    from crucible.report import render_markdown
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("critic_verdict", gate="plan", round=2, payload={"verdict": "APPROVE", "summary": "s"})
    run.append("gate_consensus", gate="plan", round=2, max_rounds=3, on_cap="halt")
    md = render_markdown(run)
    assert "CONSENSUS at round 2 (round cap 3, on_cap halt)" in md


def test_report_gate_policy_absent_on_legacy_terminal(tmp_path):
    # F5: a legacy terminal event without the policy fields renders no suffix (still readable).
    from crucible.report import render_markdown
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    run.append("critic_verdict", gate="plan", round=1, payload={"verdict": "APPROVE", "summary": "s"})
    run.append("gate_consensus", gate="plan", round=1)   # no max_rounds/on_cap (pre-F5)
    md = render_markdown(run)
    assert "CONSENSUS at round 1" in md
    assert "CONSENSUS at round 1 (round cap" not in md   # no policy suffix for a legacy terminal


def test_san_neutralizes_markdown_image_and_link():
    from crucible.report import _san
    # F6: Markdown image/link syntax must be neutralized — an ![](url) renders an ACTIVE <img> request
    # (tracking pixel / content injection) in report.md even with no HTML. Escaping the link/image
    # brackets makes both ![…](…) and […](…) inert (Markdown won't parse a bracket-escaped link).
    out = _san("![pwn](https://evil.example/track.png)")
    assert "\\[" in out and "\\]" in out            # brackets escaped -> image cannot render
    assert "![pwn]" not in out                       # the unescaped image opener is gone
    assert "pwn" in out and "evil.example" in out    # content preserved, just inert
    assert "\\[click\\]" in _san("[click](https://evil.example)")  # link text brackets escaped
    # existing neutralizations still hold
    assert _san("<b>") == "&lt;b&gt;"
    assert _san("a`b") == "a\\`b"
    assert _san("a|b") == "a\\|b"


def test_report_dependency_table_id_is_injection_safe(tmp_path):
    # F6: an untrusted node id in the dependency table must be plain _san text (never a backtick code
    # span a backtick could break out of), and any Markdown image/link syntax in it must be inert so
    # report.md cannot emit an active <img> tracking request.
    from crucible.report import render_markdown, _san
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    nid = "a`![x](http://tracker/p.png)"
    run.save_dag({"nodes": [_node(nid)], "edges": []})
    md = render_markdown(run)
    # the SANITIZED id is rendered as plain text, NOT wrapped in a code span (re-adding the wrapper
    # around _san(id) would make this fail — the raw-id check the reviewer flagged would not).
    assert _san(nid) in md
    assert f"`{_san(nid)}`" not in md
    assert "![x](http://tracker/p.png)" not in md     # active image opener gone
    assert "\\[x\\]" in md                            # rendered with escaped brackets (inert)


def test_report_gate_finding_id_is_injection_safe(tmp_path):
    # F6: an untrusted finding id in the Gates section must not be code-span-wrapped (backtick
    # breakout) nor carry live image syntax.
    from crucible.report import render_markdown, _san
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    bad_id = "F1`![i](http://t/p.png)"
    run.append("critic_verdict", gate="plan", round=1, payload={
        "verdict": "REQUEST_CHANGES", "summary": "s",
        "findings": [{"id": bad_id, "severity": "major", "location": "x",
                      "claim": "c", "suggestion": "s"}],
    })
    md = render_markdown(run)
    assert _san(bad_id) in md
    assert f"`{_san(bad_id)}`" not in md               # finding id not code-span-wrapped
    assert "![i](http://t/p.png)" not in md            # active image opener gone
    assert "\\[i\\]" in md                            # escaped brackets (inert)


def test_report_gate_header_is_injection_safe(tmp_path):
    # F6: the `### Gate:` header renders the gate id (untrusted — a dep:<node-id> derives from a
    # model-authored id). It must be plain _san text, never a code span, and image syntax must be inert.
    from crucible.report import render_markdown, _san
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag({"nodes": [], "edges": []})
    gate = "dep:a`![g](http://t/g.png)"
    run.append("gate_capped", gate=gate, round=5, open_findings=["Fx"])
    md = render_markdown(run)
    assert f"### Gate: {_san(gate)}" in md             # header rendered as plain _san text
    assert f"### Gate: `{_san(gate)}`" not in md       # ...never wrapped in a code span
    assert "![g](http://t/g.png)" not in md            # active image opener gone
    assert "\\[g\\]" in md


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


# --- symmetric workflow: peer provenance + accepted-set integrity ------------

def _sym_candidate(source_gate="dep:auth", fid="F1", severity="major"):
    return {"summary": "auth review", "findings": [{
        "source_gate": source_gate, "id": fid, "severity": severity,
        "location": "src/auth.py:42", "claim": "Expired refresh tokens are accepted.",
        "suggestion": "Reject expired refresh tokens.",
    }]}


def _sym_peers(gate, rnd, bindings, outer_objs=()):
    """A valid persisted A/B peers object matching the CLI write path (round-9)."""
    from crucible.symmetric import peer_slot_provenance
    prov = peer_slot_provenance(Config.from_dict({}))
    per = {"A": [], "B": []}
    for o in outer_objs:
        slot, _, base = str(o["id"]).partition(":")
        if slot in per:
            per[slot].append({**o, "id": base})
    peers = {}
    for slot in ("A", "B"):
        objs = per[slot]
        has_blocking = any(o["severity"] in ("blocker", "major") for o in objs)
        att = {"peer": slot, "gate": gate, "round": rnd,
               "verdict": "REQUEST_CHANGES" if has_blocking else "APPROVE",
               "summary": f"peer {slot} review", "objections": objs, **bindings}
        peers[slot] = {**prov[slot], "raw": json.dumps(att), "attestation": att}
    return peers


def _symmetric_dep_run(tmp_path, *, accepted="pre", workflow="pr-review",
                       severity="major"):
    """A symmetric run with one done node 'auth' backed by a symmetric dependency gate.

    ``accepted`` selects the accepted_finding_set placement/validity: ``"pre"`` (valid, before the
    terminal), ``"missing"`` (never appended), ``"post"`` (appended after the terminal —
    orphan/invalid), or ``"malformed"`` (appended before the terminal but with an invalid-severity
    payload — invalid). ``workflow`` chooses ``pr-review`` (default) or ``deep-dive``; ``severity``
    sets the single accepted finding's severity.
    """
    cfg = Config.from_dict({"final_review": False})
    run = init_run("symmetric dependency", cfg, base_dir=tmp_path, workflow=workflow)
    # Only a pr-review run binds a target; a deep-dive run must NOT record one.
    tbind = {"target_sha256": _load_target(run)} if workflow == "pr-review" else {}
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    plan_payload = "investigation plan"
    plan_art = artifact_sha256(plan_payload.encode("utf-8"))
    plan_bind = {"artifact_sha256": plan_art, "dag_sha256": dsha, **tbind}
    run.append("builder_output", gate="plan", round=1, payload=plan_payload,
               artifact_sha256=plan_art)
    run.append("symmetric_verdict", gate="plan", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("plan", 1, plan_bind), **plan_bind)
    run.append("gate_consensus", gate="plan", round=1, **plan_bind)

    candidate = _sym_candidate(severity=severity)
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha, **tbind}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS",
               peers=_sym_peers("dep:auth", 1, bindings),
               objections=[], candidate=candidate, **bindings)
    malformed = {"summary": "x", "findings": [{
        "source_gate": "dep:auth", "id": "F1", "severity": "not-a-severity",
        "location": "l", "claim": "c", "suggestion": "s"}]}
    if accepted in ("pre", "malformed"):
        payload = malformed if accepted == "malformed" else candidate
        run.append("accepted_finding_set", gate="dep:auth", round=1, payload=payload, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    if accepted == "post":
        run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("node_status_change", node="auth", status="done")
    return run


def _sym_final_run(tmp_path):
    """A CLEAN pr-review run (final_review enabled) with one done node 'auth' and an accepted FINAL
    set that carries the dependency finding plus a cross-cutting ``source_gate: final`` finding — so
    the effective result spans two source gates (``dep:auth`` and ``final``)."""
    cfg = Config.from_dict({"final_review": True})
    run = init_run("symmetric final", cfg, base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

    candidate = _sym_candidate()
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("dep:auth", 1, bindings), candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")

    final_payload = {"summary": "final", "findings": candidate["findings"] + [{
        "source_gate": "final", "id": "C1", "severity": "nit",
        "location": "src/auth.py:1", "claim": "Cross-cutting note.", "suggestion": "Consider it."}]}
    final_text = json.dumps(final_payload)
    final_art = artifact_sha256(final_text.encode("utf-8"))
    fbind = {"artifact_sha256": final_art, "dag_sha256": dsha, "target_sha256": _TGT}
    run.append("builder_output", gate="final", round=1, payload=final_text, artifact_sha256=final_art)
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("final", 1, fbind), candidate=final_payload, **fbind)
    run.append("accepted_finding_set", gate="final", round=1, payload=final_payload, **fbind)
    run.append("gate_consensus", gate="final", round=1, **fbind)
    return run


def _sym_flags_run(tmp_path):
    """A settled pr-review run whose dependency gate PROCEEDED WITH FLAGS: it persists an accepted
    finding set AND carries an unresolved blocking peer objection. Exercises the report's separation
    of accepted findings (result section) from peer objections (gate provenance)."""
    cfg = Config.from_dict({"final_review": False, "on_cap": "proceed_with_flags"})
    run = init_run("symmetric flags", cfg, base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

    candidate = _sym_candidate(severity="nit")
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    objections = [{"id": "A:OBJ1", "severity": "blocker", "location": "candidate:F1",
                   "claim": "Peer A disputes the finding set.", "suggestion": "Add the missing case."}]
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="PROCEED_WITH_FLAGS",
               objections=objections, peers=_sym_peers("dep:auth", 1, bindings, objections),
               candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate,
               accepted_with_flags=True, open_objections=["A:OBJ1"], **bindings)
    run.append("gate_proceeded_with_flags", gate="dep:auth", round=1,
               open_findings=["A:OBJ1"], **bindings)
    run.append("node_status_change", node="auth", status="done")
    return run


def test_report_renders_symmetric_peer_attestations(tmp_path):
    run = init_run("sym", Config.from_dict({}), base_dir=tmp_path, workflow="pr-review")
    run.save_dag({"nodes": [{"id": "auth", "title": "Auth", "description": "", "files": [],
                             "test_plan": "", "status": "in_review"}], "edges": []})
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CHANGES",
               peers={
                   "A": {"model": "m", "effort": "e", "raw": "{}",
                         "attestation": {"peer": "A", "verdict": "REQUEST_CHANGES",
                                         "summary": "peer A dissents", "objections": [
                                             {"id": "F1", "severity": "major",
                                              "location": "candidate:F1", "claim": "weak",
                                              "suggestion": "cite"}]}},
                   "B": {"model": "m", "effort": "e", "raw": "{}",
                         "attestation": {"peer": "B", "verdict": "APPROVE",
                                         "summary": "peer B approves", "objections": []}},
               },
               objections=[{"id": "A:F1", "severity": "major", "location": "candidate:F1",
                            "claim": "weak", "suggestion": "cite"}],
               candidate={"summary": "", "findings": []},
               artifact_sha256="a" * 64, dag_sha256="d" * 64, node_sha256="n" * 64)
    md = render_markdown(run)
    assert "Peer A" in md and "Peer B" in md
    assert "REQUEST_CHANGES" in md
    assert "peer A dissents" in md and "peer B approves" in md
    assert "A:F1" in md


def test_report_symmetric_dependency_clean_with_accepted_set(tmp_path):
    run = _symmetric_dep_run(tmp_path, accepted="pre")
    md = render_markdown(run)
    assert "CLEAN" in md


def test_report_symmetric_missing_accepted_set_is_invalid(tmp_path):
    run = _symmetric_dep_run(tmp_path, accepted="missing")
    md = render_markdown(run)
    assert "INVALID" in md
    assert "CLEAN" not in md


def test_report_symmetric_post_terminal_accepted_set_is_invalid(tmp_path):
    run = _symmetric_dep_run(tmp_path, accepted="post")
    md = render_markdown(run)
    assert "INVALID" in md
    assert "CLEAN" not in md


# --- F1/F2: corrupt run metadata fails the report closed (INVALID, no result, no crash) -----------

def _rewrite_run_events(run, mutate):
    """Rewrite a run's append-only log after applying ``mutate`` to the parsed records in place — used
    to forge corrupt history the CLI never writes (a tampered workflow value, a PLAN accepted set)."""
    path = run.path / "runlog.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    mutate(records)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_report_corrupt_workflow_metadata_renders_invalid(tmp_path):
    # F1: a schema-v2 run whose run_start records a PRESENT but unrecognized workflow value is corrupt.
    # The report must render INVALID with a clear workflow-metadata issue, render NO symmetric result,
    # and never crash — not silently default the corrupt value to a build report.
    run = _symmetric_dep_run(tmp_path, accepted="pre")  # otherwise a CLEAN pr-review run

    def mutate(records):
        for r in records:
            if r.get("event") == "run_start":
                r["workflow"] = "bogus"
    _rewrite_run_events(run, mutate)
    md = render_markdown(run)  # must not raise
    assert "INVALID" in md
    assert "CLEAN" not in md
    assert "workflow" in md.lower()
    assert "## Review result" not in md and "## Investigation result" not in md


def test_report_corrupt_null_workflow_metadata_renders_invalid(tmp_path):
    # A present JSON null workflow is corrupt too (distinct from an ABSENT key, which reads as build).
    run = _symmetric_dep_run(tmp_path, accepted="pre")

    def mutate(records):
        for r in records:
            if r.get("event") == "run_start":
                r["workflow"] = None
    _rewrite_run_events(run, mutate)
    md = render_markdown(run)
    assert "INVALID" in md and "CLEAN" not in md


def test_report_plan_accepted_finding_set_renders_invalid(tmp_path):
    # F2: a forged PLAN accepted finding set (a symmetric PLAN never carries one) fails the report
    # closed to INVALID and renders no symmetric result — even though the workflow value is valid.
    run = _symmetric_dep_run(tmp_path, accepted="pre")

    def mutate(records):
        sv = next(r for r in records
                  if r.get("event") == "symmetric_verdict" and r.get("gate") == "plan")
        bind = {k: sv[k] for k in ("artifact_sha256", "dag_sha256", "node_sha256") if k in sv}
        term_idx = next(i for i, r in enumerate(records)
                        if r.get("event") == "gate_consensus" and r.get("gate") == "plan")
        records.insert(term_idx, {"event": "accepted_finding_set", "gate": "plan",
                                  "round": sv.get("round", 1),
                                  "payload": {"summary": "", "findings": []}, **bind})
    _rewrite_run_events(run, mutate)
    md = render_markdown(run)
    assert "INVALID" in md
    assert "CLEAN" not in md
    assert "## Review result" not in md


# --- Task 4: mode-aware symmetric result rendering ---------------------------

def _section(md, heading):
    """The lines of the ``## heading`` section (up to the next ``## `` heading), or ''."""
    lines = md.split("\n")
    out, capturing = [], False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = line[3:].strip().startswith(heading)
            if capturing:
                continue
        elif capturing:
            out.append(line)
    return "\n".join(out)


def test_report_symmetric_header_labels_peers_not_builder_critic(tmp_path):
    # A symmetric run's header attributes the two configured role slots as Peer A / Peer B — never the
    # Builder/Critic asymmetry. The configured models still appear; the Builder:/Critic: header labels
    # do not.
    run = _symmetric_dep_run(tmp_path, accepted="pre")
    md = render_markdown(run)
    header = md.split("## ", 1)[0]  # everything before the first section heading
    assert "**Peer A:**" in header and "**Peer B:**" in header
    assert DEFAULTS["builder"]["model"] in header and DEFAULTS["critic"]["model"] in header
    assert "**Builder:**" not in md and "**Critic:**" not in md


def test_report_build_header_retains_builder_critic_labels(tmp_path):
    # Backward compatibility: the asymmetric build workflow keeps the Builder/Critic header and never
    # relabels to peers.
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "**Builder:**" in md and "**Critic:**" in md
    assert "Peer A" not in md and "Peer B" not in md


def test_report_symmetric_accepted_findings_grouped_by_source_gate(tmp_path):
    # The effective (FINAL) result spans two source gates; the report groups accepted findings under
    # each source_gate heading, keeping every finding under its own gate.
    run = _sym_final_run(tmp_path)
    md = render_markdown(run)
    result = _section(md, "Review result")
    assert "dep:auth" in result and "final" in result
    assert "F1" in result and "C1" in result
    # The dep finding is grouped under dep:auth and the cross-cutting one under final: the final
    # group header appears after the dep:auth group header.
    assert result.index("dep:auth") < result.index("final")


def test_report_symmetric_clean_status_coexists_with_request_changes(tmp_path):
    # Workflow status (CLEAN = every configured phase accepted and bound) is independent of the PR
    # recommendation (derived from the accepted finding set): a CLEAN run with an accepted blocking
    # finding still recommends REQUEST_CHANGES.
    run = _symmetric_dep_run(tmp_path, accepted="pre", severity="major")
    md = render_markdown(run)
    assert "**Status:** CLEAN" in md
    assert "**Review recommendation:** REQUEST_CHANGES" in md


def test_report_symmetric_clean_no_findings_recommends_approve(tmp_path):
    # No accepted findings and no unresolved objections -> APPROVE, rendered as the exact separate
    # recommendation line.
    run = init_run("clean approve", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    dag = DAG.from_dict({"nodes": [{"id": "auth", "title": "Auth", "description": "d",
                                    "files": ["auth.py"], "test_plan": "pytest",
                                    "status": "done"}], "edges": []})
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")
    _append_plan_consensus(run, dsha)
    empty = {"summary": "clean", "findings": []}
    cand_text = json.dumps(empty)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text, artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("dep:auth", 1, bindings), candidate=empty, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=empty, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")
    md = render_markdown(run)
    assert "**Status:** CLEAN" in md
    assert "**Review recommendation:** APPROVE" in md


def test_report_symmetric_peer_objections_separate_from_accepted_findings(tmp_path):
    # A proceeded-with-flags gate persists an accepted finding AND an unresolved blocking objection.
    # The accepted finding renders in the result section; the peer objection stays in gate
    # provenance — never mixed into the accepted-findings result.
    run = _sym_flags_run(tmp_path)
    md = render_markdown(run)
    result = _section(md, "Review result")
    gates = _section(md, "Gates")
    assert "A:OBJ1" in gates  # objection lives in gate provenance
    assert "A:OBJ1" not in result  # never surfaced as an accepted finding
    assert "F1" in result  # the accepted finding is in the result section
    # A settled proceed-with-flags run is a COMPLETE result (review-result semantics): Summary is
    # FLAGGED and the recommendation is still derived from the unresolved blocking objection.
    assert "**Status:** FLAGGED" in md
    assert "**Review recommendation:** REQUEST_CHANGES" in md


def test_report_symmetric_complete_flagged_renders_recommendation(tmp_path):
    # A pr-review run settled via gate_proceeded_with_flags is FLAGGED (not CLEAN) yet COMPLETE under
    # the review-result command semantics (every node done and backed). The report must render Summary
    # FLAGGED AND the separate, deterministic recommendation derived from the unresolved blocking
    # objection — never suppress it as a partial result. The rendered recommendation must equal the
    # projection's review_result output for the same run.
    from crucible.symmetric import review_result, workflow_kind
    run = _sym_flags_run(tmp_path)
    events = run.read_events()
    cfg = Config.from_dict(json.loads((run.path / "config.json").read_text()))
    expected = review_result(events, cfg, workflow_kind(events),
                             DAG.from_dict(run.load_dag()))["recommendation"]
    assert expected == "REQUEST_CHANGES"
    md = render_markdown(run)
    assert "**Status:** FLAGGED" in md
    assert f"**Review recommendation:** {expected}" in md
    # A complete result is never labelled partial.
    assert "_Partial result" not in _section(md, "Review result")


def test_report_symmetric_incomplete_missing_final_is_partial_no_recommendation(tmp_path):
    # A run whose only node is done + backed but whose configured FINAL gate has not been reached is
    # INCOMPLETE (a missing configured prerequisite): the report shows the partial accepted union with
    # the explicit "no recommendation until complete" note and derives NO recommendation.
    run = init_run("symmetric missing final", Config.from_dict({"final_review": True}),
                   base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    dag = DAG.from_dict({"nodes": [{"id": "auth", "title": "Auth", "description": "d",
                                    "files": ["auth.py"], "test_plan": "pytest",
                                    "status": "done"}], "edges": []})
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")
    _append_plan_consensus(run, dsha)
    candidate = _sym_candidate()
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text, artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_sym_peers("dep:auth", 1, bindings), candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")
    md = render_markdown(run)
    assert "**Status:** IN PROGRESS" in md
    result = _section(md, "Review result")
    assert "F1" in result  # the accepted dependency union so far is shown ...
    assert "_Partial result" in result  # ... explicitly marked partial ...
    assert "**Review recommendation:**" not in md  # ... with no recommendation until complete


def test_report_symmetric_capped_gate_has_no_recommendation(tmp_path):
    # A capped (halt) dependency gate persists NO accepted set and does not advance its node, so the
    # run is incomplete: the report shows BLOCKED and derives no recommendation (matching review-result,
    # which fails such a run closed).
    run = init_run("symmetric capped", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    dag = DAG.from_dict({"nodes": [{"id": "auth", "title": "Auth", "description": "d",
                                    "files": ["auth.py"], "test_plan": "pytest",
                                    "status": "in_review"}], "edges": []})
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")
    _append_plan_consensus(run, dsha)
    candidate = _sym_candidate()
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    objections = [{"id": "A:OBJ1", "severity": "blocker", "location": "candidate:F1",
                   "claim": "Peer A disputes the finding set.", "suggestion": "Add the missing case."}]
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text, artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CAPPED",
               objections=objections, peers=_sym_peers("dep:auth", 1, bindings, objections),
               candidate=candidate, **bindings)
    run.append("gate_capped", gate="dep:auth", round=1, open_findings=["A:OBJ1"], **bindings)
    md = render_markdown(run)
    assert "**Status:** BLOCKED" in md
    assert "## Review result" not in md
    assert "**Review recommendation:**" not in md


def test_report_deep_dive_omits_recommendation(tmp_path):
    # A deep-dive investigation returns a finding set, never an Approve/Comment/Request-changes call.
    run = _symmetric_dep_run(tmp_path, accepted="pre", workflow="deep-dive")
    md = render_markdown(run)
    assert "**Status:** CLEAN" in md
    assert "recommendation" not in md.lower()
    result = _section(md, "Investigation result")
    assert "F1" in result  # the accepted finding is still rendered


def test_report_symmetric_malformed_accepted_history_is_invalid_no_result(tmp_path):
    # Malformed accepted finding history renders INVALID in the Summary and NEVER a success-shaped
    # result: no result section and no fabricated recommendation.
    run = _symmetric_dep_run(tmp_path, accepted="malformed")
    md = render_markdown(run)
    assert "INVALID" in md
    assert "CLEAN" not in md
    assert "**Review recommendation:**" not in md
    assert "## Review result" not in md


# --- Task 2: the report renders the immutable review target identity -----------------------------

def _load_target_manifest(run, manifest):
    """Append a target_loaded event for an arbitrary target manifest; return its hash."""
    target = ReviewTarget.from_dict(manifest)
    sha = target_sha256(target)
    run.append("target_loaded", target=target.to_dict(), target_sha256=sha)
    return sha


_GITHUB_TARGET = {
    "version": 1, "kind": "github-pr", "revision_bound": True, "repository": "base/repo",
    "pr_number": 7, "url": "https://github.com/base/repo/pull/7",
    "base": {"repository": "base/repo", "ref": "main", "sha": "1" * 40},
    "head": {"repository": "fork/repo", "ref": "feature", "sha": "2" * 40},
    "merge_base_sha": "3" * 40,
    "is_cross_repository": True, "diff_sha256": "a" * 64, "changed_files": ["src/a.py"],
    "intent": {"title": "Fix A", "body": "Details"},
}
_LOCAL_TARGET = {
    "version": 1, "kind": "local-range", "revision_bound": True,
    "repository": "https://github.com/owner/repo.git",
    "base": {"ref": "main", "sha": "1" * 40}, "head": {"ref": "feature", "sha": "2" * 40},
    "merge_base_sha": "3" * 40, "diff_sha256": "b" * 64, "changed_files": ["src/a.py"],
    "intent": {"title": "Local range review", "body": "..."},
}
_DIFF_TARGET = {
    "version": 1, "kind": "diff-file", "revision_bound": False, "repository": None,
    "diff_sha256": "c" * 64, "changed_files": ["src/a.py"],
    "intent": {"title": "Patch review", "body": "..."},
}


def _pr_review_with_target(tmp_path, manifest):
    """A pr-review run whose only recorded work is the loaded target (enough to render the section)."""
    run = init_run("target report", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    sha = _load_target_manifest(run, manifest)
    return run, sha


def test_report_renders_github_target_identity(tmp_path):
    run, sha = _pr_review_with_target(tmp_path, _GITHUB_TARGET)
    md = render_markdown(run)
    target = _section(md, "Review target")
    assert "base/repo#7" in target
    assert "1" * 40 in target
    assert "fork/repo" in target
    assert "2" * 40 in target
    assert "3" * 40 in target  # the recorded PR merge base (fork point) is rendered
    assert "Merge base" in target
    assert "cross-repository" in target
    assert sha in target  # the authoritative target hash is rendered


def test_report_renders_snapshot_derived_changed_files(tmp_path):
    # Round-2 F1: the report surfaces the snapshot-DERIVED changed-files list verbatim — including a
    # rename's old+new pair (which GitHub's rename-detected `files` view would collapse to a single
    # path). The report never re-derives or filters the list against any external file view.
    manifest = dict(_GITHUB_TARGET, changed_files=["src/new.py", "src/old.py"], diff_sha256="d" * 64)
    run, _ = _pr_review_with_target(tmp_path, manifest)
    target = _section(render_markdown(run), "Review target")
    assert "Changed files (2)" in target
    assert "src/new.py" in target and "src/old.py" in target


def test_report_renders_local_range_target(tmp_path):
    run, _ = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    target = _section(render_markdown(run), "Review target")
    assert "owner/repo" in target
    assert "1" * 40 in target and "2" * 40 in target
    assert "3" * 40 in target  # merge base


def test_report_diff_file_states_revision_unbound(tmp_path):
    run, _ = _pr_review_with_target(tmp_path, _DIFF_TARGET)
    assert "Revision:** unbound (patch identity only)" in render_markdown(run)


def test_report_review_target_section_precedes_summary(tmp_path):
    run, _ = _pr_review_with_target(tmp_path, _DIFF_TARGET)
    md = render_markdown(run)
    assert "## Review target" in md and "## Summary" in md
    assert md.index("## Review target") < md.index("## Summary")


def test_report_target_section_sanitizes_untrusted_fields(tmp_path):
    manifest = dict(_DIFF_TARGET)
    manifest["intent"] = {"title": "pwn <img src=x onerror=alert(1)>", "body": "b | c"}
    manifest["changed_files"] = ["src/<script>.py"]
    run, _ = _pr_review_with_target(tmp_path, manifest)
    target = _section(render_markdown(run), "Review target")
    assert "<img" not in target and "<script>" not in target
    assert "&lt;img" in target or "&lt;script&gt;" in target


def test_report_no_target_section_before_load(tmp_path):
    # An init-only pr-review run has no target yet: no target section, and the Summary is IN PROGRESS.
    run = init_run("no target", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    md = render_markdown(run)
    assert "## Review target" not in md
    assert "**Status:** IN PROGRESS" in md


def test_report_downstream_work_without_target_is_invalid(tmp_path):
    # pr-review protocol work with no loaded target is INVALID and renders no target section.
    run = init_run("no target work", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    dag = DAG.from_dict({"nodes": [{"id": "auth", "title": "Auth", "description": "d",
                                    "files": ["auth.py"], "test_plan": "pytest",
                                    "status": "pending"}], "edges": []})
    run.save_dag(dag.to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    run.append("gate_consensus", gate="plan", round=1,
               artifact_sha256=artifact_sha256(b"plan"), dag_sha256=dag_sha256(dag))
    md = render_markdown(run)
    assert "## Review target" not in md
    assert "**Status:** INVALID" in md


def test_report_no_target_section_for_build(tmp_path):
    run = _build_run(tmp_path)
    assert "## Review target" not in render_markdown(run)


def test_report_no_target_section_for_deep_dive(tmp_path):
    run = _symmetric_dep_run(tmp_path, accepted="pre", workflow="deep-dive")
    assert "## Review target" not in render_markdown(run)


def test_report_renders_source_snapshot_when_materialized(tmp_path):
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    run.append("source_materialized", kind="local-range", target_sha256=sha,
               archive_sha256="d" * 64)
    target = _section(render_markdown(run), "Review target")
    assert "materialized" in target.lower()


def test_report_ignores_source_snapshot_bound_to_other_target(tmp_path):
    # A source_materialized whose target hash does not match the loaded target is not a valid snapshot
    # of THIS target, so its status is not rendered as if it were.
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    run.append("source_materialized", kind="local-range", target_sha256="9" * 64,
               archive_sha256="d" * 64)
    target = _section(render_markdown(run), "Review target")
    assert "materialized" not in target.lower()


def test_report_ignores_forged_diff_file_source(tmp_path):
    # A diff-file target is revision-unbound: it never has a source snapshot. A forged
    # source_materialized bound to it (even with the correct target hash) never renders and the run
    # is INVALID.
    run, sha = _pr_review_with_target(tmp_path, _DIFF_TARGET)
    run.append("source_materialized", kind="diff-file", target_sha256=sha, archive_sha256="d" * 64)
    md = render_markdown(run)
    assert "materialized" not in _section(md, "Review target").lower()
    assert "**Status:** INVALID" in md


def test_report_ignores_duplicate_source_materialization(tmp_path):
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    run.append("source_materialized", kind="local-range", target_sha256=sha, archive_sha256="d" * 64)
    run.append("source_materialized", kind="local-range", target_sha256=sha, archive_sha256="e" * 64)
    md = render_markdown(run)
    assert "materialized" not in _section(md, "Review target").lower()
    assert "**Status:** INVALID" in md


def test_report_ignores_malformed_archive_source(tmp_path):
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    run.append("source_materialized", kind="local-range", target_sha256=sha,
               archive_sha256="NOT-A-HEX-DIGEST")
    md = render_markdown(run)
    assert "materialized" not in _section(md, "Review target").lower()
    assert "**Status:** INVALID" in md


def test_report_ignores_wrong_kind_source(tmp_path):
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    run.append("source_materialized", kind="github-pr", target_sha256=sha, archive_sha256="d" * 64)
    md = render_markdown(run)
    assert "materialized" not in _section(md, "Review target").lower()
    assert "**Status:** INVALID" in md


def test_report_ignores_out_of_order_source(tmp_path):
    # A source_materialized recorded AFTER DAG/PLAN work is out of order: no snapshot, INVALID.
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    dag = DAG.from_dict({"nodes": [{"id": "a", "title": "A", "description": "d",
                                    "files": ["a.py"], "test_plan": "pytest",
                                    "status": "pending"}], "edges": []})
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=1)
    run.append("source_materialized", kind="local-range", target_sha256=sha, archive_sha256="d" * 64)
    md = render_markdown(run)
    assert "materialized" not in _section(md, "Review target").lower()
    assert "**Status:** INVALID" in md


# --- Task 2 (round-2 finding): the absent-DAG report validates general target-event state ----------
#
# A schema-v2 pr-review report with NO dag.json present (nothing to certify yet) still fails closed on
# an INVALID target-event history: a duplicate, malformed/hash-mismatched, or late ``target_loaded``,
# or pr-review protocol work recorded with no target, renders INVALID rather than being masked as
# merely IN PROGRESS. This reuses the central ``crucible.target.target_event_issues`` validator — the
# same one the DAG-present path already applies via ``workflow_issues`` — so both paths agree, and it
# layers cleanly with the existing source-materialization fail-closed check (source issues once, no
# duplicate messages). An init-only run (no target yet) and a single valid target stay IN PROGRESS.

def _absent_dag_pr_review(tmp_path, name="absent dag"):
    """An init-only pr-review run with NO dag.json saved (the absent-tree report path)."""
    return init_run(name, Config.from_dict({"final_review": False}),
                    base_dir=tmp_path, workflow="pr-review")


def test_report_absent_dag_init_only_no_target_is_in_progress(tmp_path):
    # No target, no work, no tree: nothing to certify yet -> IN PROGRESS, and no target section.
    md = render_markdown(_absent_dag_pr_review(tmp_path))
    assert "**Status:** IN PROGRESS" in md
    assert "## Review target" not in md
    assert "**Status:** INVALID" not in md


def test_report_absent_dag_single_valid_target_is_in_progress_with_section(tmp_path):
    # A single valid loaded target with no tree yet stays IN PROGRESS but may render its identity.
    run, sha = _pr_review_with_target(tmp_path, _LOCAL_TARGET)
    md = render_markdown(run)
    assert "**Status:** IN PROGRESS" in md
    assert "## Review target" in md
    assert sha in _section(md, "Review target")


def test_report_absent_dag_duplicate_target_is_invalid(tmp_path):
    # Two target_loaded events with no tree: INVALID (a target is immutable; load exactly one). No
    # success-shaped target or result section is fabricated.
    run = _absent_dag_pr_review(tmp_path)
    _load_target_manifest(run, _LOCAL_TARGET)
    _load_target_manifest(run, _LOCAL_TARGET)
    md = render_markdown(run)
    assert "**Status:** INVALID" in md
    assert "## Review target" not in md
    assert "## Review result" not in md
    assert "multiple target_loaded events" in _section(md, "Summary")


def test_report_absent_dag_hash_mismatched_target_is_invalid(tmp_path):
    # A target_loaded whose recorded target_sha256 disagrees with its payload is malformed -> INVALID,
    # even with no tree present.
    run = _absent_dag_pr_review(tmp_path)
    run.append("target_loaded", target=ReviewTarget.from_dict(_LOCAL_TARGET).to_dict(),
               target_sha256="9" * 64)
    md = render_markdown(run)
    assert "**Status:** INVALID" in md
    assert "## Review target" not in md
    assert "## Review result" not in md
    assert "malformed target_loaded event" in _section(md, "Summary")


def test_report_absent_dag_protocol_work_without_target_is_invalid(tmp_path):
    # pr-review protocol work (a builder output) recorded with no loaded target and no tree -> INVALID.
    run = _absent_dag_pr_review(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    md = render_markdown(run)
    assert "**Status:** INVALID" in md
    assert "## Review target" not in md
    assert "## Review result" not in md
    assert "without a loaded target" in _section(md, "Summary")


def test_report_absent_dag_late_target_after_protocol_is_invalid(tmp_path):
    # A target loaded AFTER protocol work began is late -> INVALID even without a tree.
    run = _absent_dag_pr_review(tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    _load_target_manifest(run, _LOCAL_TARGET)
    md = render_markdown(run)
    assert "**Status:** INVALID" in md
    assert "target loaded after" in _section(md, "Summary")


def test_report_absent_dag_source_issue_reported_once(tmp_path):
    # A forged source snapshot on a valid revision-unbound diff-file target renders INVALID with the
    # single source issue listed exactly ONCE — the added target-event layer must not duplicate the
    # centrally-reported source-materialization message.
    run, sha = _pr_review_with_target(tmp_path, _DIFF_TARGET)
    run.append("source_materialized", kind="diff-file", target_sha256=sha, archive_sha256="d" * 64)
    md = render_markdown(run)
    assert "**Status:** INVALID" in md
    assert _section(md, "Summary").count("revision-unbound diff-file target") == 1


def test_report_absent_dag_corrupt_workflow_metadata_is_invalid(tmp_path):
    # Corrupt workflow metadata still fails closed on the absent-tree path (handled BEFORE the target
    # checks), rendering INVALID with a workflow-metadata issue — never a target message, never a crash.
    run = _absent_dag_pr_review(tmp_path)
    _load_target_manifest(run, _LOCAL_TARGET)

    def mutate(records):
        for r in records:
            if r.get("event") == "run_start":
                r["workflow"] = "bogus"
    _rewrite_run_events(run, mutate)
    md = render_markdown(run)  # must not raise
    assert "**Status:** INVALID" in md
    assert "workflow" in md.lower()
    assert "## Review result" not in md
