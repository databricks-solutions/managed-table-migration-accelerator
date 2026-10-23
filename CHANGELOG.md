# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is `0.x`, a minor
release may change job parameters or control-table columns; every such change is called out under
**Changed** with the matching contract in `backend/docs/contracts/`.

## [Unreleased]

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
- Release tooling: CI (backend pytest, app typecheck / tests / lint) and a tag-driven release
  workflow that checks the tag matches the version in every versioned file.

[Unreleased]: https://github.com/databricks-solutions/managed-table-migration-accelerator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/databricks-solutions/managed-table-migration-accelerator/releases/tag/v0.1.0
