# Integrating an external control plane

This page is the "how to build against it" guide. It assumes you have skimmed
[control-tables.md](./control-tables.md) (the data contract) and
[job-api.md](./job-api.md) (the invoke surface).

## The integration model in one paragraph

Read almost everything; write almost nothing; trigger jobs, never SQL. An external control
plane polls the control views to know estate state, drives its own work tracking off
`migration_actions`, surfaces `migration_next_actions` and `migration_runbook` to humans, and
triggers stages through the Jobs API with the documented parameters. The only data it writes
is the operator-owned column set on `migration_actions`.

> **The conversion is human-triggered.** `job_04_convert` and `job_06_rollback` are the only
> paths to `SET MANAGED` / `UNSET MANAGED`, and both refuse to guess a scope. An external
> control plane may *recommend* and *prepare*; a person decides to convert. Do not issue
> `ALTER TABLE ... SET MANAGED`, `UNSET MANAGED`, or `VACUUM` from your own code, whether from a
> notebook, a SQL warehouse, or an orchestrator step.

## 1. Discover the control plane

Do not hardcode a catalog. Two options, in order of preference:

**Read the handshake table** at the location you deployed to:

```sql
SELECT catalog, schema, warehouse_id, dashboard_id, genie_space_id, updated_at
FROM <manifest_catalog>.<manifest_schema>.migration_control_config;
```

**Or search for a marker table** (`bootstrap.CONTROL_MARKER_TABLES`) when you only know the
metastore:

```sql
SELECT table_catalog, table_schema
FROM system.information_schema.tables
WHERE table_name IN ('migration_next_actions', 'table_manifest')
GROUP BY table_catalog, table_schema;
```

Exactly one result means you have found the control schema. Zero or many means fail fast and
ask the operator. The cockpit applies the same rule.

## 2. Poll the read surfaces

All of these are read-only views. Poll them on whatever cadence suits you; they are cheap
aggregates over Delta tables.

### Estate progress

```sql
SELECT catalog, schema, total_tables, migrated_tables, remaining_tables,
       pct_migrated, pct_validated, blocked
FROM <manifest_catalog>.<manifest_schema>.migration_progress
ORDER BY pct_migrated ASC, remaining_tables DESC;
```

### Wave-level state and readiness

```sql
SELECT plan_run_id, wave_id, wave_order, bucket, wave_state,
       total_tables, remaining_tables,
       open_blocking, open_pre_convert, open_post_convert,
       recommended_warehouse, recommended_cores
FROM <manifest_catalog>.<manifest_schema>.wave_status
ORDER BY wave_order;
```

`wave_state` is the field to key automation off:

| `wave_state` | What it means for you |
|---|---|
| `PLANNED` | Not yet dry-run. Run `job_03_dry_run` (or `job_10_campaign`) for the scope. |
| `READY` | Dry-run clean, no open pre-convert work. Surface to a human to convert. |
| `AWAITING_ACTIONS` | Blocked on the action queue, not on data. Work the queue (`freeze` / `actions` / `unfreeze`). |
| `IN_PROGRESS` | Conversion under way or partially done. |
| `BLOCKED` | Blockers must be fixed; nothing converted yet. |
| `DONE` | Validated (and/or cleaned) with no open post-convert work. |
| `ROLLED_BACK` | Every table rolled back. |

### Open work

```sql
-- The prioritised open queue (blocking first, PRE_CONVERT before POST_CONVERT).
SELECT action_key, action_kind, phase, blocking, status,
       catalog, schema, full_name, target_kind, target_id,
       title, detail, remediation_sql, remediation_api,
       owner, assignee, confidence, plan_run_id, wave_id
FROM <manifest_catalog>.<manifest_schema>.action_queue_open
WHERE catalog = :catalog AND schema = :schema;
```

```sql
-- Rollup for dashboards / SLOs.
SELECT catalog, schema, action_kind, total, `open`, in_progress, done, obsolete, open_blocking
FROM <manifest_catalog>.<manifest_schema>.action_summary
WHERE open_blocking > 0
ORDER BY open_blocking DESC;
```

`open` is quoted above because it collides with a keyword in some SQL clients.

### Everything a human must do by hand

