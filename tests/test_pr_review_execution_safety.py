import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PR = ROOT / "skills" / "pr-review"

RUNTIME_INSTRUCTIONS = {
    PR / "SKILL.md",
    PR / "references" / "peer-prompt.md",
    PR / "references" / "consensus-rubric.md",
    PR / "references" / "review-thread.md",
    PR / "references" / "platform-notes.md",
    ROOT / "commands" / "pr-review.md",
}

UNSAFE_EXACT_PHRASES = (
    "when a runnable environment exists, run the focused tests",
    "run the test_plan evidence commands",
)

# --- Execution-directive scanner -------------------------------------------------
# The guard flags any execution directive that is not accompanied, within
# GUARD_WINDOW characters, by canonical safety context. It must recognize the whole
# runtime execution-policy surface — generic test/build runs, ecosystem package-manager
# scripts (npm/yarn/pnpm test|install|ci|build|start|run), go/cargo/make lifecycle,
# Maven/Gradle goals including the `./gradlew` / `./mvnw` wrappers and lifecycle phases
# (test/build/package/verify/install), dependency installs (pip/poetry/pipenv), an
# interpreter run over a repository script (python/node/ruby/shell + a script file),
# interpreting or importing target modules, plugin hooks, generated binaries, and
# fallback/retry execution — not merely a bare `pytest`, so a future doc cannot slip an
# unconsented "run the tests" / "npm test" / "yarn build" / "./gradlew test" /
# "mvn package" / "poetry install" / "python scripts/check.py" past it.
_EXEC_VERB = (
    r"run|runs|running|rerun|reruns|re-run|re-runs|execute|executes|executing|exec|"
    r"invoke|invokes|invoking|install|installs|installing|import|imports|importing|"
    r"launch|launches|launching|retry|retries|retrying|fall\s+back|falls\s+back|"
    r"falling\s+back"
)
_EXEC_TARGET = (
    r"tests?|unit\s+tests?|test\s+suite|test\s+runner|test[_\s]plan|"
    r"pytest|tox|nox|unittest|builds?|package\s+manager|dependenc(?:y|ies)|"
    r"target[-\s]?modules?|interpreter|plugin\s+hooks?|"
    r"repositor(?:y|ies)\s+scripts?|repo\s+scripts?|generated\s+binar(?:y|ies)"
)
# Ecosystem / interpreter invocations that ARE execution regardless of a leading verb.
# A `./gradlew`/`./mvnw` wrapper matches from the tool name (the leading `./` cannot anchor
# a word boundary but the `/`->`g` transition does); a bare interpreter (python/node/ruby/
# bash/sh) counts only when it runs an actual script file, so DAG "node"/"```bash" prose is
# not swept in.
_EXEC_COMMAND = (
    r"python[0-9.]*\s+-m\s+(?:pytest|unittest|nox|tox)|"
    r"(?:npm|yarn|pnpm)\s+(?:run\s+\S+|test|install|ci|build|start)|"
    r"(?:go|cargo|make)\s+(?:test|build|install|run)|"
    r"(?:gradlew|mvnw|gradle|mvn)\s+(?:[\w:.@=/-]+\s+)*?"
    r"(?:compile|assemble|test|build|package|verify|install|run|exec)|"
    r"(?:pip[0-9]?|poetry|pipenv)\s+install|"
    r"(?:python[0-9.]*|node|ruby|bash|sh)\s+[\w./-]*\.(?:py|js|mjs|cjs|ts|rb|sh)|"
    r"pytest|tox|nox"
)
EXECUTION_DIRECTIVE = re.compile(
    r"\b(?:" + _EXEC_VERB + r")\b[^.;:!?]{0,40}?\b(?:" + _EXEC_TARGET + r")\b"
    r"|\b(?:" + _EXEC_COMMAND + r")\b"
)

