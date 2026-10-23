# Rollback and Cleanup — The 14-Day Window and What Happens to the Old Data

A conversion is reversible, but only for 14 days: while the old external-location data still
exists, `UNSET MANAGED` returns the table to EXTERNAL; after that the old data is removed and the
conversion is permanent. This document answers how long a rollback is possible and how it works,
what a rollback restores (the pre-migration maintenance state, the Predictive Optimization and
liquid clustering the finish step turned on), what happens at day 14 (Predictive Optimization's
automatic delete or the cleanup VACUUM), how the convert grants are revoked at cleanup, why VACUUM
must never run inside the window, and what happens to data written while a rolled-back table was
MANAGED. It is read-only guidance: the Agent explains these operations; a human runs them from the
deployed jobs.

## The 14-day window

For 14 days after a table becomes MANAGED, its old external-location data is still present, and
that is what makes a rollback possible. The window is counted from the table's successful
conversion (its convert audit row). Once the old data is gone there is nothing to roll back to.
Everything in this document follows from that window: rollback only inside it, cleanup only after
it, and nothing that deletes the old data while it is open.

## Rolling back a table or a wave

Rollback (`job_06_rollback`) reverses a conversion by issuing:

```sql
ALTER TABLE <catalog>.<schema>.<table> UNSET MANAGED;
```

- **Manual and human-only.** There is no schedule and no trigger behind it, and it **requires a
  reason**, recorded in the audit trail so the estate always shows why a table was reverted.
- **Selector.** One table (`full_name`) or one wave (`plan_run_id` + `wave_id`); `full_name` wins
  if both are given. A wave rollback derives its targets from the recorded **conversion history**
  — the audit rows of tables that actually converted under that pin — not from the current plan,
  so a re-plan cannot misdirect it.
- **Preconditions.** The table is `CONVERTED` or `VALIDATED`, its successful conversion is at most
  14 days old, and the job runs on Serverless or DBR 17.3 LTS+ (see
  [Platform reference](platform_reference.md)). By default it refuses, before any `UNSET MANAGED`,
  to act on a table whose catalog another workspace owns (`require_owning_workspace=true`;
  `owning_workspace` asserts this workspace owns it), because an `UNSET MANAGED` from a non-owning
  workspace is as dangerous as the convert.
- **Per table, fail loud.** Tables roll back independently, so a wave can partly roll back. A
  failure becomes `BLOCKED`, its audit row is written, and the job raises. The terminal `ROLLBACK`
  audit row is written before the terminal manifest state. A crash between `UNSET MANAGED` and those
  writes can leave a table in `ROLLING_BACK`; a re-run selects only `CONVERTED`/`VALIDATED` tables,
  so such a table needs inspection by hand.
- **After it.** The table is `ROLLED_BACK` (EXTERNAL again). Rollback clears the table's
  clustering-step stamp (`optimize_run_at`, `optimize_po_declined`), so a re-conversion is offered
  that step again. Converting it again is not an automatic inverse: it goes through a new dry run
  and convert after reassessment.

## What rollback restores

Rollback returns a table to its **exact pre-migration posture**, not just to EXTERNAL. Unfreeze is
forward-only; exact restore belongs to rollback.

**The maintenance state the pre-convert pause changed** (`restore_prior_state=true`, the default).
After a successful `UNSET MANAGED`, rollback re-applies the exact reverse of what the freeze
recorded in `freeze_ledger`: it resumes a paused OPTIMIZE or writer job only if it was unpaused
before, re-enables Predictive Optimization only if it was on, and restarts stopped streams and
pipelines from their checkpoints. Each restore is verified; a verified one stamps the ledger row
`ROLLED_BACK` and retires the paired freeze and unfreeze items, so a rolled-back table shows no
stale pause work in the runbook and a later unfreeze skips it. It touches reversible metadata only —
never a checkpoint, never VACUUM. With `restore_prior_state=false` it only prints the checklist.

**The finish-step changes** (`reconcile_finalize=true`, the default). The clustering step can leave
two one-way changes on a table, and rollback reverses each one independently, only where the audit
trail proves the step applied it and the live table still shows it:

- **Predictive Optimization.** If the step turned PO on for the table (an `ALTER TABLE … ENABLE
  PREDICTIVE OPTIMIZATION` it ran because PO read off), rollback runs `DISABLE PREDICTIVE
  OPTIMIZATION`. PO that was on only by *inheritance* (nothing changed per table) needs no undo, and
  it never hides the clustering reversal.
- **Liquid clustering.** Rollback restores the layout the step **recorded before acting**: the
  `ENABLE_AUTO_LIQUID` audit row's `from_state`, counting only rows after the table's latest
  successful convert.
  - No clustering before (a ZORDERed table included, since ZORDER is not clustering) →
    `ALTER TABLE … CLUSTER BY NONE`.
  - Manual clustering keys before (switched to AUTO by the step) → `ALTER TABLE … CLUSTER BY
    (<prior keys>)`, restoring exactly those keys.
  - A failed ZORDER pair whose keys were left behind → those keys are removed too.
  - Partitioned or already on AUTO before, or a live layout that cannot be read → **manual**, left
    for a human (fail-closed).
  - Audit rows written before the step recorded `from_state` fall back to the profiler's
    pre-migration layout: a clean table gets `CLUSTER BY NONE`, anything else stays manual, and a
    table whose AUTO is already off is a no-op.

  A clustering reversal is **verified by a live re-read**: AUTO off and exactly the expected keys.
  `CLUSTER BY NONE` does not remove the `clustering`, `domainMetadata` and `rowTracking` table
  features clustering added.

