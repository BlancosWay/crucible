#!/usr/bin/env python3
"""Unified governance entrypoint for Crucible.

One command that runs every deterministic check (structural validation, markdown
links, the pytest suite, and — when available — shellcheck on the git hooks) and
aggregates the result, plus a self-installing pre-commit hook. Mirrors the
TradingDesk ``scripts/check.py`` governance pattern, adapted to Crucible's stack
(a ``pytest`` suite under ``tests/`` with ``pythonpath=scripts``).

Pure stdlib for orchestration; the suite step uses whichever interpreter has
``pytest`` (prefers a repo ``.venv``). Exit 0 iff every check passes, else 1.

Run:
  python3 scripts/check.py              # run all checks
  python3 scripts/check.py --install-hook   # wire .githooks/pre-commit (sets core.hooksPath)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# Stdlib-only checks run with the SAME interpreter that launched check.py.
CHECKS: List[Tuple[str, List[str]]] = [
    ("structural", [sys.executable, "tests/validate_structure.py"]),
    ("links", [sys.executable, "tests/check_links.py"]),
]


def _pytest_python() -> Optional[str]:
    """Return a python interpreter that can import pytest, preferring a repo .venv."""
    candidates = [
        ROOT / ".venv" / "bin" / "python",          # POSIX venv
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows venv
        Path(sys.executable),
    ]
    for c in candidates:
        if Path(c).exists():
            probe = subprocess.run([str(c), "-c", "import pytest"], capture_output=True)
            if probe.returncode == 0:
                return str(c)
    return None


def run_check(name: str, argv: List[str], cwd: str) -> Tuple[bool, str]:
    """Run one check as a subprocess; return (passed, combined output)."""
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _shell_hooks() -> List[str]:
    return sorted(str(p.relative_to(ROOT)) for p in (ROOT / ".githooks").glob("*") if p.is_file())


def _build_registry() -> List[Tuple[str, List[str]]]:
    """Base checks + pytest suite (if available) + shellcheck on hooks (if available)."""
    checks = list(CHECKS)
    py = _pytest_python()
    if py:
        checks.append(("suite", [py, "-m", "pytest", "-q"]))
    if shutil.which("shellcheck"):
        hooks = _shell_hooks()
        if hooks:
            checks.append(("shellcheck", ["shellcheck", "--severity=error", *hooks]))
    return checks


def run_all() -> int:
    """Run every check; print a per-check PASS/FAIL line and a summary. Return 0/1."""
    failed: List[str] = []
    for name, argv in _build_registry():
        ok, output = run_check(name, argv, cwd=str(ROOT))
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)
            sys.stdout.write(output if output.endswith("\n") else output + "\n")
    # The pytest suite is a core gate, not optional: if no interpreter can import
    # pytest, that is a FAILURE (don't silently pass with the suite unrun).
    if _pytest_python() is None:
        print("FAIL  suite")
        print("pytest is not available — install it (`pip install pytest`) or create a repo "
              ".venv with pytest. The test suite is a required gate and was not run.")
        failed.append("suite")
    if shutil.which("shellcheck") is None:
        print("note  shellcheck not installed — skipped (CI still runs it)")
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("\nAll checks passed.")
    return 0


_HOOK_BODY = """\
#!/usr/bin/env bash
# Crucible pre-commit guard — runs the unified deterministic checks.
# Installed via: python3 scripts/check.py --install-hook
set -u
ROOT="$(git rev-parse --show-toplevel)"
exec python3 "$ROOT/scripts/check.py"
"""


def install_hook(repo_root: str) -> str:
    """Point git at .githooks and ensure an executable pre-commit hook exists."""
    root = Path(repo_root)
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=str(root), check=True)
    hooks_dir = root / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "pre-commit"
    if not hook.exists():
        hook.write_text(_HOOK_BODY, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | 0o111)
    return str(hook)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Crucible unified governance checks.")
    p.add_argument("--install-hook", action="store_true",
                   help="set core.hooksPath to .githooks and ensure the pre-commit hook")
    args = p.parse_args(argv)

    if args.install_hook:
        hook = install_hook(str(ROOT))
        print(f"Installed pre-commit hook at {hook} (core.hooksPath=.githooks).")
        return 0
    return run_all()


if __name__ == "__main__":
    raise SystemExit(main())
