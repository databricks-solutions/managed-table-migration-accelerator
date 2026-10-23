# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is `0.x`, a minor
release may change job parameters or control-table columns; every such change is called out under
**Changed** with the matching contract in `backend/docs/contracts/`.

## [Unreleased]

### Changed

- **Shorter README, fuller docs.** `README.md` is now a short front door: what the tool does, how it
  works (a lifecycle diagram and app screenshots), prerequisites, the corrected convert
  permissions (ownership **or** `MANAGE`, plus `SELECT` + `MODIFY`; ownership never changes), two
  install paths with a success check, a first migration, and rollback. The detail moved into
  `docs/`, including two new pages: `docs/00-installation.md` (install, permissions, configuration,
  troubleshooting) and `docs/16-dashboard-and-genie.md`. `app/docs/app-sp-grants.md` drops the
  stale claim that `SET MANAGED` requires table ownership.
- **Permissions, pictured.** The README's permissions section is now a one-glance diagram and three
  bullets. `docs/00-installation.md` adds a diagram of which identity acts on what and a
  step-by-step table of what each step needs; `docs/07b-convert-permissions.md` adds the lifecycle
  of an Authorize grant through the 14-day window to its revoke at cleanup.
- **Settings → "What you can see" now generates grants.** Tick a discovered schema, or type one,
  and its Tier 2 grant SQL appears before the first profiler run. Previously "Add scope" only
  added a badge. Discovered schemas are unticked suggestions (Tier 2 includes `MANAGE`), and the
  selection is kept in the browser. The scan lists each catalog's table summaries eight at a time
  within a 20-second deadline instead of walking every schema one call at a time; a stopped scan
  says how much it covered instead of looking complete. "No scopes yet." no longer shows while
  the scan is still running, invalid typed names explain why, and typed scopes can be removed.
- **Settings → "What you can see" marks schemas the profiler already lists.** Their chips read
  "already covered by the scan" instead of offering a tick that changed nothing, because their
  grants are already in Step 2. "Select all" and the selected count skip them.
  `GET /api/grants/plan` accepts an optional `scopes` parameter (validated server-side, max 50).
- **Genie Code, front and center.** The README gains a "Drive it with Genie Code" section (what the
  app's MCP server is, how to connect it, example prompts, the `confirm=true` safety model).
  `docs/15-genie-code-mcp.md` adds a "What you can ask" table mapping prompts to tools; the
  overview, app, installation (connect step and the `mcp_tier` variable) and Genie knowledge
  pages now name Genie Code as the third way to drive the migration and separate it from the
  app's Ask Genie button.

## [0.1.0] - 2026-10-22

First public release of the Unity Catalog EXTERNAL → MANAGED table migration accelerator, packaged
as one Databricks Asset Bundle.

### Added

- **Manifest-driven stage jobs** (`backend/`): profiler (`00`), optional source scan (`01`), wave
  planner (`02`), dry run (`03`), convert (`04`, `ALTER TABLE ... SET MANAGED`, selector-pinned per
  wave), validate (`05`), rollback (`06`, `UNSET MANAGED` within the 14-day window) and cleanup
  (`07`), all driven by the `table_manifest` control table with a full `audit_log`.
- **Pre-convert safety gates**: name-based-access holds (path-based access with per-consumer
  remediation and an eligibility-gated redirect opt-in), recent OPTIMIZE / VACUUM / write
  recency checks, machine-verified compaction and stream stop coverage, a fail-closed
  managed-location gate, and convert-authority checks with an optional, verified Authorize step
  (`03d`).
- **Freeze / unfreeze bracket** (`03c` / `05b`) with a never-deleted `freeze_ledger`, exact
  restore on rollback and forward-only unfreeze.
- **Action queue and runbook**: `migration_actions` work queue (15 action kinds), the
  `migration_runbook` view, and a read-only cockpit export.
- **Finish steps**: Delta Sharing re-share (`08`), external readers / Compatibility Mode (`08b`),
  and Predictive Optimization plus liquid clustering (`08c`).
- **Wave planner v2** with risk buckets, a capped pilot, and parallel-safe batches for the bulk
  campaign (`11*` jobs, `11e` app-free cockpit).
- **Read-only conductor** (`08_conductor`) that recommends the next step after every stage, with
  per-scope focus and team pins.
- **Admin control-plane app** (`app/`, Databricks App on AppKit) over the same control tables, plus
  a Genie Code MCP server at `POST /mcp`.
- **AI/BI scoping dashboard and Genie space**.
- **Opt-out usage telemetry** with a closed vocabulary (no names, paths, IDs or SQL).
- **Generic `e2e` bundle target** and environment-driven live e2e drivers (`.env.e2e.example`).
- Local release tooling: `make ci` (backend pytest, app typecheck / tests / lint) and
  `make release TAG=vX.Y.Z` (`scripts/release.py`), which checks the tag matches the version in
  every versioned file and publishes the GitHub Release from this changelog.

[Unreleased]: https://github.com/databricks-solutions/managed-table-migration-accelerator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/databricks-solutions/managed-table-migration-accelerator/releases/tag/v0.1.0
