# 08 - Conversion and validation

This is the core: the dry-run pre-flight, the fail-closed managed-location gate, the atomic
per-wave convert, and post-convert validation. The per-table lifecycle these stages drive is
authoritative in [`backend/docs/state-machine.md`](../backend/docs/state-machine.md).

## Dry run (`03_dry_run`)

`backend/notebooks/03_dry_run.py` (`job_03_dry_run`) runs the gating checks per table and
advances a passing table to `DRY_RUN_OK`, or sets `BLOCKED` with a blocker code that carries a
remediation into the runbook. It never runs `SET MANAGED`. Two checks are worth calling out:

- **Runtime gate.** `SET MANAGED` needs DBR 17.3 LTS+ (or Serverless). The notebook's runtime gate
  explicitly accepts "Serverless"; all stage jobs run serverless, which natively satisfies it.
- **Managed-location gate (Check 12): fail-closed.** `locations.effective_managed_location(...)`
  resolves the hierarchy **schema -> catalog -> metastore** and returns one of three statuses:

  | Status (`locations.ML_*`) | Meaning | Effect |
  |---|---|---|
  | `CONFIGURED` | a location was found at some scope | passes |
  | `NOT_CONFIGURED` | every scope resolved and none has a location | blocks with `no_managed_location` |
  | `UNRESOLVED` | a lookup errored, so absence could not be proven | blocks with the distinct `managed_location_unresolved` |

  The gate is **fail-closed**: absence of proof is never treated as "configured". Dry-run
  replaces only the blocker codes it owns (`locations.DRY_RUN_OWNED_BLOCKERS` via
  `replace_owned_blockers`), so repairing a location clears the blocker without wiping blockers
  owned by other stages.

## Convert (`04_convert` + `04_select_waves`)

`backend/notebooks/04_convert.py` is the only stage that mutates tables. It is deliberately
constrained:

- **No implicit convert-all.** It requires a selector: `target_schema`, `bucket`, or
  `wave_ids` (a selector matching no wave fails). A `select_waves` task
  (`04_select_waves.py`) resolves the selector to an ordered `(plan_run_id, wave_id)` list via
  `lineage.resolve_wave_targets(...)`, and a `for_each` converts each wave at **concurrency 1**
  (the serial `job_04_convert` path). To convert several **non-conflicting** waves at once, the
  bulk `job_11_bulk_convert` fans this same notebook out over a `wave_parallel_groups` batch at
  `concurrency=bulk_max_parallel`. See [Bulk convert](./14-bulk-convert.md).
- **Waves are atomic and resumable.** The converter takes the whole remaining wave
  (`DRY_RUN_OK` + `CONVERTING` members) or fails before mutating anything; an interrupted run
  resumes, and a fully-migrated scope is an idempotent no-op.
- **Gates re-asserted before every mutation.** The convert re-asserts the managed-location gate
  (fail-closed), re-checks for a recent OPTIMIZE/compaction commit (`recent_optimize_minutes`),
  and cross-checks the machine-verified **compaction-coverage**, **stream-stop**, and
  **convert-permission** gates (see [The freeze bracket](./07-freeze-bracket.md) and
  [Streaming](./04-streaming.md)). It also refuses a wave with open **blocking** `PRE_CONVERT`
  actions unless `require_pre_convert_actions_cleared=false`.
- **Convert-permission gate: fail-closed.** `SET MANAGED` needs the job's `run_as` to hold
  `USE CATALOG` / `USE SCHEMA`, ownership or `MANAGE`, and `SELECT` / `MODIFY` (unless it owns
  the table). `require_ownership_leased` (default `true`; the name is kept from the lease)
  re-reads the permission live for every table the wave will convert, stores the verdicts in
  `convert_authority`, and stops before any change unless each table is `AUTHORIZED`. On a gap
  the convert writes a `GATE_OWNERSHIP_COVERAGE` evaluation, prints each missing `GRANT` and who
  can run it, and suggests Authorize (`03d_lease_ownership`); it never auto-runs it. It is
  skippable with `require_ownership_leased=false`. See [Convert permissions and Authorize](./07b-convert-permissions.md).
- **Streaming stays isolated.** A `target_schema` / `bucket` scope skips streaming waves unless
  `include_streaming=true` (or `bucket=streaming`); the converter fails loud if a streaming
  table ever appears in a non-streaming wave.
- **Fail loud, audit first.** If any `SET MANAGED` fails, the stage raises **after persisting
  all state and audit rows**, so the `for_each` stops rather than marching on green; re-running
  resumes `CONVERTING`. Terminal **audit rows are written before the terminal manifest state**,
  so a converted table always already has its rollback history.

The per-table state path is `DRY_RUN_OK -> CONVERTING -> CONVERTED` (or `BLOCKED` on failure).
A table already found `MANAGED` is reconciled straight to `CONVERTED` via the audit log without
re-running `SET MANAGED`.

## Advisory cluster sizing

`04_convert` detects the running cluster's cores and compares them to the wave's
`recommended_cores` via `eligibility.cluster_size_status(...)`, stamping `actual_cores` /
`recommended_cores` / `cluster_size_status` on the audit row. Undersized compute produces a
**warning only**: it never blocks a conversion (serverless / unknown cores -> INFO). Wave
sizing treats unknown-size members conservatively (top band), never as 0 bytes.

## Validate (`05_validate`)

`backend/notebooks/05_validate.py` (`job_05_validate`) selects `CONVERTED` tables, runs the
post-conversion checks, writes `validation_results`, and advances a full pass to `VALIDATED`. A
failed check leaves the row at `CONVERTED` and records the failure; it does not roll back. It
is read-only with respect to table data.

Once a wave is validated, the next steps are the optional right bracket
(`05b_unfreeze_verify`), post-convert papercuts (`08_reshare` / `08b_enable_compat`), and,
after 14 days, cleanup. See [Rollback and cleanup](./09-rollback-and-cleanup.md).

Full parameter truth for these stages (all knobs, defaults, gates) is in
[`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md).
