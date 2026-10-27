## What and why

<!-- One or two sentences: what changes, and the problem it solves. -->

## How it was tested

<!-- Commands run and what you saw. `make ci` alone is not enough for a gate or executor change. -->

## Checklist

- [ ] `make ci` passes locally (backend tests, docs site build, app typecheck, tests and lint — the CI workflow runs the same checks).
- [ ] `databricks bundle validate -t dev --profile <profile>` passes if bundle, job or dashboard config changed.
- [ ] A changed control-table column or job parameter is reflected in `backend/docs/contracts/control-tables.md` / `jobs.md`.
- [ ] New manifest columns are in both `MANIFEST_COLUMNS` and `_DEFERRED_DEFAULTS`; new pure logic has a `backend/src/tests/test_*.py`.
- [ ] No secrets, workspace hosts / IDs, bucket names or profile names in tracked files; no hardcoded table lists.
- [ ] Nothing here runs `SET MANAGED` / `UNSET MANAGED`, hard-deletes data, or VACUUMs inside the 14-day window outside the gated jobs.
- [ ] User-visible change noted under `## [Unreleased]` in `CHANGELOG.md`.
- [ ] A new signal, knob or stage is documented on its `docs/` page (`README.md` only if install, permissions or the first-migration steps changed).
