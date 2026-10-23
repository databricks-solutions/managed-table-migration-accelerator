#!/usr/bin/env python3
"""Seed EXTERNAL Delta tables (varied sizes, multiple schemas) + the migration control
schema on the e2e workspace, so the profiler -> wave planner -> dry run have real EXTERNAL
tables to exercise.

All tables are EXTERNAL (explicit LOCATION under the writable external location). Idempotent:
CREATE SCHEMA IF NOT EXISTS + CREATE OR REPLACE TABLE. Tiny row counts keep it fast.

Env (all required; see .env.e2e.example): DATABRICKS_CONFIG_PROFILE, E2E_WAREHOUSE_ID,
E2E_CATALOG, E2E_EXTERNAL_BASE.
"""
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

import _e2e_env

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
WAREHOUSE = _e2e_env.require("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.require("E2E_CATALOG")
MANIFEST_SCHEMA = "migration"
BUCKET = _e2e_env.require("E2E_EXTERNAL_BASE").rstrip("/")

# schema -> list of (table_suffix, rows, payload_chars). Sizes stay tiny (fast); the wave
# planner mostly keys off counts + a dense pilot schema. e2e_bronze is the densest (pilot).
PLAN = {
    "e2e_bronze": [(f"raw_{i:02d}", rows, pad) for i, (rows, pad) in enumerate([
        (2_000, 40), (5_000, 60), (50_000, 120), (1_000, 40), (3_000, 50),
        (8_000, 60), (20_000, 90), (1_500, 40), (4_000, 55), (12_000, 70),
        (600, 30), (900, 30), (30_000, 100), (2_500, 45), (7_000, 60),
        (1_200, 40), (15_000, 80), (400, 30)], start=1)],  # 18 -> pilot 15 + 3 overflow
    "e2e_silver": [(f"clean_{i:02d}", rows, pad) for i, (rows, pad) in enumerate([
        (10_000, 60), (250_000, 100), (1_000, 40), (80_000, 130), (5_000, 50),
        (40_000, 70), (2_000, 45), (18_000, 65)], start=1)],
    "e2e_gold": [(f"agg_{i:02d}", rows, pad) for i, (rows, pad) in enumerate([
        (500, 30), (5_000, 60), (150_000, 140), (900, 35)], start=1)],
}

w = WorkspaceClient(profile=PROFILE)


def run(sql: str, label: str) -> bool:
    t0 = time.time()
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, statement=sql, wait_timeout="50s")
    sid = resp.statement_id
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        resp = w.statement_execution.get_statement(sid)
    st = resp.status.state if resp.status else None
    if st != StatementState.SUCCEEDED:
        err = resp.status.error.message if (resp.status and resp.status.error) else st
        print(f"  FAIL {label}: {err}")
        return False
    print(f"  ok   {label}  ({time.time()-t0:.0f}s)")
    return True


# Control schema for the manifest/audit/plan tables.
run(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{MANIFEST_SCHEMA}", f"control schema {MANIFEST_SCHEMA}")

n = 0
for schema, tables in PLAN.items():
    run(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{schema}", f"schema {schema}")
    for suffix, rows, pad in tables:
        name = suffix
        fq = f"{CATALOG}.{schema}.{name}"
        loc = f"{BUCKET}/{schema}/{name}"
        sql = f"""
        CREATE OR REPLACE TABLE {fq}
        USING DELTA LOCATION '{loc}' AS
        SELECT id,
               CAST(id % 97 AS INT) AS category_id,
               rand() * 1000.0 AS amount,
               date_add(DATE'2024-01-01', CAST(id % 365 AS INT)) AS event_date,
               concat('rec-', CAST(id AS STRING), '-', repeat('x', {pad})) AS payload
        FROM range({rows})
        """
        if run(sql, f"{schema}.{name} ({rows:,} rows)"):
            n += 1

print(f"seed complete: {n} external tables across {len(PLAN)} schemas + control schema {MANIFEST_SCHEMA}")
