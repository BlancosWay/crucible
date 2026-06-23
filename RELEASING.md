# Releasing Crucible

Crucible follows [Semantic Versioning](https://semver.org/). Releasing is a deliberate,
human-initiated action: pushing a `vX.Y.Z` tag that matches the manifest version turns the
recorded CHANGELOG section into a published GitHub Release. Nothing auto-bumps the version.

## Versioning

- `MAJOR` — incompatible changes to the skill/CLI contract or run-log format.
- `MINOR` — new, backward-compatible capability (new gate option, new CLI subcommand, etc.).
- `PATCH` — backward-compatible fixes.

The version lives in **two** places that must always agree (enforced by
`tests/test_version_consistency.py`):

- `.claude-plugin/plugin.json` → `version`
- `.claude-plugin/marketplace.json` → `plugins[0].version`

## Cutting a release

1. **Record the changes.** Move the `## [Unreleased]` notes into a new dated section:

   ```markdown
   ## [Unreleased]

   ## [0.2.0] - 2026-07-01
   ### Added
   - ...
   ```

2. **Bump both manifests** to the new version (they must match).

3. **Verify locally:**

   ```bash
   python3 scripts/check.py
   python3 -m unittest tests.test_version_consistency
   python3 scripts/changelog.py section 0.2.0   # prints the notes that will be published
   ```

   The **Release dry run** job in `.github/workflows/validate.yml` runs these same steps on every
   PR/push, so a broken release is caught before tagging.

4. **Open a PR, let it merge** once the required checks pass.

5. **Tag the merged commit** on `main` and push the tag:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

   `.github/workflows/release.yml` then verifies the tag matches the manifest version, rebuilds the
   notes from `CHANGELOG.md`, and publishes the GitHub Release.

## Notes

- The tag (`v0.2.0`) and the manifest version (`0.2.0`) must match exactly, or the release job
  fails by design.
- Release notes come straight from the matching `## [0.2.0]` CHANGELOG section; keep it non-empty.