```sql
SELECT phase, phase_rank, blocking, scope, catalog, schema, full_name,
       source_kind, item_kind, title, statement, status, plan_run_id, wave_id
FROM <manifest_catalog>.<manifest_schema>.migration_runbook
WHERE plan_run_id = :plan_run_id AND wave_id = :wave_id
ORDER BY phase_rank, blocking DESC;
```

`statement` is the exact SQL or CLI command. Render it; do not paraphrase it. The view is
guaranteed non-empty whenever work with a known remediation is owed
(see [the manual-statement contract](../reference/action-queue.md#the-manual-statement-contract)).

### Outstanding freezes

```sql
SELECT ledger_key, op_kind, catalog, schema, full_name, target_kind, target_id,
       prior_pause_status, prior_po_enabled, dry_run, status, frozen_at, frozen_by
FROM <manifest_catalog>.<manifest_schema>.freeze_ledger_open
ORDER BY status, catalog, schema;
```

A row with `dry_run = true` is planned only: nothing was actually paused. Only
`status='FROZEN' AND dry_run=false` counts toward the convert-time compaction coverage gate.

### Convert permission: verdicts and added grants

```sql
-- latest verdict per table: can the convert identity SET MANAGED it?
SELECT full_name, actor, status, reason, grant_sql, evaluated_at
FROM <manifest_catalog>.<manifest_schema>.convert_authority
QUALIFY ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) = 1;

-- privileges Authorize added that cleanup still owes a revoke
SELECT full_name, securable_type, securable_name, privilege, grantee, granted_at
FROM <manifest_catalog>.<manifest_schema>.ownership_ledger
WHERE op_kind = 'GRANT' AND status = 'GRANTED' AND added AND NOT dry_run
ORDER BY full_name;
```

`SET MANAGED` runs as the job's `run_as`, which needs USE CATALOG, USE SCHEMA, OWNER or MANAGE,
and SELECT + MODIFY unless it owns the table. `convert_authority` holds the verdict
(`AUTHORIZED` / `SELF_AUTHORIZABLE` / `NEEDS_GRANT` / `UNKNOWN`) and the exact grants with who can
run each; `04_convert` blocks every table that is not `AUTHORIZED`. `job_03d_lease_ownership`
(Authorize) runs the missing grants for self-grantable tables and records each in
`ownership_ledger`; a `dry_run = true` row is planned only. `07` revokes only `added` rows, at
cleanup or after rollback, not at unfreeze, because an in-window `UNSET MANAGED` needs the same
privileges. Ownership never changes.

### The recommended next step

```sql
SELECT *
FROM <manifest_catalog>.<manifest_schema>.migration_next_actions
QUALIFY ROW_NUMBER() OVER (ORDER BY as_of DESC) = 1;
```

The table is append-only, so always take the row with the latest `as_of`. The fields that
matter for automation are `next_step_kind`, `next_selector`, `next_selector_kind`, `mutates`,
`reversible`, `exact_command`, and `directive`.

**Treat `mutates = true` as a hard stop for automation.** Only the `convert` and `resume`
step kinds set it. Present `exact_command` and `directive` to a human; do not run them.

The remaining step kinds (`replan`, `prep`, `validate`, `blocked`, `done`, `freeze`,
`lease`, `unfreeze`, `actions`, `reshare`, `compat`, `optimize`) are non-mutating or gated, and are safe to
surface as suggestions or, for the read-only ones, to trigger.

### Audit history

```sql
SELECT full_name, action, phase, from_state, to_state, operator,
       sql_command, error_class, error_message, start_time, end_time,
       plan_run_id, wave_id
FROM <manifest_catalog>.<manifest_schema>.audit_log
WHERE full_name = :full_name
ORDER BY created_at;
```

Append-only, so this is safe to tail incrementally on `created_at`. Terminal audit rows are
written before the terminal manifest state, so history is never missing for a converted table.

## 3. The safe-write contract

The only table an external control plane may write is `migration_actions`, and only its
seven operator-owned columns:

```
status, assignee, resolution, evidence, completed_at, completed_by, notes
```

Key on `action_key`. Never write identity columns
(`action_key`, `action_id`, `action_kind`, `scope`, `catalog`, `schema`, `table_name`,
`full_name`, `target_kind`, `target_id`, `phase`). Changing one breaks the MERGE and
duplicates the action on the next regeneration. Never insert rows: actions are derived from
manifest signals, and a hand-inserted row will be marked `OBSOLETE` when its scope is next
regenerated.

```sql
-- Claim a piece of work.
UPDATE <manifest_catalog>.<manifest_schema>.migration_actions
SET status     = 'IN_PROGRESS',
    assignee   = :assignee,
    notes      = :external_ticket_ref
WHERE action_key = :action_key
  AND status IN ('OPEN', 'IN_PROGRESS');
```

```sql
-- Close it, with evidence.
UPDATE <manifest_catalog>.<manifest_schema>.migration_actions
SET status       = 'DONE',
    resolution   = :how_it_was_resolved,
    evidence     = :observed_proof,
    completed_at = current_timestamp(),
    completed_by = :who
WHERE action_key = :action_key
  AND status IN ('OPEN', 'IN_PROGRESS');
```

**Only mark `DONE` on observed truth.** The tooling's own executors verify against the
platform (job actually paused, PO actually disabled, a run actually started after the
convert, the table actually back in the share, the property actually set) and leave
applied-but-unverified work at `IN_PROGRESS`. Hold your integration to the same bar: a
falsely-`DONE` blocking pre-convert action removes a real safety gate from `job_04_convert`.

### Assignee routing

If your control plane owns the on-call mapping, either write `assignee` directly as above, or
supply an `{owner: assignee}` map (with an optional `"*"` catch-all) to
`actions.assign_actions_from_owner_map`, which writes only `assignee`, only on
`OPEN`/`IN_PROGRESS` rows, and is idempotent. `actions.resolve_assignee` returns `None` when
nothing matches: it never guesses, and neither should you.

Everything about the queue's semantics is in
[reference/action-queue.md](../reference/action-queue.md).

### What you must never write

| Object | Why |
|---|---|
| `table_manifest.state` | State transitions are effected by the stage performing the matching mutation, together with its audit row. Writing it desynchronises the audit trail from reality. |
| `table_manifest` (any column) | Every column is owned by a stage; the discovery MERGE and enrichment passes will overwrite or be confused by external writes. |
| `audit_log` | Append-only history written by the stages. A forged row corrupts rollback targeting (stage 06 derives targets from `CONVERT` audit rows). |
| `wave_plan` / `comms_feed` | Fully rewritten by the planner on every run; your write disappears and, worse, may look like plan drift. |
| `freeze_ledger` | The exact-restore contract. A wrong `prior_pause_status` or `prior_po_enabled` causes the unfreeze to restore the wrong state. |
| `ownership_ledger` | The exact-restore contract for ownership. A wrong `prior_owner` causes the cleanup restore to hand the table back to the wrong owner. |
| `migration_next_actions` | Conductor output. Append your own recommendations elsewhere. |
| Any view | Views are recreated by their owning stage. |

## 4. Trigger stages

Use the Jobs API (`POST /api/2.2/jobs/run-now` with `job_parameters`), the CLI
(`databricks jobs run-now`), or `databricks bundle run`. Resolve job ids by name or tag. Every
job carries `project: managed-migration` and a `stage: <nn>-<name>` tag, and in development
mode names are `[dev <user>]`-prefixed, so match by suffix.

Recommended automation posture:

| Stage | Automate? |
|---|---|
| `job_00_profiler` | Yes, read-only. |
| `job_01_source_scan` | Yes, when you want tier-2 path evidence. Off by default. |
| `job_02_wave_planner` | Yes, but **not mid-flight**: a replan stamps a new `plan_run_id` and rewrites `wave_plan`, invalidating any pins your automation is holding. |
| `job_03_dry_run` | Yes, read-only. |
| `job_03b_compaction_scan` | Yes, read-only. Run it against the target schema immediately before a convert. |
| `job_08_conductor` | Yes, read-only. This is how you refresh `migration_next_actions`. |
| `job_10_campaign` | Yes. This is the intended headless "prep one schema and stop" entry point. |
| `job_03c_freeze` / `job_05b_unfreeze_verify` | Dry-run: yes. `confirm_execute=true`: only under explicit human approval, because it pauses jobs and stops streams in the customer's estate. |
| `job_03d_lease_ownership` | Dry-run: yes. `confirm_execute=true`: only under explicit human approval, because it grants the convert identity Unity Catalog privileges in the customer's estate (only the missing ones; revoked by `07`). App-triggerable (took the slot freed by folding source-scan into the profiler); also Jobs UI / CLI runnable. |
| `job_08_reshare` / `job_08b_enable_compat` | Same: dry-run freely, execute only with approval. |
| `job_04_convert` / `job_06_rollback` / `job_07_post_convert_cleanup` | **No.** Human-triggered. Surface the exact command; let a person run it. |

Example job-parameter payloads (see [job-api.md](./job-api.md) for the full parameter sets):

```json
// prep one schema, read-only, never converts
{ "job_id": <job_10_campaign>,
  "job_parameters": { "target_schema": "main.sales", "include_streaming": "false" } }
```

```json
// plan the freeze for a wave, no API calls
{ "job_id": <job_03c_freeze>,
  "job_parameters": { "plan_run_id": "<id>", "wave_id": "<id>", "confirm_execute": "false" } }
```

### Pinning and drift

`job_04_convert` and `job_06_rollback` pin to an exact `(plan_run_id, wave_id)` and **fail
before mutating** if the plan has drifted. If your automation caches a `plan_run_id`, re-read
`wave_status` before acting on it. Note also that **multiple `wave_ids` cannot be passed on the
CLI**, because the comma is the `--params` delimiter; use the Jobs API `job_parameters`, the
Jobs UI, or one run per wave.

## 5. A worked read-only loop

```
1.  Resolve the control schema (migration_control_config, or a marker-table search).
2.  Trigger job_00_profiler.                        -> table_manifest, actions, detail tables
3.  Trigger job_02_wave_planner (first time only).  -> wave_plan, plan_run_id, progress views
4.  For each schema you want to advance:
      trigger job_10_campaign(target_schema=...)    -> dry-run + conductor, then STOPS
5.  Poll wave_status.
      wave_state = 'AWAITING_ACTIONS'
        -> read action_queue_open + migration_runbook for the wave
        -> assign, track, and (with approval) run the gated executors
        -> update ONLY the operator-owned columns as work completes
      wave_state = 'READY'
        -> read the latest migration_next_actions row
        -> present exact_command + directive to a human. STOP.
6.  After the human runs job_04_convert:
      trigger job_05_validate                       -> validation_results, VALIDATED
      read wave_status again; AWAITING_ACTIONS means post-convert work is owed
        -> job_05b_unfreeze_verify (restart streams, restore PO/jobs)
        -> job_08_reshare / job_08b_enable_compat as the queue indicates
7.  Day 14+: a human unpauses job_07_post_convert_cleanup. Never VACUUM yourself.
```

## 6. Reliability notes for integrators

- **Tolerate missing tables.** `streaming_plan`, `consumer_runtime_plan`,
  `path_access_locations`, and `validation_results` are created lazily by their stage.
- **Tolerate new columns.** Control tables auto-evolve; select by name.
- **`NULL` is not "safe".** Lineage and history signals are sampled and retention-bounded. A
  `NULL` `has_streaming_writer` or `last_compaction_at` means *no evidence*, not *no
  streaming* / *no compaction*. Never let your automation clear a prerequisite on absence of
  evidence.
- **Distinguish degraded scans.** `compaction_notes` values `history_unavailable` and
  `attribution_unavailable`, and `location_map.resolution_status = 'ERROR'`, mean the tool
  could not determine the answer. Surface them as unknown, not as clear.
- **Stages fail loud after persisting state.** A failed run has already written its audit and
  control rows; re-running resumes. Retry the same invocation rather than compensating.
- **Detail tables are keyed by `full_name` only.** `streaming_plan` has no `catalog`/`schema`
  columns; a scope predicate there throws. Filter on `full_name`.

## Related pages

- [control-tables.md](./control-tables.md): full schemas for everything referenced above.
- [job-api.md](./job-api.md): every parameter, default, and gate.
- [../reference/action-queue.md](../reference/action-queue.md): the ownership split in detail.
- [../operations/runbook.md](../operations/runbook.md): the human-facing sequence.
- [../state-machine.md](../state-machine.md): the `state` column's domain.
