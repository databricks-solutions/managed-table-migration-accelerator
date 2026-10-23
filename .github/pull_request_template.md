## What and why

<!-- One or two sentences: what changes, and the problem it solves. -->

## How it was tested

<!-- Commands run and what you saw. "CI" alone is not enough for a gate or executor change. -->

## Checklist

- [ ] `python -m pytest backend/src/tests/ -q` passes (and app `npm run typecheck`, `npx vitest run`, `npx eslint client/src server` if `app/` changed).
- [ ] `databricks bundle validate -t dev --profile <profile>` passes if bundle, job or dashboard config changed.
- [ ] A changed control-table column or job parameter is reflected in `backend/docs/contracts/control-tables.md` / `jobs.md`.
- [ ] New manifest columns are in both `MANIFEST_COLUMNS` and `_DEFERRED_DEFAULTS`; new pure logic has a `backend/src/tests/test_*.py`.
- [ ] No secrets, workspace hosts / IDs, bucket names or profile names in tracked files; no hardcoded table lists.
- [ ] Nothing here runs `SET MANAGED` / `UNSET MANAGED`, hard-deletes data, or VACUUMs inside the 14-day window outside the gated jobs.
- [ ] User-visible change noted under `## [Unreleased]` in `CHANGELOG.md`.
