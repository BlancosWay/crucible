# Opus 4.8 Builder and GPT-5.5 Critic Defaults Design

## Goal

Change Crucible's shipped default model pairing to:

- Builder: `claude-opus-4.8` with effort `max`
- Critic: `gpt-5.5` with effort `xhigh`

Explicit `--config` overrides and all other run settings remain unchanged.

## Architecture

`config.defaults.json` remains the sole live source of shipped configuration values. The change
updates only its `builder` and `critic` objects; no Python, schema, CLI, run-log, report, or dispatch
interface changes are needed.

Runtime configuration continues to flow through `load_defaults()` and `Config.from_dict()`.
`init-run` writes the resolved pairing to the run's `config.json`, and downstream orchestration and
reporting continue to consume that per-run configuration.

## Behavior and Compatibility

New runs without model overrides use Opus 4.8 max for the Builder and GPT-5.5 xhigh for the Critic.
Partial role overrides continue to inherit the unspecified field from the new role default.

Existing runs retain the resolved values already recorded in their `RUN/config.json`. User-provided
configuration files continue to override the shipped defaults without migration or compatibility
logic.

## Documentation and History

Add an Unreleased changelog entry describing the user-visible default change. Current-facing
documentation continues to reference `config.defaults.json` rather than duplicate model literals.

Preserve released changelog entries and dated specifications and plans because they accurately
describe the defaults and decisions of earlier releases.

## Testing and Validation

Exercise the resolved empty configuration and partial Builder/Critic overrides against the shipped
defaults, then run the full `python3 scripts/check.py` governance suite. Existing tests continue to
derive expected values from `config.defaults.json`; no live test or documentation file will
duplicate the model identifiers.

## Non-Goals

- No change to model availability detection or runtime model substitution.
- No change to configuration keys, accepted effort strings, or override precedence.
- No rewrite of historical design, plan, or release documentation.
