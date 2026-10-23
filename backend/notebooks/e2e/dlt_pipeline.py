# E2E-ONLY (INTEGRATION TEST ASSET) — Lakeflow Declarative Pipeline source.
#
# A minimal Spark Declarative Pipeline that STREAMS FROM an EXTERNAL Delta table
# (``<catalog>.e2e_dlt.src_bronze`` — the conversion target) into a pipeline-managed streaming
# table. Its only purpose is to establish a real READ-lineage edge on the external source so the
# profiler detects a PIPELINE consumer via ``system.access.table_lineage`` and the freeze/restart
# bracket runs ``OP_STOP_PIPELINE`` / ``RESTORE_START_PIPELINE(full_refresh=False)`` for real.
#
# This is a plain transformations file (NOT a notebook) per SDP requirements. The source table is
# passed via pipeline configuration (``source_table``) — no hardcoded prod table lists.
from pyspark import pipelines as dp
from pyspark.sql import functions as F

_SOURCE = spark.conf.get("source_table")  # noqa: F821  (spark is provided by the pipeline runtime)


@dp.table(
    name="stream_out",
    comment="E2E streaming passthrough of the external bronze source (integration test only).",
)
def stream_out():
    # A streaming read of the EXTERNAL Delta table. After that table is SET MANAGED, this pipeline
    # must be stopped and restarted (without full refresh) — exactly the bracket under test.
    return (
        spark.readStream.table(_SOURCE)  # noqa: F821
        .withColumn("_ingested_at", F.current_timestamp())
    )
