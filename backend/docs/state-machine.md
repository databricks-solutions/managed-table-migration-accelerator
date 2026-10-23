# Per-table state machine

Every row in `<manifest_catalog>.<manifest_schema>.table_manifest` carries a `state` column
that tracks where that table sits in its migration lifecycle. The state machine is owned by
`src/migration/audit.py` — the single source of truth — and re-exported through
`src/migration/eligibility.py` so notebooks can import every state from one place.

Two columns are easy to confuse. Keep them distinct in any integration:

| Column | Owner | Domain |
|---|---|---|
| `table_manifest.state` | the state machine (`audit.py`) | the 13 lifecycle states below |
| `table_manifest.eligibility` | the profiler's assessment (`eligibility.assess_eligibility`) | `ELIGIBLE`, `BLOCKED`, `NEEDS_REVIEW` |

`eligibility` is the profiler's verdict on whether a table *can* be converted;
`state` is how far along it *is*. A table can be `state='DISCOVERED'` and
`eligibility='BLOCKED'` at the same time.

A third dimension is **ownership**, tracked in `ownership_ledger`, not in `state`.
`ALTER TABLE ... SET MANAGED` must run as the job's `run_as` (in production the App SP), which
needs the convert privileges (USE CATALOG, USE SCHEMA, OWNER or MANAGE, and SELECT + MODIFY unless
it owns the table). `job_03d_lease_ownership` (Authorize) grants the ones it is missing where it
can; they are **held through convert, validate, and the entire 14-day rollback window**, and
revoked by `job_07_post_convert_cleanup` once the table is `CLEANED` or `ROLLED_BACK`. Convert
permission is orthogonal to `state` — it is **not** a lifecycle state; each grant moves on its own
`ownership_ledger` track, `PLANNED → GRANTED → REVOKED` (a legacy ownership lease: `PLANNED →
LEASED → RESTORED`).

## The 13 states

| State | Meaning |
|---|---|
| `DISCOVERED` | The profiler has inserted the row. Signals may still be filling in. |
| `ASSESSED` | Assessment complete; declared in the machine as a re-entry point after rollback or unblocking. |
| `ELIGIBLE` | Passed assessment; declared in the machine as a re-entry point. |
| `BLOCKED` | A hard blocker prevents conversion (see `blockers`). Re-enterable — fix the blocker and re-run. |
| `DRY_RUN_OK` | Every dry-run gating check passed. The only normal entry point to conversion. |
| `CONVERTING` | `ALTER TABLE ... SET MANAGED` has been issued and not yet confirmed. |
| `CONVERTED` | Conversion succeeded. Rollback is possible **for 14 days**. |
| `VALIDATED` | All post-conversion validation checks passed. |
| `ROLLING_BACK` | `ALTER TABLE ... UNSET MANAGED` has been issued and not yet confirmed. |
| `ROLLED_BACK` | Rollback succeeded; the table is EXTERNAL again and can re-enter the pipeline. |
| `CONVERTED_EXTERNAL` | The profiler's reconcile pass found the table live `MANAGED` but this tooling did not convert it (converted **outside** the app). Counts as migrated; validate still applies. Never auto-rolled-back (no provable 14-day window). |
| `DROPPED` | The reconcile pass proved the table gone via a positive `NOT_FOUND` probe (dropped/renamed away). Left the active universe; re-enters discovery if the name reappears. |
| `CLEANED` | Terminal. Day-14 cleanup has run (PO handled the data, or `VACUUM` ran). No transitions out. |

`CONVERTED_EXTERNAL` and `DROPPED` are written only by the `job_00_profiler` **reconcile pass**
on **proven positive evidence** — a live `table_type = 'MANAGED'`, or a positive
`TABLE_OR_VIEW_NOT_FOUND` probe. Mere absence from a listing is never proof: it stays in the
`stale_manifest` view (honest degrade). See the reconcile pass in "Which stage effects each
transition" below.

## Legal transitions

Exactly as declared in `audit._TRANSITIONS`:

