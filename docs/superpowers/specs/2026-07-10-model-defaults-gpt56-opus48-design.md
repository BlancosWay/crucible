# GPT-5.6 Sol Builder and Opus 4.8 Critic Design

## Goal

Change Crucible's default model pairing to:

- Builder: `gpt-5.6-sol` with effort `max`
- Critic: `claude-opus-4.8` with effort `max`

Explicit `--config` overrides and all other run settings remain unchanged.

## Scope

This is a minimal consistency update. Change the executable defaults, the existing assertions that
cover those defaults, the example configuration, and current-facing documentation that states the
default pairing. Add an Unreleased changelog entry for the new behavior.

Preserve older changelog entries and dated design or implementation documents because they
accurately describe prior releases and decisions.

## Behavior and Compatibility

`Config.from_dict({})` returns the new pairing. Partial nested overrides continue to inherit the
unchanged sibling field from the new role default. Run reports continue to render the effective
configuration without format changes.

No configuration keys, command-line arguments, run-log fields, or dispatch interfaces change.

## Testing

Update the existing config tests for the new defaults and partial-override inheritance. Update the
report assertion to require the new model identifiers. Run the targeted config and report tests,
then the repository's full `python3 scripts/check.py` governance suite.
