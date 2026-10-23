# Databricks notebook source
# MAGIC %md
# MAGIC # E2E — REAL path-based access reads (source-scan + deep-link fixture)
# MAGIC
# MAGIC Reads a handful of `e2e_real_pathaccess` fixture tables **by their storage path**
# MAGIC (not by name). Each cell below contains a FULL LITERAL storage path on the
# MAGIC `.load(...)` line so:
# MAGIC
# MAGIC 1. the tier-2 source scan (`migration.source_scan.find_path_literals`) finds the literal, and
# MAGIC 2. the profiler attributes a real PATH read to the table, so `has_path_based_access=true`, and
# MAGIC 3. the dashboard/app deep link to this notebook + offending line resolves.
# MAGIC
# MAGIC This is the REAL replacement for the previously-FAKE path-access fixture — the notebook and
# MAGIC the job that runs it are real, so the deep links now resolve to a genuine source.
# MAGIC
# MAGIC **Guard-safe:** pure Spark reads only. This notebook runs no conversion, rollback, or
# MAGIC data-deletion statements. It never mutates a table. The literal base is the default e2e base;
# MAGIC the driver string-replaces it when a different `--external-base` is provisioned.

# COMMAND ----------

spark.read.format("delta").load("s3://e2e-external-bucket/mtm_e2e/e2e_real_pathaccess/orders_by_path").count()

# COMMAND ----------

spark.read.format("delta").load("s3://e2e-external-bucket/mtm_e2e/e2e_real_pathaccess/events_by_path").count()

# COMMAND ----------

spark.read.format("delta").load("s3://e2e-external-bucket/mtm_e2e/e2e_real_pathaccess/metrics_by_path").count()
