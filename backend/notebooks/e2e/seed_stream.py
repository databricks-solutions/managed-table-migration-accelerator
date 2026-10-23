# Databricks notebook source
# MAGIC %md
# MAGIC # E2E seed — real Structured Streaming writer (INTEGRATION TEST ASSET)
# MAGIC
# MAGIC Writes a genuine Structured Streaming sink to an **EXTERNAL** Delta table so the profiler's
# MAGIC detection wiring can be proven end-to-end: a real `STREAMING UPDATE` commit lands in
# MAGIC `DESCRIBE HISTORY`, which `streaming.is_streaming_history` turns into a high-confidence
# MAGIC `streaming_plan` WRITE row and flips `has_streaming_writer=true`.
# MAGIC
# MAGIC This is NOT part of a migration. It is gated integration tooling driven by
# MAGIC `.e2e/e2e_streaming.py`. Running it AS A JOB also lets lineage attribute the writer to a
# MAGIC JOB, exercising the attributed `STOP_STREAM_JOB` freeze path.
# MAGIC
# MAGIC Two modes (`trigger_mode`):
# MAGIC - `available_now` (**default, cheap**): one bounded run (`Trigger.AvailableNow`) — nothing is
# MAGIC   left running, so it proves *detection* without holding serverless compute.
# MAGIC - `live` (**opt-in**): keeps the JOB RUN genuinely alive by LOOPING bounded `AvailableNow`
# MAGIC   micro-batches (serverless job compute rejects infinite triggers), so the freeze bracket can be
# MAGIC   proven against a running stream job (`03c` cancels the active run + pauses the schedule; `05b`
# MAGIC   restarts it + resumes the schedule). A `max_runtime_s` watchdog bounds the cost — the run
# MAGIC   stops itself if the driver never freezes it.
# MAGIC
# MAGIC Idempotent: `reset=true` clears the target location + checkpoint first so a run starts
# MAGIC clean. It defaults to **false** — the e2e driver opts in on the INITIAL launch, but every
# MAGIC product-driven restart (`05b`'s `RESTART_STREAM` -> `start_job` with default params) inherits
# MAGIC the false default so a restarted stream never drops its own sink table mid-migration.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catalog for the streaming EXTERNAL table")
dbutils.widgets.text("schema", "e2e_stream", "Schema for the streaming EXTERNAL table")
dbutils.widgets.text("table", "src_01", "Streaming EXTERNAL table name")
dbutils.widgets.text("external_base", "", "Writable external-location base (e.g. s3://.../mtm_e2e)")
dbutils.widgets.text("rows_per_second", "50", "Rate-source rows/second")
dbutils.widgets.dropdown("reset", "false", ["true", "false"], "Clear location + checkpoint for a clean run")
dbutils.widgets.dropdown("trigger_mode", "available_now", ["available_now", "live"],
                         "available_now = one bounded batch (cheap, default); live = loop batches, hold run open")
dbutils.widgets.text("max_runtime_s", "1200",
                     "Watchdog: max seconds to hold a live run open (0 = until the run is cancelled)")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
TABLE = dbutils.widgets.get("table").strip()
EXTERNAL_BASE = dbutils.widgets.get("external_base").strip().rstrip("/")
ROWS_PER_SECOND = max(1, int(dbutils.widgets.get("rows_per_second") or 50))
RESET = dbutils.widgets.get("reset") == "true"
TRIGGER_MODE = dbutils.widgets.get("trigger_mode").strip() or "available_now"
MAX_RUNTIME_S = max(0, int(dbutils.widgets.get("max_runtime_s") or 0))
LIVE = TRIGGER_MODE == "live"

if not CATALOG:
    raise ValueError("catalog is required")
if not EXTERNAL_BASE:
    raise ValueError("external_base is required (e.g. the seed bucket 's3://.../mtm_e2e')")

FQ = f"{CATALOG}.{SCHEMA}.{TABLE}"


def _existing_location() -> str | None:
    """The current external location of FQ, if it is registered — else None."""
    try:
        rows = spark.sql(f"DESCRIBE DETAIL {FQ}").select("location").collect()
        return rows[0]["location"] if rows else None
    except Exception:  # noqa: BLE001 — not registered / not describable
        return None


if RESET:
    # Fresh, UNIQUE subpath every reset. A prior live e2e converts src_01 to MANAGED, which orphans
    # the original EXTERNAL path (its UC table id is gone); writing to that same FIXED path again
    # fails resolving the dead id (TABLE_DOES_NOT_EXIST). Per AGENTS.md, rebuilding on a fresh
    # external-base subpath is the reliable one-shot that stale registrations can't defeat.
    import time as _time_reset
    _token = str(int(_time_reset.time()))
    LOCATION = f"{EXTERNAL_BASE}/{SCHEMA}/{TABLE}__{_token}"
else:
    # Restart (product start_job with default params): reuse the live table's own location. A
    # restarted stream only needs to START a run — the migration contract under test is the restart
    # LAUNCH + schedule resume (verified separately), not a successful post-conversion path-write.
    LOCATION = _existing_location() or f"{EXTERNAL_BASE}/{SCHEMA}/{TABLE}"

