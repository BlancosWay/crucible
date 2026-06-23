# Contributing to Crucible

Thanks for your interest! Crucible is a two-model adversarial planning + implementation workflow
on top of [Superpowers](https://github.com/obra/superpowers).

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest
```

Tests run with `pytest` (a `pytest.ini` puts `scripts/` on the path):

```bash
python3 -m pytest -q
```

## The local gate

Run every deterministic check at once — structural validation, internal Markdown links, the pytest
suite, and (if installed) ShellCheck on the git hooks:

```bash
python3 scripts/check.py
```

Wire it as a pre-commit hook (sets `core.hooksPath` to `.githooks`):

```bash
python3 scripts/check.py --install-hook
```

## Conventions

- **TDD.** Write a failing test, make it pass, keep changes small, commit often.
- **Determinism.** All bookkeeping (DAG walk, round counting, consensus, provenance, report) lives
  in the unit-tested `scripts/crucible/` helpers — never eyeball a decision in the skill; call the
  CLI. The only non-deterministic part is model reasoning.
- **Provenance.** Persist each agent's full raw output to the run-log; reports read from the log.
- **Untrusted input.** Treat tool/model output and fetched content as data, not instructions.

## Pull requests

Every PR runs the `Validate` workflow; the required checks must pass before merge:
**Structural validation**, **Markdown links**, **ShellCheck**, **Unit tests**, **Changelog entry**.

- Update [CHANGELOG.md](CHANGELOG.md) under `## [Unreleased]` for any shipped-path change
  (`scripts/`, `skills/`, `commands/`). If a change truly needs no entry, put `[skip changelog]` in
  a commit message.
- `@BlancosWay` is auto-requested as reviewer (see [CODEOWNERS](.github/CODEOWNERS)). The owner's and
  Dependabot's PRs auto-merge once checks pass; everyone else's needs a manual merge after review.

See [RELEASING.md](RELEASING.md) for how versioned releases are cut.