| From | Allowed to |
|---|---|
| `DISCOVERED` | `ASSESSED`, `ELIGIBLE`, `BLOCKED`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `ASSESSED` | `ELIGIBLE`, `BLOCKED`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `ELIGIBLE` | `DRY_RUN_OK`, `BLOCKED`, `CONVERTING`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `DRY_RUN_OK` | `CONVERTING`, `BLOCKED`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `CONVERTING` | `CONVERTED`, `BLOCKED`, `DROPPED` |
| `CONVERTED` | `VALIDATED`, `ROLLING_BACK`, `BLOCKED`, `ASSESSED`, `DROPPED` |
| `VALIDATED` | `ROLLING_BACK`, `CLEANED`, `ASSESSED`, `DROPPED` |
| `ROLLING_BACK` | `ROLLED_BACK`, `BLOCKED`, `DROPPED` |
| `ROLLED_BACK` | `ASSESSED`, `ELIGIBLE`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `BLOCKED` | `ASSESSED`, `ELIGIBLE`, `DRY_RUN_OK`, `CONVERTED_EXTERNAL`, `DROPPED` |
| `CONVERTED_EXTERNAL` | `VALIDATED`, `BLOCKED`, `DROPPED` |
| `DROPPED` | `DISCOVERED`, `ASSESSED` |
| `CLEANED` | *(terminal — empty set)* |

