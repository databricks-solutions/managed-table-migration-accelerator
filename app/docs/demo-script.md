# Demo & first end-to-end test script

Drive the whole migration lifecycle through the app's own UI against one real table.

This is both the **first real execution** of the safety apparatus and the script to
follow when demoing the app to someone else. The two are the same walkthrough: if it
isn't good enough to demo, it isn't finished.

**Subject table**

```
<catalog>.e2e_stream.src_01
```

A real EXTERNAL Delta table with genuine Structured Streaming history, created by
`job_e2e_seed_stream`. Chosen because it exercises the richest path — the freeze
bracket, the stream-stop coverage gate, and the streaming-wave routing on the Waves
board — not just the happy path.

**Ground rules for the test run**

- Everything is driven from the **UI**, not the CLI. The point is to test the product.
- **Fix nothing mid-run.** A fix invalidates every step after it. Write findings down.
- Expect to find things. Findings are the deliverable.
- The app's service principal only *triggers* jobs. The jobs run as the deploying
  user, so a permission failure during conversion is about that identity — not the SP.

---

## Step 0 · Preconditions

```bash
cd ~/Projects/ManagedTableMigration
P="--profile <profile>"
T="-t e2e"

# 0a. FIRST: is the fixture even present? Check before running teardown — this control
#      plane may already be clean, and teardown is not free.
#
#      SELECT COUNT(*) FROM <cat>.migration.table_manifest
#      WHERE full_name LIKE '%fx_%' OR plan_run_id = 'zzzz_fx_seed42';
#
#      → 0   : clean, skip 0b
#      → >0  : run 0b. NOT optional — fx_* rows feed the planner, the conductor and
#              the executors, and every trigger in the app is now live.

# 0b. Tear down the synthetic fixture (only if 0a returned rows)
databricks bundle run job_e2e_seed_fixture_estate $T $P   # teardown=true

# 0c. Seed the real subject table
databricks bundle run job_e2e_seed_stream $T $P

# 0d. Read-only pre-flight: auth, job resolution, streaming state
python backend/.e2e/e2e_streaming.py --probe
```

Open the app:

```bash
databricks apps get managed-table-migration $P -o json | grep -i url
```

---

## Step 1 · Overview — read the current state

The first-run state is **not** tested here. Seeing it would mean dropping the control
plane schema and losing the profile, the plan and the audit log, to verify a screen
that is not on the critical path. Verify it separately against a scratch control plane
— the catalog and schema are bundle variables, so a second deploy gives a genuine
virgin plane without destroying this one. See "First-run, tested separately" below.

**Do:** open the app and read the Overview without clicking anything.

**Look for**

- The staleness stamp ("Profiled N ago") is present and plausible.
- KPIs are internally consistent: migrated + validated + remaining + blocked reconcile.
- **The journey rail and the Resolution card agree on where you are.** If the rail
  highlights one stage and the card recommends a different one, the rail is what people
  read first — treat the disagreement as a bug, not a display quirk.
- The directive line and the fact grid quote the **same** numbers. A directive saying
  "1.5 min downtime" beside a MAX DOWNTIME of "2 min" is a finding.
- No implausible-looking zero. `0 min` for a fifteen-table convert reads as broken even
  when it is arithmetically right; `<1 min` does not.

**Bug if:** any number appears that nothing computed.

---

## Step 2 · Profiler

**Do:** fire it from the first-run Resolution card.

**Look for**

- The run pill appears in the header within one poll (~7s).
- `view run ↗` opens the correct Databricks run page.
- Clicking the trigger a second time is **blocked before it fires** — the button is
  disabled from shared active-run state, not by catching a 409 afterwards.
- On completion the Overview repopulates and the recommendation advances.

**Verify underneath**

```sql
SELECT COUNT(*), MAX(assessed_at) FROM <cat>.migration.table_manifest;

SELECT full_name, has_streaming_writer, table_type
FROM <cat>.migration.table_manifest WHERE full_name LIKE '%e2e_stream%';
```

`has_streaming_writer` must be `true`. If it isn't, the profiler's streaming detection
is broken and everything downstream is testing the wrong path.

---

## Step 3 · Wave planner

**Do:** trigger it. Set `pilot_schema = e2e_stream` so the subject lands in the pilot
wave rather than somewhere deep in the plan.

**Look for**

- A plan appears with wave ids of the form `wave_NN` — **not** composite
  `<plan>::<wave>` strings.
- The subject table is in the wave the pilot setting predicted.

**Verify underneath**

```sql
SELECT wave_id, bucket, total_tables, wave_state
FROM <cat>.migration.wave_status ORDER BY wave_order;
```

---

## Step 4 · Dry run

**Do:** trigger it for the pilot wave.

**Look for**

- The wave reaches `DRY_RUN_OK`, or names a **specific** blocker — never a generic
  failure.
- The Waves board reflects the new state without a manual reload.

**Verify underneath**

