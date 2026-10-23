# Databricks notebook source
# MAGIC %md
# MAGIC # e2e :: scheduled maintenance seed (OPTIMIZE / VACUUM)
# MAGIC
# MAGIC Ephemeral helper notebook uploaded by `backend/.e2e/seed_real_estate.py`. It runs a real,
# MAGIC **guaranteed-to-commit** `OPTIMIZE` or `VACUUM` on one or more estate tables so a scheduled
# MAGIC JOB is genuinely attributed in `DESCRIBE HISTORY` — the profiler then records a `JOB` actor in
# MAGIC `compaction_sources` (the VACUUM job carrying `operation_class=VACUUM`), and both jobs flow
# MAGIC through the real `PAUSE_JOB`/`RESUME_JOB` freeze bracket the tooling exercises around a convert.
# MAGIC
# MAGIC **Why the extra fragmentation step.** On a freshly-CTAS'd single-file table an `OPTIMIZE` is a
# MAGIC **no-op** (nothing to compact → no commit → no JOB attribution), and a default-retention
# MAGIC `VACUUM` that removes nothing writes **no** `VACUUM START/END` history at all. So we first
# MAGIC write a few tiny files (fragmentation): the OPTIMIZE path then has something to compact, and
# MAGIC the VACUUM path compacts (tombstoning those files) and `VACUUM ... RETAIN 0 HOURS` removes the
# MAGIC tombstones — writing the JOB-attributed `VACUUM START/END` commit the freeze bracket needs.
# MAGIC
# MAGIC **Never converts, never runs the managed-conversion DDL, never hard-deletes source-of-truth
# MAGIC rows.** VACUUM only removes NON-current (tombstoned) files; the latest snapshot is untouched, so
# MAGIC a later conversion still validates it. Runs only on the throwaway `e2e_*` estate. Idempotent + re-runnable.

# COMMAND ----------

dbutils.widgets.text("operation", "OPTIMIZE", "OPTIMIZE | VACUUM")
dbutils.widgets.text("target", "", "One fully-qualified table (catalog.schema.table)")
dbutils.widgets.text("targets", "", "Optional comma-separated list of fully-qualified tables (overrides `target`)")

operation = (dbutils.widgets.get("operation") or "OPTIMIZE").strip().upper()
target = (dbutils.widgets.get("target") or "").strip()
targets_raw = (dbutils.widgets.get("targets") or "").strip()

targets = [t.strip() for t in targets_raw.split(",") if t.strip()] if targets_raw else ([target] if target else [])
if not targets:
    raise ValueError("no target(s) supplied — set `target` or `targets`")
if operation not in ("OPTIMIZE", "VACUUM"):
    raise ValueError(f"unsupported operation {operation!r} — use OPTIMIZE or VACUUM")

# COMMAND ----------


def _qualify(fq: str) -> str:
    # Backtick-qualify each identifier part so odd names are safe.
    return ".".join(f"`{p}`" for p in fq.split("."))


def _fragment(tbl: str, batches: int = 4) -> None:
    """Write a few tiny files so a subsequent OPTIMIZE has real work to do (guarantees a commit).

    ``INSERT INTO t SELECT * FROM t LIMIT 2`` is schema-agnostic (no column list needed) and appends
    one small file per statement — turning a single-file table into a fragmented one.
    """
    for _ in range(batches):
        spark.sql(f"INSERT INTO {tbl} SELECT * FROM {tbl} LIMIT 2")


ran = []
for fq in targets:
    tbl = _qualify(fq)
    if operation == "OPTIMIZE":
        _fragment(tbl)                        # ensure >1 file so the OPTIMIZE is not a no-op
        stmt = f"OPTIMIZE {tbl}"
        print(f"[maintenance] {stmt}")
        spark.sql(stmt)
    else:  # VACUUM
        # Getting a JOB-attributed VACUUM commit is subtle: (1) VACUUM only writes VACUUM START/END
        # history when `delta.vacuum.logging.enabled` is on — it defaults OFF on object-store
        # (S3/ADLS) backends; (2) a default-retention VACUUM removes nothing on a fresh table, so it
        # would have nothing to log. So we enable logging (best-effort — some serverless sessions
        # pin the conf), drop the deleted-file retention to zero via a TABLE PROPERTY (the
        # serverless-safe path; the `spark.databricks.delta.*` session conf is blocked on SQL
        # warehouses), then fragment + OPTIMIZE to create tombstones and run a plain VACUUM that
        # removes them and writes the JOB-attributed VACUUM START/END commit. Only non-current files
        # go; the latest snapshot stays, so a later conversion still validates it. Estate-only.
        try:
            spark.conf.set("spark.databricks.delta.vacuum.logging.enabled", "true")
        except Exception as _e:   # noqa: BLE001 — pinned on some serverless sessions; log-and-continue
            print(f"[maintenance] note: could not enable vacuum logging in-session ({_e}); "
                  f"the VACUUM may not surface in DESCRIBE HISTORY on this backend")
        spark.sql(f"ALTER TABLE {tbl} SET TBLPROPERTIES ('delta.deletedFileRetentionDuration'='interval 0 hours')")
        _fragment(tbl)                        # tiny files ...
        spark.sql(f"OPTIMIZE {tbl}")          # ... compacted here, tombstoning them
        stmt = f"VACUUM {tbl}"
        print(f"[maintenance] {stmt}")
        spark.sql(stmt)
    ran.append(fq)

print(f"[maintenance] {operation} committed on {len(ran)} table(s): {ran}")
dbutils.notebook.exit(f"{operation}:{len(ran)}")