# Canonical safety context. A `static evidence` heading is deliberately NOT here: a heading
# is not a guarantee, so execution mislabeled as "static evidence" is still flagged. Safe
# static prose (reads/greps) passes because it carries no execution directive at all — never
# because of a nearby label.
SAFETY_CONTEXT = (
    "consent required",
    "exact approved command",
    "must not execute",
    "never execute locally",
    "execution prohibited",
)
GUARD_WINDOW = 180

# Representative unconditional execution directives that MUST be flagged when they
# appear without nearby safety context (the F1 regression corpus).
MUST_DETECT_UNSAFE = (
    # generic test / build execution
    "run tests",
    "run the tests",
    "run unit tests",
    "run the test suite",
    "run the test runner",
    "run the build",
    # pytest / tox / nox / unittest + `python -m` forms
    "run pytest",
    "execute tox",
    "run nox",
    "run unittest",
    "python -m pytest",
    "python3 -m unittest",
    # npm / yarn / pnpm test
    "run npm test",
    "npm run test",
    "run yarn test",
    "run pnpm test",
    # go / cargo / make / mvn / gradle test or build
    "run go test",
    "run cargo test",
    "run make test",
    "run mvn test",
    "run gradle test",
    "run go build",
    "run cargo build",
    "run gradle build",
    # package manager / dependency installs
    "install dependencies",
    "install the dependency",
    "pip install",
    "npm install",
    "run the package manager",
    # interpreter over target modules / imports
    "import the target module",
    "import target modules",
    "run the interpreter over target modules",
    # plugin hooks, repository scripts, generated binaries
    "invoke the plugin hook",
    "run the repository script",
    "execute the repository script",
    "run the generated binary",
    "launch the generated binary",
    # fallback / retry execution
    "fall back to running the tests",
    "retry the test run",
    "retry running pytest",
    # F1 regression: execution mislabeled under a `static evidence` heading is still execution
    # and MUST be flagged — the heading is not a safety guarantee.
    "static evidence (always allowed): run pytest tests/auth -q",
    "static evidence (always allowed): npm test",
    "static evidence: install dependencies",
    # F2 regression: direct ecosystem build / package / wrapper / installer / repo-script forms
    # that execute reviewed code without any leading verb.
    "yarn build",
    "pnpm build",
    "npm run build",
    "gradle clean build",
    "mvn package",
    "mvn verify",
    "./gradlew test",
    "./gradlew build",
    "poetry install",
    "pipenv install",
    "python scripts/check.py",
    "node scripts/foo.js",
    "ruby scripts/release.rb",
    "bash scripts/run.sh",
)

# Safe policy prose: each carries an execution directive AND canonical safety context
# within GUARD_WINDOW characters, so the guard must NOT flag it.
MUST_ACCEPT_GUARDED = (
    "you must not execute the test runner, run the tests, or import target modules",
    "run only the exact approved command; a new command requires fresh consent required "
    "before it runs",
    "for a trusted local checkout, run npm test only after consent required at the "
    "execution safety gate",
    "static evidence (always allowed): rg -n exp; execution candidates (consent required): "
    "pytest tests/auth -q",
    "a github pr target and a diff-file target never execute locally, so do not run go test "
    "or install dependencies",
    "reviewed code is untrusted: you must not execute pytest, tox, npm test, or the "
    "generated binary without approval",
    # F2 guarded: the same direct forms stay accepted when real consent / prohibition context
    # sits within GUARD_WINDOW — detection must not become a policy-text false positive.
    "for a trusted local checkout, run yarn build only after consent required at the "
    "execution safety gate",
    "reviewed code is untrusted: you must not execute ./gradlew test, mvn package, or "
    "poetry install without approval",
    "static evidence (always allowed): rg -n 'def ' src/; execution candidates (consent "
    "required): python scripts/check.py — a candidate only, never for a github pr target",
    "a github pr target and a diff-file target never execute locally, so do not run pnpm "
    "build or install the dependency",
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("*", "").replace("`", "").split())