# Keep the stream checkpoint UNDER the data location so it is always derivable from LOCATION alone
# (a leading-underscore subdir Delta ignores as a data file), even on a fresh token path.
CHECKPOINT = f"{LOCATION}/_stream_ckpt"
print(f"target={FQ}\nlocation={LOCATION}\ncheckpoint={CHECKPOINT}\nreset={RESET}\n"
      f"trigger_mode={TRIGGER_MODE} (live={LIVE}) max_runtime_s={MAX_RUNTIME_S}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Clean start (optional) — synthetic e2e data only, safe to remove

# COMMAND ----------

if RESET:
    # Drop any lingering src_01 registration (metadata only), then clear the FRESH token path in
    # the unlikely event it collides. Synthetic e2e data — no rollback window concern. The fresh
    # subpath (above) is what actually sidesteps a prior converted cycle's orphaned registration.
    spark.sql(f"DROP TABLE IF EXISTS {FQ}")
    for path in (LOCATION, CHECKPOINT):
        try:
            dbutils.fs.rm(path, True)
        except Exception as ex:  # noqa: BLE001
            print(f"  rm {path} skipped ({str(ex)[:100]})")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming write (produces real STREAMING UPDATE commits)
# MAGIC
# MAGIC `available_now` runs ONE bounded batch and stops. `processing_time` (live) can't use an
# MAGIC infinite trigger on serverless job compute (`INFINITE_STREAMING_TRIGGER_NOT_SUPPORTED`), so it
# MAGIC keeps the JOB RUN genuinely alive by LOOPING bounded `AvailableNow` micro-batches — each one
# MAGIC commits a real `STREAMING UPDATE`, and the run stays active so `03c_freeze` can stop a live run.

# COMMAND ----------

import time as _time

from pyspark.sql import functions as F

src = (
    spark.readStream.format("rate")
    .option("rowsPerSecond", ROWS_PER_SECOND)
    .load()
    .select(
        F.col("value").alias("id"),
        (F.col("value") % F.lit(97)).cast("int").alias("category_id"),
        (F.rand() * F.lit(1000.0)).alias("amount"),
        F.col("timestamp").alias("event_ts"),
    )
)

writer = (
    src.writeStream.format("delta")
    .option("checkpointLocation", CHECKPOINT)
    .outputMode("append")
)


def _run_one_batch():
    """Process everything available now, then stop (one real STREAMING UPDATE commit)."""
    q = writer.trigger(availableNow=True).start(LOCATION)
    q.awaitTermination()
    return q


# First bounded batch (both modes) so a STREAMING UPDATE commit exists to register + detect.
_run_one_batch()
print("first bounded batch finished")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register the location as an EXTERNAL UC table + verify streaming history

# COMMAND ----------


def _register_and_check() -> bool:
    """Register the EXTERNAL table (idempotent) and report whether a STREAMING UPDATE commit exists."""
    spark.sql(f"CREATE TABLE IF NOT EXISTS {FQ} USING DELTA LOCATION '{LOCATION}'")
    ops = [r["operation"]
           for r in spark.sql(f"DESCRIBE HISTORY {FQ} LIMIT 50").select("operation").collect()]
    return any((op or "").strip().upper() == "STREAMING UPDATE" for op in ops)


has_streaming_update = _register_and_check()
deadline = _time.time() + 60  # brief poll for read-your-writes visibility of the first commit
while not has_streaming_update and _time.time() < deadline:
    _time.sleep(5)
    has_streaming_update = _register_and_check()

n = spark.table(FQ).count()
print(f"registered EXTERNAL table {FQ}: rows={n}")
print(f"STREAMING UPDATE present in history: {has_streaming_update}")
if not has_streaming_update:
    raise RuntimeError(
        "no STREAMING UPDATE commit in history — the streaming write did not land; "
        "detection cannot be proven. Check the external_base is writable."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Live mode only — keep the JOB RUN alive (watchdog-bounded)
# MAGIC
# MAGIC `STOP_STREAM_JOB` freeze cancels the JOB RUN (+ pauses the schedule), so all the live test
# MAGIC needs is a genuinely ACTIVE run. After the first batch (which committed the STREAMING UPDATE
# MAGIC that detection needs), we simply HOLD the run open by sleeping — a sleeping notebook task is a
# MAGIC RUNNING run — without spinning more serverless streaming queries (which would contend with the
# MAGIC migration stage jobs running alongside and slow the whole e2e down). We refresh one bounded
# MAGIC batch every few minutes so the stream is demonstrably still live. Cancelling the run (03c) or
# MAGIC the `max_runtime_s` watchdog ends it, so a forgotten run can't hold serverless forever.

# COMMAND ----------

if LIVE:
    print(f"live: holding the JOB RUN open up to {MAX_RUNTIME_S or '∞'}s or until the run is "
          f"cancelled (e.g. by 03c_freeze); refreshing a bounded batch every ~3 min.")
    start = _time.time()
    last_batch = _time.time()
    while MAX_RUNTIME_S == 0 or (_time.time() - start) < MAX_RUNTIME_S:
        _time.sleep(15)  # sleeping keeps the run ACTIVE with ~zero serverless contention
        if _time.time() - last_batch >= 180:  # occasional refresh batch (still-live evidence)
            try:
                _run_one_batch()
            except Exception as e:  # noqa: BLE001
                print(f"  refresh batch error (continuing): {str(e)[:200]}")
            last_batch = _time.time()
    print(f"watchdog: max_runtime_s={MAX_RUNTIME_S} reached — stopping.")
