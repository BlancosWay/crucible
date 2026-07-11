# Configuration Defaults Source of Truth Design

## Problem

Crucible currently declares defaults in `scripts/crucible/config.py` and repeats the same values in
`config.example.json`, tests, reports, platform instructions, commands, and user documentation.
The v0.13.0 model swap therefore changed 11 product files. The Crucible workflow also added a design
and implementation-plan document, bringing the merged PR to 13 files.

Those edits were necessary to keep the existing duplicated surfaces consistent, but the duplication
is the architectural problem. A default-value change should not require synchronized literal edits.

## Goal

Make `config.defaults.json` the only live source of shipped configuration values. After this
refactor, changing a Builder or Critic default requires:

1. one functional edit to `config.defaults.json`; and
2. a changelog entry when repository release policy requires recording the user-visible change.

Tests, runtime code, examples, commands, and current-facing documentation must not repeat the model
identifiers.

## Architecture

Rename `config.example.json` to `config.defaults.json` and load it from
`scripts/crucible/config.py` using a path resolved relative to the module, not the current working
directory. JSON remains the format because Crucible already uses the Python standard library's JSON
support and should not add a YAML dependency.

`config.py` continues to own the schema and validation rules. The JSON file owns values only:

- required top-level keys remain fixed by code;
- `builder` and `critic` must each contain exactly `model` and `effort`;
- existing type, severity, round-cap, and policy validation remains unchanged;
- explicit `--config` files remain partial overrides merged over the loaded defaults.

Each `init-run` invocation already writes the fully resolved configuration to `RUN/config.json`.
That per-run file becomes the orchestration authority after run initialization, including explicit
overrides.

## Documentation and Orchestration

Current-facing documentation will describe configuration keys without embedding shipped values and
will link to `config.defaults.json` for the current defaults. The defaults file itself is also a
valid full `--config` template; existing inline examples continue to demonstrate partial overrides.

`skills/crucible/SKILL.md` and `references/platform-notes.md` will require the orchestrator to read
`RUN/config.json` after `init-run` and dispatch the Critic using its resolved `critic.model` and
`critic.effort`. This is safer than consulting static documentation because the run may have
overrides.

`AGENTS.md`, `CLAUDE.md`, `commands/crucible.md`, and installation guidance will point to the
authoritative defaults rather than restating model names or effort values.

Historical changelog entries and dated design or implementation documents remain unchanged because
they accurately describe older releases.

## Testing and Validation

Configuration tests will derive expected default values from the loaded `DEFAULTS` mapping instead
of hardcoding model identifiers. They will also cover:

- loading the shipped JSON file;
- rejecting a missing, malformed, non-object, incomplete, or structurally invalid defaults file;
- preserving nested partial-override behavior;
- rendering the resolved Builder and Critic values in run reports.

Structural validation and CI's JSON syntax check will require `config.defaults.json`. A regression
test will scan current-facing docs and orchestration files to ensure the model identifiers from the
defaults JSON are not copied into them. Separate tests will require the skill and platform notes to
read `RUN/config.json`.

The full `python3 scripts/check.py` governance suite remains the completion gate.

## Migration and Compatibility

There is no CLI or run-log format change. Existing user override files continue to work unchanged.
The only repository-path migration is `config.example.json` to `config.defaults.json`; live links
will be updated in the same change. Because `config.defaults.json` contains the complete valid
configuration, users who previously copied the example can copy the defaults file instead.

If the shipped defaults file is missing or invalid, Crucible must fail explicitly rather than
silently falling back to stale code values.