```sql
SELECT full_name, state, blockers FROM <cat>.migration.table_manifest
WHERE full_name LIKE '%e2e_stream%';

-- Did dry-run write coverage rows?
SELECT gate_kind, verdict, item_count, source, evaluated_at
FROM <cat>.migration.wave_gate_evaluation ORDER BY evaluated_at DESC LIMIT 10;
```

---

## Step 5 · The Waves board

**Do:** just read it.

**Look for**

- The streaming wave routes to **"Open the gate ▸"** — never an inline convert button.
- A BLOCKED wave shows **no** action and states why.
- **NEXT CONVERT** at the top names the lowest-`wave_order` runnable wave, and its
  button goes through the identical on-ramp as the row below it.

**Bug if:** a streaming wave offers a one-click convert. That was the original defect
this whole gate exists to prevent.

---

## Step 6 · The convert gate — the surface that matters most

**Do:** open the gate on the streaming wave. Don't confirm yet.

**Look for, in order**

1. Coverage reads **UNEVALUATED** before anything has evaluated it. Not green.
2. **"Freeze first"** appears, because an active stream is an uncovered writer.
3. The freshness line names its **source** and an **"evaluated N ago"** age.
4. **TABLES THIS WILL CHANGE** names `...e2e_stream.src_01` by its real name.
5. The confirm button is inert until the wave id is typed exactly.

**Then freeze** (from the gate), and return.

6. Coverage flips to **PASS without re-running the dry run.**

That last one is the specific reason `03c_freeze` calls the coverage helper at all. It
has never executed for real. If the board still says UNEVALUATED after a successful
freeze, that call site is not wired.

**Verify underneath**

```sql
SELECT ledger_key, op_kind, status, prior_pause_status, frozen_at
FROM <cat>.migration.freeze_ledger_open;

SELECT gate_kind, verdict, item_count, source, evaluated_at
FROM <cat>.migration.wave_gate_evaluation
ORDER BY evaluated_at DESC LIMIT 10;   -- expect NEW rows with source = '03c_freeze'
```

---

## Step 7 · Convert — the irreversible one

**Do:** type the wave id, confirm.

**Look for**

- The run is watchable from inside the app: header pill, the Runs page panel, and a
  working deep link.
- No second convert can be launched while this one is active.

**Verify underneath — the single most important query in this document**

```sql
SELECT gate_kind, verdict, passed, reason, item_count, source, evaluated_at
FROM <cat>.migration.wave_gate_evaluation
WHERE source = '04_convert' ORDER BY evaluated_at DESC;
```

Every row that has ever appeared in `wave_gate_evaluation` until this moment has been
**synthetic**. That `job_04_convert` actually persists through
`try_append_gate_eval_rows` has only ever been unit-tested. Confirm the rows exist, and
that `reason` is byte-identical to what the job printed in its log.

```sql
SELECT action, phase, from_state, to_state, error_message
FROM <cat>.migration.audit_log ORDER BY created_at DESC LIMIT 20;
```

---

## Step 8 · Validate, then unfreeze

**Do:** trigger validate scoped to `catalog_filter` / `schema_filter = e2e_stream`,
then unfreeze-verify.

**Look for**

- The table reaches `VALIDATED`.
- Runs & history shows the full trail in order.
- The freeze ledger closes — the stream is restored to its **prior** pause status, not
  blanket-resumed.
- Overview's recommendation has advanced to cleanup.

```sql
SELECT full_name, state, validation_failed FROM <cat>.migration.table_manifest
WHERE full_name LIKE '%e2e_stream%';

SELECT check_name, status, detail FROM <cat>.migration.validation_results
ORDER BY created_at DESC;
```

---

## Step 9 · Rollback rehearsal (optional, but do it once)

The one path nobody wants to exercise for the first time under pressure.

`job_06_rollback` takes `full_name`. Confirm the app sends `plan_run_id` alongside
`wave_id` where it uses the wave selector — a `wave_id` with no `plan_run_id` is
ambiguous across plans, and that bug has been found here once already.

---

## First-run, tested separately

Deliberately excluded from the run above. To test it without destroying a working
control plane, deploy a second one at a scratch location:

```bash
databricks bundle deploy -t e2e $P \
  --var="manifest_schema=migration_firstrun"
```

Then open the app pointed at it, before any job has run, and confirm:

- KPIs render `—`, never `0`. A fabricated zero is a claim about an estate nobody has
  looked at.
- The Resolution card offers exactly one thing: run the profiler.
- No wave board, no convert affordance, anywhere.
- The five LAZY tables (`path_access_locations`, `streaming_plan`,
  `consumer_runtime_plan`, `validation_results`, `dry_run_results`) do not exist yet —
  a query against a missing table is an **error**, not an empty result. Every screen
  that touches them must render its empty state rather than an error.

That last point is the one nobody has verified, and it is the literal first thing a new
installer sees.

---

## Findings log

Record everything, including cosmetic issues, before fixing any of it.

| # | Step | What happened | Expected | Severity |
|---|---|---|---|---|
| | | | | |