Both reversals are metadata-only (no data rewrite or delete). Verified reversals mark the paired
`ENABLE_*` items obsolete; errors are audited and never fail the rollback. Set
`reconcile_finalize=false` to leave those changes in place. How the step chose each table's
clustering is in [Finish steps](finish_steps.md).

## Day-14 cleanup: how the old data goes away

After a conversion the table's data lives at its managed location, but the old external-location
copy remains through the window. It is removed one of two ways, depending on Predictive
Optimization:

- **Predictive Optimization is on.** It **auto-deletes** the old external-location data roughly 14
  days after the conversion. Nothing needs to be scheduled.
- **Predictive Optimization is off.** There is no automatic delete, so the cleanup stage
  (`job_07_post_convert_cleanup`) runs a VACUUM to remove the old data once the window has closed.

The cleanup stage is scheduled daily (09:00 America/Chicago) but **paused by default**; an operator
unpauses it deliberately. It acts only on `VALIDATED` tables whose known conversion time is at least
14 days old, capped by `max_tables` (default 50) per run. It reads the freeze records when deciding
whether PO was really on, so a schema that was mid-freeze (its PO temporarily disabled for the
conversion) is treated as PO-on and is **not** VACUUMed prematurely. A table PO still maintains is
skipped; the deploy-fixed `force_vacuum_po_tables` (default `false`) is the only way to VACUUM one,
so a run-now click can never blanket-VACUUM PO-managed tables. A per-table VACUUM failure leaves the
table `VALIDATED` for a retry and does not fail the run. A cleaned table is `CLEANED`, which is
terminal, and the removal is irreversible: it ends rollback.

## Revoking the convert grants at cleanup

Cleanup also closes the **Authorize** step. When the convert identity was missing a privilege it
could grant itself, Authorize granted it and recorded a `GRANT` row with `added=true` in
`ownership_ledger` (how Authorize works is in [Stages and gates](stages_and_gates.md)). Those grants
are held through convert, validate and the whole window, because an in-window rollback needs the
same permission, so they are revoked only once a table is `CLEANED` or `ROLLED_BACK`. Ownership
never changes, so there is no owner to restore.

- The revoke has its own `confirm_execute` (default `false`, a dry run that prints the exact
  `REVOKE`s), independent of the VACUUM.
- It revokes **only** what Authorize added; a privilege the identity held before is never revoked.
- A schema or catalog grant recorded for several tables is revoked once, only after every one of
  them is `CLEANED` or `ROLLED_BACK`; until then it prints as `[HELD]`.
- Each revoke is audited first (`REVOKE_PRIVILEGE`), verified with `SHOW GRANTS`, and only then flips
  the ledger `GRANTED → REVOKED` and closes the table's `RESTORE_OWNERSHIP` item. An unverified or
  failed revoke stays `GRANTED` and the run raises after both sections; revoking again is a no-op.
- Residuals: a table PO still maintains (VACUUM skipped) also waits for its revoke, and a dropped
  table's recorded schema or catalog grant is never revoked automatically; revoke either from the
  runbook's exact statements.

Tables changed under the older ownership model (legacy `LEASED` rows) are still restored to their
captured `prior_owner` at cleanup, and a table whose prior owner was never captured stays manual
rather than being handed to an invented owner.

## The one rule: never VACUUM inside the 14-day window

This is the sharpest safety rule in the migration: **never run `VACUUM` inside the 14-day rollback
window.** VACUUM removes the old data files, and removing them is exactly what makes rollback
impossible. The tooling enforces it — cleanup only touches tables past the window, and no stage ever
runs an ad-hoc VACUUM — and you must not work around it by running VACUUM by hand. The question that
decides whether VACUUM is allowed at all is timing: "is this table still inside its window?". The
post-window VACUUM itself needs the same runtime as `SET MANAGED` (see
[Platform reference](platform_reference.md)).

## Abandoned data after a rollback

When a table is rolled back with `UNSET MANAGED`, it returns to being external, but data written
under the **managed** location while it was MANAGED can be left behind as abandoned data. That data
is cleaned up as a separate, deliberate step; the rollback does not delete it implicitly. As with all
cleanup, data is removed only once it is safe, and never in a way that would compromise an open
rollback window.

## Epistemic rule

The facts behind these decisions — whether PO is on, when a table converted, whether it is still
inside its window — are facts to confirm, not assume. An unknown or absent PO state is **not** "safe
to VACUUM"; when in doubt about a table's window or PO state, treat it as still protected and do not
VACUUM. A rollback reversal the live re-read cannot confirm is surfaced as manual, not reported as
done. Absence of evidence is never a licence to delete.

## Related documents

- [Finish steps](finish_steps.md) — the unfreeze, Predictive Optimization and clustering changes rollback reverses.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — what the pre-convert pause records in `freeze_ledger`.
- [Stages and gates](stages_and_gates.md) — where rollback and cleanup sit, and how Authorize grants.
- [Job commands](job_commands.md) — the exact rollback and cleanup commands.
- [Overview and glossary](overview_and_glossary.md) — the `CONVERTED` → `VALIDATED` → `CLEANED` and `ROLLED_BACK` states.
- [Platform reference](platform_reference.md) — the runtime floor for `UNSET MANAGED` and the post-window VACUUM.
