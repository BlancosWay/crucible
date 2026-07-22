"""Regression tests for ``scripts/check.py`` Git environment isolation (Task 2 round-1 F1).

``check.py`` self-installs as a Git ``pre-commit`` hook. Git runs hooks with its per-repository
"local" environment variables exported (``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, ...),
each pointing at the OUTER repository. Any ``git`` subprocess a check spawns — notably the pytest
suite's git-backed target tests, which run ``git`` inside their own temporary repos — would otherwise
inherit those variables and operate on the outer repo, mutating its HEAD/branches/index. ``check.py``
must scrub every variable named by ``git rev-parse --local-env-vars`` from the environment of every
check subprocess so each resolves its own repository.

These prove the property behaviorally at the ``run_check`` boundary and for a pytest subprocess,
using a real outer repo plus a real temp repo — never the repository under test.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import check


def _git(repo: Path, *args: str, env=None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True, env=env).stdout


def _clean_git_env() -> dict:
    """An INDEPENDENT read oracle: the environment with every ``GIT_*`` variable removed (a superset
    of check.py's authoritative ``git rev-parse --local-env-vars`` list) so verification reads never
    depend on the code under test."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    clean = _clean_git_env()
    _git(path, "init", "-q", "-b", "main", env=clean)
    _git(path, "config", "user.email", "t@example.com", env=clean)
    _git(path, "config", "user.name", "Tester", env=clean)
    _git(path, "config", "commit.gpgsign", "false", env=clean)
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt", env=clean)
    _git(path, "commit", "-q", "-m", "seed", env=clean)
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD", env=_clean_git_env()).strip()


def _branches(repo: Path) -> set:
    return set(_git(repo, "branch", "--format=%(refname:short)", env=_clean_git_env()).split())


def _point_git_env_at(monkeypatch, repo: Path) -> None:
    """Simulate a Git ``pre-commit`` hook environment: export the local env vars pointing at ``repo``
    so a naive subprocess would resolve THIS repo rather than its own working directory."""
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git" / "index"))


# A check payload: from cwd, create a branch pointing at HEAD — a pure ref write (no commit, so no
# git background auto-maintenance that could inherit run_check's captured pipe). This is exactly the
# ref mutation that leaked in the reported corruption (junk `feature`/`main` branches created in the
# outer repo). With a scrubbed environment it targets cwd's repo; without it, GIT_DIR names the outer
# repo and the branch is created THERE.
_BRANCH_PROG = (
    "import subprocess;"
    "subprocess.run(['git', 'branch', 'probe-branch'], check=True)"
)


def test_isolated_env_removes_git_local_vars_preserving_others(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/some/outer/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/some/outer")
    monkeypatch.setenv("GIT_INDEX_FILE", "/some/outer/.git/index")
    monkeypatch.setenv("CRUCIBLE_KEEP_ME", "yes")
    env = check._isolated_env()
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_INDEX_FILE" not in env
    # Non-Git variables are preserved verbatim (cross-platform: a plain dict copy minus git vars).
    assert env.get("CRUCIBLE_KEEP_ME") == "yes"
    assert env.get("PATH") == os.environ.get("PATH")


def test_git_local_env_vars_lists_dir_and_index_or_degrades():
    names = check._git_local_env_vars()
    # When git is available the authoritative names include the repo-local ones; when git is
    # unavailable the helper degrades to [] so the caller falls back to an unscrubbed env copy.
    if names:
        assert "GIT_DIR" in names
        assert "GIT_INDEX_FILE" in names
        assert "GIT_WORK_TREE" in names


def test_run_check_subprocess_cannot_move_outer_repo(tmp_path, monkeypatch):
    outer = _init_repo(tmp_path / "outer")
    temp = _init_repo(tmp_path / "temp")
    outer_head, outer_branches = _head(outer), _branches(outer)
    _point_git_env_at(monkeypatch, outer)

    ok, output = check.run_check("probe", [sys.executable, "-c", _BRANCH_PROG], cwd=str(temp))

    assert ok, output
    # The subprocess resolved its OWN cwd repo: the branch landed in temp ...
    assert "probe-branch" in _branches(temp)
    # ... while the outer worktree's ref store was untouched — no junk branch, HEAD unmoved.
    assert "probe-branch" not in _branches(outer)
    assert _branches(outer) == outer_branches
    assert _head(outer) == outer_head


def test_pytest_subprocess_cannot_move_outer_repo(tmp_path, monkeypatch):
    if subprocess.run([sys.executable, "-c", "import pytest"],
                      capture_output=True).returncode != 0:
        pytest.skip("pytest is not importable by this interpreter")
    outer = _init_repo(tmp_path / "outer")
    temp = _init_repo(tmp_path / "temp")
    outer_head, outer_branches = _head(outer), _branches(outer)
    # A throwaway pytest module that mutates ITS OWN repo (cwd) exactly like the target suite's
    # git-backed tests: it must resolve the temp repo even though GIT_DIR names the outer one.
    (temp / "test_probe_branch.py").write_text(
        "import subprocess\n"
        "def test_branch_in_local_repo():\n"
        "    subprocess.run(['git', 'branch', 'probe-branch'], check=True)\n"
    )
    _point_git_env_at(monkeypatch, outer)

    ok, output = check.run_check(
        "suite",
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe_branch.py"],
        cwd=str(temp))

    assert ok, output
    assert "probe-branch" in _branches(temp)
    assert "probe-branch" not in _branches(outer)
    assert _branches(outer) == outer_branches
    assert _head(outer) == outer_head
