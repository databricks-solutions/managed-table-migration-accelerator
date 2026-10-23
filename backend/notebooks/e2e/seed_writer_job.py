# Databricks notebook source
# MAGIC %md
# MAGIC # e2e :: scheduled batch writer seed (INSERT)
# MAGIC
# MAGIC Ephemeral helper notebook uploaded by `backend/.e2e/seed_real_estate.py`. It runs a real,
# MAGIC **guaranteed-to-commit** `INSERT` on one estate table so a scheduled JOB is genuinely
# MAGIC attributed as a WRITER in `system.access.table_lineage` — the profiler then records a
# MAGIC `SCHEDULED_JOB` writer in `writer_sources`, and that job flows through the real
# MAGIC `STOP_WRITER_JOB` / `RESTART_WRITER_JOB` freeze bracket the tooling exercises around a convert.
# MAGIC
# MAGIC **This is a WRITE, not an OPTIMIZE.** `INSERT INTO t SELECT * FROM t LIMIT 1` appends one
# MAGIC small file (a real commit + a WRITE lineage edge attributed to the JOB), so the job is
# MAGIC classified as a `SCHEDULED_JOB` writer — distinct from the OPTIMIZE/VACUUM maintenance jobs
# MAGIC (which are compaction actors). Never converts, never runs the managed-conversion DDL, never
# MAGIC deletes rows. Runs only on the throwaway `e2e_*` estate. Idempotent + re-runnable.

# COMMAND ----------

dbutils.widgets.text("target", "", "One fully-qualified table (catalog.schema.table)")

target = (dbutils.widgets.get("target") or "").strip()
if not target:
    raise ValueError("no target supplied — set `target`")

# COMMAND ----------


def _qualify(fq: str) -> str:
    # Backtick-qualify each identifier part so odd names are safe.
    return ".".join(f"`{p}`" for p in fq.split("."))


tbl = _qualify(target)
stmt = f"INSERT INTO {tbl} SELECT * FROM {tbl} LIMIT 1"
print(f"[writer] {stmt}")
spark.sql(stmt)
print(f"[writer] committed one WRITE on {target} (SCHEDULED_JOB writer attribution)")
dbutils.notebook.exit(f"WRITE:{target}")
