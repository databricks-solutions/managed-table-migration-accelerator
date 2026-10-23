# 09 - Rollback and cleanup

After a conversion there is a **14-day window** in which the old external-location data still
exists and the table can be reverted. After that window Predictive Optimization (or a scheduled
VACUUM) removes the old data and rollback is no longer possible. The two stages here live on
opposite ends of that window.

> Two rules that are never violated: **never hard-delete data**, and **never run `VACUUM`
> inside the 14-day rollback window**.

## Rollback (`06_rollback`)

`backend/notebooks/06_rollback.py` (`job_06_rollback`) reverts a conversion with
`ALTER TABLE ... UNSET MANAGED`, but only within the window:

- **14-day window enforced.** The stage reads the conversion end time from `audit_log` and
  **refuses** a table that is outside the 14-day window.
- **Manual only, `reason` required.** Rollback is never on a happy path; it needs a non-empty
  `reason` (in six months it is the only explanation anyone will have).
- **Targets derived from audit, pinned by plan.** Rollback by wave derives its targets from the
  successful convert `audit_log` rows pinned by `plan_run_id + wave_id` (not the mutable
  `wave_plan`), or a single `full_name`.
- **State path.** `CONVERTED` / `VALIDATED` -> `ROLLING_BACK` -> `ROLLED_BACK` (or `BLOCKED` on
  failure). Like convert, it writes terminal audit rows before the terminal manifest state and
  fails loud after persisting state.
- **Exact-restore of the freeze bracket (RB-M1).** With `restore_prior_state=true` (default),
  rollback restores the pre-migration maintenance topology exactly (the `POLICY_ROLLBACK_EXACT`
  policy), verified via `verifier.py` - resuming a job only if it was `UNPAUSED` before,
  re-enabling PO only if it was `ON`, never a blanket enable. It also stamps the table's
  `freeze_ledger` row `ROLLED_BACK` (RB-M4) and obsoletes the paired freeze/unfreeze actions, so
  a rolled-back EXTERNAL table shows no stale freeze ops and a later `05b` skips it.
- **Restore 08c's recorded layout (RB-M2).** With `reconcile_finalize=true` (default), after the
  exact-restore pass rollback restores the clustering state `08c` recorded before any changes
  ([Post-convert maintenance](./08c-post-convert-maintenance.md)): if the table had no clustering
  before `08c` it returns to `CLUSTER BY NONE`; if it had manual keys it returns `CLUSTER BY
  (those keys)`; if a hints pair was applied but failed partway it removes the leftover keys; if
  the table was partitioned or already-AUTO before, or the pre-migration layout is unreadable,
  it stays MANUAL. It also reverses `REVERSE_PO` (`DISABLE PREDICTIVE OPTIMIZATION`) if PO was
  OFF before `08c`. Each reversal is verified by a live re-read and open `ENABLE_*` finalize
  actions are obsoleted. The two are reversed **independently**, so PO enabled by inheritance
  (no per-table `ALTER`) never masks the layout restoration.

After rollback the abandoned managed-location data is auto-deleted by the platform roughly a
week later; the table is EXTERNAL again and can re-enter the pipeline. Remember to **restart
streaming jobs** after a rollback as well.

## Cleanup (`07_post_convert_cleanup`)

`backend/notebooks/07_post_convert_cleanup.py` (`job_07_post_convert_cleanup`) handles the old
external-location data **after** the window, advancing `VALIDATED` -> `CLEANED`:

- **Day-14 only.** It VACUUMs a table only once it is past the 14-day window; it **never
  VACUUMs inside the rollback window**.
- **PO / ledger-aware.** `cleanup.resolve_po_enabled(...)` reads the freeze ledger's
  **original** `prior_po_enabled` so a schema that was mid-freeze (its PO temporarily disabled)
  whose PO was originally on is not VACUUMed prematurely; Predictive Optimization will
  handle the deletion. `cleanup.vacuum_decision(po_enabled, force_vacuum_po)` decides whether a
  VACUUM is needed (PO on -> PO deletes the data; PO off -> schedule the VACUUM).
- **Scheduled, paused by default.** The job carries a daily schedule that is paused by default;
  an operator enables it (or triggers it manually) once conversions are past the window.
- **Revokes the grants Authorize added.** Cleanup is also the **right bracket of Authorize**
  ([Convert permissions and Authorize](./07b-convert-permissions.md)).
  A `confirm_execute` parameter (default `false`) gates the **revoke only** (VACUUM behavior is
  unchanged). The added grants deliberately stay through convert -> validate -> the entire
  14-day rollback window, because in-window rollback (`UNSET MANAGED`) needs the same
  permission. So the revoke happens **here, at cleanup (once a table is `CLEANED` or
  `ROLLED_BACK`), not at unfreeze (`05b`)**. It revokes only `added=true` grants (a privilege
  held before is never revoked; a shared schema or catalog grant waits for every table that
  recorded it), and verifies with `SHOW GRANTS` before the `ownership_ledger` row flips to
  `REVOKED`. Leases taken under the older ownership model are still restored to their captured
  `prior_owner`. Ownership never changes.

This is the one irreversible stage: once the old data is gone, rollback is gone with it, which
is exactly why the window and the PO-awareness exist. Full parameters (including
`confirm_execute`, `force_vacuum_po` and `max_tables`) are in
[`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md).
