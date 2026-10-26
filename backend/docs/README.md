# Documentation

Integration-oriented reference for the **External → Managed table migration** tooling.

This tool converts Unity Catalog `EXTERNAL` tables to `MANAGED` tables with
`ALTER TABLE ... SET MANAGED`. It has **no HTTP/REST API of its own**. Its integration
surface is made of four things, and each is documented here as a public API:

1. **Control-plane Delta tables and views** — the data contract an external system reads
   (and, in a narrow, documented way, writes).
2. **Workflow job parameters** — the invoke surface: how an orchestrator triggers a stage.
3. **The per-table state machine and the action queue** — the work-tracking contract.
4. **The read-only recommendation surface** (`migration_next_actions`, `migration_runbook`)
   and the gated campaign sequencer.

> **Safety, first and always.** A **human** runs the conversion from the deployed Workflow
> jobs. Nothing in this documentation authorises running `ALTER TABLE ... SET MANAGED`,
> `UNSET MANAGED`, or `VACUUM` from an editor, a notebook attached by hand, or an external
> automation. External control planes **recommend and track**; they do not convert. See
> [Golden safety rules](#golden-safety-rules) below.

## Contents

| Page | What it covers |
|---|---|
| [architecture.md](./architecture.md) | What the tool is, the manifest-driven design, pure-logic + thin-Spark-edge convention, and the deploy/config handshake (`migration_control_config` + smart schema resolution). |
| [install-and-permissions.md](./install-and-permissions.md) | **(Design, proposed)** Frictionless install: notebook and CLI paths over one engine, the four grant sets (with the tested proof that `READ METADATA` cannot replace `SELECT` for the profiler), the installer grant rule, and the upfront rights check. |
| [state-machine.md](./state-machine.md) | The per-table lifecycle `DISCOVERED → … → CLEANED`: every state, every legal transition, and which stage effects it. |
| [integration/control-tables.md](./integration/control-tables.md) | **The core reference.** Every control table and view: full column schema, writer, readers, merge keys, idempotency semantics, and how to query or safely write it. |
| [integration/job-api.md](./integration/job-api.md) | Every Workflow job as an "endpoint": purpose, trigger command, all parameters with defaults, selector rules, gating, side effects, and terminal states. |
| [integration/external-control-plane.md](./integration/external-control-plane.md) | How to integrate: the recommended read-only polling pattern, the narrow safe-write contract on `migration_actions`, assignee routing, and example SQL. |
| [integration/genie-code-mcp.md](./integration/genie-code-mcp.md) | **(Implemented, v1 — `POST /mcp`)** Exposing the control plane to **Genie Code** as a custom MCP server: transport/placement in the app, the v1 (reads + non-mutating stages) → v2 (full conversion) tiering, the tool catalog mapped 1:1 onto `job-params.ts` aliases + control-table reads, the `registerMcpServer` skeleton, the four-layer guardrail model + the read-only `mtm_conversion_preflight` guidance tool that steers an unreasonable request to the right next step, and how ad-hoc "convert this table/schema" requests are funneled through the pipeline. |
| [operations/runbook.md](./operations/runbook.md) | The operational sequence and every safety gate, including the machine-verified compaction-pause gate. |
| [operations/telemetry.md](./operations/telemetry.md) | Opt-out usage telemetry: the signal catalogue, what is never sent, the opt-out switches, and how to extend it. |
| [reference/action-queue.md](./reference/action-queue.md) | Action-queue deep dive: all action kinds, freeze/restore pairs, deterministic `action_key`, the never-delete rule, and the three-way column-ownership split. |
| [app/PRD.md](./app/PRD.md) | Product requirements for the **Migration Control Plane App** (a Databricks App over this API): personas, the cockpit-derived CUJs, and functional/safety requirements mapped to the exact tables and job parameters. |
| [app/ui-design.md](./app/ui-design.md) | Screen-by-screen UI design for that App: information architecture, wireframes per CUJ, component inventory, the UI→API mapping, and design tokens. |

Concepts, tuning knobs, wave-planning heuristics, and the operator narrative live in
[`docs/`](../../docs/README.md) (the root [`README.md`](../../README.md) is the short front
door); engineering conventions live in [`AGENTS.md`](../../AGENTS.md). These pages deliberately
do not restate them — they link back.

## Table naming

Every control object is addressed as:

```
<manifest_catalog>.<manifest_schema>.<object_name>
```

`manifest_catalog` and `manifest_schema` are bundle variables (defaults `main` and
`migration`). They are never hardcoded to a workspace catalog anywhere in the tooling, and
should never be hardcoded in an integration either — read them from the deploy, or resolve
them through `migration_control_config` (see [architecture.md](./architecture.md#config-handshake)).

## Golden safety rules

These are reproduced verbatim in spirit from [`AGENTS.md`](../../AGENTS.md) and apply to every
integration:

- **Never run `ALTER TABLE ... SET MANAGED` / `UNSET MANAGED` from outside the deployed
  jobs.** Conversions are executed by a human triggering `job_04_convert`.
- **Never hard-delete data. Never run `VACUUM` inside the 14-day rollback window.**
- **Never put credentials, tokens, or secrets in code** — read them from Databricks secret
  scopes.
- **Everything must be idempotent and re-runnable** (`SET MANAGED` resumes if interrupted).

## Non-negotiable Databricks facts

Restated here because integrations need them; do not substitute alternatives.

1. Convert: `ALTER TABLE <cat>.<sch>.<tbl> SET MANAGED;`
   With Iceberg reads (UniForm): `ALTER TABLE <cat>.<sch>.<tbl> SET MANAGED TRUNCATE UNIFORM HISTORY;`
2. Rollback (**within 14 days only**): `ALTER TABLE <cat>.<sch>.<tbl> UNSET MANAGED;`
3. Runtimes: `SET MANAGED` / `UNSET MANAGED` / `TRUNCATE UNIFORM HISTORY` require
   **DBR 17.3 LTS+** or **Serverless** (post-14-day `VACUUM` shares the same floor).
   Readers/writers should be on **DBR 15.4 LTS+** (14.3 LTS or below loses timestamp
   time-travel after conversion and may need `DROP FEATURE inCommitTimestamp`; reader
   zero-downtime needs 16.4 LTS+; path-based auto-redirect needs 18.1+/18.2+).
4. Pre-conversion prerequisites: name-based access only (no path-based); no concurrent
   OPTIMIZE / ZORDER / liquid-clustering / compaction; `DESCRIBE DETAIL` succeeds.
5. After conversion, **restart streaming jobs** (read or write) that touch the table.
6. Predictive Optimization auto-deletes old external-location data ~14 days after
   conversion. If PO is disabled, schedule `VACUUM` after 14 days. **Rollback is only
   possible within 14 days.**
7. **Compatibility Mode** applies to MANAGED tables *after* conversion (read-only for
   external clients). External tables do not support it.
8. **Delta Sharing (D2D)** recipients are not auto-updated — re-share after conversion.

Authoritative doc:
<https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed>
