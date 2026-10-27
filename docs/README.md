# External to Managed Table Migration: Documentation

This is the canonical, narrative documentation for the repository. It explains what the
tool does, how it is built, and how to run a migration safely at scale. It links down into
the authoritative contract docs rather than restating them.

The tool converts Unity Catalog **EXTERNAL** tables to **MANAGED** tables with
`ALTER TABLE ... SET MANAGED`, packaged as a single **Databricks Asset Bundle**. It
discovers, assesses, plans, dry-runs, freezes, authorizes the convert, converts, validates,
unfreezes, re-shares, enables Compatibility Mode and Predictive Optimization, cleans up, and can roll
back, all driven by a control **manifest** Delta table (no
hardcoded table lists) with a full audit trail.

> **You build the tooling; a human runs the conversion.** Nothing here authorises running
> `SET MANAGED`, `UNSET MANAGED`, or `VACUUM` from an editor. A person triggers the deployed
> Workflow jobs. See [Overview](./01-overview.md#golden-safety-rules).

## Ways to use this repo

There is one engine and three front doors onto it.

1. **Run the stages straight from the notebooks.** Deploy the bundle, then trigger the
   stage jobs (`backend/notebooks/00..09*`, plus the `11*` bulk family for parallel-safe
   batches) with `databricks bundle run` or from the Jobs UI. The interactive cockpit
   notebook (`backend/notebooks/09_cockpit.py`) is the guided driver, and
   `11_bulk_cockpit.py` is its app-free bulk twin. See
   [Using the notebooks](./11-using-notebooks.md).
2. **Drive it through the app UI.** The `app/` control-plane (Databricks App:
   AppKit/React/TypeScript) reads the same control Delta tables and triggers the same jobs.
   It reflects state and launches one job per click; the job still enforces every gate. See
   [Using the app](./12-using-the-app.md).
3. **Drive it through Genie Code (MCP).** The app also exposes the control plane as a custom **MCP
   server** (`POST /mcp`), so an LLM can call governed tools (reads, stages, gated conversions behind
   `confirm=true`). Connect it directly in Genie Code as a *Custom MCP server* — no account admin
   needed (a Unity Catalog MCP Service registration is an optional governance alternative). See
   [Genie Code MCP](./15-genie-code-mcp.md).

All surfaces talk to the engine only through the control tables and the job parameters.
That interface is the authoritative contract, documented under `backend/docs/`.

At estate scale, either front door can convert a whole **parallel-safe batch** of waves at
once (the `job_11*` bulk jobs and the app's `/bulk` screen) instead of one wave at a time.
These are still separate, deliberate launches, with streaming waves kept serial. See
[Bulk convert](./14-bulk-convert.md).

## Table of contents

| Page | What it covers |
|---|---|
| [00 - Installation](./00-installation.md) | Prerequisites, who needs which permissions, bundle variables, the two install paths, the success check, troubleshooting, and uninstalling. |
| [01 - Overview](./01-overview.md) | What the migration is; the golden safety rules and non-negotiable `SET MANAGED` facts; the three surfaces (jobs, app, Genie Code). |
| [02 - Architecture](./02-architecture.md) | Manifest-driven design; pure logic + thin Spark edges; module map; the one bundle; two system diagrams. |
| [03 - Discovery and signals](./03-discovery-and-signals.md) | The profiler: discovering EXTERNAL tables, hard-signal eligibility, the signals it writes, the config handshake. |
| [04 - Streaming](./04-streaming.md) | Detecting, stopping, converting, and restarting streaming readers/writers. |
| [05 - External access and buckets](./05-external-access-and-buckets.md) | External (non-Databricks) writers/readers, path-based access, below-floor runtimes, and how the flags feed the planner. |
| [06 - Wave planning](./06-wave-planning.md) | Buckets, precedence, pilot cap, grain and chunking: how one schema becomes several waves, and how waves are colored into parallel-safe batches. |
| [07 - The freeze bracket](./07-freeze-bracket.md) | Compaction attribution, reversible freeze, the ledger, exact restore, and the convert-time coverage gates. |
| [07b - Convert permissions and Authorize](./07b-convert-permissions.md) | What the convert identity needs on each table, the stored `convert_authority` verdict, Authorize (`03d`) granting only what is missing, and cleanup revoking it. Ownership never changes. |
| [08 - Conversion and validation](./08-conversion-and-validation.md) | Dry-run gates, the fail-closed managed-location gate, the atomic per-wave convert (and the bulk fan-out), and validation. |
| [08c - Post-convert maintenance policy](./08c-post-convert-maintenance.md) | The optimize finish step: enabling Predictive Optimization (opt-in) and automatic liquid clustering (opt-out) on the converted set; the `optimize_run_at` stamp; retiring the redundant OPTIMIZE or VACUUM job; the rollback reversal of those deltas. |
| [09 - Rollback and cleanup](./09-rollback-and-cleanup.md) | The 14-day rollback window and the day-14 VACUUM cleanup. |
| [10 - Cockpit and conductor](./10-cockpit-and-conductor.md) | The recommend/approve/launch control loop (one wave end to end: finish the current wave before the next), the runbook, the digest, and the app-free bulk cockpit. |
| [11 - Using the notebooks](./11-using-notebooks.md) | Stage order (incl. the `11*` bulk family), `bundle` commands, and safe convert invocation. |
| [12 - Using the app](./12-using-the-app.md) | The app as UI orchestration: pages (incl. Bulk Convert), reads, the one narrow write, job triggers, and finishing a wave (finish steps, Defer / Reopen). |
| [13 - Estate migration playbook](./13-estate-migration-playbook.md) | The step-by-step "migrate an estate safely at scale" runbook that ties it all together. |
| [14 - Bulk convert](./14-bulk-convert.md) | Converting non-conflicting waves in parallel: parallel-safe batches, the `11*` jobs, the app-free cockpit, and the `/bulk` screen. |
| [15 - Genie Code MCP](./15-genie-code-mcp.md) | Exposing the control plane to Genie Code / AI Playground as a custom MCP server: the v2 tool tiers, the recommended direct-connect path (no account admin) + the CORS-persist gotcha, and the Unity Catalog MCP Service governance alternative (not bundle-managed). |
| [16 - AI/BI dashboard and Genie space](./16-dashboard-and-genie.md) | The read-only dashboard pages and how to edit them safely, the Genie space build and knowledge library, and their bundle variables. |

## Authoritative contracts (do not duplicate; link)

These docs under `backend/docs/` and `app/docs/` are the source of truth for the interface
between the jobs and the app. This narrative summarises them and links back; it never
restates full column lists or full parameter tables.

- [`backend/docs/contracts/control-tables.md`](../backend/docs/contracts/control-tables.md):
  the column contract for every control-plane Delta table and view (schema, writer, readers,
  merge keys, idempotency).
- [`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md): the invoke and
  operational contract for every Workflow job (every parameter, default, selector, gate, and
  side effect).
- [`backend/docs/state-machine.md`](../backend/docs/state-machine.md): the per-table
  lifecycle, every state and every legal transition.
- [`backend/docs/architecture.md`](../backend/docs/architecture.md): the manifest-driven
  design and the deploy/config handshake, at engineering depth.
- [`app/docs/control_plane.md`](../app/docs/control_plane.md): the app's functional and
  journey specification ("the app reflects; the job enforces").
- [`app/docs/design-deltas.md`](../app/docs/design-deltas.md): where the built app
  deliberately diverges from its mockup, plus its read/write/trigger conventions.

The root [`README.md`](../README.md) is the short front door (what it is, install, a first
migration); the detail lives on these pages. Engineering conventions live in
[`AGENTS.md`](../AGENTS.md).