The reconcile-specific edges above encode: a human can `SET MANAGED` a table out from under any
EXTERNAL-side state (incl. `ROLLED_BACK`) → `CONVERTED_EXTERNAL`; an out-of-band `UNSET MANAGED`
sends `CONVERTED`/`VALIDATED` back to `ASSESSED`; any non-terminal state can be proven `DROPPED`;
and `CONVERTED_EXTERNAL` has the same *forward* downstream as `CONVERTED` **minus** `ROLLING_BACK`
(its 14-day window can't be proven without an app-side `CONVERT/SUCCESS` audit row). `CONVERTING`
is deliberately **not** a `CONVERTED_EXTERNAL` source — `job_04_convert` owns that mid-flight
reconciliation (its `ALREADY_MANAGED` path), so the two never double-classify a live conversion.

`audit.is_valid_transition(from_state, to_state)` additionally allows:

- **self-transitions** (`from == to`) — so idempotent re-runs never trip the guard, and
- any target when `from_state` is `None` or unknown — so a manifest row created before a
  state was tracked is not wedged.

```
DISCOVERED ──▶ ASSESSED ──▶ ELIGIBLE ──▶ DRY_RUN_OK ──▶ CONVERTING ──▶ CONVERTED ──▶ VALIDATED ──▶ CLEANED
     │             │            │             │              │              │             │
     └─────────────┴────────────┴─────────────┴──────────────┴──────────────┘             │
                              BLOCKED ◀───────────────────────┐                           │
                                 │                            │                           │
                                 └──▶ ASSESSED/ELIGIBLE/DRY_RUN_OK                        │
                                                              │                           │
                             ROLLED_BACK ◀── ROLLING_BACK ◀───┴───────────────────────────┘
                                 │                    │
                                 └──▶ ASSESSED/ELIGIBLE└──▶ BLOCKED
```

## Which stage effects each transition

| Stage (job / notebook) | Writes `state` | Notes |
|---|---|---|
| `job_00_profiler` / `00_profiler.py` | `DISCOVERED` **on INSERT only** | `merge_manifest_rows` preserves `state` on `WHEN MATCHED`, so re-running the profiler never resets a table that has already advanced. Also writes `eligibility` + `blockers`. |
| `job_00_profiler` **reconcile pass** (gated by `reconcile_lifecycle`, default true) | `CONVERTED_EXTERNAL`, `DROPPED`, or out-of-band-rollback `→ ASSESSED` | Diffs each in-scope manifest row's LIVE `table_type` (`information_schema`) against its `state` and flips **only on proven positive evidence** via a separate evidence-gated `merge_manifest_columns` MERGE (never the discovery MERGE). Writes a `RECONCILE` audit row **before** the state flip. Fail-closed on foreign ownership. Refreshes `table_type` for reconciled rows. |
| `job_01_source_scan` / `01_source_scan.py` | — | Enrichment only (path-access signals). |
| `job_02_wave_planner` / `02_wave_planner.py` | — | Writes `wave_plan` / `comms_feed` and rebuilds the progress views. |
| `job_03_dry_run` / `03_dry_run.py` | `DRY_RUN_OK` or `BLOCKED` | Per-table verdict from the 13 gating checks; also rewrites the dry-run-owned subset of `blockers`. |
| `job_03b_compaction_scan` | — | Read-only rescan + action-queue refresh. |
| `job_03c_freeze` | — | Touches `freeze_ledger` and action rows, never `state`. |
| `job_03d_lease_ownership` | — | Leases ownership into `ownership_ledger` and closes `LEASE_OWNERSHIP` actions, never `state`. |
| `job_04_convert` / `04_convert.py` | `CONVERTING` → `CONVERTED`, or `BLOCKED` on failure | Selects `DRY_RUN_OK` **or** `CONVERTING` (resumable). Also reconciles tables already found MANAGED straight to `CONVERTED`. |
| `job_05_validate` / `05_validate.py` | `VALIDATED` (full pass only) | Selects `CONVERTED`. A failed check leaves the row at `CONVERTED` and sets `validation_failed`. |
| `job_05b_unfreeze_verify` | — | Restores frozen units and closes actions; never `state`. |
| `job_06_rollback` / `06_rollback.py` | `ROLLING_BACK` → `ROLLED_BACK`, or `BLOCKED` on failure | Selects `CONVERTED` or `VALIDATED`. Manual trigger only. |
| `job_07_post_convert_cleanup` | `CLEANED` | Selects `VALIDATED`. Only past the 14-day window. Also revokes the convert privileges Authorize added for `CLEANED` / `ROLLED_BACK` tables (`ownership_ledger` `GRANTED → REVOKED`), restores any legacy lease (`LEASED → RESTORED`), and closes `RESTORE_OWNERSHIP` actions. |
| `job_08_conductor`, `job_08_reshare`, `job_08b_enable_compat`, `job_10_campaign`, `09_cockpit` | — | Never write `state`. |

## Audit trail

Every state change is accompanied by an append-only row in
`<manifest_catalog>.<manifest_schema>.audit_log` carrying `from_state`, `to_state`, the
`action`, the `phase`, and — for conversions — the exact `sql_command` and the pre-conversion
Delta `version` / `num_files` / `size_bytes`.

`action` values: `CONVERT`, `ROLLBACK`, `CLEANUP`, `VALIDATE`, `RECONCILE`, `FREEZE`, `UNFREEZE`,
`STOP_STREAM`, `RESTART_STREAM`, `RESHARE`, `ENABLE_COMPAT`, `LEASE_OWNERSHIP`,
`RESTORE_OWNERSHIP`, `DEFER_ACTION`, `REOPEN_ACTION`. `DEFER_ACTION` / `REOPEN_ACTION` record an
`08`/`08b` `finish_action=defer`/`reopen` on a `migration_actions` row (written before the status
flips); they never change `state`. `RECONCILE` is the out-of-band reconcile pass's state flip (evidence-backed,
not a mutation this tooling performed) — `phase=SUCCESS` for `CONVERTED_EXTERNAL`/rollback,
`phase=SKIPPED` for `DROPPED`.

`phase` values: `STARTED`, `SUCCESS`, `FAILED`, `SKIPPED`.

**Ordering invariant:** terminal audit rows are written **before** the terminal manifest
state, so a table recorded as `CONVERTED` always already has its rollback history. An
integration reconstructing history from `audit_log` can rely on this.

## Reading state safely from outside

- Use the `migration_progress` and `wave_status` views rather than aggregating `state`
  yourself — they already encode the "migrated" and "remaining" definitions the dashboard
  and conductor use. See
  [integration/control-tables.md](./integration/control-tables.md#migration_progress-view).
- `state` alone does not tell you whether a table is *safe* to convert. The open
  pre-convert action queue and the managed-location gate are separate conditions. See
  [reference/action-queue.md](./reference/action-queue.md) and
  [operations/runbook.md](./operations/runbook.md#safety-gates).
- **Never write `state` from an external system.** State transitions are effected by the
  stages that perform the corresponding mutation, together with their audit rows. The only
  columns an external control plane may write are the operator-owned columns on
  `migration_actions` —
  see [external-control-plane.md](./integration/external-control-plane.md#3-the-safe-write-contract).