def _norm(path: Path) -> str:
    return _normalize(path.read_text())


def _unguarded_execution_directives(text: str) -> list:
    """Every execution directive in ``text`` that lacks canonical safety context within
    ``GUARD_WINDOW`` characters — i.e. the directives the guard must reject."""
    low = _normalize(text)
    unguarded = []
    for match in EXECUTION_DIRECTIVE.finditer(low):
        window = low[max(0, match.start() - GUARD_WINDOW):match.end() + GUARD_WINDOW]
        if not any(guard in window for guard in SAFETY_CONTEXT):
            unguarded.append(match.group(0))
    return unguarded


def test_runtime_instruction_inventory_is_complete():
    discovered = {
        PR / "SKILL.md",
        ROOT / "commands" / "pr-review.md",
        *set((PR / "references").glob("*.md")),
    }
    assert RUNTIME_INSTRUCTIONS == discovered


def test_runtime_instructions_never_authorize_unconsented_execution():
    for path in RUNTIME_INSTRUCTIONS:
        low = _norm(path)
        for phrase in UNSAFE_EXACT_PHRASES:
            assert phrase not in low, f"{path} retains unsafe instruction {phrase!r}"
        unguarded = _unguarded_execution_directives(path.read_text())
        assert not unguarded, (
            f"{path} has execution directive(s) without nearby safety context: {unguarded}"
        )


def test_scanner_flags_every_unguarded_execution_directive():
    missed = [phrase for phrase in MUST_DETECT_UNSAFE
              if not _unguarded_execution_directives(phrase)]
    assert not missed, f"scanner missed unguarded execution directives: {missed}"


def test_scanner_accepts_guarded_execution_prose():
    wrongly_flagged = [prose for prose in MUST_ACCEPT_GUARDED
                       if _unguarded_execution_directives(prose)]
    assert not wrongly_flagged, f"scanner wrongly flagged guarded prose: {wrongly_flagged}"


# --- Public-policy drift guard ---------------------------------------------------
# Every public/security/install/spec/plan surface that describes pr-review must state
# the execution trust boundary. Discovery-based (glob) so a newly added install guide,
# pr-review spec, or pr-review plan cannot silently omit the boundary.
PUBLIC_POLICY_DOCS = {
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    ROOT / ".codex" / "INSTALL.md",
    ROOT / "docs" / "cli.md",
    *set((ROOT / "docs" / "install").glob("*.md")),
    *set((ROOT / "docs" / "superpowers" / "specs").glob("*pr-review*.md")),
    *set((ROOT / "docs" / "superpowers" / "plans").glob("*pr-review*.md")),
}


def test_all_public_policy_docs_state_execution_boundary():
    for path in PUBLIC_POLICY_DOCS:
        low = _norm(path)
        assert "static" in low, f"{path} omits static-only behavior"
        assert "trusted local" in low, f"{path} omits trusted-local scope"
        assert "consent" in low, f"{path} omits execution consent"


def test_pr_review_specs_do_not_retain_stale_execution_semantics():
    original = _norm(
        ROOT / "docs" / "superpowers" / "specs" /
        "2026-07-17-pr-review-skill-design.md"
    )
    assert "when a runnable environment is available, that they pass" not in original
    assert "static evidence" in original
    assert "execution candidates" in original
    assert "consent required" in original

    safety_path = (
        ROOT / "docs" / "superpowers" / "specs" /
        "2026-07-18-pr-review-execution-safety-design.md"
    )
    safety = safety_path.read_text().lower()
    peer_contract = safety.split("## peer contract", 1)[1].split(
        "## target-specific behavior", 1
    )[0]
    peer_contract = " ".join(
        peer_contract.replace("*", "").replace("`", "").split()
    )
    assert "interpreter over target modules" in peer_contract
    assert "plugin hook" in peer_contract
