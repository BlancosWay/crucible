"""F10: a behavioral end-to-end pr-review CLI pipeline test.

Drives the REAL crucible CLI from a normalized LOCAL target through source materialization and the
symmetric two-peer review to the deterministic ``review-result`` recommendation — exercising actual
behavior (git range normalization, pinned-source materialization, symmetric consensus, and the
recommendation projection), not the prose/structure tokens the skill-doc guards check. It reuses the
CLI test helpers rather than re-implementing the whole flow.
"""
import json
from pathlib import Path

from tests.test_cli import (
    _accepted_finding,
    _diverged_repo,
    _events,
    _head_archive,
    _load,
    _load_local_target,
    _run,
    _settle_symmetric_plan,
    _start,
    _symmetric_dep_verdict,
)

NODE = "review-node"


def _pr_review_local_run(tmp_path, *, final_review=False):
    """The real acquisition + materialization half of the pipeline:
    ``init-run --workflow pr-review`` (``final_review`` configurable so the run legitimately reaches
    ``review-result`` without a FINAL gate) -> ``normalize-target local`` over a diverged git repo ->
    ``load-target`` -> ``materialize-target`` -> ``load-dag`` with one node. Returns the run dir.
    """
    cfg = Path(tmp_path) / "pipeline-cfg.json"
    cfg.write_text(json.dumps({"final_review": final_review}))
    r = _run(["init-run", "--goal", "review the PR", "--base-dir", str(tmp_path),
              "--workflow", "pr-review", "--config", str(cfg)])
    assert r.returncode == 0, r.stderr
    run_dir = r.stdout.strip()
    repo = _diverged_repo(tmp_path)
    _load_local_target(run_dir, tmp_path, repo)          # normalize-target local + load-target
    archive = _head_archive(tmp_path, repo)
    assert _run(["materialize-target", "--run", run_dir,
                 "--archive", str(archive)]).returncode == 0
    _load(run_dir, tmp_path, {NODE: "pending"})          # load-dag with one node
    return run_dir


def _authoritative_target_sha(run_dir):
    from crucible.target import target_from_events, target_sha256
    return target_sha256(target_from_events(_events(run_dir)))


def test_pr_review_pipeline_local_to_review_result_request_changes(tmp_path):
    run_dir = _pr_review_local_run(tmp_path, final_review=False)

    # The pinned head snapshot was materialized (exercises the materialize path), and the crash-repair
    # receipt is ADJACENT to RUN/source, not inside the reviewed tree.
    assert (Path(run_dir) / "source" / "app.py").read_text() == "feature\n"
    assert (Path(run_dir) / "source.receipt.json").exists()
    assert not (Path(run_dir) / "source" / "source.receipt.json").exists()

    _settle_symmetric_plan(run_dir, tmp_path)            # PLAN symmetric consensus (two peers)
    _start(run_dir, NODE)

    # The dependency review reaches consensus (peers raise no objection) while carrying a BLOCKING
    # accepted finding — which the deterministic recommendation projection must turn into
    # REQUEST_CHANGES.
    finding = _accepted_finding(f"dep:{NODE}", "PR1", "major")
    candidate = {"summary": f"{NODE} review", "findings": [finding]}
    r = _symmetric_dep_verdict(run_dir, tmp_path, NODE, candidate=candidate)
    assert r.stdout.strip() == "CONSENSUS", r.stdout + r.stderr
    assert _run(["set-status", "--run", run_dir, "--node", NODE,
                 "--status", "done"]).returncode == 0

    # accepted-findings assembles the accepted union; review-result derives the deliverable.
    assert _run(["accepted-findings", "--run", run_dir]).returncode == 0
    res = _run(["review-result", "--run", run_dir])
    assert res.returncode == 0, res.stderr
    result = json.loads(res.stdout)

    assert result["workflow"] == "pr-review"
    assert result["recommendation"] == "REQUEST_CHANGES"          # derived from the blocking finding
    assert "PR1" in [f["id"] for f in result["findings"]]         # the seeded finding flowed through
    assert result["target_sha256"] == _authoritative_target_sha(run_dir)  # self-bound to the target


def test_pr_review_pipeline_clean_review_recommends_approve(tmp_path):
    # A clean review (no accepted findings, no unresolved objections) deterministically recommends
    # APPROVE — the full pipeline still runs end to end from the local target to review-result.
    run_dir = _pr_review_local_run(tmp_path, final_review=False)
    _settle_symmetric_plan(run_dir, tmp_path)
    _start(run_dir, NODE)
    candidate = {"summary": f"{NODE} review", "findings": []}
    assert _symmetric_dep_verdict(run_dir, tmp_path, NODE,
                                  candidate=candidate).stdout.strip() == "CONSENSUS"
    assert _run(["set-status", "--run", run_dir, "--node", NODE,
                 "--status", "done"]).returncode == 0
    result = json.loads(_run(["review-result", "--run", run_dir]).stdout)
    assert result["recommendation"] == "APPROVE"
    assert result["findings"] == []
