<!-- Thanks for contributing to Crucible! -->

## What does this change?


## Why?


## Checklist
- [ ] `python3 scripts/check.py` passes locally (structural validation, links, pytest suite).
- [ ] New/changed behavior has tests (TDD); `python3 -m pytest -q` is green.
- [ ] If the skill/command/prompts changed: the two-model loop contract in
      `skills/crucible/SKILL.md` and `references/*.md` is still consistent.
- [ ] Updated `CHANGELOG.md` under `## [Unreleased]` for any shipped-path change
      (`scripts/`, `skills/`, `commands/`), or used `[skip changelog]` if truly N/A.
