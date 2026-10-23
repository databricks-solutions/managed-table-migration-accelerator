#!/usr/bin/env python3
"""Seed-and-leave REAL-estate builder (dev-only terminal driver).

Composes stages (provision->activity->lineage_wait->profile->scan->plan->e2e) or presets
(provision_only | populate | full). Lineage corroboration is REQUIRED by default and
opt-out only via --no-lineage. --reset = teardown + re-seed a brand-new estate.

NEVER converts tables from here; the e2e stage triggers the deployed stage jobs, which
perform the conversion. Idempotent + resumable. See backend/.e2e/README.md.

The ``provision`` stage builds REAL EXTERNAL tables directly via the SQL statement API:
one CREATE SCHEMA per distinct schema, then per table a CTAS at an explicit external
LOCATION — Delta (varied real sizes) for normal tables and PARQUET (non-Delta, so the
Delta size probe is null) for the NON_DELTA schema. Pass ``--dry-run`` to print the exact
statements without creating a client or touching the network (offline verification).
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# Make ``migration`` importable (driver lives at backend/.e2e/; src is ../src).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from migration import real_estate as re_  # noqa: E402
from migration import eligibility as elig_  # noqa: E402  (pure module — no Spark/SDK; offline-safe)

# Control (manifest/audit/plan) schema seeded alongside the estate.
CONTROL_SCHEMA = "migration"

# Named L4 app/UI fixtures (see e2e_app_ui.py --fixtures). Shaped on an already-populated
# (F-PLANNED) estate via --fixture.
FIXTURE_READY_BATCH = "F-READY-BATCH"
FIXTURE_WRITER_WAVE = "F-WRITER-WAVE"
_FIXTURES = (FIXTURE_READY_BATCH, FIXTURE_WRITER_WAVE)

# No defaults: the estate is built in a real workspace, so these are named explicitly (flags or
# the E2E_* environment variables; see .env.e2e.example).
_DEFAULT_WAREHOUSE = os.environ.get("E2E_WAREHOUSE_ID", "")
_DEFAULT_CATALOG = os.environ.get("E2E_CATALOG", "")
_DEFAULT_EXTERNAL_BASE = os.environ.get("E2E_EXTERNAL_BASE", "")

# The placeholder external base literally EMBEDDED in backend/notebooks/e2e/read_by_path_real.py.
# This MUST equal the literal base hardcoded on the notebook's ``.load(...)`` lines — the
# notebook upload anchors its base substitution on THIS constant (not the env-derived
# default), so the committed placeholder is always replaced with the real --external-base.
NOTEBOOK_BASE_LITERAL = "s3://e2e-external-bucket/mtm_e2e"

# Real streaming + DLT activity (Task 7): the builder REUSES the already-deployed bundle assets
# (it never creates new jobs/pipelines, nor duplicates them under e2e_real_*). Both are resolved
# by name suffix at run time. Streaming writes to <catalog>.e2e_stream.src_01; the DLT pipeline
# reads from <catalog>.e2e_dlt.src_bronze. Mirrors e2e_streaming.py / e2e_dlt_pipeline.py.
STREAM_SCHEMA = "e2e_stream"
STREAM_SEED_TABLE = "src_01"
STREAM_SEED_JOB_SUFFIX = "managed-table-migration :: e2e seed stream (integration)"
DLT_SCHEMA = "e2e_dlt"
DLT_SRC_BRONZE = "src_bronze"
DLT_PIPELINE_SUFFIX = "managed-table-migration :: e2e dlt (integration)"

# Real compaction papercut (Task 8): a small deterministic handful of OPTIMIZE runs (one per
# estate schema below) plus schema-level Predictive Optimization on PO_SCHEMA. Together these
# create the exact compaction actors (warehouse/user OPTIMIZE + PREDICTIVE_OPTIMIZATION) the
# profiler attributes as freezable sources — the papercut the freeze bracket must cover.
PO_SCHEMA = "e2e_real_bronze"
COMPACTION_TARGET_SCHEMAS = ("e2e_real_bronze", "e2e_real_silver", "e2e_real_gold")

# Real Delta Share (D2D) papercut (Task 9): create a genuine share over the first Delta table
# of the SHARE schema so ``is_delta_shared=true`` is truly true for a seeded table. Delta Sharing
# may be disabled on the workspace — degrade honestly (WARN + skip) rather than fake it.
SHARE_NAME = "e2e_real_share_d2d"
SHARE_SCHEMA = "e2e_real_share"
SHARE_RECIPIENT = "e2e_real_recipient_d2d"

# Real SCHEDULED maintenance jobs (VACUUM defense e2e): the manual OPTIMIZE above only exercises
# DISABLE_PO; to genuinely exercise the PAUSE_JOB/RESUME_JOB freeze bracket we seed two real
# scheduled jobs on a pilot-schema (e2e_real_bronze) table — one OPTIMIZE, one VACUUM — each a
# notebook_task with a CronSchedule left UNPAUSED, then run once so DESCRIBE HISTORY attributes the
# commit to a JOB (the profiler records a JOB actor in compaction_sources; the VACUUM job carries
# operation_class=VACUUM). The freeze bracket pauses them for the conversion window and 05b resumes
# them (exact-restore). Resolved by EXACT name at e2e/teardown time (never recreated when present).
MAINT_OPTIMIZE_JOB_NAME = "mtm_e2e :: scheduled optimize (maintenance)"
MAINT_VACUUM_JOB_NAME = "mtm_e2e :: scheduled vacuum (maintenance)"
# A far-future cron so the schedule never actually fires on its own during a test window; the freeze
# bracket only cares about the schedule's pause_status, which we toggle. (Sat at 03:00, timezone UTC.)
MAINT_CRON = "0 0 3 ? * SAT"
MAINT_TZ = "UTC"
_MAINT_NOTEBOOK_SRC = os.path.join(
    os.path.dirname(__file__), "..", "notebooks", "e2e", "seed_maintenance_jobs.py")
_MAINT_WS_NAME = "seed_maintenance_jobs"

# Real SCHEDULED batch writer job (feature F): a scheduled JOB that WRITEs (INSERT), so lineage
# attributes it as a SCHEDULED_JOB writer in writer_sources — distinct from the OPTIMIZE/VACUUM
# maintenance jobs (compaction actors). It flows through the STOP_WRITER_JOB/RESTART_WRITER_JOB
# freeze bracket (03c pauses, 05b forward-restarts). Resolved by EXACT name at e2e/teardown time.
WRITER_JOB_NAME = "mtm_e2e :: scheduled batch writer (INSERT)"
_WRITER_NOTEBOOK_SRC = os.path.join(
    os.path.dirname(__file__), "..", "notebooks", "e2e", "seed_writer_job.py")
_WRITER_WS_NAME = "seed_writer_job"

SCHED_FAR_JOB_NAME = "mtm_e2e :: schedule twin far-out (SAFE_NOW)"
SCHED_IMMINENT_JOB_NAME = "mtm_e2e :: schedule twin imminent (WARN_WINDOW)"
# Every 5 min -> next fire <= 5 min, always < the profiler's 15-min schedule margin -> WARN_WINDOW.
SCHED_IMMINENT_CRON = "0 */5 * ? * *"

# Writer-wave imminent writer (feature G / L4 F-WRITER-WAVE): ONE more imminent WARN_WINDOW writer,
# but on the TINY isolated ``e2e_real_writer`` schema (re_.SCHEMAS) rather than the dense bronze
# pilot. Because that schema is its own small wave, the attributed writer is NOT buried in the 259-
# action bronze domain wave — a dry-run + 03c_freeze makes a runnable wave whose Convert-Gate pause
# banner (A1) names exactly this one job with a WARN_WINDOW verdict. Deliberately DISTINCT from the
# bronze twins above so the L3 MCP oracle (pilot-schema scan) is unaffected.
WRITER_WAVE_SCHEMA = "e2e_real_writer"
SCHED_WRITER_WAVE_JOB_NAME = "mtm_e2e :: writer-wave imminent (WARN_WINDOW)"

# Profile stage (Task 11): run the DEPLOYED profiler job scoped to the estate, then corroborate
# the seeded papercuts against the control tables. The profiler is name-resolved by suffix (never
# recreated) and scoped via its overridable job parameters (catalog_filter / schema_filter globs +
# force_rescan). The ``e2e_*`` schema glob covers the e2e_real_* estate PLUS e2e_stream + e2e_dlt.
PROFILER_SUFFIX = "managed-table-migration :: 00 profiler"
NONDELTA_SCHEMA = "e2e_real_nondelta"
# Size-capture sample tiers: one deterministic Delta table per tier is size-checked against a live
# DESCRIBE DETAIL, with a LOOSE [0.5x, 2x] tolerance (real bytes are approximate by design).
_PROFILE_SIZE_TIERS = ("XS", "S", "M", "L", "XL")

# Lineage corroboration wait (Task 10): the estate's real activity (streaming write, DLT read,
# by-path read) only registers in system tables after propagation latency (can lag hours). The
# lineage_wait stage polls system.access.table_lineage until representative PIPELINE / JOB / PATH
# edges appear, then FAILS LOUD on timeout so a downstream profile never corroborates on lineage
# that has not landed. Tunable via E2E_LINEAGE_TIMEOUT_S (mirrors e2e_dlt_pipeline.py's 900s knob;
# default higher here as this waits on THREE edges, not one).
_LINEAGE_TIMEOUT_S = int(os.environ.get("E2E_LINEAGE_TIMEOUT_S", "1200"))
_LINEAGE_POLL_S = 30


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed-and-leave real-estate builder")
    p.add_argument("--profile", required=True)
    p.add_argument("--warehouse-id", default=_DEFAULT_WAREHOUSE)
    p.add_argument("--catalog", default=_DEFAULT_CATALOG)
    p.add_argument("--external-base", default=_DEFAULT_EXTERNAL_BASE,
                   help="external LOCATION prefix for the seeded tables")
    p.add_argument("--preset", choices=sorted(re_.PRESETS))
    p.add_argument("--stage", action="append", choices=list(re_.STAGES), default=[])
    p.add_argument("--no-lineage", action="store_true",
                   help="skip lineage-wait; downgrade corroboration to WARN")
    p.add_argument("--dry-run", action="store_true",
                   help="print the exact SQL every stage would run; no client, no network")
    p.add_argument("--reset", action="store_true", help="teardown then re-seed a new estate")
    p.add_argument("--teardown", action="store_true", help="remove the e2e_real_* estate and exit")
    p.add_argument("--no-seed", action="store_true",
                   help="with --reset: teardown (+ file clear) then STOP — leave a fresh, estate-cleared "
                        "control plane instead of re-seeding")
    p.add_argument("--wipe-control-plane", action="store_true",
                   help="full fresh-install reset: empty EVERY control base table so the app shows its "
                        "first-run screen (requires --confirm-wipe), then exit. migration_control_config "
                        "is preserved.")
    p.add_argument("--confirm-wipe", action="store_true",
                   help="required to actually run --wipe-control-plane (guard against an accidental wipe)")
    p.add_argument("--keep-files", action="store_true",
                   help="teardown: keep the estate's external files (default: clear them under the "
                        "guarded mtm_e2e prefix so --reset reuses the one stable base)")
    p.add_argument("--foreign-owner", default=None,
                   help="activity: a principal (not the convert identity) to own one seeded table "
                        "(convert-permission check on a foreign-owned table); unset = not seeded")
    p.add_argument("--owner-group", default=None,
                   help="activity: a group to own one seeded table (convert-permission check on a "
                        "group-owned table); unset = not seeded")
    p.add_argument("--table-count", type=int, default=120)
    p.add_argument("--rng-seed", type=int, default=42)
    p.add_argument("--confirm-execute", action="store_true",
                   help="required for the e2e stage (real table conversion runs in the stage jobs)")
    p.add_argument("--e2e-through", choices=["convert", "validate", "rollback"], default="rollback")
    p.add_argument("--fixture", choices=sorted(_FIXTURES),
                   help="shape a named L4 UI fixture on an already-populated (F-PLANNED) estate, then "
                        "exit. Does NOT teardown/reseed. See e2e_app_ui.py --fixtures.")
    p.add_argument("--fixture-batch", default=None,
                   help="pin the target parallel batch_id for --fixture F-READY-BATCH (default: "
                        "auto-pick the parallel batch closest to READY).")
    a = p.parse_args()
    for flag, value in (("--warehouse-id / E2E_WAREHOUSE_ID", a.warehouse_id),
                        ("--catalog / E2E_CATALOG", a.catalog),
                        ("--external-base / E2E_EXTERNAL_BASE", a.external_base)):
        if not (value or "").strip():
            p.error(f"{flag} is required (no default; see .env.e2e.example)")
    return a


# --------------------------------------------------------------------------- #
# SQL builders (pure) — shared by dry-run printing and live execution.
# --------------------------------------------------------------------------- #

def _create_schema_sql(catalog: str, schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}"


def _payload_expr(pad: int) -> str:
    """SQL for an INCOMPRESSIBLE ~``pad``-char payload (per-row random SHA-512 hex).

    Every row gets DIFFERENT high-entropy content: each 128-char block is
    ``sha2(concat(id, rand(), <k>), 512)`` (hex), driven off ``id``/``rand()`` so
    no two rows repeat and Delta/Parquet cannot compress it away. Wider payloads
    concatenate several distinct blocks (different salt ``<k>``) and ``substr`` to
    ``pad`` — random hex does not compress like a repeated char, so real on-disk
    bytes track the payload width.
    """
    nblocks = max(1, -(-pad // 128))  # ceil(pad / 128); each SHA-512 hex block is 128 chars
    blocks = " || ".join(
        f"sha2(concat(CAST(id AS STRING), CAST(rand() AS STRING), '{k}'), 512)"
        for k in range(nblocks)
    )
    return f"substr({blocks}, 1, {pad})"


def _select_sql(rows: int, pad: int) -> str:
    """The shared SELECT body (incompressible payload) for one estate table."""
    return (
        f"SELECT id,\n"
        f"       CAST(id % 97 AS INT) AS category_id,\n"
        f"       rand() * 1000.0 AS amount,\n"
        f"       date_add(DATE'2024-01-01', CAST(id % 365 AS INT)) AS event_date,\n"
        f"       {_payload_expr(pad)} AS payload\n"
        f"FROM range({rows})"
    )


# --------------------------------------------------------------------------- #
# Path-based-access fixtures (Task 6) — pure SQL builders shared by dry-run + live.
# --------------------------------------------------------------------------- #
# The committed notebook lives here (repo path); the driver reads it, patches the base
# literal if --external-base differs, and uploads it for the source scan + deep link.
_PATHACCESS_NOTEBOOK_SRC = os.path.join(
    os.path.dirname(__file__), "..", "notebooks", "e2e", "read_by_path_real.py")
_PATHACCESS_WS_DIR = ".mtm_e2e_real"          # under /Workspace/Users/<current_user>/
_PATHACCESS_WS_NAME = "read_by_path_real"


def _pathaccess_create_table_sql(catalog: str, external_base: str, name: str) -> str:
    """CTAS for one small (S-tier) REAL Delta fixture at its external path LOCATION."""
    fq = f"{catalog}.{re_.PATH_ACCESS_SCHEMA}.{name}"
    loc = re_.path_access_fixture_location(external_base, name)
    rows, pad = re_.rows_pad_for(re_.SIZE_TIERS[1]["target_bytes"])   # S-tier ≈ 1MB nominal
    return f"CREATE OR REPLACE TABLE {fq}\nUSING DELTA LOCATION '{loc}' AS\n" + _select_sql(rows, pad)


def _pathaccess_read_by_path_sql(external_base: str, name: str) -> str:
    """A genuine BY-PATH read (delta.`<path>`) so the profiler flags has_path_access."""
    loc = re_.path_access_fixture_location(external_base, name)
    return f"SELECT count(*) FROM delta.`{loc}`"


def _dlt_src_bronze_create_sql(catalog: str, external_base: str) -> str:
    """Idempotent CTAS for the EXTERNAL Delta source the deployed DLT pipeline reads from.

    Mirrors ``seed_src_bronze`` in ``e2e_dlt_pipeline.py`` — ``IF NOT EXISTS`` so re-runs reuse
    the same external table + files. Starting the pipeline over this table establishes a real
    PIPELINE read-lineage edge the profiler attributes.
    """
    fq = f"{catalog}.{DLT_SCHEMA}.{DLT_SRC_BRONZE}"
    loc = f"{external_base.rstrip('/')}/{DLT_SCHEMA}/{DLT_SRC_BRONZE}"
    return (
        f"CREATE TABLE IF NOT EXISTS {fq} USING DELTA LOCATION '{loc}' AS "
        f"SELECT id, CAST(id % 97 AS INT) AS category_id, rand() * 1000 AS amount, "
        f"current_timestamp() AS event_ts FROM range(2000)"
    )


# --------------------------------------------------------------------------- #
# Lineage-edge COUNT(*) builders (pure) — shared by dry-run printing + live polling.
# Each returns (edge_name, count_sql). A count>0 means that representative edge has
# propagated into system.access.table_lineage. Mirrors e2e_dlt_pipeline.lineage_pipeline_count.
# --------------------------------------------------------------------------- #

def _lineage_pipeline_edge_sql(catalog: str) -> str:
    """PIPELINE read edge: the deployed DLT pipeline consumed e2e_dlt.src_bronze."""
    return (
        f"SELECT COUNT(*) FROM system.access.table_lineage "
        f"WHERE source_table_full_name='{catalog}.{DLT_SCHEMA}.{DLT_SRC_BRONZE}' "
        f"AND entity_type IN ('PIPELINE','DLT_PIPELINE','LAKEFLOW_PIPELINE','DLT') "
        f"AND event_date >= date_sub(current_date(), 3)"
    )


def _lineage_job_edge_sql(catalog: str) -> str:
    """JOB write edge: the streaming-seed job wrote e2e_stream.src_01."""
    return (
        f"SELECT COUNT(*) FROM system.access.table_lineage "
        f"WHERE target_table_full_name='{catalog}.{STREAM_SCHEMA}.{STREAM_SEED_TABLE}' "
        f"AND entity_type IN ('JOB','NOTEBOOK') "
        f"AND event_date >= date_sub(current_date(), 3)"
    )


def _lineage_path_edge_sql(catalog: str) -> str:
    """PATH read edge: a by-path read of a path-access fixture (source_path set)."""
    return (
        f"SELECT COUNT(*) FROM system.access.table_lineage "
        f"WHERE source_path IS NOT NULL "
        f"AND source_path LIKE '%{re_.PATH_ACCESS_SCHEMA}%' "
        f"AND event_date >= date_sub(current_date(), 3)"
    )


def _lineage_edges(catalog: str) -> list[tuple[str, str]]:
    """The three representative estate lineage edges as (edge_name, count_sql), pure."""
    return [
        ("pipeline", _lineage_pipeline_edge_sql(catalog)),
        ("job", _lineage_job_edge_sql(catalog)),
        ("path", _lineage_path_edge_sql(catalog)),
    ]


def _optimize_sql(fq: str) -> str:
    """Compact one Delta table's small files (warehouse/user-attributed OPTIMIZE commit)."""
    return f"OPTIMIZE {fq}"


def _enable_po_sql(catalog: str) -> str:
    """Turn on schema-level Predictive Optimization (a freezable compaction actor)."""
    return f"ALTER SCHEMA {catalog}.{PO_SCHEMA} ENABLE PREDICTIVE OPTIMIZATION"


def _create_share_sql() -> str:
    """Create the D2D share (idempotent)."""
    return f"CREATE SHARE IF NOT EXISTS {SHARE_NAME}"


def _share_add_table_sql(fq: str) -> str:
    """Add a table to the share, keeping its history so recipients get time-travel."""
    return f"ALTER SHARE {SHARE_NAME} ADD TABLE {fq} WITH HISTORY"


def _create_recipient_sql() -> str:
    """Best-effort recipient for the share (idempotent)."""
    return f"CREATE RECIPIENT IF NOT EXISTS {SHARE_RECIPIENT}"


def _share_target(catalog: str, plan: list[dict]) -> str | None:
    """Deterministic share target: the FIRST Delta table (list order) of ``SHARE_SCHEMA``.

    The SHARE schema is Delta and always seeds at least one table, so this normally resolves
    to ``<catalog>.e2e_real_share.share_000``. Returns None if the plan has no such table.
    """
    first = next(
        (t for t in plan
         if t.get("is_delta", True) and t["catalog_relative_schema"] == SHARE_SCHEMA),
        None,
    )
    return f"{catalog}.{first['full_name_suffix']}" if first is not None else None


def _create_table_stmts(catalog: str, external_base: str, spec: dict) -> list[str]:
    """Build the statement(s) that (re)create one estate table.

    Delta tables use ``CREATE OR REPLACE ... AS SELECT`` (the Delta log reconciles
    on re-run). Non-Delta (PARQUET) tables have no Delta log, so a re-run of
    ``CREATE OR REPLACE ... AS SELECT`` could leave stale data files at the fixed
    LOCATION and duplicate rows — instead create the table once
    (``CREATE TABLE IF NOT EXISTS ... (schema) USING PARQUET LOCATION``) and load
    it with ``INSERT OVERWRITE`` (which replaces the directory's data files),
    making the non-Delta path idempotent on re-provision.
    """
    schema = spec["catalog_relative_schema"]
    name = spec["table_name"]
    fq = f"{catalog}.{schema}.{name}"
    loc = f"{external_base}/{schema}/{name}"
    rows, pad = re_.rows_pad_for(spec["target_bytes"])
    if spec.get("is_delta", True):
        return [
            f"CREATE OR REPLACE TABLE {fq}\n"
            f"USING DELTA LOCATION '{loc}' AS\n" + _select_sql(rows, pad)
        ]
    create = (
        f"CREATE TABLE IF NOT EXISTS {fq} (\n"
        f"  id BIGINT,\n"
        f"  category_id INT,\n"
        f"  amount DOUBLE,\n"
        f"  event_date DATE,\n"
        f"  payload STRING\n"
        f") USING PARQUET LOCATION '{loc}'"
    )
    insert = f"INSERT OVERWRITE TABLE {fq}\n" + _select_sql(rows, pad)
    return [create, insert]


# --------------------------------------------------------------------------- #
# SQL statement API helpers (live only; imported lazily so --dry-run is offline).
# --------------------------------------------------------------------------- #

def _make_runner(profile: str, warehouse_id: str):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient(profile=profile)

    def _run(sql: str, label: str, retries: int = 0) -> bool:
        # ``retries`` > 0 retries ONLY the transient DROP-then-CREATE async-delete race
        # (Option 2 settle-wait/retry); every other failure still FAILs fast. retries=0 is the
        # historical no-retry behaviour for all non-provision callers.
        delays = re_.provision_retry_delays(retries) if retries else []
        attempt = 0
        while True:
            t0 = time.time()
            resp = w.statement_execution.execute_statement(
                warehouse_id=warehouse_id, statement=sql, wait_timeout="50s")
            sid = resp.statement_id
            while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
                time.sleep(3)
                resp = w.statement_execution.get_statement(sid)
            st = resp.status.state if resp.status else None
            if st == StatementState.SUCCEEDED:
                print(f"  ok   {label}  ({time.time()-t0:.0f}s)")
                return True
            err = resp.status.error.message if (resp.status and resp.status.error) else st
            if attempt < len(delays) and re_.is_retryable_provision_error(str(err)):
                delay = delays[attempt]
                print(f"  retry {label}: transient async-delete race — backing off {delay:.0f}s "
                      f"[attempt {attempt + 1}/{len(delays)}]: {err}")
                time.sleep(delay)
                attempt += 1
                continue
            print(f"  FAIL {label}: {err}")
            return False

    return _run


def _make_scalar(profile: str, warehouse_id: str):
    """Live-only scalar SQL reader: returns a ``_scalar(sql)->value`` closure.

    SDK imports are LAZY (inside this function) so ``--dry-run`` stays fully offline. Mirrors
    ``sql``/``scalar`` in ``e2e_dlt_pipeline.py`` — execute + poll until the statement settles,
    then return the first cell of the first row (or None). Unlike ``_make_runner`` (returns a
    bool ok/FAIL), this returns the queried VALUE so the lineage poll can read COUNT(*)s.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient(profile=profile)

    def _scalar(sql: str):
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=sql, wait_timeout="50s")
        sid = resp.statement_id
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            resp = w.statement_execution.get_statement(sid)
        st = resp.status.state if resp.status else None
        if st != StatementState.SUCCEEDED:
            err = resp.status.error.message if (resp.status and resp.status.error) else st
            raise RuntimeError(f"SQL failed [{st}]: {err}\n  {sql}")
        rows = (resp.result.data_array if resp.result else None) or []
        return rows[0][0] if rows and rows[0] else None

    return _scalar


def _make_query(profile: str, warehouse_id: str):
    """Live-only multi-row SQL reader: returns a ``_query(sql)->list[list[str]]`` closure.

    SDK imports are LAZY (inside this function) so ``--dry-run`` stays fully offline. Like
    ``_make_scalar`` but returns the FULL ``data_array`` (every row, every cell) — the fixture
    recipes need to read batch membership + open-action lists, not just one cell.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient(profile=profile)

    def _query(sql: str) -> list:
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=sql, wait_timeout="50s")
        sid = resp.statement_id
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            resp = w.statement_execution.get_statement(sid)
        st = resp.status.state if resp.status else None
        if st != StatementState.SUCCEEDED:
            err = resp.status.error.message if (resp.status and resp.status.error) else st
            raise RuntimeError(f"SQL failed [{st}]: {err}\n  {sql}")
        return (resp.result.data_array if resp.result else None) or []

    return _query


def _make_describe_detail_size(profile: str, warehouse_id: str):
    """Live-only DESCRIBE DETAIL size reader: returns ``f(full_name)->int | None``.

    SDK imports are LAZY (inside this function) so ``--dry-run`` stays fully offline. Spark SQL
    cannot wrap ``DESCRIBE DETAIL`` in a subquery, so we run it directly, locate the
    ``sizeInBytes`` column by NAME from ``resp.manifest.schema.columns`` (each has ``.name`` +
    ``.position``), and return ``int(cell)`` for that column in the first row — or ``None`` when
    the column/row is absent or the cell is null.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient(profile=profile)

    def _dd_size(full_name: str) -> int | None:
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=f"DESCRIBE DETAIL {full_name}",
            wait_timeout="50s")
        sid = resp.statement_id
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(2)
            resp = w.statement_execution.get_statement(sid)
        st = resp.status.state if resp.status else None
        if st != StatementState.SUCCEEDED:
            err = resp.status.error.message if (resp.status and resp.status.error) else st
            raise RuntimeError(f"DESCRIBE DETAIL failed [{st}]: {err}\n  {full_name}")
        cols = (resp.manifest.schema.columns
                if resp.manifest and resp.manifest.schema else None) or []
        pos = next((c.position for c in cols if c.name == "sizeInBytes"), None)
        if pos is None:
            return None
        rows = (resp.result.data_array if resp.result else None) or []
        if not rows or not rows[0] or pos >= len(rows[0]):
            return None
        cell = rows[0][pos]
        return int(cell) if cell is not None else None

    return _dd_size


def _run_deployed_job(a: argparse.Namespace, name_suffix: str,
                      params: dict | None = None, timeout_s: int = 3600) -> str:
    """Resolve a DEPLOYED job by name suffix and run it to a terminal state (live only).

    SDK imports are LAZY (inside this function) so ``--dry-run`` stays fully offline. REUSES the
    already-deployed bundle job (never creates one): lists jobs, matches the one whose
    ``settings.name`` ends with ``name_suffix``, ``run_now`` with ``job_parameters`` (overrides),
    polls ``RunLifeCycleState`` until terminal, and returns the ``result_state`` string
    ("SUCCESS"/...). Mirrors ``resolve_job_ids`` / ``run_job`` in ``e2e_streaming.py``. Fails loud
    (raises ``SystemExit``) when the job is not deployed. Resolves and runs any deployed job by
    name suffix (profiler / source scan / wave planner).
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    job_id = None
    for j in w.jobs.list():
        name = j.settings.name if j.settings else ""
        if name and name.endswith(name_suffix):
            job_id = j.job_id
            break
    if job_id is None:
        raise SystemExit(f"deployed job with name suffix {name_suffix!r} not found — deploy the bundle to the e2e target first")

    run_id = w.jobs.run_now(job_id=job_id, job_parameters=(params or None)).run_id
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        run = w.jobs.get_run(run_id=run_id)
        life = run.state.life_cycle_state if run.state else None
        if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                    RunLifeCycleState.INTERNAL_ERROR):
            result = run.state.result_state if run.state else None
            return result.value if result else "UNKNOWN"
        time.sleep(10)
    raise SystemExit(f"deployed job run {run_id} did not finish within {timeout_s}s")


# --------------------------------------------------------------------------- #
# Stage: provision
# --------------------------------------------------------------------------- #

def _provision(a: argparse.Namespace, plan: list[dict]) -> None:
    catalog = a.catalog
    base = a.external_base
    schemas = sorted({t["catalog_relative_schema"] for t in plan})

    if a.dry_run:
        print(f"[dispatch] provision (dry-run) — {len(plan)} tables across {len(schemas)} schemas")
        print(f"-- {_create_schema_sql(catalog, CONTROL_SCHEMA)};")
        for schema in schemas:
            print(f"-- {_create_schema_sql(catalog, schema)};")
        for spec in plan:
            print(f"-- {spec['full_name_suffix']}  ({spec['size_tier']}, "
                  f"delta={spec.get('is_delta', True)})")
            for stmt in _create_table_stmts(catalog, base, spec):
                print(stmt + ";")
            print()
        print(f"[provision] dry-run: would create {len(plan)} tables "
              f"across {len(schemas)} schemas + control schema {CONTROL_SCHEMA}")
        return

    run = _make_runner(a.profile, a.warehouse_id)
    run(_create_schema_sql(catalog, CONTROL_SCHEMA), f"control schema {CONTROL_SCHEMA}")
    for schema in schemas:
        run(_create_schema_sql(catalog, schema), f"schema {schema}")
    n = 0
    for spec in plan:
        label = f"{spec['full_name_suffix']} ({spec['size_tier']})"
        stmts = _create_table_stmts(catalog, base, spec)
        ok = all(
            run(s, label if len(stmts) == 1 else f"{label} [{k + 1}/{len(stmts)}]",
                retries=re_.PROVISION_MAX_RETRIES)
            for k, s in enumerate(stmts)
        )
        if ok:
            n += 1
    print(f"[provision] created {n}/{len(plan)} external tables across "
          f"{len(schemas)} schemas + control schema {CONTROL_SCHEMA}")


# --------------------------------------------------------------------------- #
# Stage: activity — real path-based-access papercut (notebook + job + by-path reads)
# --------------------------------------------------------------------------- #

def _upload_and_submit_notebook(a: argparse.Namespace) -> None:
    """Patch the base literal (if needed), upload the notebook, best-effort submit a run.

    SDK imports are LAZY (inside this live path) so ``--dry-run`` stays fully offline.
    The warehouse by-path reads already establish the path edge; the notebook upload adds
    the scanner's literal + a resolvable deep link, and the (best-effort) job run adds JOB
    attribution. Serverless-submit failures are tolerated with a clear note.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.workspace import ImportFormat, Language
    from databricks.sdk.service.jobs import SubmitTask, NotebookTask

    w = WorkspaceClient(profile=a.profile)
    with open(_PATHACCESS_NOTEBOOK_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    # Keep the committed notebook's literals matching the tables we provision. Anchor on the
    # literal ACTUALLY embedded in the notebook (NOTEBOOK_BASE_LITERAL), never the env-derived
    # default: if E2E_EXTERNAL_BASE is set, a.external_base could equal _DEFAULT_EXTERNAL_BASE
    # while the notebook still hardcodes the e2e literal. Always replace — a no-op when the
    # active base already equals the embedded literal, correct when they differ.
    source = source.replace(NOTEBOOK_BASE_LITERAL, a.external_base)

    user = w.current_user.me().user_name
    ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_PATHACCESS_WS_NAME}"
    w.workspace.mkdirs(f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}")
    w.workspace.upload(ws_path, source.encode("utf-8"),
                       format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
    print(f"  ok   uploaded notebook -> {ws_path}")

    try:
        resp = w.jobs.submit(
            run_name="mtm_e2e_real :: read_by_path_real",
            tasks=[SubmitTask(task_key="read_by_path",
                              notebook_task=NotebookTask(notebook_path=ws_path))])
        print(f"  ok   submitted one-off run: run_id={resp.run_id} "
              f"(job attribution for path-access source scan)")
    except Exception as e:   # serverless-submit can fail; the by-path read + notebook suffice
        print(f"  note submit skipped ({type(e).__name__}: {e}); "
              f"warehouse by-path read already established the path edge, "
              f"and the uploaded notebook gives the scanner its literal + deep link")


def _run_stream_seed_job(a: argparse.Namespace) -> None:
    """Resolve the deployed streaming-seed job by name suffix and run it (live only).

    SDK imports are LAZY (inside this live path) so ``--dry-run`` stays fully offline. This
    REUSES the already-deployed bundle job (it never creates a new job): running it writes a
    real streaming commit to ``<catalog>.e2e_stream.src_01``, which the profiler detects as a
    STRUCTURED_STREAMING WRITE. Mirrors ``resolve_job_ids`` / ``run_job`` in ``e2e_streaming.py``
    but only for this one job. Resilient by design — a missing job or a run failure prints a
    clear note and returns (never raises), so the other activity sub-steps still stand.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    job_id = None
    for j in w.jobs.list():
        name = j.settings.name if j.settings else ""
        if name and name.endswith(STREAM_SEED_JOB_SUFFIX):
            job_id = j.job_id
            break
    if job_id is None:
        print(f"  note streaming seed job not found (name ending {STREAM_SEED_JOB_SUFFIX!r}); "
              f"deploy the bundle to the e2e target first — skipping streaming sub-step")
        return

    try:
        # reset=true: this is the INITIAL provisioning seed, so it must build a clean table on a
        # FRESH external subpath (the job default is reset=false so product-driven RESTART_STREAM
        # re-launches are non-destructive; the initial seed must opt in). available_now = one bounded
        # commit (the default) — this is detection seeding, not the --live freeze bracket.
        run_id = w.jobs.run_now(
            job_id=job_id, job_parameters={"reset": "true"}).run_id
        print(f"  ok   started streaming seed job {job_id}: run_id={run_id}")
        # Block (generously) so the streaming commit lands before any later profiling; stay
        # resilient — a timeout just leaves the run going and the profiler can pick it up later.
        timeout_s = 1800
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            run = w.jobs.get_run(run_id=run_id)
            life = run.state.life_cycle_state if run.state else None
            if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                        RunLifeCycleState.INTERNAL_ERROR):
                result = run.state.result_state if run.state else None
                print(f"  ok   streaming seed run finished: {result.value if result else 'UNKNOWN'} "
                      f"(real streaming commit -> {a.catalog}.{STREAM_SCHEMA}.{STREAM_SEED_TABLE})")
                return
            time.sleep(10)
        print(f"  note streaming seed run {run_id} still going after {timeout_s}s; leaving it "
              f"running (the streaming commit / detection can settle later)")
    except Exception as e:   # never crash the stage — the other sub-steps already ran
        print(f"  note streaming seed run skipped ({type(e).__name__}: {e}); "
              f"other activity sub-steps already ran")


def _start_dlt_pipeline(a: argparse.Namespace) -> None:
    """Resolve the deployed DLT pipeline by name suffix and start a non-refresh update (live only).

    SDK imports are LAZY (inside this live path) so ``--dry-run`` stays fully offline. This
    REUSES the already-deployed bundle pipeline (it never creates one): the update establishes a
    real PIPELINE read-lineage edge from ``<catalog>.e2e_dlt.src_bronze``. Fire-and-forget — it
    prints the update id and returns WITHOUT blocking (that lineage latency is handled later by
    the ``lineage_wait`` stage). ``full_refresh`` is ALWAYS False (a full refresh reprocesses
    from scratch). Mirrors ``resolve_pipeline_id`` in ``e2e_dlt_pipeline.py``. Resilient — a
    missing pipeline or a start failure prints a clear note and returns (never raises).
    """
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=a.profile)
    pipeline_id = None
    for p in w.pipelines.list_pipelines():
        if p.name and p.name.endswith(DLT_PIPELINE_SUFFIX):
            pipeline_id = p.pipeline_id
            break
    if pipeline_id is None:
        print(f"  note dlt pipeline not found (name ending {DLT_PIPELINE_SUFFIX!r}); "
              f"deploy the bundle to the e2e target first — skipping DLT pipeline start")
        return

    try:
        upd = w.pipelines.start_update(pipeline_id=pipeline_id, full_refresh=False)
        print(f"  ok   started dlt pipeline {pipeline_id}: update_id={upd.update_id} "
              f"(read-lineage edge from {a.catalog}.{DLT_SCHEMA}.{DLT_SRC_BRONZE}; "
              f"lineage settle handled by the lineage_wait stage)")
    except Exception as e:   # never crash the stage — the other sub-steps already ran
        print(f"  note dlt pipeline start skipped ({type(e).__name__}: {e}); "
              f"other activity sub-steps already ran")


def _activity_pathaccess(a: argparse.Namespace) -> None:
    catalog = a.catalog
    base = a.external_base
    # NB: e2e_real_pathaccess also holds random-plan tables from ``provision``; the FIXED
    # PATH_ACCESS_FIXTURES here are the ones the notebook/deep-links deliberately target.
    fixtures = re_.PATH_ACCESS_FIXTURES

    if a.dry_run:
        print(f"[dispatch] activity (dry-run) — path-based access: "
              f"{len(fixtures)} fixtures under {catalog}.{re_.PATH_ACCESS_SCHEMA}")
        print(f"-- {_create_schema_sql(catalog, re_.PATH_ACCESS_SCHEMA)};")
        for name in fixtures:
            print(_pathaccess_create_table_sql(catalog, base, name) + ";")
            print()
        print("-- genuine BY-PATH reads (establish has_path_access lineage/query edge):")
        for name in fixtures:
            print(_pathaccess_read_by_path_sql(base, name) + ";")
        print(f"-- would upload {_PATHACCESS_NOTEBOOK_SRC} -> "
              f"/Workspace/Users/<current_user>/{_PATHACCESS_WS_DIR}/{_PATHACCESS_WS_NAME} "
              f"(SOURCE/PYTHON, overwrite) then best-effort submit a one-off run for JOB attribution")
        if base != NOTEBOOK_BASE_LITERAL:
            print(f"-- (would string-replace embedded notebook base {NOTEBOOK_BASE_LITERAL!r} "
                  f"with {base!r} in the notebook source so literals match)")
        print(f"[activity] dry-run: would create {len(fixtures)} path-access fixtures + "
              f"by-path reads + notebook upload/submit")
        return

    run = _make_runner(a.profile, a.warehouse_id)
    run(_create_schema_sql(catalog, re_.PATH_ACCESS_SCHEMA), f"schema {re_.PATH_ACCESS_SCHEMA}")
    for name in fixtures:
        run(_pathaccess_create_table_sql(catalog, base, name), f"fixture {name}")
    for name in fixtures:
        run(_pathaccess_read_by_path_sql(base, name), f"by-path read {name}")
    _upload_and_submit_notebook(a)
    print(f"[activity] created {len(fixtures)} path-access fixtures + by-path reads + notebook")


def _activity_streaming(a: argparse.Namespace) -> None:
    """Sub-step (b): REUSE the deployed streaming-seed job to write a real streaming commit."""
    catalog = a.catalog
    if a.dry_run:
        print(f"[dispatch] activity streaming (dry-run) — REUSE the deployed job whose name ends "
              f"{STREAM_SEED_JOB_SUFFIX!r}")
        print(f"-- would resolve that job by name suffix and run_now it (job_parameters="
              f"{{'reset': 'true'}} — initial clean seed on a fresh subpath); it writes a "
              f"real streaming commit to {catalog}.{STREAM_SCHEMA}.{STREAM_SEED_TABLE} "
              f"(profiler detects a STRUCTURED_STREAMING WRITE row)")
        print("[activity] dry-run: would run the deployed streaming seed job (reuse; no new job)")
        return
    _run_stream_seed_job(a)


def _activity_dlt(a: argparse.Namespace) -> None:
    """Sub-step (c): seed src_bronze (idempotent) + REUSE the deployed DLT pipeline (start_update)."""
    catalog = a.catalog
    base = a.external_base
    create_sql = _dlt_src_bronze_create_sql(catalog, base)
    if a.dry_run:
        print(f"[dispatch] activity dlt (dry-run) — seed src_bronze + REUSE the deployed pipeline "
              f"whose name ends {DLT_PIPELINE_SUFFIX!r}")
        print(f"-- {_create_schema_sql(catalog, DLT_SCHEMA)};")
        print(create_sql + ";")
        print(f"-- would resolve that pipeline by name suffix and start_update(full_refresh=False); "
              f"establishes a real PIPELINE read-lineage edge from "
              f"{catalog}.{DLT_SCHEMA}.{DLT_SRC_BRONZE} (not blocked here — lineage_wait handles settle)")
        print("[activity] dry-run: would seed src_bronze + start the deployed DLT pipeline (reuse)")
        return
    run = _make_runner(a.profile, a.warehouse_id)
    run(_create_schema_sql(catalog, DLT_SCHEMA), f"schema {DLT_SCHEMA}")
    run(create_sql, f"seed {DLT_SCHEMA}.{DLT_SRC_BRONZE}")
    _start_dlt_pipeline(a)


def _activity_compaction(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (d): real OPTIMIZE on the first EXISTING Delta table per target schema + PO.

    Creates the REAL compaction papercut the freeze bracket must cover: one warehouse/user-
    attributed OPTIMIZE commit per target schema (surfaced in ``DESCRIBE HISTORY`` as OPTIMIZE)
    plus one schema-level Predictive Optimization enable (the profiler attributes as a
    PREDICTIVE_OPTIMIZATION source). No data-deletion command of any kind — the papercut is
    left in place.

    RESILIENT target selection: per target schema, iterate the ordered candidate list from
    ``re_.compaction_candidates`` and run ``OPTIMIZE <fq>`` until ONE succeeds, then stop for that
    schema. A hardcoded first target can fail to exist (e.g. a re-seed race left the first-planned
    table un-provisioned) — falling through candidates guarantees a real compaction source as long
    as ANY Delta table in the schema exists. If every candidate in a schema fails, print a clear
    note and continue (degrade honestly — never raise). Where the workspace disallows PO, likewise
    print a note and continue without crashing the stage.
    """
    catalog = a.catalog
    candidates = re_.compaction_candidates(catalog, plan, COMPACTION_TARGET_SCHEMAS)
    if a.dry_run:
        print(f"[dispatch] activity compaction (dry-run) — real OPTIMIZE on the first EXISTING "
              f"Delta table across {len(candidates)} schema(s) + Predictive Optimization on "
              f"{catalog}.{PO_SCHEMA}")
        for schema, fqns in candidates:
            print(f"-- schema {schema}: try candidates in order until one OPTIMIZE succeeds "
                  f"({len(fqns)} candidate(s)):")
            for fq in fqns:
                print(_optimize_sql(fq) + ";")
        print("-- enable Predictive Optimization (schema-level compaction actor):")
        print(_enable_po_sql(catalog) + ";")
        print(f"[activity] dry-run: would OPTIMIZE the first existing Delta table per schema "
              f"across {len(candidates)} schema(s) + enable Predictive Optimization on {PO_SCHEMA}")
        return
    run = _make_runner(a.profile, a.warehouse_id)
    optimized: list[str] = []
    for schema, fqns in candidates:
        chosen = next((fq for fq in fqns if run(_optimize_sql(fq), f"optimize {fq}")), None)
        if chosen is not None:
            optimized.append(chosen)
        else:
            print(f"  note no OPTIMIZE succeeded for any of the {len(fqns)} Delta candidate(s) in "
                  f"{catalog}.{schema} (none provisioned?); no compaction source created for this "
                  f"schema — continuing")
    if not run(_enable_po_sql(catalog), f"enable Predictive Optimization {catalog}.{PO_SCHEMA}"):
        print(f"  note Predictive Optimization enable not allowed on {catalog}.{PO_SCHEMA}; "
              f"the OPTIMIZE commits already established a real compaction papercut — continuing")
    print(f"[activity] ran OPTIMIZE on {len(optimized)} schema(s) + Predictive Optimization on "
          f"{PO_SCHEMA}")


def _maintenance_targets(catalog: str, plan: list[dict]) -> list[str]:
    """All Delta tables of the pilot schema (``e2e_real_bronze``) in plan order (pure).

    The scheduled OPTIMIZE/VACUUM jobs target the FIRST of these; the vacuum-block probe VACUUMs
    ALL of them (so the whole pilot wave is uniformly blocked and the probe converts nothing).
    """
    cands = re_.compaction_candidates(catalog, plan, (PO_SCHEMA,))
    return cands[0][1] if cands else []


def _upload_maintenance_notebook(w) -> str:
    """Upload the ephemeral OPTIMIZE/VACUUM notebook under the estate's workspace dir; return its path."""
    from databricks.sdk.service.workspace import ImportFormat, Language

    with open(_MAINT_NOTEBOOK_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    user = w.current_user.me().user_name
    ws_dir = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}"
    ws_path = f"{ws_dir}/{_MAINT_WS_NAME}"
    w.workspace.mkdirs(ws_dir)
    w.workspace.upload(ws_path, source.encode("utf-8"),
                       format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
    return ws_path


def _find_job_id_by_name(w, name: str):
    """Exact-name job lookup (the estate maintenance jobs use fixed names). None when absent."""
    for j in w.jobs.list():
        if j.settings and j.settings.name == name:
            return j.job_id
    return None


def _create_or_get_scheduled_maintenance_job(w, name: str, notebook_path: str, operation: str,
                                             target: str):
    """Idempotently ensure ONE scheduled (UNPAUSED) maintenance job exists; return its job_id.

    The job runs the ephemeral notebook with ``operation`` (OPTIMIZE|VACUUM) + ``target``. It is
    created with a serverless notebook task and a CronSchedule left UNPAUSED (the freeze bracket
    toggles ``pause_status``). Re-seeding reuses the existing job by exact name (never duplicates).
    """
    from databricks.sdk.service.jobs import (CronSchedule, JobSettings, NotebookTask,
                                             PauseStatus, Task)

    existing = _find_job_id_by_name(w, name)
    task = Task(
        task_key="maintenance",
        notebook_task=NotebookTask(
            notebook_path=notebook_path,
            base_parameters={"operation": operation, "target": target},
        ),
    )
    schedule = CronSchedule(quartz_cron_expression=MAINT_CRON, timezone_id=MAINT_TZ,
                            pause_status=PauseStatus.UNPAUSED)
    if existing is not None:
        w.jobs.update(job_id=existing,
                      new_settings=JobSettings(name=name, tasks=[task], schedule=schedule))
        return existing
    return w.jobs.create(name=name, tasks=[task], schedule=schedule).job_id


def _writer_target(catalog: str, plan: list[dict]) -> Optional[str]:
    """The pilot Delta table the scheduled batch writer INSERTs into (pure).

    Reuses the maintenance target (first pilot Delta table). The writer job is a DISTINCT job id,
    so it produces a distinct ``writer_sources`` row (SCHEDULED_JOB) — no collision with the
    OPTIMIZE/VACUUM ``compaction_sources`` actors on the same table.
    """
    targets = _maintenance_targets(catalog, plan)
    return targets[0] if targets else None


def _schedule_twin_targets(catalog: str, plan: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """(far_out_table, imminent_table) — two DISTINCT pilot tables, each the sole schedule writer.

    Uses pilot Delta tables ``[1]`` and ``[2]`` (``[0]`` already carries the maintenance jobs + the
    M4 writer). Returns ``(None, None)`` when the pilot has fewer than 3 Delta tables (assertion then
    degrades to WARN). Pure + deterministic — the seeder and the ``_e2e`` assertion both call it.
    """
    tables = _maintenance_targets(catalog, plan)
    if len(tables) < 3:
        return (None, None)
    return (tables[1], tables[2])


def _writer_wave_target(catalog: str, plan: list[dict]) -> Optional[str]:
    """The single isolated Delta table the writer-wave imminent writer INSERTs into (pure).

    First Delta table of the tiny ``e2e_real_writer`` schema. That schema exists SOLELY to give the
    wave planner a small, papercut-free wave holding exactly this one scheduled writer, so the
    Convert-Gate pause banner (L4 A1) names one job in its own runnable wave — instead of the writer
    being buried in the dense bronze domain wave. Returns None when the plan has no Delta table there
    (the seed/fixture then degrades to WARN). Pure + deterministic.
    """
    cands = re_.compaction_candidates(catalog, plan, (WRITER_WAVE_SCHEMA,))
    return cands[0][1][0] if cands and cands[0][1] else None


def _upload_writer_notebook(w) -> str:
    """Upload the ephemeral writer-seed notebook under the estate's workspace dir; return its path."""
    from databricks.sdk.service.workspace import ImportFormat, Language

    with open(_WRITER_NOTEBOOK_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    user = w.current_user.me().user_name
    ws_dir = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}"
    ws_path = f"{ws_dir}/{_WRITER_WS_NAME}"
    w.workspace.mkdirs(ws_dir)
    w.workspace.upload(ws_path, source.encode("utf-8"),
                       format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
    return ws_path


def _create_or_get_writer_job(w, name: str, notebook_path: str, target: str, cron: str = MAINT_CRON):
    """Idempotently ensure ONE scheduled (UNPAUSED) batch writer job exists; return its job_id.

    The job runs the ephemeral writer notebook with ``target``. Created with a serverless notebook
    task + a CronSchedule from the caller-supplied ``cron`` (default ``MAINT_CRON``, weekly) left
    UNPAUSED. A weekly cron fires only via the immediate ``run_now`` (for lineage) within a run; a
    sub-daily cron (e.g. the imminent schedule twin) will fire repeatedly until the schedule is
    paused. Re-seeding reuses by exact name.
    """
    from databricks.sdk.service.jobs import (CronSchedule, JobSettings, NotebookTask,
                                             PauseStatus, Task)

    existing = _find_job_id_by_name(w, name)
    task = Task(
        task_key="writer",
        notebook_task=NotebookTask(
            notebook_path=notebook_path,
            base_parameters={"target": target},
        ),
    )
    schedule = CronSchedule(quartz_cron_expression=cron, timezone_id=MAINT_TZ,
                            pause_status=PauseStatus.UNPAUSED)
    if existing is not None:
        w.jobs.update(job_id=existing,
                      new_settings=JobSettings(name=name, tasks=[task], schedule=schedule))
        return existing
    return w.jobs.create(name=name, tasks=[task], schedule=schedule).job_id


def _seed_maintenance_jobs(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (f): seed + run two REAL scheduled maintenance jobs (OPTIMIZE + VACUUM) on the pilot.

    This is what genuinely exercises the ``PAUSE_JOB``/``RESUME_JOB`` freeze bracket end-to-end: a
    scheduled JOB (not a manual OPTIMIZE) is attributed in ``DESCRIBE HISTORY``, discovered as a
    ``compaction_sources`` JOB actor, paused by ``03c_freeze`` and resumed by ``05b_unfreeze_verify``.
    Both jobs are created UNPAUSED and run once. Degrades honestly (WARN + continue) if the workspace
    disallows job create/run; never raises (the other activity sub-steps already stand).
    """
    catalog = a.catalog
    targets = _maintenance_targets(catalog, plan)
    target = targets[0] if targets else None

    if a.dry_run:
        print(f"[dispatch] activity maintenance-jobs (dry-run) — seed two SCHEDULED jobs "
              f"(UNPAUSED) on the pilot schema {catalog}.{PO_SCHEMA} + run each once:")
        print(f"-- upload {_MAINT_NOTEBOOK_SRC} -> "
              f"/Workspace/Users/<current_user>/{_PATHACCESS_WS_DIR}/{_MAINT_WS_NAME}")
        print(f"-- jobs.create name={MAINT_OPTIMIZE_JOB_NAME!r} (notebook OPTIMIZE {target}), "
              f"schedule cron={MAINT_CRON!r} pause_status=UNPAUSED; then run_now once")
        print(f"-- jobs.create name={MAINT_VACUUM_JOB_NAME!r} (notebook op=VACUUM on {target}), "
              f"schedule cron={MAINT_CRON!r} pause_status=UNPAUSED; then run_now once")
        print(f"[activity] dry-run: would seed + run OPTIMIZE and VACUUM scheduled jobs on "
              f"{target or '<no pilot Delta table>'} (JOB attribution for the freeze bracket)")
        return

    if target is None:
        print(f"[activity] maintenance-jobs: no Delta table in {catalog}.{PO_SCHEMA}; "
              f"skipping scheduled OPTIMIZE/VACUUM job seed")
        return

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    try:
        nb_path = _upload_maintenance_notebook(w)
        print(f"  ok   uploaded maintenance notebook -> {nb_path}")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not upload maintenance notebook ({type(e).__name__}: {e}); "
              f"skipping scheduled-job seed — the manual OPTIMIZE papercut still stands")
        return

    def _seed_one(name: str, operation: str) -> None:
        try:
            job_id = _create_or_get_scheduled_maintenance_job(w, name, nb_path, operation, target)
            print(f"  ok   ensured scheduled job {name!r} (job_id={job_id}, UNPAUSED)")
            run_id = w.jobs.run_now(job_id=job_id).run_id
            t0 = time.time()
            while time.time() - t0 < 1800:
                run = w.jobs.get_run(run_id=run_id)
                life = run.state.life_cycle_state if run.state else None
                if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                            RunLifeCycleState.INTERNAL_ERROR):
                    result = run.state.result_state if run.state else None
                    print(f"  ok   {operation} job run finished: "
                          f"{result.value if result else 'UNKNOWN'} (JOB attribution -> {target})")
                    return
                time.sleep(10)
            print(f"  note {operation} job run still going after 1800s; the profiler can pick up "
                  f"the JOB-attributed commit later — continuing")
        except Exception as e:   # noqa: BLE001 — degrade honestly
            print(f"  WARN could not seed/run {operation} job {name!r} "
                  f"({type(e).__name__}: {e}); continuing")

    _seed_one(MAINT_OPTIMIZE_JOB_NAME, "OPTIMIZE")
    _seed_one(MAINT_VACUUM_JOB_NAME, "VACUUM")
    print(f"[activity] seeded + ran scheduled OPTIMIZE + VACUUM jobs on {target} "
          f"(real PAUSE_JOB/RESUME_JOB coverage for the freeze bracket)")


def _seed_writer_job(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (g): seed + run ONE real scheduled batch WRITER job (INSERT) on the pilot.

    This exercises the ``STOP_WRITER_JOB``/``RESTART_WRITER_JOB`` freeze bracket end-to-end: a
    scheduled JOB that WRITEs (not OPTIMIZEs) is attributed in ``system.access.table_lineage`` as a
    ``SCHEDULED_JOB`` writer, discovered into ``writer_sources``, blocked PRE_CONVERT, paused by
    ``03c_freeze`` and resumed by ``05b_unfreeze_verify``. Created UNPAUSED, run once for lineage.
    Degrades honestly (WARN + continue) if the workspace disallows job create/run; never raises.
    """
    catalog = a.catalog
    target = _writer_target(catalog, plan)

    if a.dry_run:
        print(f"[dispatch] activity writer-job (dry-run) — seed ONE SCHEDULED batch writer "
              f"(UNPAUSED) on the pilot schema {catalog}.{PO_SCHEMA} + run once:")
        print(f"-- upload {_WRITER_NOTEBOOK_SRC} -> "
              f"/Workspace/Users/<current_user>/{_PATHACCESS_WS_DIR}/{_WRITER_WS_NAME}")
        print(f"-- jobs.create name={WRITER_JOB_NAME!r} (notebook INSERT on {target}), "
              f"schedule cron={MAINT_CRON!r} pause_status=UNPAUSED; then run_now once")
        print(f"[activity] dry-run: would seed + run a scheduled batch writer job on "
              f"{target or '<no pilot Delta table>'} (SCHEDULED_JOB writer attribution -> "
              f"STOP_WRITER_JOB/RESTART_WRITER_JOB bracket)")
        return

    if target is None:
        print(f"[activity] writer-job: no Delta table in {catalog}.{PO_SCHEMA}; "
              f"skipping scheduled batch-writer job seed")
        return

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    try:
        nb_path = _upload_writer_notebook(w)
        print(f"  ok   uploaded writer notebook -> {nb_path}")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not upload writer notebook ({type(e).__name__}: {e}); "
              f"skipping batch-writer job seed")
        return

    try:
        job_id = _create_or_get_writer_job(w, WRITER_JOB_NAME, nb_path, target)
        print(f"  ok   ensured scheduled writer job {WRITER_JOB_NAME!r} (job_id={job_id}, UNPAUSED)")
        run_id = w.jobs.run_now(job_id=job_id).run_id
        t0 = time.time()
        while time.time() - t0 < 1800:
            run = w.jobs.get_run(run_id=run_id)
            life = run.state.life_cycle_state if run.state else None
            if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                        RunLifeCycleState.INTERNAL_ERROR):
                result = run.state.result_state if run.state else None
                print(f"  ok   writer job run finished: "
                      f"{result.value if result else 'UNKNOWN'} (WRITE attribution -> {target})")
                break
            time.sleep(10)
        else:
            print(f"  note writer job run still going after 1800s; the profiler can pick up the "
                  f"WRITE-attributed edge later — continuing")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not seed/run writer job {WRITER_JOB_NAME!r} "
              f"({type(e).__name__}: {e}); continuing")

    print(f"[activity] seeded + ran scheduled batch writer job on {target} "
          f"(real STOP_WRITER_JOB/RESTART_WRITER_JOB coverage for the freeze bracket)")


def _seed_schedule_twins(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (h): seed + run TWO real scheduled writer jobs on distinct pilot tables (feature G).

    A FAR-OUT twin (daily cron ~8h out -> SAFE_NOW) and an IMMINENT twin (every-5-min cron ->
    WARN_WINDOW). The profiler's schedule pass stamps ``writer_schedule_verdict`` /
    ``next_scheduled_write_at`` on each twin's manifest row from the live cron + Jobs-API read; the
    ``e2e`` stage asserts the two deterministic verdicts. Created UNPAUSED, each run once for lineage.
    Degrades honestly (WARN + continue) if the workspace disallows job create/run; never raises.
    """
    catalog = a.catalog
    far_target, imm_target = _schedule_twin_targets(catalog, plan)

    if a.dry_run:
        print(f"[dispatch] activity schedule-twins (dry-run) — seed TWO SCHEDULED writer jobs "
              f"(UNPAUSED) on distinct pilot tables + run each once:")
        print(f"-- jobs.create name={SCHED_FAR_JOB_NAME!r} (INSERT on {far_target or '<n/a>'}), "
              f"schedule cron='<daily ~8h from now>' -> SAFE_NOW; then run_now once")
        print(f"-- jobs.create name={SCHED_IMMINENT_JOB_NAME!r} (INSERT on {imm_target or '<n/a>'}), "
              f"schedule cron={SCHED_IMMINENT_CRON!r} -> WARN_WINDOW; then run_now once")
        print(f"[activity] dry-run: would seed the far-out (SAFE_NOW) + imminent (WARN_WINDOW) "
              f"schedule twins on {far_target or '<n/a>'} / {imm_target or '<n/a>'} "
              f"(live writer_schedule_verdict from the cron)")
        return

    if far_target is None or imm_target is None:
        print(f"[activity] schedule-twins: pilot schema {catalog}.{PO_SCHEMA} has < 3 Delta tables; "
              f"skipping schedule-twin seed")
        return

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    try:
        nb_path = _upload_writer_notebook(w)
        print(f"  ok   uploaded writer notebook (twins) -> {nb_path}")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not upload writer notebook for twins ({type(e).__name__}: {e}); "
              f"skipping schedule-twin seed")
        return

    def _seed_one(name: str, target: str, cron: str, verdict: str) -> None:
        try:
            job_id = _create_or_get_writer_job(w, name, nb_path, target, cron=cron)
            print(f"  ok   ensured schedule twin {name!r} (job_id={job_id}, cron={cron!r} -> {verdict})")
            run_id = w.jobs.run_now(job_id=job_id).run_id
            t0 = time.time()
            while time.time() - t0 < 1800:
                run = w.jobs.get_run(run_id=run_id)
                life = run.state.life_cycle_state if run.state else None
                if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                            RunLifeCycleState.INTERNAL_ERROR):
                    result = run.state.result_state if run.state else None
                    print(f"  ok   {name!r} run finished: "
                          f"{result.value if result else 'UNKNOWN'} (WRITE attribution -> {target})")
                    return
                time.sleep(10)
            print(f"  note {name!r} run still going after 1800s; the profiler can pick up the "
                  f"WRITE-attributed edge later — continuing")
        except Exception as e:   # noqa: BLE001 — degrade honestly
            print(f"  WARN could not seed/run schedule twin {name!r} "
                  f"({type(e).__name__}: {e}); continuing")

    _seed_one(SCHED_FAR_JOB_NAME, far_target, re_.far_out_cron(datetime.now(timezone.utc)), "SAFE_NOW")
    _seed_one(SCHED_IMMINENT_JOB_NAME, imm_target, SCHED_IMMINENT_CRON, "WARN_WINDOW")
    print(f"[activity] seeded + ran the far-out (SAFE_NOW) + imminent (WARN_WINDOW) schedule twins "
          f"on {far_target} / {imm_target} (live schedule-aware convert-window coverage)")


def _seed_writer_wave(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (i): seed + run ONE imminent scheduled writer on the isolated e2e_real_writer table.

    The L4 F-WRITER-WAVE building block. Unlike the bronze schedule twins (which the L3 MCP oracle
    reads on the PILOT schema), this writer lives in the tiny ``e2e_real_writer`` schema, so the wave
    planner slices it into its own small ``normal`` wave. Every-5-min cron -> WARN_WINDOW; attributed
    as a ``SCHEDULED_JOB`` writer in ``writer_sources`` so the Convert-Gate pause banner (A1) names
    exactly this one job with an imminent next-fire. Created UNPAUSED, run once for lineage. Degrades
    honestly (WARN + continue) if the workspace disallows job create/run; never raises.
    """
    catalog = a.catalog
    target = _writer_wave_target(catalog, plan)

    if a.dry_run:
        print(f"[dispatch] activity writer-wave (dry-run) — seed ONE imminent SCHEDULED writer "
              f"(UNPAUSED) on the isolated schema {catalog}.{WRITER_WAVE_SCHEMA} + run once:")
        print(f"-- upload {_WRITER_NOTEBOOK_SRC} -> "
              f"/Workspace/Users/<current_user>/{_PATHACCESS_WS_DIR}/{_WRITER_WS_NAME}")
        print(f"-- jobs.create name={SCHED_WRITER_WAVE_JOB_NAME!r} (notebook INSERT on {target}), "
              f"schedule cron={SCHED_IMMINENT_CRON!r} pause_status=UNPAUSED -> WARN_WINDOW; then "
              f"run_now once")
        print(f"[activity] dry-run: would seed + run an imminent writer on "
              f"{target or '<no e2e_real_writer Delta table>'} (isolated SCHEDULED_JOB writer for "
              f"the L4 F-WRITER-WAVE pause banner)")
        return

    if target is None:
        print(f"[activity] writer-wave: no Delta table in {catalog}.{WRITER_WAVE_SCHEMA}; "
              f"skipping isolated writer-wave seed")
        return

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    try:
        nb_path = _upload_writer_notebook(w)
        print(f"  ok   uploaded writer notebook (writer-wave) -> {nb_path}")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not upload writer notebook for writer-wave ({type(e).__name__}: {e}); "
              f"skipping isolated writer-wave seed")
        return

    try:
        job_id = _create_or_get_writer_job(w, SCHED_WRITER_WAVE_JOB_NAME, nb_path, target,
                                           cron=SCHED_IMMINENT_CRON)
        print(f"  ok   ensured writer-wave job {SCHED_WRITER_WAVE_JOB_NAME!r} "
              f"(job_id={job_id}, cron={SCHED_IMMINENT_CRON!r} -> WARN_WINDOW)")
        run_id = w.jobs.run_now(job_id=job_id).run_id
        t0 = time.time()
        while time.time() - t0 < 1800:
            run = w.jobs.get_run(run_id=run_id)
            life = run.state.life_cycle_state if run.state else None
            if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                        RunLifeCycleState.INTERNAL_ERROR):
                result = run.state.result_state if run.state else None
                print(f"  ok   writer-wave job run finished: "
                      f"{result.value if result else 'UNKNOWN'} (WRITE attribution -> {target})")
                break
            time.sleep(10)
        else:
            print(f"  note writer-wave job run still going after 1800s; the profiler can pick up the "
                  f"WRITE-attributed edge later — continuing")
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN could not seed/run writer-wave job {SCHED_WRITER_WAVE_JOB_NAME!r} "
              f"({type(e).__name__}: {e}); continuing")

    print(f"[activity] seeded + ran the imminent writer-wave job on {target} "
          f"(isolated SCHEDULED_JOB writer for the L4 F-WRITER-WAVE pause banner)")


# Labeled gap note surfaced by the share sub-step. Databricks-to-Databricks recipients are not
# auto-updated when a table's managed location changes on conversion — the share must be
# re-created (re-shared) by hand afterwards. The product's ``RESHARE_D2D`` action surfaces this.
_RESHARE_GAP_NOTE = (
    "  GAP  Delta Sharing (D2D) recipients are NOT auto-updated after a shared table is "
    "converted — re-share is a MANUAL post-conversion step (the product's RESHARE_D2D action "
    "surfaces it)."
)


def _activity_share(a: argparse.Namespace, plan: list[dict]) -> None:
    """Sub-step (e): create a REAL Delta Share (D2D) over the first Delta table of SHARE_SCHEMA.

    Makes ``is_delta_shared=true`` genuinely true for a seeded table. Delta Sharing may not be
    enabled on the workspace — degrade honestly (print a WARN and skip the rest of this sub-step
    without raising; do NOT fake it). The other activity sub-steps have already run.
    """
    catalog = a.catalog
    fq = _share_target(catalog, plan)
    if fq is None:
        print(f"[activity] share: no Delta table found in {SHARE_SCHEMA}; skipping share sub-step")
        return

    if a.dry_run:
        print(f"[dispatch] activity share (dry-run) — real Delta Share (D2D) over {fq}")
        print(_create_share_sql() + ";")
        print(_share_add_table_sql(fq) + ";")
        print(_create_recipient_sql() + ";  -- best-effort (where the workspace permits)")
        print(_RESHARE_GAP_NOTE)
        print(f"[activity] dry-run: would create share {SHARE_NAME} over {fq} "
              f"+ best-effort recipient {SHARE_RECIPIENT}")
        return

    run = _make_runner(a.profile, a.warehouse_id)
    if not run(_create_share_sql(), f"share {SHARE_NAME}"):
        print(f"  WARN Delta Sharing likely not enabled on this workspace; skipping the rest of "
              f"the share sub-step (not faked). Other activity sub-steps already ran.")
        return

    if not run(_share_add_table_sql(fq), f"add {fq} to share {SHARE_NAME} (with history)"):
        fallback = f"ALTER SHARE {SHARE_NAME} ADD TABLE {fq}"
        if not run(fallback, f"add {fq} to share {SHARE_NAME} (no history fallback)"):
            print(f"  WARN could not add {fq} to share {SHARE_NAME}; continuing")

    if not run(_create_recipient_sql(), f"recipient {SHARE_RECIPIENT}"):
        print(f"  note recipient {SHARE_RECIPIENT} not created (best-effort); continuing")

    print(_RESHARE_GAP_NOTE)
    print(f"[activity] created share {SHARE_NAME} over {fq} (best-effort recipient {SHARE_RECIPIENT})")


def _activity_optout(a: argparse.Namespace) -> None:
    """Sub-step (g): seed ONE PARTITIONED Delta table in the pilot schema (auto-liquid opt-out).

    Partitioned tables are excluded from the default CLUSTER BY AUTO set — 08c surfaces them as a
    manual (OPEN) ENABLE_AUTO_LIQUID action rather than auto-clustering them. Seeding it here (in
    ``activity``, before ``profile``) lets the profiler capture ``partition_columns`` so the table
    is held back. Idempotent (``CREATE OR REPLACE``); dropped by the ``e2e_real_*`` CASCADE
    teardown — no new teardown code. Best-effort: a failure WARNs (the held-back assertion then
    degrades to WARN).
    """
    catalog = a.catalog
    fq = f"{catalog}.{E2E_PILOT_SCHEMA}.{AL_OPTOUT_TABLE}"
    loc = f"{a.external_base}/{E2E_PILOT_SCHEMA}/{AL_OPTOUT_TABLE}"
    optout_sql = (
        f"CREATE OR REPLACE TABLE {fq} USING DELTA PARTITIONED BY (region) "
        f"LOCATION '{loc}' AS "
        f"SELECT col AS id, CAST(reg AS STRING) AS region "
        f"FROM (VALUES (1,'us'),(2,'eu'),(3,'us'),(4,'eu')) AS t(col, reg)"
    )
    if a.dry_run:
        print(f"[dispatch] activity opt-out (dry-run) — seed ONE PARTITIONED Delta table in the "
              f"pilot schema {catalog}.{E2E_PILOT_SCHEMA} (auto-liquid opt-out):")
        print(f"-- CREATE OR REPLACE TABLE {fq} "
              f"USING DELTA PARTITIONED BY (region) LOCATION "
              f"'{loc}' AS SELECT … "
              f"(auto-liquid opt-out: partitioned -> held back from default CLUSTER BY AUTO)")
        print(f"[activity] dry-run: would seed partitioned opt-out table {fq} "
              f"(held back from default auto-liquid)")
        for label, sql in _clustering_layout_sql(catalog, a.external_base):
            print(f"-- {label}: {sql[:110]} …")
        return
    run = _make_runner(a.profile, a.warehouse_id)
    if not run(optout_sql, f"auto-liquid opt-out (partitioned) {fq}"):
        print(f"  WARN could not seed partitioned opt-out table {fq}; the held-back assertion "
              f"will degrade to WARN — continuing")
    print(f"[activity] seeded partitioned opt-out table {fq} "
          f"(partitioned -> held back from default CLUSTER BY AUTO)")
    _activity_clustering_layouts(a, run)


def _clustering_layout_sql(catalog: str, external_base: str) -> list[tuple[str, str]]:
    """``(label, sql)`` for the ZORDERed and manual-key pilot tables, in order (pure)."""
    zfq = f"{catalog}.{E2E_PILOT_SCHEMA}.{AL_ZORDER_TABLE}"
    mfq = f"{catalog}.{E2E_PILOT_SCHEMA}.{AL_MANUAL_KEYS_TABLE}"
    zloc = f"{external_base}/{E2E_PILOT_SCHEMA}/{AL_ZORDER_TABLE}"
    mloc = f"{external_base}/{E2E_PILOT_SCHEMA}/{AL_MANUAL_KEYS_TABLE}"
    return [
        (f"ZORDERed table {zfq}",
         f"CREATE OR REPLACE TABLE {zfq} USING DELTA LOCATION '{zloc}' AS "
         f"SELECT CAST(id AS INT) AS id, date_add('2026-01-01', CAST(id % 200 AS INT)) AS event_date, "
         f"concat('c', CAST(id % 97 AS STRING)) AS customer_id, CAST(id AS DOUBLE) * 1.5 AS amount "
         f"FROM range(0, 2000) AS t(id)"),
        (f"ZORDER {zfq}", f"OPTIMIZE {zfq} ZORDER BY ({', '.join(AL_ZORDER_COLUMNS)})"),
        (f"manual-key table {mfq}",
         f"CREATE OR REPLACE TABLE {mfq} USING DELTA CLUSTER BY ({', '.join(AL_MANUAL_KEYS)}) "
         f"LOCATION '{mloc}' AS "
         f"SELECT CAST(id AS INT) AS id, concat('r', CAST(id % 7 AS STRING)) AS region, "
         f"timestamp_millis(1760000000000 + id) AS ts FROM range(0, 1000) AS t(id)"),
    ]


def _activity_clustering_layouts(a: argparse.Namespace, run) -> None:
    """Seed the ZORDERed and manual-key pilot tables (see ``AL_ZORDER_TABLE``). Best-effort: a
    failure WARNs and the matching --e2e layout check degrades to WARN."""
    for label, sql in _clustering_layout_sql(a.catalog, a.external_base):
        if not run(sql, label):
            print(f"  WARN could not seed {label}; its clustering check will degrade to WARN — continuing")
    print(f"[activity] seeded {AL_ZORDER_TABLE} (ZORDER BY {', '.join(AL_ZORDER_COLUMNS)}) and "
          f"{AL_MANUAL_KEYS_TABLE} (CLUSTER BY {', '.join(AL_MANUAL_KEYS)}) in {E2E_PILOT_SCHEMA}")


def _activity_owner_variants(a: argparse.Namespace) -> None:
    """Sub-step (h): seed the foreign-owned and group-owned EXTERNAL tables in the pilot schema.

    The convert-permission check must see tables the convert identity does not own. The owner is
    set once here, at seed time; the migration tooling itself never changes an owner. Owners come
    only from ``--foreign-owner`` / ``--owner-group`` (never invented); an unset flag seeds nothing.
    The seeding identity must be able to transfer ownership (metastore admin, or catalog / schema
    owner). Best-effort: a failure WARNs and the profile check degrades to WARN. Dropped by the
    ``e2e_real_*`` CASCADE teardown.
    """
    variants = re_.owner_variants(getattr(a, "foreign_owner", None), getattr(a, "owner_group", None))
    if not variants:
        print("[activity] owner variants: --foreign-owner / --owner-group unset — not seeded")
        return
    run = None if a.dry_run else _make_runner(a.profile, a.warehouse_id)
    for table, owner in variants:
        fq = f"{a.catalog}.{E2E_PILOT_SCHEMA}.{table}"
        loc = f"{a.external_base}/{E2E_PILOT_SCHEMA}/{table}"
        create_sql = (f"CREATE OR REPLACE TABLE {fq} USING DELTA LOCATION '{loc}' AS "
                      f"SELECT col AS id FROM (VALUES (1),(2),(3)) AS t(col)")
        owner_sql = f"ALTER TABLE {fq} OWNER TO `{owner}`"
        if a.dry_run:
            print(f"-- {create_sql};\n-- {owner_sql};")
            print(f"[activity] dry-run: would seed {fq} owned by {owner}")
            continue
        if run(create_sql, f"owner variant {fq}") and run(owner_sql, f"owner of {fq} -> {owner}"):
            print(f"[activity] seeded {fq} owned by {owner}")
        else:
            print(f"  WARN could not seed {fq} owned by {owner}; the owner-variant check will WARN — continuing")


def _activity(a: argparse.Namespace, plan: list[dict]) -> None:
    # Ordered sub-steps: path-based access (unchanged), then real streaming, then real DLT,
    # then the real compaction papercut (OPTIMIZE + Predictive Optimization), then the real
    # Delta Share (D2D) papercut, then the partitioned auto-liquid opt-out table, then the real
    # scheduled batch writer job (INSERT), then the far-out + imminent schedule twins, then the
    # isolated writer-wave imminent writer (L4 F-WRITER-WAVE).
    _activity_pathaccess(a)
    _activity_streaming(a)
    _activity_dlt(a)
    _activity_compaction(a, plan)
    _seed_maintenance_jobs(a, plan)
    _seed_writer_job(a, plan)
    _seed_schedule_twins(a, plan)
    _seed_writer_wave(a, plan)
    _activity_share(a, plan)
    _activity_optout(a)
    _activity_owner_variants(a)


# --------------------------------------------------------------------------- #
# Stage: lineage_wait — poll system.access.table_lineage until the estate's edges land
# --------------------------------------------------------------------------- #

def _lineage_wait(a: argparse.Namespace) -> None:
    """Poll for the three representative lineage edges (pipeline / job / path) or FAIL LOUD.

    The estate's real activity (DLT read, streaming-job write, by-path read) only appears in
    ``system.access.table_lineage`` after propagation latency (can lag hours). This stage blocks
    until every edge has count>0 so a downstream ``profile`` corroborates on lineage that has
    actually landed. On timeout it raises ``SystemExit`` listing the still-missing edges — never
    a silent pass. ``resolve_stages`` DROPS this stage under ``--no-lineage``, so if it runs at
    all, lineage is REQUIRED. Mirrors ``poll_lineage_ready`` in ``e2e_dlt_pipeline.py``.
    """
    catalog = a.catalog
    edges = _lineage_edges(catalog)
    names = ", ".join(name for name, _ in edges)

    if a.dry_run:
        print(f"[dispatch] lineage_wait (dry-run) — poll {len(edges)} lineage edges over "
              f"system.access.table_lineage")
        for name, count_sql in edges:
            print(f"-- edge {name}:")
            print(count_sql + ";")
        print(f"[lineage_wait] dry-run: would poll {_LINEAGE_TIMEOUT_S}s "
              f"(every {_LINEAGE_POLL_S}s) for edges: {names}")
        return

    _scalar = _make_scalar(a.profile, a.warehouse_id)
    t0 = time.time()
    while True:
        counts: dict[str, int] = {}
        for name, count_sql in edges:
            try:
                counts[name] = int(_scalar(count_sql) or 0)
            except Exception as e:   # noqa: BLE001 — a flaky per-edge query never crashes the poll
                counts[name] = 0
                print(f"  lineage query for edge {name!r} failed (will retry): "
                      f"{type(e).__name__}: {e}")
        elapsed = int(time.time() - t0)
        print(f"  [{elapsed:4d}s] " + " ".join(f"{name}={counts[name]}" for name, _ in edges))
        missing = [name for name, _ in edges if counts[name] <= 0]
        if not missing:
            print(f"[lineage_wait] all edges present after {elapsed}s: {names}")
            return
        if elapsed >= _LINEAGE_TIMEOUT_S:
            print(f"[lineage_wait] TIMEOUT after {elapsed}s — edges still missing: {missing}")
            raise SystemExit(
                f"lineage_wait: edges not present within {_LINEAGE_TIMEOUT_S}s: {missing}")
        time.sleep(_LINEAGE_POLL_S)


# --------------------------------------------------------------------------- #
# Stage: profile — scoped profiler run + detection corroboration (Task 11)
# --------------------------------------------------------------------------- #
# Corroboration COUNT(*) / value builders (pure) shared by dry-run printing + live evaluation.
# Each asserts a papercut that is ACTUALLY seeded, mapped to its REAL location. Papercuts that
# cannot be seeded for real (compat) or live on placeholder plan schemas are LABELED SYNTHETIC
# GAPS below (printed, never asserted) — controller reconciliation, not a blind assert-everything.

def _profiler_run_params(catalog: str) -> dict:
    """Job parameters that scope the deployed profiler to the estate (globs; force a full rescan).

    ``schema_filter='e2e_*'`` covers the e2e_real_* estate PLUS e2e_stream + e2e_dlt in one run.
    """
    return {"catalog_filter": catalog, "schema_filter": "e2e_*", "force_rescan": "true"}


def _pathaccess_action_count_sql(catalog: str) -> str:
    """CORE (a): path-access papercut surfaced as a FIX_PATH_ACCESS action on the pathaccess schema."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='FIX_PATH_ACCESS' AND schema='{re_.PATH_ACCESS_SCHEMA}'"
    )


def _streaming_type_count_sql(catalog: str, full_name: str, stream_type: str) -> str:
    """CORE (b/c): a streaming_plan row of ``stream_type`` for ``full_name`` (the REAL writer/reader)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.streaming_plan "
        f"WHERE full_name='{full_name}' AND type='{stream_type}'"
    )


def _compaction_source_count_sql(catalog: str, schema: str) -> str:
    """CORE (d): the OPTIMIZE / Predictive Optimization actor surfaced in compaction_sources."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.compaction_sources "
        f"WHERE schema='{schema}'"
    )


def _reshare_action_count_sql(catalog: str, schema: str) -> str:
    """CONDITIONAL (e): the D2D share papercut surfaced as a RESHARE_D2D action (Delta Sharing may be off)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='RESHARE_D2D' AND schema='{schema}'"
    )


def _manifest_size_sql(catalog: str, full_name: str) -> str:
    """Manifest's captured on-disk bytes for one table (profiler DESCRIBE DETAIL sizeInBytes)."""
    return (
        f"SELECT table_size_bytes FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE full_name='{full_name}'"
    )


def _manifest_size_band_sql(catalog: str, full_name: str) -> str:
    """Non-Delta corroboration: the size classification (expected ``unknown`` when no Delta probe)."""
    return (
        f"SELECT size_band FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE full_name='{full_name}'"
    )


def _size_sample_targets(catalog: str, plan: list[dict]) -> list[tuple[str, str]]:
    """Deterministic (tier, fq) samples: the FIRST Delta table (list order) per XS/S/M/L/XL tier.

    Pure — a tier with no Delta table in ``plan`` is skipped gracefully. Fully-qualified with
    ``catalog`` so the size-capture check compares manifest bytes to a live DESCRIBE DETAIL.
    """
    out: list[tuple[str, str]] = []
    for tier in _PROFILE_SIZE_TIERS:
        first = next(
            (t for t in plan if t.get("is_delta", True) and t["size_tier"] == tier),
            None,
        )
        if first is not None:
            out.append((tier, f"{catalog}.{first['full_name_suffix']}"))
    return out


def _nondelta_sample_target(catalog: str, plan: list[dict]) -> str | None:
    """Deterministic fq of the FIRST non-Delta table of NONDELTA_SCHEMA (or None if the plan has none)."""
    first = next(
        (t for t in plan
         if not t.get("is_delta", True) and t["catalog_relative_schema"] == NONDELTA_SCHEMA),
        None,
    )
    return f"{catalog}.{first['full_name_suffix']}" if first is not None else None


# LABELED SYNTHETIC GAPS — printed every run, never asserted (controller reconciliation).
_PROFILE_SYNTHETIC_GAPS = (
    "  GAP  ENABLE_COMPAT on e2e_real_compat stays SYNTHETIC — a non-Databricks external writer "
    "cannot be seeded for real, so this papercut is NOT asserted.",
    "  GAP  e2e_real_stream / e2e_real_dlt plan tables are plain placeholder tables — the REAL "
    "streaming / DLT papercuts live in e2e_stream / e2e_dlt (asserted above), not on these placeholders.",
)


# --------------------------------------------------------------------------- #
# Synthetic-TB size patch (Task 12) — pure builders shared by dry-run + live.
# --------------------------------------------------------------------------- #
# The estate can't afford real multi-TB tables, so a few gold tables are created TINY
# and REAL (so the in-stage profiler discovers them and their full_name resolves), then
# their manifest size is PATCHED into the true TB bands, LABELED SYNTHETIC, so band /
# copy / cluster / final_large bucketing is visible live.

def _syn_size_fixture_create_sql(catalog: str, external_base: str, row: dict) -> str:
    """CTAS one TINY REAL Delta fixture (XS-tier) at its external path (real bytes patched later)."""
    fq = f"{catalog}.{row['full_name_suffix']}"
    loc = f"{external_base.rstrip('/')}/{row['catalog_relative_schema']}/{row['table_name']}"
    rows, pad = re_.rows_pad_for(re_.SIZE_TIERS[0]["target_bytes"])   # XS ≈ 10KB nominal
    return f"CREATE OR REPLACE TABLE {fq}\nUSING DELTA LOCATION '{loc}' AS\n" + _select_sql(rows, pad)


def _syn_size_patch_sql(catalog: str, full_name: str, target_bytes: int, band: str) -> str:
    """UPDATE-only MERGE stamping SYNTHETIC bytes + Python-derived band on one manifest row.

    Patching ``size_band`` too is REQUIRED so the later wave planner (which copies size_band
    from the manifest) reflects the synthetic band. ``band`` is computed in Python via
    ``elig_.size_band`` and embedded here as a literal.
    """
    return (
        f"MERGE INTO {catalog}.{CONTROL_SCHEMA}.table_manifest t\n"
        f"USING (SELECT '{full_name}' AS full_name) s\n"
        f"ON t.full_name = s.full_name\n"
        f"WHEN MATCHED THEN UPDATE SET t.table_size_bytes = {target_bytes}, "
        f"t.size_source = 'SYNTHETIC', t.size_band = '{band}'"
    )


def _syn_size_patches(catalog: str) -> list[tuple[str, int, str]]:
    """(full_name, target_bytes, band) per synthetic-size row; band derived in PURE Python."""
    return [
        (f"{catalog}.{r['full_name_suffix']}", r["target_bytes"], elig_.size_band(r["target_bytes"]))
        for r in re_.synthetic_size_rows()
    ]


def _syn_size_report(syn_patches: list[tuple[str, int, str]]) -> None:
    """Pure band/cluster derivation summary (no live read).

    Documents the band/cluster derivation from each patched value: asserts each byte value
    lands in a non-``unknown`` band and that ``recommended_cores`` is positive and
    non-decreasing across ~1TB -> ~10TB -> >10TB. Prints one PASS line per patched table.
    """
    prev = 0
    for full_name, target_bytes, band in syn_patches:
        cores = elig_.recommended_cores(target_bytes)
        assert band != "unknown"              # pure invariant: TB bytes always band
        assert cores > 0 and cores >= prev    # positive + non-decreasing across the three bands
        prev = cores
        print(f"  PASS synthetic-size {full_name}  (size_band={band}, "
              f"recommended_cores={cores}, bytes={target_bytes})")


def _profile(a: argparse.Namespace, plan: list[dict]) -> None:
    """Run the scoped profiler, then corroborate the seeded papercuts + captured sizes.

    Default mode collects CORE (a-d) + non-Delta failures and FAILS LOUD (``SystemExit``) at the
    end listing them. ``--no-lineage`` downgrades those to WARN (printed, never raised). The share
    check is CONDITIONAL (WARN only — Delta Sharing may be disabled) and size-sample tolerance is
    PASS/WARN (sizes are approximate). Synthetic gaps are printed but never asserted.
    """
    catalog = a.catalog
    params = _profiler_run_params(catalog)
    stream_fq = f"{catalog}.{STREAM_SCHEMA}.{STREAM_SEED_TABLE}"
    dlt_fq = f"{catalog}.{DLT_SCHEMA}.{DLT_SRC_BRONZE}"

    core_checks = [
        ("path-access FIX_PATH_ACCESS on " + re_.PATH_ACCESS_SCHEMA,
         _pathaccess_action_count_sql(catalog)),
        ("streaming STRUCTURED_STREAMING on " + stream_fq,
         _streaming_type_count_sql(catalog, stream_fq, "STRUCTURED_STREAMING")),
        ("dlt DLT on " + dlt_fq,
         _streaming_type_count_sql(catalog, dlt_fq, "DLT")),
        ("compaction source on " + PO_SCHEMA,
         _compaction_source_count_sql(catalog, PO_SCHEMA)),
    ]
    share_sql = _reshare_action_count_sql(catalog, SHARE_SCHEMA)
    nd_fq = _nondelta_sample_target(catalog, plan)
    size_samples = _size_sample_targets(catalog, plan)
    syn_rows = re_.synthetic_size_rows()
    syn_schema = syn_rows[0]["catalog_relative_schema"]
    syn_patches = _syn_size_patches(catalog)

    if a.dry_run:
        print(f"[dispatch] profile (dry-run) — REUSE the deployed job whose name ends "
              f"{PROFILER_SUFFIX!r}")
        print("-- BEFORE profiling: create the synthetic-TB size fixtures TINY + REAL so the "
              "in-stage profiler discovers them and their full_name resolves for the patch:")
        print(f"-- {_create_schema_sql(catalog, syn_schema)};")
        for r in syn_rows:
            print(_syn_size_fixture_create_sql(catalog, a.external_base, r) + ";")
            print()
        print(f"-- would resolve that job by name suffix and run_now it scoped to the estate:")
        print(f"-- run_now(job_parameters={params})")
        print("-- CORE corroboration (HARD in default mode; WARN under --no-lineage):")
        for label, count_sql in core_checks:
            print(f"--   {label}:")
            print(count_sql + ";")
        print("-- CONDITIONAL corroboration (WARN only — Delta Sharing may be disabled):")
        print(f"--   share RESHARE_D2D on {SHARE_SCHEMA}:")
        print(share_sql + ";")
        print("-- non-Delta corroboration (HARD in default mode; WARN under --no-lineage):")
        if nd_fq:
            nd_cores = elig_.recommended_cores(None)
            print(f"--   {nd_fq} expects size_band='unknown' "
                  f"(recommended_cores={nd_cores} conservative, derived in Python):")
            print(_manifest_size_band_sql(catalog, nd_fq) + ";")
        else:
            print(f"--   (plan has no non-Delta table in {NONDELTA_SCHEMA}; sample skipped)")
        print("-- size-capture samples (PASS/WARN — manifest bytes vs live DESCRIBE DETAIL, [0.5x,2x]):")
        for tier, fq in size_samples:
            print(f"--   {tier} {fq}:")
            print(_manifest_size_sql(catalog, fq) + ";")
            print(f"-- DESCRIBE DETAIL {fq};  (read the sizeInBytes column)")
        for note in _PROFILE_SYNTHETIC_GAPS:
            print(note)
        print("-- AFTER profiler+corroboration: PATCH manifest size into the true TB bands "
              "(UPDATE-only MERGE on full_name; band computed in Python + embedded as a literal). "
              "A later manual re-profile recomputes the real (tiny) size, but the SYNTHETIC label "
              "persists via _DEFERRED_DEFAULTS preserve-on-update — fine for this seed-and-leave demo:")
        for full_name, target_bytes, band in syn_patches:
            print(_syn_size_patch_sql(catalog, full_name, target_bytes, band) + ";")
        print("-- pure band-derivation summary (no live read):")
        _syn_size_report(syn_patches)
        print(f"[profile] dry-run: would run the scoped profiler + {len(core_checks)} CORE + share "
              f"+ {len(size_samples)} size-sample + non-Delta corroboration checks "
              f"+ {len(syn_patches)} synthetic-TB size patches")
        return

    # BEFORE profiling: create the synthetic-TB size fixtures TINY + REAL so the in-stage
    # profiler discovers them and their full_name resolves for the patch below.
    run = _make_runner(a.profile, a.warehouse_id)
    run(_create_schema_sql(catalog, syn_schema), f"schema {syn_schema}")
    for r in syn_rows:
        run(_syn_size_fixture_create_sql(catalog, a.external_base, r),
            f"synthetic-size fixture {r['full_name_suffix']}")

    print(f"[profile] running deployed profiler ({PROFILER_SUFFIX!r}) scoped to {params}")
    state = _run_deployed_job(a, PROFILER_SUFFIX, params=params)
    if state != "SUCCESS":
        raise SystemExit(f"profiler run did not succeed (result_state={state})")
    print(f"[profile] profiler run SUCCESS — corroborating against control tables")

    _scalar = _make_scalar(a.profile, a.warehouse_id)
    _dd_size = _make_describe_detail_size(a.profile, a.warehouse_id)
    hard_failures: list[str] = []

    def _eval_hard(label: str, count_sql: str) -> None:
        n = int(_scalar(count_sql) or 0)
        if n > 0:
            print(f"  PASS {label}  ({n})")
        elif a.no_lineage:
            print(f"  WARN {label}  (0) — downgraded under --no-lineage")
        else:
            print(f"  FAIL {label}  (0)")
            hard_failures.append(label)

    for label, count_sql in core_checks:
        _eval_hard(label, count_sql)

    # Non-Delta: HARD (default) / WARN (--no-lineage) that the sample's size_band is 'unknown'.
    # The conservative recommended-cores implication is derived in PURE PYTHON (never a SQL read):
    # ``table_manifest`` has no ``recommended_cores`` column at profile time (it is written later
    # by job_02_wave_planner), so the only live read here is ``size_band``.
    if nd_fq:
        band = _scalar(_manifest_size_band_sql(catalog, nd_fq))
        cores = elig_.recommended_cores(None)   # pure: unknown/None size -> conservative 256
        nd_label = f"non-Delta {nd_fq} size_band='unknown' (recommended_cores={cores} conservative)"
        nd_ok = str(band) == "unknown"
        detail = f"size_band={band}, recommended_cores={cores}"
        assert cores == 256  # pure Python invariant (no network); guards the conservative band
        if nd_ok:
            print(f"  PASS {nd_label}  ({detail})")
        elif a.no_lineage:
            print(f"  WARN {nd_label}  ({detail}) — downgraded under --no-lineage")
        else:
            print(f"  FAIL {nd_label}  ({detail})")
            hard_failures.append(nd_label)
    else:
        print(f"  WARN non-Delta sample: plan has no table in {NONDELTA_SCHEMA}; skipped")

    # CONDITIONAL: share (WARN only — never a hard failure).
    n_share = int(_scalar(share_sql) or 0)
    share_label = f"share RESHARE_D2D on {SHARE_SCHEMA}"
    if n_share > 0:
        print(f"  PASS {share_label}  ({n_share})")
    else:
        print(f"  WARN {share_label}  (0) — Delta Sharing may be disabled; not a hard failure")

    # CONDITIONAL: owner variants (WARN only — the verdict depends on the convert identity's grants).
    variants = re_.owner_variants(getattr(a, "foreign_owner", None), getattr(a, "owner_group", None))
    if variants:
        _query = _make_query(a.profile, a.warehouse_id)
        for table, owner in variants:
            fq = f"{catalog}.{E2E_PILOT_SCHEMA}.{table}"
            try:
                rows = _query(_owner_variant_verdict_sql(catalog, fq))
            except Exception as e:  # noqa: BLE001 — a failed read never hard-fails the stage
                print(f"  WARN owner variant {fq}: verdict read failed ({type(e).__name__}: {e})")
                continue
            verdict = dict(zip(("status", "basis", "table_owner"), rows[0])) if rows else None
            tag, msg = re_.owner_variant_outcome(table=table, owner=owner, verdict=verdict)
            print(f"  {tag} owner variant {msg}")

    # Size-capture samples: PASS/WARN only (sizes are approximate; loose [0.5x, 2x] tolerance).
    for tier, fq in size_samples:
        try:
            m_bytes = int(_scalar(_manifest_size_sql(catalog, fq)) or 0)
            d_bytes = int(_dd_size(fq) or 0)
        except Exception as e:  # noqa: BLE001 — a flaky size read never hard-fails the stage
            print(f"  WARN size[{tier}] {fq}: size read failed ({type(e).__name__}: {e})")
            continue
        both_pos = m_bytes > 0 and d_bytes > 0
        within = both_pos and (0.5 * d_bytes) <= m_bytes <= (2.0 * d_bytes)
        detail = f"manifest={m_bytes}, describe_detail={d_bytes}"
        if within:
            print(f"  PASS size[{tier}] {fq}  ({detail})")
        else:
            print(f"  WARN size[{tier}] {fq}  ({detail}) — outside [0.5x,2x] or non-positive")

    for note in _PROFILE_SYNTHETIC_GAPS:
        print(note)

    # Task 12: PATCH the synthetic-size fixtures' manifest size into the true TB bands, LABELED
    # SYNTHETIC (band computed in Python + embedded). Patching size_band too is REQUIRED so the
    # later wave planner (which copies size_band from the manifest) reflects the synthetic band.
    # A later manual re-profile recomputes the real (tiny) size, but the SYNTHETIC label persists
    # via the manifest's _DEFERRED_DEFAULTS preserve-on-update — acceptable for the seed-and-leave
    # demo (populate profiles once, then patch).
    print(f"[profile] patching {len(syn_patches)} synthetic-TB manifest sizes (LABELED SYNTHETIC)")
    for full_name, target_bytes, band in syn_patches:
        run(_syn_size_patch_sql(catalog, full_name, target_bytes, band),
            f"synthetic-size patch {full_name} -> {band}")
    _syn_size_report(syn_patches)

    if hard_failures and not a.no_lineage:
        raise SystemExit(
            f"profile: {len(hard_failures)} corroboration check(s) FAILED: {hard_failures}")
    mode = "WARN-only (--no-lineage)" if a.no_lineage else "HARD"
    print(f"[profile] corroboration complete ({mode} mode) — "
          f"{len(hard_failures)} hard failure(s)")


# --------------------------------------------------------------------------- #
# Stage: scan + plan (Task 13) — deployed source-scan + wave planner runs.
# --------------------------------------------------------------------------- #
# Both stages REUSE already-deployed bundle jobs (resolved by name suffix, never recreated)
# and corroborate against the control tables. ``scan`` runs job_01 in broad mode scoped to the
# estate's uploaded path-access notebook dir to fill path_access_locations for real; ``plan`` runs
# job_02 with a REDUCED byte cap so the real
# size spread yields multiple waves + synthetic-TB singletons. Pure SQL builders below are
# shared by dry-run printing and live evaluation.

SOURCE_SCAN_SUFFIX = "managed-table-migration :: 01 source scan (optional)"
WAVE_PLANNER_SUFFIX = "managed-table-migration :: 02 wave planner"


def _scan_run_params(user: str, directory: str) -> dict:
    """Job parameters that make job_01 scan in BROAD mode scoped to the uploaded notebook dir.

    ``scan_source='true'`` is REQUIRED (the default is off). ``scan_mode='broad'`` walks
    ``workspace_paths`` (here the single ``/Users/<user>/<directory>`` root where the estate's
    path-access notebook was uploaded) up to ``max_notebooks`` and scans notebook SOURCE for path
    literals. That is how THIS seeded estate's path-based access is discoverable: its edge comes
    from warehouse by-path reads + a best-effort one-off notebook run (NOT a registered JOB in
    ``path_access_entities``), so ``targeted_jobs`` finds nothing. manifest_catalog/schema are
    deploy-fixed on the job. Pure (``user`` + ``directory`` args) so it is unit-testable.
    """
    return {
        "scan_source": "true",
        "scan_mode": "broad",
        "workspace_paths": f"/Users/{user}/{directory}",
        "max_notebooks": "50",
    }


def _pathaccess_scan_rows_sql(catalog: str) -> str:
    """WARN-only corroboration: path_access_locations rows for the path-access schema.

    ``path_access_locations`` has no ``schema`` column (see the control-tables contract), so
    the estate slice is matched on ``full_name LIKE '%.e2e_real_pathaccess.%'``.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.path_access_locations "
        f"WHERE full_name LIKE '%.{re_.PATH_ACCESS_SCHEMA}.%'"
    )


def _wave_max_tb_param() -> str:
    """The REDUCED wave byte cap expressed in TB (planner parses it as FLOAT).

    ``real_chunking_wave_max_bytes()`` (1 GB) expressed in TB ≈ ``"0.0009765625"`` — small
    enough that the estate's real size spread yields multiple waves and the synthetic-TB gold
    tables each form a flagged oversized singleton wave.
    """
    return str(re_.real_chunking_wave_max_bytes() / re_.TB)


def _plan_run_params() -> dict:
    """Job parameters for job_02: only the REDUCED byte cap (other params keep deploy defaults)."""
    return {"wave_max_tb": _wave_max_tb_param()}


def _synthetic_tb_full_names(catalog: str) -> list[str]:
    """Fully-qualified full_names of the synthetic-TB gold rows (from ``re_.synthetic_size_rows``)."""
    return [f"{catalog}.{r['full_name_suffix']}" for r in re_.synthetic_size_rows()]


def _wave_distinct_count_sql(catalog: str) -> str:
    """Assertion (a): number of distinct waves the planner produced (expect > 1)."""
    return f"SELECT COUNT(DISTINCT wave_id) FROM {catalog}.{CONTROL_SCHEMA}.wave_plan"


def _wave_exceeds_cap_syn_sql(catalog: str) -> str:
    """Assertion (b): synthetic-TB rows flagged ``exceeds_wave_byte_cap`` (expect > 0)."""
    in_list = ", ".join(f"'{n}'" for n in _synthetic_tb_full_names(catalog))
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.wave_plan "
        f"WHERE full_name IN ({in_list}) AND exceeds_wave_byte_cap = true"
    )


def _wave_streaming_count_sql(catalog: str) -> str:
    """Assertion (c): rows bucketed ``streaming`` (streaming is always isolated; expect > 0)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.wave_plan "
        f"WHERE bucket='streaming'"
    )


def _scan(a: argparse.Namespace) -> None:
    """Run the deployed source-scan job in BROAD mode scoped to the estate's uploaded notebook dir
    → real path_access_locations, then WARN-check rows.

    The job run itself MUST succeed (fail loud otherwise). This seeded estate's path edge comes from
    warehouse by-path reads + a best-effort one-off notebook run (NOT a registered JOB), so a
    ``targeted_jobs`` scan finds nothing; a ``broad`` scan scoped to the uploaded notebook directory
    (``/Users/<current_user>/{_PATHACCESS_WS_DIR}``) is what populates path_access_locations.
    Source-scan attribution can still lag, so an empty result stays a WARN (never a hard failure).
    Prints a PASS/WARN line for the estate's path-access rows.
    """
    catalog = a.catalog
    rows_sql = _pathaccess_scan_rows_sql(catalog)

    if a.dry_run:
        placeholder = f"/Users/<current_user>/{_PATHACCESS_WS_DIR}"
        params = _scan_run_params("<current_user>", _PATHACCESS_WS_DIR)
        print(f"[dispatch] scan (dry-run) — REUSE the deployed job whose name ends "
              f"{SOURCE_SCAN_SUFFIX!r}")
        print("-- would resolve that job by name suffix and run_now it in BROAD mode scoped to "
              "the estate's uploaded notebook dir:")
        print(f"-- run_now(job_parameters={params})")
        print(f"--   (scan_source=true actually scans; scan_mode=broad walks workspace_paths="
              f"{placeholder!r} = the uploaded path-access notebook dir; targeted_jobs finds "
              "nothing for this estate)")
        print("-- WARN-only corroboration (source-scan attribution can lag; empty stays a WARN):")
        print(f"--   path_access_locations rows for {re_.PATH_ACCESS_SCHEMA}:")
        print(rows_sql + ";")
        print(f"[scan] dry-run: would run the deployed source scan (BROAD mode, scoped to "
              f"{placeholder}) + WARN-check path_access_locations rows for {re_.PATH_ACCESS_SCHEMA}")
        return

    # LIVE path only: lazily import the SDK and resolve the current user so the broad-mode scope
    # matches where ``_upload_and_submit_notebook`` uploaded the estate's path-access notebook.
    from databricks.sdk import WorkspaceClient
    user = WorkspaceClient(profile=a.profile).current_user.me().user_name
    params = _scan_run_params(user, _PATHACCESS_WS_DIR)
    workspace_paths = params["workspace_paths"]

    print(f"[scan] running deployed source scan ({SOURCE_SCAN_SUFFIX!r}) in BROAD mode scoped to "
          f"{workspace_paths}")
    state = _run_deployed_job(a, SOURCE_SCAN_SUFFIX, params=params)
    if state != "SUCCESS":
        raise SystemExit(f"source scan run did not succeed (result_state={state})")
    print(f"[scan] source scan run SUCCESS — checking path_access_locations")

    _scalar = _make_scalar(a.profile, a.warehouse_id)
    n = int(_scalar(rows_sql) or 0)
    label = f"path-access scan rows for {re_.PATH_ACCESS_SCHEMA}"
    if n > 0:
        print(f"  PASS {label}  ({n})")
    else:
        print(f"  WARN {label}  (0) — broad scan found nothing yet; source-scan attribution "
              f"can lag. Not a hard failure.")
    print(f"[scan] complete")


def _plan(a: argparse.Namespace) -> None:
    """Run the deployed wave planner with a REDUCED byte cap → multi-wave + synthetic-TB singletons.

    The job run itself MUST succeed (fail loud otherwise). Then assert three wave-plan shape
    invariants — HARD (collect + ``SystemExit``) in default mode, WARN-only (never raise) under
    ``--no-lineage``:
      (a) multi-wave: COUNT(DISTINCT wave_id) > 1;
      (b) synthetic-TB rows exceed the cap: exceeds_wave_byte_cap=true count > 0;
      (c) streaming isolated: bucket='streaming' count > 0.
    """
    catalog = a.catalog
    params = _plan_run_params()
    multi_sql = _wave_distinct_count_sql(catalog)
    exceeds_sql = _wave_exceeds_cap_syn_sql(catalog)
    streaming_sql = _wave_streaming_count_sql(catalog)
    stream_fq = f"{catalog}.{STREAM_SCHEMA}.{STREAM_SEED_TABLE}"

    if a.dry_run:
        print(f"[dispatch] plan (dry-run) — REUSE the deployed job whose name ends "
              f"{WAVE_PLANNER_SUFFIX!r}")
        print("-- would resolve that job by name suffix and run_now it with the REDUCED byte cap:")
        print(f"-- run_now(job_parameters={params})")
        print(f"--   (wave_max_tb={params['wave_max_tb']} TB = {re_.real_chunking_wave_max_bytes()} "
              f"bytes = real_chunking_wave_max_bytes(); other planner params left at deploy defaults)")
        print("-- assertions (HARD in default mode; WARN under --no-lineage):")
        print("--   (a) multi-wave: COUNT(DISTINCT wave_id) > 1:")
        print(multi_sql + ";")
        print("--   (b) synthetic-TB rows exceed cap (>0 of syn_1tb/syn_10tb/syn_11tb):")
        print(exceeds_sql + ";")
        print(f"--   (c) streaming isolated: bucket='streaming' rows > 0 (ideally {stream_fq}):")
        print(streaming_sql + ";")
        print(f"[plan] dry-run: would run the deployed wave planner (reduced cap "
              f"wave_max_tb={params['wave_max_tb']}) + 3 assertions (multi-wave, exceeds-cap, streaming)")
        return

    print(f"[plan] running deployed wave planner ({WAVE_PLANNER_SUFFIX!r}) with {params}")
    state = _run_deployed_job(a, WAVE_PLANNER_SUFFIX, params=params)
    if state != "SUCCESS":
        raise SystemExit(f"wave planner run did not succeed (result_state={state})")
    print(f"[plan] wave planner run SUCCESS — asserting wave plan shape")

    _scalar = _make_scalar(a.profile, a.warehouse_id)
    failures: list[str] = []

    def _eval(label: str, count_sql: str, threshold: int) -> None:
        n = int(_scalar(count_sql) or 0)
        if n > threshold:
            print(f"  PASS {label}  ({n})")
        elif a.no_lineage:
            print(f"  WARN {label}  ({n}) — downgraded under --no-lineage")
        else:
            print(f"  FAIL {label}  ({n})")
            failures.append(label)

    _eval("multi-wave (COUNT(DISTINCT wave_id) > 1)", multi_sql, threshold=1)
    _eval("synthetic-TB rows exceed byte cap", exceeds_sql, threshold=0)
    _eval("streaming isolated (bucket='streaming')", streaming_sql, threshold=0)

    if failures and not a.no_lineage:
        raise SystemExit(f"plan: {len(failures)} assertion(s) FAILED: {failures}")
    mode = "WARN-only (--no-lineage)" if a.no_lineage else "HARD"
    print(f"[plan] assertions complete ({mode} mode) — {len(failures)} failure(s)")
    print(f"[plan] estate is now fully populated + planned (seed-and-leave state; "
          f"--populate stops here)")


# --------------------------------------------------------------------------- #
# Stage: e2e — full backend lifecycle over the PILOT SCHEMA (Task 15).
# --------------------------------------------------------------------------- #
# The e2e stage drives the DEPLOYED lifecycle jobs end-to-end over the builder's PILOT SCHEMA
# only (bounded blast radius). It NEVER mutates a table inline — every conversion runs INSIDE
# the deployed 04 job and every reversal inside the deployed 06 job. Order:
#   03 dry run -> 03c freeze -> 04 convert -> 05 validate -> 05b unfreeze+verify -> 06 rollback.
# Guarded by --confirm-execute: a bare --stage e2e without it is a safe no-op, and --preset
# populate (which omits e2e) is unaffected. The pilot schema == PO_SCHEMA: it received
# Predictive Optimization + OPTIMIZE in the activity stage, so the freeze bracket is genuinely
# exercised (convert would otherwise block on require_compaction_frozen). The reduced-cap plan
# keeps the tiny bronze tables in a single wave, so the pilot schema is treated as one wave.

E2E_PILOT_SCHEMA = PO_SCHEMA   # "e2e_real_bronze" — the PO/OPTIMIZE schema, so freeze is real

DRY_RUN_SUFFIX = "managed-table-migration :: 03 dry run"
FREEZE_SUFFIX = "managed-table-migration :: 03c freeze"
CONVERT_SUFFIX = "managed-table-migration :: 04 convert"
VALIDATE_SUFFIX = "managed-table-migration :: 05 validate"
UNFREEZE_SUFFIX = "managed-table-migration :: 05b unfreeze + verify"
ROLLBACK_SUFFIX = "managed-table-migration :: 06 rollback (manual)"
ENABLE_OPT_SUFFIX = "managed-table-migration :: 08c enable predictive optimization"

# audit_log action strings written by 08c (pinned to migration.audit.ACTION_ENABLE_*).
_ACTION_ENABLE_PO = "ENABLE_PREDICTIVE_OPTIMIZATION"
_ACTION_ENABLE_AL = "ENABLE_AUTO_LIQUID"

# A single PARTITIONED Delta table seeded into the pilot schema to prove auto-liquid's
# default-on-with-opt-out: partitioned tables are excluded from CLUSTER BY AUTO (surfaced as a
# manual ENABLE_AUTO_LIQUID action, never auto-run). Dropped by the e2e_real_* CASCADE teardown.
AL_OPTOUT_TABLE = "al_optout_part"

# Two more pilot tables for the clustering step's other paths (liquid-clustering-step M7):
# - a ZORDERed table: 08c reads its ZORDER columns from DESCRIBE HISTORY and runs
#   CLUSTER BY (those columns) then CLUSTER BY AUTO; rollback returns it to no clustering;
# - a table with manual clustering keys: 08c switches it to AUTO (keeping the keys as the start);
#   rollback restores the keys with AUTO off.
# Its OPTIMIZE ZORDER is a user (warehouse) compaction actor, like the existing compaction papercut,
# so it is a manual freeze item; the --e2e convert relaxes that gate (require_pre_convert_actions_
# cleared=false, recent_optimize_minutes=0). Both are dropped by the e2e_real_* CASCADE teardown.
AL_ZORDER_TABLE = "al_zorder"
AL_ZORDER_COLUMNS = ["event_date", "customer_id"]
AL_MANUAL_KEYS_TABLE = "al_manual_keys"
AL_MANUAL_KEYS = ["region"]

# --e2e-through ranking: convert < validate < rollback (default). A lower rank truncates earlier.
_E2E_THROUGH_RANK = {"convert": 0, "validate": 1, "rollback": 2}


def _e2e_plan_run_id_sql(catalog: str) -> str:
    """Resolve the pilot wave's plan_run_id (the tiny bronze tables sit in a single wave)."""
    return (
        f"SELECT plan_run_id FROM {catalog}.{CONTROL_SCHEMA}.wave_plan "
        f"WHERE schema='{E2E_PILOT_SCHEMA}' LIMIT 1"
    )


def _e2e_wave_id_sql(catalog: str) -> str:
    """Resolve the pilot wave's wave_id (paired with plan_run_id above; both pin the rollback)."""
    return (
        f"SELECT wave_id FROM {catalog}.{CONTROL_SCHEMA}.wave_plan "
        f"WHERE schema='{E2E_PILOT_SCHEMA}' LIMIT 1"
    )


def _e2e_state_count_sql(catalog: str, states: tuple[str, ...]) -> str:
    """COUNT of pilot-schema manifest rows in the given lifecycle state(s)."""
    in_list = ", ".join(f"'{s}'" for s in states)
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE schema='{E2E_PILOT_SCHEMA}' AND state IN ({in_list})"
    )


def _e2e_freeze_frozen_count_sql(catalog: str) -> str:
    """COUNT applied FROZEN ledger rows for the pilot schema (dry_run=false).

    ``freeze_ledger`` carries ``catalog`` + ``schema`` ALWAYS (per control-tables.md); ``full_name``
    is NULL for schema-scoped Predictive-Optimization rows. Scoping on ``catalog`` + ``schema``
    therefore covers BOTH the schema-scoped PO freeze AND the table-scoped compaction freezes for
    ``e2e_real_bronze`` in one predicate. ``dry_run=false`` + ``status='FROZEN'`` proves the freeze
    actually froze (a PLANNED/dry-run row does not satisfy convert's require_compaction_frozen gate).
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.freeze_ledger "
        f"WHERE dry_run=false AND status='FROZEN' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_freeze_ledger_any_count_sql(catalog: str) -> str:
    """COUNT ANY-status applied (dry_run=false) ledger rows for the pilot schema.

    Mirrors ``_e2e_freeze_frozen_count_sql`` but DROPS the ``status='FROZEN'`` predicate, so it
    counts every applied ledger row regardless of status. 0 means the pilot had no auto-freezable
    actor (a manual OPTIMIZE / not-yet-run Predictive Optimization freezes nothing) — in which
    case 0 FROZEN is CORRECT and convert proceeds with no coverage gap.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.freeze_ledger "
        f"WHERE dry_run=false "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_freeze_unfrozen_count_sql(catalog: str) -> str:
    """COUNT ledger rows flipped to UNFROZEN for the pilot schema (05b restore evidence)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.freeze_ledger "
        f"WHERE status='UNFROZEN' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_audit_action_phase_count_sql(catalog: str, action: str, phase: str) -> str:
    """COUNT audit_log rows for the pilot schema by action + phase (08c enablement evidence)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.audit_log "
        f"WHERE action='{action}' AND phase='{phase}' "
        f"AND full_name LIKE '{catalog}.{E2E_PILOT_SCHEMA}.%'"
    )


def _e2e_rollback_reversal_count_sql(catalog: str, kind: str) -> str:
    """COUNT verified finalize-reconcile reversals of ``kind`` (RB-M2) for the pilot schema.

    06 rollback's reconcile-finalize pass writes an ``action='ROLLBACK'`` audit row per reversed
    delta whose ``detail`` begins ``finalize-reconcile <kind> (...) acted=... verified=...``. A
    ``verified=True`` detail confirms the DISABLE PREDICTIVE OPTIMIZATION / CLUSTER BY NONE took
    effect (live re-read). ``kind`` is ``'REVERSE_PO'`` or ``'REVERSE_AUTO_LIQUID'``.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.audit_log "
        f"WHERE action='ROLLBACK' "
        f"AND detail LIKE 'finalize-reconcile {kind}%verified=True%' "
        f"AND full_name LIKE '{catalog}.{E2E_PILOT_SCHEMA}.%'"
    )


def _e2e_obsolete_finalize_actions_sql(catalog: str) -> str:
    """COUNT ENABLE_PREDICTIVE_OPTIMIZATION / ENABLE_AUTO_LIQUID actions now OBSOLETE (RB-M2)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind IN ('{_ACTION_ENABLE_PO}', '{_ACTION_ENABLE_AL}') "
        f"AND status='OBSOLETE' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_ledger_rolled_back_count_sql(catalog: str) -> str:
    """COUNT freeze_ledger rows stamped ROLLED_BACK for the pilot schema (RB-M4)."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.freeze_ledger "
        f"WHERE status='ROLLED_BACK' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_ledger_applied_count_sql(catalog: str) -> str:
    """COUNT applied (non-dry-run) freeze_ledger rows for the pilot: FROZEN, UNFROZEN, or ROLLED_BACK.

    Proof the freeze bracket really ran this run — survives the RB-M4 status flip (a ROLLED_BACK row
    was a FROZEN/UNFROZEN row), so this precondition holds whether read before or after rollback.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.freeze_ledger "
        f"WHERE dry_run=false AND status IN ('FROZEN', 'UNFROZEN', 'ROLLED_BACK') "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_open_auto_liquid_actions_sql(catalog: str) -> str:
    """COUNT ENABLE_AUTO_LIQUID actions left OPEN (held back from default auto-liquid) for the pilot."""
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='{_ACTION_ENABLE_AL}' AND status='OPEN' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_pilot_full_names_sql(catalog: str) -> str:
    """CSV of converted (MANAGED) pilot full_names — the set to read effective PO on.

    Returned as a single comma-joined cell so the scalar reader (``_make_scalar``) can fetch it in
    one round-trip (mirrors ``_e2e_retired_job_ids_sql``). Read after 05 validate: rows are
    CONVERTED/VALIDATED (MANAGED), so their effective PO flag is meaningful.
    """
    return (
        f"SELECT array_join(collect_list(full_name), ',') "
        f"FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}' "
        f"AND state IN ('CONVERTED', 'VALIDATED')"
    )


def _owner_variant_verdict_sql(catalog: str, full_name: str) -> str:
    """(status, basis, table_owner) of the latest stored convert-permission verdict for one table."""
    return (
        f"SELECT status, basis, table_owner FROM {catalog}.{CONTROL_SCHEMA}.convert_authority "
        f"WHERE full_name='{full_name}' ORDER BY evaluated_at DESC LIMIT 1"
    )


def _e2e_optout_held_back_sql(catalog: str) -> str:
    """COUNT OPEN ENABLE_AUTO_LIQUID actions for the seeded partitioned opt-out table (held back).

    Keys on full_name (populated) NOT table_name: 08c writes ENABLE_AUTO_LIQUID action rows with
    table_name=None (an IDENTITY column, never backfilled), so a table_name filter never matches.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='{_ACTION_ENABLE_AL}' AND status='OPEN' "
        f"AND full_name='{catalog}.{E2E_PILOT_SCHEMA}.{AL_OPTOUT_TABLE}'"
    )


_MIGRATED = ("CONVERTED", "VALIDATED", "CLEANED", "CONVERTED_EXTERNAL")


def _e2e_clustering_layout_check(catalog: str, _scalar, failures: list, table: str, label: str, *,
                                 expect_auto: bool, expect_keys: list, states: tuple) -> None:
    """Print + collect one live clustering-layout assertion for a seeded pilot table."""
    fq = f"{catalog}.{E2E_PILOT_SCHEMA}.{table}"
    try:
        state = _scalar(f"SELECT state FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
                        f"WHERE full_name='{fq}'")
    except Exception:  # noqa: BLE001
        state = None
    layout = None
    if state in states:
        try:
            layout = re_.layout_from_extended_json(
                _scalar("DESCRIBE TABLE EXTENDED " + ".".join(f"`{p}`" for p in fq.split(".")) + " AS JSON"))
        except Exception as e:  # noqa: BLE001
            print(f"  note could not read the layout of {fq} ({type(e).__name__})")
    tag, msg = re_.clustering_layout_outcome(label, layout=layout, expect_auto=expect_auto,
                                             expect_keys=expect_keys, seeded=state is not None,
                                             converted=state in states)
    print(f"  [{tag}] {msg}")
    if tag == "FAIL":
        failures.append(label)


def _e2e_retired_job_ids_sql(catalog: str) -> str:
    """CSV of RETIRE_MAINTENANCE_JOB ``target_id`` (job ids) 05b raised for the pilot schema (RB-M3).

    Under ``post_convert_maintenance_policy=prefer_po``, 05b leaves a PO-redundant maintenance job
    PAUSED and records a ``RETIRE_MAINTENANCE_JOB`` action whose ``target_id`` is the job id. A job
    whose id appears here must remain PAUSED after 05b; one that does not must be UNPAUSED (resumed).
    """
    return (
        f"SELECT array_join(array_sort(collect_set(target_id)), ',') "
        f"FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='RETIRE_MAINTENANCE_JOB' AND target_kind='JOB' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_writer_attributed_sql(catalog: str, job_id: str) -> str:
    """COUNT writer_sources rows attributing the seeded batch writer job (SCHEDULED_JOB/EVENT_JOB).

    The profiler writes one ``writer_sources`` row per classified WRITE-lineage edge; a scheduled
    ``INSERT`` job attributes as ``writer_class='SCHEDULED_JOB'`` carrying ``entity_id=job_id``.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.writer_sources "
        f"WHERE entity_id='{job_id}' AND writer_class IN ('SCHEDULED_JOB', 'EVENT_JOB') "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_writer_stop_actions_sql(catalog: str, job_id: str) -> str:
    """COUNT blocking PRE_CONVERT STOP_WRITER_JOB actions targeting the seeded writer job (feature F).

    ``actions._writer_stop_actions`` emits one blocking (``blocking=true``) ``STOP_WRITER_JOB``
    (``phase='PRE_CONVERT'``, ``target_kind='JOB'``) per attributed stoppable writer job.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.migration_actions "
        f"WHERE action_kind='STOP_WRITER_JOB' AND target_kind='JOB' AND target_id='{job_id}' "
        f"AND blocking=true AND phase='PRE_CONVERT' "
        f"AND catalog='{catalog}' AND schema='{E2E_PILOT_SCHEMA}'"
    )


def _e2e_schedule_verdict_sql(catalog: str, full_name: str) -> str:
    """The live writer_schedule_verdict the profiler stamped on a twin's manifest row (feature G)."""
    return (
        f"SELECT writer_schedule_verdict FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE full_name='{full_name}'"
    )


def _e2e_schedule_next_write_sql(catalog: str, full_name: str) -> str:
    """The live next_scheduled_write_at (cron next-fire) as STRING, or NULL, for a twin (feature G)."""
    return (
        f"SELECT CAST(next_scheduled_write_at AS STRING) FROM {catalog}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE full_name='{full_name}'"
    )


def _e2e_convert_audit_count_sql(catalog: str) -> str:
    """COUNT successful conversion audit rows (action='CONVERT', phase='SUCCESS').

    ``audit.ACTION_CONVERT == 'CONVERT'`` and ``phase`` values include ``'SUCCESS'`` (control-tables
    contract); the deployed 04 job appends one such row per converted table. Mirrors the reference
    driver ``e2e_control_plane.py``.
    """
    return (
        f"SELECT COUNT(*) FROM {catalog}.{CONTROL_SCHEMA}.audit_log "
        f"WHERE action='CONVERT' AND phase='SUCCESS'"
    )


def _e2e_finish(failures: list[str], through: str) -> None:
    """Print the closing lifecycle note (+ re-runnable note for a full run) and fail loud on failures."""
    print(f"\n[e2e] lifecycle complete (through={through})")
    if through == "rollback":
        print("[e2e] estate left converted -> validated -> rolled-back = RE-RUNNABLE (the reversal "
              "stayed within the 14-day window). This is the backend end-to-end test; the "
              "--populate preset never reaches the e2e stage.")
    else:
        print(f"[e2e] stopped early at --e2e-through={through}; the pilot schema is NOT fully "
              f"reversed (re-run with --e2e-through=rollback to leave the estate re-runnable).")
    if failures:
        raise SystemExit(f"e2e: {len(failures)} assertion(s) FAILED: {failures}")
    print(f"[e2e] all assertions green ({through})")


def _submit_vacuum_all(a: argparse.Namespace, w, targets: list[str]) -> bool:
    """One-off run of the maintenance notebook that VACUUMs EVERY pilot table (fresh commit). True on success."""
    from databricks.sdk.service.jobs import NotebookTask, RunLifeCycleState, SubmitTask

    user = w.current_user.me().user_name
    ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_MAINT_WS_NAME}"
    try:
        resp = w.jobs.submit(
            run_name="mtm_e2e :: vacuum-block probe (VACUUM all pilot)",
            tasks=[SubmitTask(
                task_key="vacuum_all",
                notebook_task=NotebookTask(
                    notebook_path=ws_path,
                    base_parameters={"operation": "VACUUM", "targets": ",".join(targets)}),
            )])
        t0 = time.time()
        while time.time() - t0 < 1800:
            run = w.jobs.get_run(run_id=resp.run_id)
            life = run.state.life_cycle_state if run.state else None
            if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                        RunLifeCycleState.INTERNAL_ERROR):
                result = run.state.result_state if run.state else None
                ok = (result is not None and result.value == "SUCCESS")
                print(f"  {'ok  ' if ok else 'note'} vacuum-all run: "
                      f"{result.value if result else 'UNKNOWN'} ({len(targets)} table(s))")
                return ok
            time.sleep(10)
        print("  note vacuum-all run did not finish within 1800s — skipping probe")
        return False
    except Exception as e:   # noqa: BLE001 — degrade honestly
        print(f"  WARN vacuum-all submit failed ({type(e).__name__}: {e}); skipping vacuum-block probe")
        return False


def _vacuum_block_probe(a, w, catalog, pilot, tgt, converted_sql, dry_ok_sql, _scalar,
                        _run_stage, check, failures) -> None:
    """Prove the Layer-1 recent-VACUUM convert gate blocks, then restore DRY_RUN_OK (self-contained)."""
    plan = re_.build_estate_plan(table_count=a.table_count, rng_seed=a.rng_seed)
    targets = _maintenance_targets(catalog, plan)
    if not targets:
        print("  [WARN] vacuum-block probe: no pilot Delta tables resolved — skipping")
        return
    print(f"\n== e2e :: vacuum-block probe (VACUUMs {len(targets)} pilot table(s) then convert) ==")
    # The activity-seeded pilot tables are not in the provision plan; VACUUM them too so the whole
    # pilot wave is uniformly blocked and the probe still converts nothing.
    targets = targets + [f"{catalog}.{pilot}.{t}" for t in (AL_OPTOUT_TABLE, AL_ZORDER_TABLE, AL_MANUAL_KEYS_TABLE)]
    if not _submit_vacuum_all(a, w, targets):
        print("  [WARN] vacuum-block probe: could not freshly VACUUM the pilot — skipping the assertion")
        return
    # VACUUM only surfaces in DESCRIBE HISTORY when `delta.vacuum.logging.enabled` is on. It defaults
    # OFF on object-store (S3/ADLS) backends and cannot be turned on from serverless (the
    # `spark.databricks.delta.vacuum.logging.enabled` session conf is blocked, and it is not a table
    # property). Where VACUUM leaves no history footprint, the Layer-1 recent-vacuum convert gate
    # (which reads DESCRIBE HISTORY) cannot fire — so running the probe convert would (correctly, given
    # no detectable vacuum) CONVERT the pilot and derail the lifecycle. Detect that and DEGRADE
    # HONESTLY: skip the probe with a WARN rather than converting prematurely. The gate logic itself is
    # proven offline (test_convert.py::reassert_gating(recent_vacuum=True) -> BLOCKED).
    probe_tbl = ".".join(f"`{p}`" for p in targets[0].split("."))
    vac_hist_sql = (
        f"SELECT COUNT(*) FROM (DESCRIBE HISTORY {probe_tbl}) "
        f"WHERE operation IN ('VACUUM START', 'VACUUM END') "
        f"AND timestamp > current_timestamp() - INTERVAL 120 MINUTES")
    if int(_scalar(vac_hist_sql) or 0) == 0:
        print("  [WARN] vacuum-block probe: this backend does not log VACUUM to DESCRIBE HISTORY "
              "(vacuum logging off / not enableable on serverless-S3), so the Layer-1 recent-vacuum "
              "convert gate is not exercisable LIVE here — skipping the block assertion (it is unit- "
              "tested offline). NOT running the probe convert, to avoid a premature conversion.")
        return
    # Convert with ONLY the recent-vacuum gate active (coverage gates relaxed): must convert nothing.
    probe_params = {
        "target_schema": tgt,
        "confirm_compaction_paused": "true",
        "recent_optimize_minutes": "0",
        "recent_vacuum_minutes": "120",
        "require_compaction_frozen": "false",
        "require_streams_stopped": "false",
        "require_ownership_leased": "false",
    }
    _run_stage("04 convert (vacuum-block probe)", CONVERT_SUFFIX, probe_params)
    check("recent VACUUM blocks convert (nothing converted)",
          int(_scalar(converted_sql) or 0) == 0, "pilot stays unconverted under recent_vacuum_minutes>0")
    # Restore: re-run 03 dry run to clear the recent-vacuum BLOCKED state back to DRY_RUN_OK.
    _run_stage("03 dry run (restore after probe)", DRY_RUN_SUFFIX,
               {"catalog_filter": catalog, "schema_filter": pilot})
    check("dry-run restored pilot to DRY_RUN_OK after probe",
          int(_scalar(dry_ok_sql) or 0) > 0, f"schema={pilot}")


def _e2e(a: argparse.Namespace) -> None:
    """Full backend lifecycle over the pilot schema, driven entirely through the DEPLOYED jobs.

    Guarded by ``--confirm-execute`` (the stage triggers real conversion/reversal via the deployed
    04/06 jobs). ``--dry-run`` prints the full lifecycle intent (per-stage job suffix + exact
    ``job_parameters`` + the assertion queries it WOULD run + the ``--e2e-through`` truncation
    point) with NO SDK import and NO network. In the live path every deployed-job run that is not
    SUCCESS fails loud (``SystemExit``); scalar assertions are collected via a local ``check`` and
    any failures raise at the end. The e2e stage is always HARD (``--no-lineage`` does not apply).
    """
    catalog = a.catalog
    through = a.e2e_through
    through_rank = _E2E_THROUGH_RANK[through]
    pilot = E2E_PILOT_SCHEMA
    tgt = f"{catalog}.{pilot}"

    # Per-stage deployed-job parameters (all pilot-schema scoped).
    dry_params = {"catalog_filter": catalog, "schema_filter": pilot}
    freeze_params = {"target_schema": tgt, "confirm_execute": "true"}
    # The estate was intentionally OPTIMIZEd AND VACUUMed in the activity stage, so the 04 convert
    # recent-optimize / recent-vacuum time-window checks (deploy default 120 min) would pre-check-
    # BLOCK this pilot table in a single one-shot run. Disable only those time-window catches here;
    # the machine-verified freeze-coverage gate (require_compaction_frozen) still enforces a real
    # freeze. (The recent-vacuum gate itself is proven by the terminal vacuum-block probe below.)
    # The activity stage also seeds a MANUAL OPTIMIZE papercut (a USER-attributed compaction actor),
    # which generates a BLOCKING PRE_CONVERT ``FREEZE_COMPACTION`` action with no job/PO target that
    # 03c freeze legitimately cannot auto-clear (you cannot pause a human). That manual-acknowledgment
    # gate would block the one-shot convert, so we override it here — the machine-verified freeze-
    # coverage gate (``require_compaction_frozen=true``, left at its default) still HARD-enforces that
    # every schedulable JOB / schema-PO actor is really FROZEN before any conversion runs.
    convert_params = {
        "target_schema": tgt,
        "confirm_compaction_paused": "true",
        "recent_optimize_minutes": "0",
        "recent_vacuum_minutes": "0",
        "require_pre_convert_actions_cleared": "false",
    }
    validate_params = {"catalog_filter": catalog, "schema_filter": pilot}
    unfreeze_params = {"target_schema": tgt, "confirm_execute": "true"}
    # 08c enable optimization (POST_CONVERT): turn Predictive Optimization ON for every converted
    # pilot table and apply CLUSTER BY AUTO to the eligible clean set (default-on, opt-out). Scoped
    # by target_schema so it covers the whole converted pilot schema regardless of wave fan-out.
    enable_opt_params = {
        "target_schema": tgt,
        "enable_po_on_convert": "true",
        "confirm_execute": "true",
        "auto_liquid_policy": "enable_eligible",
    }
    # 06 rollback params (plan_run_id + wave_id) resolve from the pilot wave at run time.

    # Pure assertion SQL (module-level builders) — shared by dry-run printing + live evaluation.
    plan_run_id_sql = _e2e_plan_run_id_sql(catalog)
    wave_id_sql = _e2e_wave_id_sql(catalog)
    dry_ok_sql = _e2e_state_count_sql(catalog, ("DRY_RUN_OK",))
    frozen_sql = _e2e_freeze_frozen_count_sql(catalog)
    ledger_any_sql = _e2e_freeze_ledger_any_count_sql(catalog)
    audit_sql = _e2e_convert_audit_count_sql(catalog)
    converted_sql = _e2e_state_count_sql(catalog, ("CONVERTED", "VALIDATED"))
    validated_sql = _e2e_state_count_sql(catalog, ("VALIDATED",))
    unfrozen_sql = _e2e_freeze_unfrozen_count_sql(catalog)
    rolled_back_sql = _e2e_state_count_sql(catalog, ("ROLLED_BACK",))

    if a.dry_run:
        print(f"[dispatch] e2e (dry-run) — full backend lifecycle over the PILOT SCHEMA {tgt} "
              f"via DEPLOYED jobs only (never inline); --e2e-through={through}")
        print("-- resolve the pilot wave FIRST (fail loud if the `plan` stage never ran):")
        print(plan_run_id_sql + ";")
        print(wave_id_sql + ";")

        print(f"\n-- [rank convert] 03 dry run — REUSE deployed job ending {DRY_RUN_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={dry_params})")
        print("--   assert run SUCCESS; assert pilot rows advanced to DRY_RUN_OK (>0):")
        print(dry_ok_sql + ";")

        print(f"\n-- [rank convert] 03c freeze — REUSE deployed job ending {FREEZE_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={freeze_params})")
        print(f"--   (freeze by SCHEMA covers BOTH the schema-scoped Predictive-Optimization actor "
              f"and the table-scoped compaction actors on {pilot})")
        print("--   assert run SUCCESS. The FROZEN assertion is CONDITIONAL — first count ANY "
              "applied (dry_run=false) ledger rows for the pilot:")
        print(ledger_any_sql + ";")
        print("--   FROZEN>0 is asserted ONLY when the pilot has an auto-freezable actor "
              "(ledger rows > 0), proving the freeze actually froze so convert won't block on "
              "require_compaction_frozen. If the pilot's only compaction is a manual OPTIMIZE / "
              "a not-yet-run Predictive Optimization, it legitimately freezes nothing (ledger "
              "rows == 0) — 0 FROZEN is then CORRECT and convert proceeds with no coverage gap, "
              "so it is a WARN, not a FAIL:")
        print(frozen_sql + ";")
        print(f"--   PAUSE_JOB round-trip: assert the seeded scheduled jobs "
              f"({MAINT_OPTIMIZE_JOB_NAME!r}, {MAINT_VACUUM_JOB_NAME!r}) have schedule.pause_status="
              f"PAUSED after freeze (platform truth via executor.read_job_pause_status).")
        print(f"--   feature F: assert the seeded batch writer job ({WRITER_JOB_NAME!r}) is "
              f"attributed in writer_sources with a blocking PRE_CONVERT STOP_WRITER_JOB, is PAUSED "
              f"after freeze, and UNPAUSED (forward-restarted, never retired) after 05b, from:")
        print(_e2e_writer_stop_actions_sql(catalog, "<writer_job_id>") + ";")
        print(f"--   feature G: assert the two seeded schedule twins read a DETERMINISTIC live "
              f"writer_schedule_verdict — {SCHED_FAR_JOB_NAME!r} -> SAFE_NOW, "
              f"{SCHED_IMMINENT_JOB_NAME!r} -> WARN_WINDOW (each with next_scheduled_write_at set), "
              f"from table_manifest.writer_schedule_verdict (asserted post-profile, pre-03c).")

        print(f"\n-- [rank convert] vacuum-block probe — REUSE {CONVERT_SUFFIX!r} on freshly-VACUUMed pilot:")
        print("-- would submit a one-off VACUUM over EVERY pilot table, then run_now(job_parameters="
              "{target_schema, recent_vacuum_minutes:120, recent_optimize_minutes:0, "
              "require_compaction_frozen:false, require_streams_stopped:false, require_ownership_leased:false})")
        print("--   assert NOTHING converts (every table blocked with recent_vacuum_activity), then "
              "re-run 03 dry run to restore DRY_RUN_OK:")
        print(converted_sql + "   -- expect 0 immediately after the probe convert;")
        print(dry_ok_sql + "   -- expect >0 again after the restore dry run;")

        print(f"\n-- [rank convert] 04 convert — REUSE deployed job ending {CONVERT_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={convert_params})  "
              f"-- recent_vacuum_minutes:0 mirrors recent_optimize_minutes:0 (the estate was "
              f"intentionally VACUUMed/OPTIMIZEd in activity; the freeze-coverage gate still enforces a real freeze)")
        print("--   tolerate SUCCESS_WITH_FAILURES (multi-wave pilot: a later wave gated by an "
              "un-freezable schema-PO/USER actor can fail its for_each iteration while earlier waves "
              "convert UNDER the freeze); the CONVERTED-count assertion enforces real progress:")
        print("--   assert CONVERT SUCCESS audit rows (>0) + pilot tables CONVERTED/VALIDATED (>0):")
        print(audit_sql + ";")
        print(converted_sql + ";")

        if through_rank < _E2E_THROUGH_RANK["validate"]:
            print(f"\n[e2e] --e2e-through={through}: STOP after 04 convert "
                  f"(would skip 05 validate / 05b unfreeze+verify / 06 rollback)")
            print("[e2e] dry-run: printed the convert-truncated lifecycle intent (fully offline)")
            return

        print(f"\n-- [rank validate] 05 validate — REUSE deployed job ending {VALIDATE_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={validate_params})")
        print("--   assert run SUCCESS; assert pilot tables VALIDATED (>0):")
        print(validated_sql + ";")

        print(f"\n-- [rank validate] 08c enable optimization — REUSE deployed job ending "
              f"{ENABLE_OPT_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={enable_opt_params})")
        print("--   assert run SUCCESS; assert audit_log has ENABLE_PREDICTIVE_OPTIMIZATION/SUCCESS "
              "rows (PO on) and ENABLE_AUTO_LIQUID/SUCCESS rows (CLUSTER BY AUTO on the eligible "
              "clean set) for the pilot schema (feature A), and the seeded partitioned opt-out "
              "table is HELD BACK — surfaced as a manual ENABLE_AUTO_LIQUID action, never "
              "CLUSTER BY AUTO'd.")
        print(f"--   assert {AL_ZORDER_TABLE} is on AUTO with keys {AL_ZORDER_COLUMNS} (its ZORDER "
              f"columns) and {AL_MANUAL_KEYS_TABLE} is on AUTO keeping {AL_MANUAL_KEYS} "
              f"(live DESCRIBE TABLE EXTENDED … AS JSON).")

        print(f"\n-- [rank validate] 05b unfreeze + verify — REUSE deployed job ending {UNFREEZE_SUFFIX!r}:")
        print(f"-- run_now(job_parameters={unfreeze_params})")
        print("--   assert run SUCCESS. UNFROZEN>0 is asserted ONLY when something was actually "
              "frozen (FROZEN rows > 0); if nothing was frozen, 0 UNFROZEN is CORRECT — a WARN, "
              "not a FAIL:")
        print(unfrozen_sql + ";")
        print("--   RB-M3 forward-only unfreeze: assert each seeded scheduled job is PAUSED iff 05b "
              "RETIRED it (RETIRE_MAINTENANCE_JOB target_id) else UNPAUSED (resumed), from:")
        print(_e2e_retired_job_ids_sql(catalog) + ";")

        if through_rank < _E2E_THROUGH_RANK["rollback"]:
            print(f"\n[e2e] --e2e-through={through}: STOP after 05b unfreeze+verify (would skip 06 rollback)")
            print("[e2e] dry-run: printed the validate-truncated lifecycle intent (fully offline)")
            return

        print(f"\n-- [rank rollback] 06 rollback (manual) — REUSE deployed job ending {ROLLBACK_SUFFIX!r}:")
        print("-- run_now(job_parameters={'plan_run_id': <resolved>, 'wave_id': <resolved>, "
              "'reason': 'e2e-real'})")
        print("--   (reverses the pilot wave within the 14-day window so the estate stays re-runnable)")
        print("--   assert run SUCCESS; assert pilot tables ROLLED_BACK (>0):")
        print(rolled_back_sql + ";")
        print("--   RB-M2: assert ROLLBACK audit rows tagged 'finalize-reconcile REVERSE_PO/"
              "REVERSE_AUTO_LIQUID … verified=True' (08c's PO + auto-liquid reversed, each asserted "
              "INDEPENDENTLY so inherited-on PO does not mask the auto-liquid reversal) and the "
              "ENABLE_* finalize actions now OBSOLETE, from:")
        print(_e2e_rollback_reversal_count_sql(catalog, "REVERSE_PO") + ";")
        print(_e2e_rollback_reversal_count_sql(catalog, "REVERSE_AUTO_LIQUID") + ";")
        print(f"--   assert {AL_ZORDER_TABLE} is back to no clustering and {AL_MANUAL_KEYS_TABLE} has "
              f"keys {AL_MANUAL_KEYS} with AUTO off (rollback restored the recorded layout).")
        print("--   RB-M4: assert 06 rollback stamped freeze_ledger rows ROLLED_BACK for the "
              "rolled-back pilot, from:")
        print(_e2e_ledger_rolled_back_count_sql(catalog) + ";")

        print("\n[e2e] dry-run: printed the FULL lifecycle intent (03 dry run -> vacuum-block "
              "probe -> 03c freeze -> 04 convert -> 05 validate -> 08c enable optimization -> "
              "05b unfreeze+verify -> 06 rollback), plus the post-profile schedule-aware "
              "convert-window (feature G) and writer-stop-bracket (feature F) assertions. After a "
              "full (through=rollback) run the estate is converted -> validated -> rolled-back = "
              "RE-RUNNABLE. This is the backend end-to-end test; --populate never reaches it. "
              "Fully offline (no SDK import, no network).")
        return

    # ------------------------------------------------------------------ live #
    if not a.confirm_execute:
        print("[e2e] SKIPPED — the e2e stage performs REAL table conversion + reversal via the "
              "DEPLOYED lifecycle jobs and requires --confirm-execute. Re-run with --confirm-execute "
              "to exercise the full backend lifecycle over the pilot schema. (No-op; --preset "
              "populate never includes e2e, and a bare --stage e2e without the flag is a safe no-op.)")
        return

    _scalar = _make_scalar(a.profile, a.warehouse_id)
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(label)

    def _run_stage(label: str, suffix: str, params: dict, allow_partial: bool = False) -> None:
        print(f"\n== e2e :: {label} ==")
        state = _run_deployed_job(a, suffix, params=params)
        ok = state == "SUCCESS" or (allow_partial and state == "SUCCESS_WITH_FAILURES")
        if not ok:
            raise SystemExit(f"e2e: {label} run did not succeed (result_state={state})")
        if state == "SUCCESS":
            print(f"  ok   {label} run SUCCESS")
        else:
            print(f"  WARN {label} run {state} — partial success TOLERATED: the target_schema-scoped "
                  f"convert fans out over the multi-wave pilot schema, so a LATER wave gated by an "
                  f"un-freezable schema-PO/USER compaction actor raises its for_each iteration even "
                  f"though earlier waves converted cleanly UNDER the freeze. The CONVERTED-count "
                  f"assertion below enforces real progress; 06 rollback reverses EVERY converted wave.")

    print(f"[e2e] full backend lifecycle over pilot schema {tgt} via deployed jobs "
          f"(--e2e-through={through})")

    # Resolve the pilot wave FIRST (fail loud — the `plan` stage must have run).
    plan_run_id = _scalar(plan_run_id_sql)
    wave_id = _scalar(wave_id_sql)
    if not plan_run_id or not wave_id:
        raise SystemExit(
            f"e2e: no wave_plan row for schema {pilot!r} — run the `plan` stage first "
            f"(plan_run_id={plan_run_id!r}, wave_id={wave_id!r})")
    print(f"[e2e] resolved pilot wave: plan_run_id={plan_run_id}, wave_id={wave_id}")

    # Resolve the seeded scheduled OPTIMIZE/VACUUM jobs (by exact name) so we can assert the REAL
    # PAUSE_JOB/RESUME_JOB round-trip against platform truth. Best-effort: if the activity stage
    # could not seed them (grants), the pause assertions are skipped (WARN) rather than failing.
    from databricks.sdk import WorkspaceClient  # noqa: PLC0415 — live-only, keeps --dry-run offline
    from migration import executor as ex_       # noqa: PLC0415 — pure module; job pause read/verify

    _w = WorkspaceClient(profile=a.profile)
    maint_jobs = {name: _find_job_id_by_name(_w, name)
                  for name in (MAINT_OPTIMIZE_JOB_NAME, MAINT_VACUUM_JOB_NAME)}
    for name, jid in maint_jobs.items():
        print(f"[e2e] maintenance job {name!r} -> job_id={jid}")

    # feature F: resolve the seeded batch writer job and assert its PRE_CONVERT stop bracket. The
    # WRITE edge is attributed via lineage (lagging) — if it hasn't landed, the bracket assertion
    # (and the pause round-trip below) degrade to WARN. writer_bracketed drives the pause checks.
    writer_job_id = _find_job_id_by_name(_w, WRITER_JOB_NAME)
    print(f"[e2e] batch writer job {WRITER_JOB_NAME!r} -> job_id={writer_job_id}")
    if writer_job_id is not None:
        _w_attr = int(_scalar(_e2e_writer_attributed_sql(catalog, str(writer_job_id))) or 0)
        _w_stop = int(_scalar(_e2e_writer_stop_actions_sql(catalog, str(writer_job_id))) or 0)
    else:
        _w_attr, _w_stop = 0, 0
    wb_tag, wb_msg = re_.writer_stop_bracket_outcome(
        attributed=_w_attr, stop_actions_blocking=_w_stop)
    print(f"  [{wb_tag}] batch writer job stop bracket (feature F) — {wb_msg}")
    if wb_tag == "FAIL":
        failures.append("writer stop bracket (feature F)")
    writer_bracketed = wb_tag == "PASS"

    # feature G: schedule-aware convert window. Two seeded twins on distinct pilot tables read a
    # DETERMINISTIC live verdict from their cron (the profiler's 15-min margin sits between the
    # imminent <=5-min gap and the far-out ~8h gap). Assert at e2e-start (pre-03c): 03c pauses the
    # twins, which would flip the live verdict to SAFE_NOW. Degrade to WARN on lineage lag / UNKNOWN.
    _twin_plan = re_.build_estate_plan(table_count=a.table_count, rng_seed=a.rng_seed)
    _far_tbl, _imm_tbl = _schedule_twin_targets(catalog, _twin_plan)
    for _label, _tbl, _expected in (("far-out", _far_tbl, "SAFE_NOW"),
                                    ("imminent", _imm_tbl, "WARN_WINDOW")):
        if _tbl is None:
            print(f"  [WARN] schedule twin {_label} (feature G): < 3 pilot tables — skipping")
            continue
        _verdict = _scalar(_e2e_schedule_verdict_sql(catalog, _tbl))
        _next_at = _scalar(_e2e_schedule_next_write_sql(catalog, _tbl))
        sv_tag, sv_msg = re_.schedule_verdict_outcome(
            label=_label, expected=_expected, actual=_verdict, next_write_present=bool(_next_at))
        print(f"  [{sv_tag}] schedule-aware convert window ({_label}, feature G) — {sv_msg}")
        if sv_tag == "FAIL":
            failures.append(f"schedule verdict {_label} (feature G)")

    def _assert_pause_status(expected: str, when: str) -> None:
        """Assert every RESOLVED maintenance job's schedule pause_status == expected (platform truth)."""
        for name, jid in maint_jobs.items():
            if jid is None:
                print(f"  [WARN] {when}: job {name!r} not seeded — skipping pause-status assertion")
                continue
            actual = ex_.read_job_pause_status(_w, jid)
            check(f"{name} schedule {expected} {when}", actual == expected,
                  f"actual={actual}")

    def _assert_unfreeze_restore(when: str) -> None:
        """RB-M3 forward-only unfreeze: each maintenance job is PAUSED iff 05b retired it.

        Retire is PO-state-driven (executor.maintenance_job_restore_decision), so which job (if any)
        gets retired is NOT hardcoded — derive it from the RETIRE_MAINTENANCE_JOB actions the engine
        emitted (target_id=job_id) and assert each job's live schedule matches.
        """
        retired_csv = _scalar(_e2e_retired_job_ids_sql(catalog)) or ""
        retired_ids = {x for x in retired_csv.split(",") if x}
        for name, jid in maint_jobs.items():
            resolved = jid is not None
            actual = ex_.read_job_pause_status(_w, jid) if resolved else None
            retired = resolved and str(jid) in retired_ids
            tag, msg = re_.maintenance_restore_outcome(
                job_name=name, resolved=resolved, actual_pause_status=actual, retired=retired)
            print(f"  [{tag}] {when}: {msg}")
            if tag == "FAIL":
                failures.append(f"{name} {when}")

    # 1. 03 dry run — read-only; advances ELIGIBLE pilot tables to DRY_RUN_OK.
    _run_stage("03 dry run", DRY_RUN_SUFFIX, dry_params)
    check("dry-run advanced pilot to DRY_RUN_OK",
          int(_scalar(dry_ok_sql) or 0) > 0, f"schema={pilot}")

    # 1b. VACUUM-BLOCK PROBE (Layer 1 convert-time hard block). Freshly VACUUM every pilot table,
    # then run 04 convert with recent_vacuum_minutes>0 (and the coverage gates relaxed so ONLY the
    # recent-vacuum gate is in play): assert it converts NOTHING (every table blocked with
    # recent_vacuum_activity). Then re-run 03 dry run to restore DRY_RUN_OK for the real lifecycle.
    # Self-restoring + non-destructive (reassert blocks BEFORE any conversion mutation). Best-effort: if the
    # maintenance notebook was not seeded, the probe is skipped (WARN) rather than failing the run.
    _vacuum_block_probe(a, _w, catalog, pilot, tgt, converted_sql, dry_ok_sql, _scalar,
                        _run_stage, check, failures)

    # 2. 03c freeze — pause PO/jobs + stop streams for the schema, write FROZEN ledger rows.
    # The FROZEN assertion is CONDITIONAL: a pilot whose only compaction is a manual OPTIMIZE +
    # a not-yet-run Predictive Optimization has nothing auto-freezable, so 0 FROZEN is CORRECT
    # (convert proceeds with no coverage gap) — a WARN, not a FAIL.
    _run_stage("03c freeze", FREEZE_SUFFIX, freeze_params)
    n_ledger = int(_scalar(ledger_any_sql) or 0)
    n_frozen = int(_scalar(frozen_sql) or 0)
    fz_tag, fz_msg = re_.freeze_frozen_outcome(n_ledger, n_frozen)
    fz_label = "freeze wrote applied FROZEN ledger rows for pilot"
    print(f"  [{fz_tag}] {fz_label} — {fz_msg}")
    if fz_tag == "FAIL":
        failures.append(fz_label)
    # The real PAUSE_JOB round-trip: the seeded scheduled OPTIMIZE/VACUUM jobs must be PAUSED now.
    _assert_pause_status("PAUSED", "after 03c freeze")

    # feature F: the seeded writer job must be PAUSED by 03c (OP_PAUSE_WRITER_JOB) when bracketed.
    wp_tag, wp_msg = re_.writer_pause_phase_outcome(
        bracketed=writer_bracketed, resolved=writer_job_id is not None, expected="PAUSED",
        actual=ex_.read_job_pause_status(_w, writer_job_id) if writer_job_id else None)
    print(f"  [{wp_tag}] after 03c freeze (writer job) — {wp_msg}")
    if wp_tag == "FAIL":
        failures.append("writer job PAUSED after 03c")

    # 3. 04 convert — the deployed 04 job converts the wave(s) via its for_each over selected waves.
    # The convert is target_schema-scoped, so with the reduced wave_max_tb the pilot schema fans out
    # over SEVERAL waves. If a LATER wave hits a per-wave gate (e.g. an un-freezable schema-PO or
    # USER compaction actor that legitimately can't be auto-frozen), its for_each iteration raises and
    # the job returns SUCCESS_WITH_FAILURES even though the earlier waves converted cleanly UNDER the
    # freeze — which is exactly the freeze->convert->unfreeze proof we are after. Tolerate partial
    # success here (allow_partial) and let the CONVERTED-count assertion enforce real progress; 06
    # rollback then reverses EVERY wave that actually converted, not just the resolved pilot wave.
    _run_stage("04 convert", CONVERT_SUFFIX, convert_params, allow_partial=True)
    n_audit = int(_scalar(audit_sql) or 0)
    check("audit_log has CONVERT SUCCESS rows", n_audit > 0, f"{n_audit} rows")
    n_conv = int(_scalar(converted_sql) or 0)
    check("pilot tables CONVERTED", n_conv > 0, f"{n_conv} tables")

    if through_rank < _E2E_THROUGH_RANK["validate"]:
        _e2e_finish(failures, through)
        return

    # 4. 05 validate — advances CONVERTED -> VALIDATED.
    _run_stage("05 validate", VALIDATE_SUFFIX, validate_params)
    n_valid = int(_scalar(validated_sql) or 0)
    check("pilot tables VALIDATED", n_valid > 0, f"{n_valid} tables")

    # 4b. 08c enable optimization — POST_CONVERT PO + auto-liquid over the converted pilot schema.
    # Runs after validate, before unfreeze. Assertions are added below (PO SUCCESS + auto-liquid).
    _run_stage("08c enable optimization", ENABLE_OPT_SUFFIX, enable_opt_params)

    # PO + auto-liquid default-on assertions. PO is asserted from the AUTHORITATIVE effective flag
    # (UC Tables API effective_predictive_optimization_flag) per converted pilot table — inherited-on
    # PO reads ENABLE even when 08c ran no ALTER, so this is a true PASS instead of the old
    # side-effect (did-08c-write-an-ENABLE-op) WARN. A real ENABLE-PO ALTER failure (po_failed>0) is
    # the HARD check; a genuinely-OFF table FAILs; an unreadable flag degrades to WARN. Auto-liquid
    # degrades to WARN when no clean-eligible table exists this run.
    _po_names_csv = _scalar(_e2e_pilot_full_names_sql(catalog)) or ""
    _po_names = [n for n in _po_names_csv.split(",") if n]
    n_po_on = n_po_unreadable = 0
    for _fn in _po_names:
        _eff = ex_.read_effective_po_for_table(_w, _fn)
        if _eff is True:
            n_po_on += 1
        elif _eff is None:
            n_po_unreadable += 1
        # _eff is False -> genuinely OFF (counted as converted - on - unreadable in the classifier)
    n_po_failed = int(_scalar(_e2e_audit_action_phase_count_sql(catalog, _ACTION_ENABLE_PO, "FAILED")) or 0)
    n_al_ok = int(_scalar(_e2e_audit_action_phase_count_sql(catalog, _ACTION_ENABLE_AL, "SUCCESS")) or 0)
    n_al_manual = int(_scalar(_e2e_open_auto_liquid_actions_sql(catalog)) or 0)
    eo_tag, eo_msg = re_.enable_optimization_outcome(
        converted=n_valid, po_effective_on=n_po_on, al_success=n_al_ok, al_manual=n_al_manual,
        po_failed=n_po_failed, po_unreadable=n_po_unreadable)
    eo_label = "08c enabled PO + auto-liquid on the converted pilot set"
    print(f"  [{eo_tag}] {eo_label} — {eo_msg}")
    if eo_tag == "FAIL":
        failures.append(eo_label)

    # Opt-out proof: the seeded partitioned table must be HELD BACK from default auto-liquid
    # (surfaced as a manual ENABLE_AUTO_LIQUID action, never CLUSTER BY AUTO'd). Degrades to WARN.
    n_optout_held = int(_scalar(_e2e_optout_held_back_sql(catalog)) or 0)
    oo_tag, oo_msg = re_.auto_liquid_optout_outcome(seeded=True, held_back=n_optout_held)
    print(f"  [{oo_tag}] auto-liquid opt-out held-back — {oo_msg}")
    if oo_tag == "FAIL":
        failures.append("auto-liquid opt-out held-back")

    # Liquid-clustering step: the ZORDERed table keeps its ZORDER columns as the starting keys, and
    # the manual-key table switches to AUTO keeping its keys (live DESCRIBE … AS JSON).
    _e2e_clustering_layout_check(catalog, _scalar, failures, AL_ZORDER_TABLE, "08c ZORDER hints",
                                 expect_auto=True, expect_keys=AL_ZORDER_COLUMNS, states=_MIGRATED)
    _e2e_clustering_layout_check(catalog, _scalar, failures, AL_MANUAL_KEYS_TABLE,
                                 "08c manual keys switched to AUTO",
                                 expect_auto=True, expect_keys=AL_MANUAL_KEYS, states=_MIGRATED)

    # 5. 05b unfreeze + verify — restore exactly + flip the ledger to UNFROZEN + close paired actions.
    # CONDITIONAL: if nothing was frozen (n_frozen == 0), 0 UNFROZEN is CORRECT — a WARN, not a FAIL.
    _run_stage("05b unfreeze + verify", UNFREEZE_SUFFIX, unfreeze_params)
    n_unfrozen = int(_scalar(unfrozen_sql) or 0)
    uf_tag, uf_msg = re_.unfreeze_outcome(n_frozen, n_unfrozen)
    uf_label = "ledger flipped to UNFROZEN for pilot"
    print(f"  [{uf_tag}] {uf_label} — {uf_msg}")
    if uf_tag == "FAIL":
        failures.append(uf_label)
    # RB-M3 forward-only unfreeze: 05b does NOT blanket-resume. For each maintenance job we paused at
    # 03c it either RESUMES it (PO off/unknown) or RETIRES it (PO now maintains the converted table —
    # schedule left PAUSED + a RETIRE_MAINTENANCE_JOB action). Derive which from the actions the
    # engine emitted (target_id=job_id) and assert each job's live schedule matches. WARN when a job
    # was not seeded / has no readable schedule.
    _assert_unfreeze_restore("after 05b unfreeze")

    # feature F: the writer job is FORWARD-restarted by 05b (RESUME_WRITER_JOB) — never retired
    # (retire is maintenance-only). When bracketed, its schedule must be UNPAUSED again.
    wr_tag, wr_msg = re_.writer_pause_phase_outcome(
        bracketed=writer_bracketed, resolved=writer_job_id is not None, expected="UNPAUSED",
        actual=ex_.read_job_pause_status(_w, writer_job_id) if writer_job_id else None)
    print(f"  [{wr_tag}] after 05b unfreeze (writer job) — {wr_msg}")
    if wr_tag == "FAIL":
        failures.append("writer job UNPAUSED after 05b")

    if through_rank < _E2E_THROUGH_RANK["rollback"]:
        _e2e_finish(failures, through)
        return

    # 6. 06 rollback (manual) — reverse the pilot within the 14-day window (re-runnable estate).
    # The convert is target_schema-scoped, so with the reduced wave_max_tb the pilot schema can span
    # SEVERAL waves; 06 rollback pins ONE (plan_run_id, wave_id), so reverse EVERY wave that actually
    # converted (derived from the CONVERT SUCCESS audit rows) rather than just the first-resolved
    # pilot wave — otherwise converted tables in the other waves are left MANAGED.
    converted_waves_csv = _scalar(
        f"SELECT array_join(array_sort(collect_set(wave_id)), ',') "
        f"FROM {catalog}.{CONTROL_SCHEMA}.audit_log "
        f"WHERE action='CONVERT' AND phase='SUCCESS' AND plan_run_id='{plan_run_id}' "
        f"AND full_name LIKE '{catalog}.{pilot}.%'") or ""
    converted_waves = [w for w in converted_waves_csv.split(",") if w] or [wave_id]
    print(f"[e2e] rolling back converted wave(s): {converted_waves}")
    for wv in converted_waves:
        _run_stage(f"06 rollback (manual) [{wv}]", ROLLBACK_SUFFIX,
                   {"plan_run_id": plan_run_id, "wave_id": wv, "reason": "e2e-real"})
    n_rb = int(_scalar(rolled_back_sql) or 0)
    check("pilot tables ROLLED_BACK", n_rb > 0, f"{n_rb} tables")

    # RB-M2: rollback's reconcile-finalize pass reverses 08c's PO + auto-liquid deltas (fail-closed,
    # metadata-only) on the rolled-back tables and obsoletes the ENABLE_* finalize actions. Evidence:
    # ROLLBACK audit rows tagged 'finalize-reconcile REVERSE_* … verified=True'. po_enabled is the
    # durable (append-only) proof 08c enabled PO this run; if it did and nothing was reversed, FAIL.
    po_enabled = int(_scalar(_e2e_audit_action_phase_count_sql(catalog, _ACTION_ENABLE_PO, "SUCCESS")) or 0)
    al_enabled = int(_scalar(_e2e_audit_action_phase_count_sql(catalog, _ACTION_ENABLE_AL, "SUCCESS")) or 0)
    po_reversed = int(_scalar(_e2e_rollback_reversal_count_sql(catalog, "REVERSE_PO")) or 0)
    al_reversed = int(_scalar(_e2e_rollback_reversal_count_sql(catalog, "REVERSE_AUTO_LIQUID")) or 0)
    fin_obsolete = int(_scalar(_e2e_obsolete_finalize_actions_sql(catalog)) or 0)
    rr_tag, rr_msg = re_.rollback_finalize_reversal_outcome(
        po_enabled=po_enabled, po_reversed=po_reversed, al_enabled=al_enabled,
        al_reversed=al_reversed, finalize_obsolete=fin_obsolete)
    print(f"  [{rr_tag}] rollback reversed 08c finalize deltas (RB-M2) — {rr_msg}")
    if rr_tag == "FAIL":
        failures.append("rollback finalize reversal (RB-M2)")
    # The layout 08c recorded before acting is what rollback restores: no clustering for the ZORDERed
    # table, its manual keys (AUTO off) for the other.
    _e2e_clustering_layout_check(catalog, _scalar, failures, AL_ZORDER_TABLE,
                                 "rollback returned the ZORDERed table to no clustering",
                                 expect_auto=False, expect_keys=[], states=("ROLLED_BACK",))
    _e2e_clustering_layout_check(catalog, _scalar, failures, AL_MANUAL_KEYS_TABLE,
                                 "rollback restored the manual keys",
                                 expect_auto=False, expect_keys=AL_MANUAL_KEYS, states=("ROLLED_BACK",))

    # RB-M4: a verified restore returns the table to EXTERNAL and stamps its freeze_ledger row
    # ROLLED_BACK (dropping it from the runbook / freeze queue; a later 05b skips it). Assert the
    # ledger closeout fired for the rolled-back pilot. applied_freeze proves the bracket ran.
    applied_freeze = int(_scalar(_e2e_ledger_applied_count_sql(catalog)) or 0)
    ledger_rb = int(_scalar(_e2e_ledger_rolled_back_count_sql(catalog)) or 0)
    lc_tag, lc_msg = re_.rollback_ledger_closeout_outcome(
        applied_freeze=applied_freeze, manifest_rolled_back=n_rb, ledger_rolled_back=ledger_rb)
    print(f"  [{lc_tag}] rollback stamped freeze_ledger ROLLED_BACK (RB-M4) — {lc_msg}")
    if lc_tag == "FAIL":
        failures.append("rollback ledger closeout (RB-M4)")

    _e2e_finish(failures, through)


# --------------------------------------------------------------------------- #
# Teardown / reset (Task 14) — remove the builder-created e2e_real_* estate.
# --------------------------------------------------------------------------- #
# Removes ONLY what THIS builder created: the nine e2e_real_* schemas (external tables —
# DROP drops metadata and leaves the source data files, so it is not a hard-delete of the
# source-of-truth), the D2D share + recipient, the uploaded path-access notebook, and the row
# slice from
# the <catalog>.migration control tables. It deliberately does NOT touch the deployed bundle
# assets the builder REUSES (pipeline_e2e_dlt / job_e2e_seed_stream) nor the e2e_stream /
# e2e_dlt schemas those assets own — only their estate control-row slice is cleared. Fully
# idempotent: each removal is best-effort (a missing object/table/column is a note, never a
# hard stop). ``--dry-run`` prints every DROP/DELETE + the notebook-delete intent with NO SDK
# import and NO network (the runner + WorkspaceClient are only touched after the dry-run return).


def _estate_fqname_predicate(col: str, catalog: str) -> str:
    """WHERE clause matching a fully-qualified-name column against the estate's schemas.

    Task 7 widened the builder scope to ``e2e_*`` (the e2e_real_* estate PLUS the reused
    ``e2e_stream`` / ``e2e_dlt`` schemas), so the slice covers all three prefixes.
    """
    return (
        f"{col} LIKE '{catalog}.e2e_real_%' "
        f"OR {col} LIKE '{catalog}.e2e_stream.%' "
        f"OR {col} LIKE '{catalog}.e2e_dlt.%'"
    )


def _estate_schema_predicate(catalog: str) -> str:
    """WHERE clause for SCHEMA-scoped control rows (NULL full_name) via catalog + schema."""
    return (
        f"(catalog = '{catalog}' AND "
        f"(schema LIKE 'e2e_real_%' OR schema = 'e2e_stream' OR schema = 'e2e_dlt'))"
    )


def _control_row_deletes(catalog: str) -> list[tuple[str, str]]:
    """(table, DELETE-sql) per control table, scoped to the estate slice only (pure).

    Deletes ONLY matching rows — the ``migration`` control schema itself is never dropped.
    Scoping columns per ``backend/docs/contracts/control-tables.md``:
      * most tables key on ``full_name``;
      * ``migration_actions`` / ``freeze_ledger`` carry NULL ``full_name`` for schema-scoped
        rows, so they also match on ``catalog`` + ``schema``;
      * ``wave_gate_evaluation`` has no ``full_name`` — match ``group_key`` (``catalog.schema``)
        plus the schema-scoped fallback;
      * ``wave_parallel_groups`` (derived-but-persisted, rewritten per ``plan_run_id``) has no
        ``full_name``/``catalog``/``schema`` either — match ``group_key`` (``catalog.schema``);
      * ``location_map`` keys on ``scope_name`` (``catalog.schema`` for SCHEMA scope; a bare
        catalog-scope row is catalog-wide and intentionally NOT matched);
      * ``migration_next_actions`` keys on ``next_group_key`` (``catalog.schema``).
      * ``stage_run_ledger`` is append-only STAGE-run history (one row per convert/campaign run)
        with no ``full_name``/``catalog``/``schema`` — it keys on ``selector`` (``catalog.schema``),
        matched like ``wave_parallel_groups``;
      * ``profiler_run_ledger`` is append-only RUN provenance with no per-table ``full_name`` — it
        keys on the run's ``catalog_filter`` (the estate catalog in this e2e workspace). Clearing
        it is what lets a same-catalog ``--reset`` return the app to the true first-run
        NEVER_PROFILED card instead of a sticky ZERO_DISCOVERY (the ``coverage_report`` view is a
        projection of this ledger). Mirrors ``real_estate.CONTROL_BASE_TABLES`` (full wipe).
    ``migration_control_config`` is the single-row control handshake (not estate-scoped) and is
    intentionally left untouched, as is ``coverage_matrix`` (one catalog-wide row per catalog x
    workspace, like a catalog-scope ``location_map`` row), which only the full wipe empties.
    """
    def _fq(col: str) -> str:
        return _estate_fqname_predicate(col, catalog)

    schema_pred = _estate_schema_predicate(catalog)

    def _d(table: str, pred: str) -> tuple[str, str]:
        return (table, f"DELETE FROM {catalog}.{CONTROL_SCHEMA}.{table} WHERE {pred}")

    fqname_only = (
        "table_manifest", "wave_plan", "comms_feed", "audit_log",
        "compaction_sources", "streaming_plan", "consumer_runtime_plan",
        "path_access_locations", "validation_results", "dry_run_results",
        "ownership_ledger", "writer_sources",
    )
    deletes = [_d(t, _fq("full_name")) for t in fqname_only]
    deletes.append(_d("migration_actions", f"({_fq('full_name')}) OR {schema_pred}"))
    deletes.append(_d("freeze_ledger", f"({_fq('full_name')}) OR {schema_pred}"))
    deletes.append(_d("wave_gate_evaluation", f"({_fq('group_key')}) OR {schema_pred}"))
    # wave_parallel_groups is derived-but-persisted (rewritten per plan_run_id); it has no
    # catalog/schema columns, only group_key (catalog.schema). Scope by group_key so a
    # same-catalog reset can't strand a ghost batch snapshot on the Bulk Convert screen.
    deletes.append(_d(
        "wave_parallel_groups",
        f"(group_key LIKE '{catalog}.e2e_real_%' "
        f"OR group_key = '{catalog}.e2e_stream' OR group_key = '{catalog}.e2e_dlt')",
    ))
    deletes.append(_d("location_map", _fq("scope_name")))
    deletes.append(_d("migration_next_actions", _fq("next_group_key")))
    # Run provenance has no full_name; scope by the run's catalog_filter (the estate catalog).
    deletes.append(_d("profiler_run_ledger", f"catalog_filter = '{catalog}'"))
    # stage_run_ledger (append-only stage-run history) has no full_name/catalog/schema — it keys
    # on selector (catalog.schema for convert/campaign). Scope by selector so a same-catalog
    # reset clears the estate's run history without touching other estates.
    deletes.append(_d(
        "stage_run_ledger",
        f"(selector LIKE '{catalog}.e2e_real_%' "
        f"OR selector = '{catalog}.e2e_stream' OR selector = '{catalog}.e2e_dlt')",
    ))
    return deletes


def _best_effort(run, sql: str, label: str) -> None:
    """Run one teardown statement, tolerating any failure (idempotent, never aborts the rest)."""
    try:
        if not run(sql, label):
            print(f"  note {label}: statement did not succeed "
                  f"(already-absent / grants / not-yet-created) — continuing")
    except Exception as e:   # noqa: BLE001 — teardown is best-effort per statement
        print(f"  skip {label}: {type(e).__name__}: {e} — continuing")


def _delete_maintenance_jobs(a: argparse.Namespace) -> None:
    """Best-effort delete of the ephemeral scheduled OPTIMIZE/VACUUM jobs + their notebook.

    Resolves each job by EXACT name and deletes it (tolerating not-found), then removes the
    uploaded maintenance notebook. Idempotent; never raises (live only; lazy SDK import).
    """
    from databricks.sdk import WorkspaceClient

    try:
        w = WorkspaceClient(profile=a.profile)
    except Exception as e:   # noqa: BLE001
        print(f"  skip maintenance-job delete: {type(e).__name__}: {e} — continuing")
        return
    for name in (MAINT_OPTIMIZE_JOB_NAME, MAINT_VACUUM_JOB_NAME):
        try:
            job_id = _find_job_id_by_name(w, name)
            if job_id is None:
                print(f"  note maintenance job {name!r} not found (already deleted) — continuing")
                continue
            w.jobs.delete(job_id=job_id)
            print(f"  ok   deleted maintenance job {name!r} (job_id={job_id})")
        except Exception as e:   # noqa: BLE001 — idempotent
            print(f"  skip delete maintenance job {name!r}: {type(e).__name__}: {e} — continuing")
    try:
        user = w.current_user.me().user_name
        ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_MAINT_WS_NAME}"
        w.workspace.delete(ws_path, recursive=False)
        print(f"  ok   deleted maintenance notebook {ws_path}")
    except Exception as e:   # noqa: BLE001 — tolerate not-found
        print(f"  note maintenance notebook delete skipped: {type(e).__name__}: {e} — continuing")


def _delete_writer_job(a: argparse.Namespace) -> None:
    """Best-effort delete of the ephemeral scheduled batch writer job + its notebook.

    Resolves the job by EXACT name and deletes it (tolerating not-found), then removes the uploaded
    writer notebook. Idempotent; never raises (live only; lazy SDK import).
    """
    from databricks.sdk import WorkspaceClient

    try:
        w = WorkspaceClient(profile=a.profile)
    except Exception as e:   # noqa: BLE001
        print(f"  skip writer-job delete: {type(e).__name__}: {e} — continuing")
        return
    try:
        job_id = _find_job_id_by_name(w, WRITER_JOB_NAME)
        if job_id is None:
            print(f"  note writer job {WRITER_JOB_NAME!r} not found (already deleted) — continuing")
        else:
            w.jobs.delete(job_id=job_id)
            print(f"  ok   deleted writer job {WRITER_JOB_NAME!r} (job_id={job_id})")
    except Exception as e:   # noqa: BLE001 — idempotent
        print(f"  skip delete writer job {WRITER_JOB_NAME!r}: {type(e).__name__}: {e} — continuing")
    try:
        user = w.current_user.me().user_name
        ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_WRITER_WS_NAME}"
        w.workspace.delete(ws_path, recursive=False)
        print(f"  ok   deleted writer notebook {ws_path}")
    except Exception as e:   # noqa: BLE001 — tolerate not-found
        print(f"  note writer notebook delete skipped: {type(e).__name__}: {e} — continuing")


def _delete_schedule_twin_jobs(a: argparse.Namespace) -> None:
    """Best-effort delete of the ephemeral schedule-twin + writer-wave jobs (by exact name).

    The shared writer notebook is removed by ``_delete_writer_job``; this only deletes the two twin
    jobs and the isolated writer-wave job (L4 F-WRITER-WAVE). Idempotent; never raises (live only;
    lazy SDK import).
    """
    from databricks.sdk import WorkspaceClient

    try:
        w = WorkspaceClient(profile=a.profile)
    except Exception as e:   # noqa: BLE001
        print(f"  skip schedule-twin delete: {type(e).__name__}: {e} — continuing")
        return
    for name in (SCHED_FAR_JOB_NAME, SCHED_IMMINENT_JOB_NAME, SCHED_WRITER_WAVE_JOB_NAME):
        try:
            job_id = _find_job_id_by_name(w, name)
            if job_id is None:
                print(f"  note schedule twin {name!r} not found (already deleted) — continuing")
                continue
            w.jobs.delete(job_id=job_id)
            print(f"  ok   deleted schedule twin {name!r} (job_id={job_id})")
        except Exception as e:   # noqa: BLE001 — idempotent
            print(f"  skip delete schedule twin {name!r}: {type(e).__name__}: {e} — continuing")


def _delete_pathaccess_notebook(a: argparse.Namespace) -> None:
    """Best-effort delete of the uploaded path-access notebook (live only; lazy SDK import).

    Tolerates a not-found object so re-runs are idempotent. One-off ``jobs.submit`` RUNS are
    ephemeral and cannot be deleted — they age out on their own (a note, not an attempt).
    """
    from databricks.sdk import WorkspaceClient

    try:
        w = WorkspaceClient(profile=a.profile)
        user = w.current_user.me().user_name
        ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_PATHACCESS_WS_NAME}"
        w.workspace.delete(ws_path, recursive=False)
        print(f"  ok   deleted notebook {ws_path}")
    except Exception as e:   # tolerate not-found / grants — idempotent
        print(f"  note notebook delete skipped ({type(e).__name__}: {e}); "
              f"the object may already be absent — continuing")
    print("  note one-off jobs.submit RUNS are ephemeral (cannot be deleted) and age out on their own")


# The inline SOURCE notebook the file-clear runs. Kept tiny and self-contained (no repo import):
# it re-asserts, per target, BOTH the ``mtm_e2e`` marker AND an ``e2e_real_`` path segment in-cluster
# (defense in depth) BEFORE removing anything, so even a mis-passed target can never remove a non-test
# location OR a preserved sibling asset (e2e_stream/ e2e_dlt/ run3/ vN). Per-target best-effort: one
# failure never aborts the rest, and a missing dir (nothing to clear) is a no-op, not a failure.
_CLEAR_NOTEBOOK_SOURCE = """# Databricks notebook source
# COMMAND ----------
dbutils.widgets.text("targets", "")
targets = [t.strip().rstrip("/") for t in dbutils.widgets.get("targets").split(",") if t.strip()]
removed = 0
for base in targets:
    segments = [p for p in base.split("://")[-1].split("/") if p]
    assert base.count("://") == 1 and len(segments) >= 2 and "mtm_e2e" in segments[1:], \\
        f"refusing to remove non-test target (mtm_e2e marker required as a path segment): {base!r}"
    assert any("e2e_real_" in s for s in segments), \\
        f"refusing to remove non-estate target (e2e_real_ path segment required): {base!r}"
    try:
        dbutils.fs.rm(base, recurse=True)
        removed += 1
        print(f"cleared {base}")
    except Exception as e:
        print(f"skip {base}: {type(e).__name__}: {e}")
print(f"estate file-clear done: removed {removed}/{len(targets)} per-schema target(s)")
"""

_CLEAR_WS_NAME = "clear_estate_files"


def _clear_estate_files(a: argparse.Namespace) -> None:
    """Physically clear the estate's external files under the guarded ``mtm_e2e`` prefix (live only).

    A convert-then-drop cycle leaves an orphaned Delta log at the external path that collides with a
    same-path re-create; clearing the prefix lets ``--reset`` reuse the one stable base instead of
    leaking a fresh run-scoped subpath every time. Guarded three ways: opt-out via ``--keep-files``,
    the pure :func:`is_safe_estate_base` gate here, and an in-cluster re-assert inside the notebook.
    Best-effort — a failure WARNs (never raises) since Option-2 provision retry is the backstop.
    SDK imports are LAZY so ``--dry-run`` stays fully offline.
    """
    if a.keep_files:
        print("  note --keep-files set: leaving external estate files in place (no removal)")
        return
    if not re_.is_safe_estate_base(a.external_base):
        print(f"  WARN external base {a.external_base!r} is not the dedicated mtm_e2e test prefix; "
              f"refusing to remove files — clear it manually or pass a base under .../mtm_e2e")
        return

    targets = re_.estate_file_clear_paths(a.external_base)
    targets_param = ",".join(targets)
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.workspace import ImportFormat, Language
    from databricks.sdk.service.jobs import SubmitTask, NotebookTask, RunLifeCycleState

    w = WorkspaceClient(profile=a.profile)
    user = w.current_user.me().user_name
    ws_path = f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}/{_CLEAR_WS_NAME}"
    try:
        w.workspace.mkdirs(f"/Workspace/Users/{user}/{_PATHACCESS_WS_DIR}")
        w.workspace.upload(ws_path, _CLEAR_NOTEBOOK_SOURCE.encode("utf-8"),
                           format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
        resp = w.jobs.submit(
            run_name="mtm_e2e_real :: clear_estate_files",
            tasks=[SubmitTask(task_key="clear_files",
                              notebook_task=NotebookTask(notebook_path=ws_path,
                                                         base_parameters={"targets": targets_param}))])
        run_id = resp.run_id
        print(f"  ok   submitted file-clear run: run_id={run_id} -> rm {len(targets)} per-schema "
              f"e2e_real_* dir(s) under {re_.estate_file_clear_path(a.external_base)} (recursive)")
        timeout_s = 900
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            run = w.jobs.get_run(run_id=run_id)
            life = run.state.life_cycle_state if run.state else None
            if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED,
                        RunLifeCycleState.INTERNAL_ERROR):
                result = run.state.result_state if run.state else None
                rv = result.value if result else "UNKNOWN"
                if rv == "SUCCESS":
                    print(f"  ok   file-clear finished: {rv} — {len(targets)} e2e_real_* dir(s) "
                          f"cleared (reused e2e_stream/ e2e_dlt/ preserved) for re-provision")
                else:
                    print(f"  WARN file-clear finished {rv}; re-provision may hit stale files under "
                          f"{re_.estate_file_clear_path(a.external_base)} (Option-2 provision retry is "
                          f"the backstop) — check run {run_id}")
                return
            time.sleep(10)
        print(f"  WARN file-clear run {run_id} still going after {timeout_s}s; leaving it — "
              f"re-provision may race stale files (Option-2 provision retry is the backstop)")
    except Exception as e:   # never abort teardown — the retry path can still recover
        print(f"  WARN file-clear skipped ({type(e).__name__}: {e}); re-provision may hit stale "
              f"files under {re_.estate_file_clear_path(a.external_base)} (Option-2 provision retry "
              f"is the backstop)")


def _teardown(a: argparse.Namespace) -> None:
    """Remove the builder-created e2e_real_* estate (idempotent). Prints exactly what is removed.

    Order: estate schemas -> estate files -> D2D share + recipient -> uploaded notebook ->
    control-plane row slice. The estate tables are EXTERNAL, so DROP removes metadata only; a
    guarded :func:`_clear_estate_files` step then removes the external files under the dedicated
    ``mtm_e2e`` test prefix (default-on; ``--keep-files`` opts out) so ``--reset`` can reuse the one
    stable base without leaking run-scoped subpaths. Deployed bundle assets (pipeline_e2e_dlt /
    job_e2e_seed_stream) and the e2e_stream / e2e_dlt schemas they own are left intact — only their
    control-row slice is cleared.
    """
    catalog = a.catalog
    estate_schemas = [s["name"] for s in re_.SCHEMAS]
    schema_drops = [(name, f"DROP SCHEMA IF EXISTS {catalog}.{name} CASCADE")
                    for name in estate_schemas]
    share_drops = [
        f"DROP SHARE IF EXISTS {SHARE_NAME}",
        f"DROP RECIPIENT IF EXISTS {SHARE_RECIPIENT}",
    ]
    control_deletes = _control_row_deletes(catalog)
    nb_rel = f"{_PATHACCESS_WS_DIR}/{_PATHACCESS_WS_NAME}"

    if a.dry_run:
        print(f"[teardown] (dry-run) — remove builder-created estate e2e_real_* in {catalog} "
              f"(idempotent; nothing is executed)")
        print(f"-- estate schemas ({len(schema_drops)}) — tables are EXTERNAL, so DROP removes "
              f"metadata only (the external data files are cleared by the next step):")
        for _name, stmt in schema_drops:
            print(stmt + ";")
        if a.keep_files:
            print("-- estate files: --keep-files set — external files left in place (no removal)")
        elif re_.is_safe_estate_base(a.external_base):
            clear_targets = re_.estate_file_clear_paths(a.external_base)
            print(f"-- estate files: would submit a one-off notebook run to recursively remove "
                  f"{len(clear_targets)} per-schema e2e_real_* dir(s) under "
                  f"{re_.estate_file_clear_path(a.external_base)} (guarded mtm_e2e + e2e_real_ markers; "
                  f"re-asserted in-cluster; reused e2e_stream/ e2e_dlt/ preserved) so --reset reuses "
                  f"the one stable base:")
            for t in clear_targets:
                print(f"     rm -r {t}")
        else:
            print(f"-- estate files: base {a.external_base!r} is not the dedicated mtm_e2e test "
                  f"prefix — would refuse to remove files (WARN, clear manually)")
        print("-- Delta Share (D2D) + recipient:")
        for stmt in share_drops:
            print(stmt + ";")
        print("-- uploaded path-access notebook (best-effort workspace delete; tolerate not-found):")
        print(f"-- would delete workspace object "
              f"/Workspace/Users/<current_user>/{nb_rel} (recursive=False)")
        print("-- ephemeral scheduled maintenance jobs (resolve by exact name, delete; tolerate not-found):")
        print(f"-- would delete jobs {MAINT_OPTIMIZE_JOB_NAME!r} + {MAINT_VACUUM_JOB_NAME!r} "
              f"and the uploaded {_MAINT_WS_NAME} notebook")
        print(f"-- would delete job {WRITER_JOB_NAME!r} and the uploaded {_WRITER_WS_NAME} notebook")
        print(f"-- would delete schedule twin jobs {SCHED_FAR_JOB_NAME!r} + {SCHED_IMMINENT_JOB_NAME!r} "
              f"+ writer-wave job {SCHED_WRITER_WAVE_JOB_NAME!r}")
        print("-- note one-off jobs.submit RUNS are ephemeral (cannot be deleted) and age out on their own")
        print(f"-- control-plane row slice in {catalog}.{CONTROL_SCHEMA} "
              f"({len(control_deletes)} tables; estate patterns only — the control schema is NOT dropped):")
        for _table, sql in control_deletes:
            print(sql + ";")
        print("-- note deployed bundle assets (pipeline_e2e_dlt / job_e2e_seed_stream) and the "
              "e2e_stream / e2e_dlt schemas they own are NOT deleted (reused, not builder-created)")
        print(f"[teardown] dry-run: would drop {len(schema_drops)} estate schemas + share/recipient "
              f"+ notebook + {len(control_deletes)} control-table row slices")
        return

    print(f"[teardown] removing builder-created estate e2e_real_* in {catalog} (idempotent)")
    run = _make_runner(a.profile, a.warehouse_id)
    for name, stmt in schema_drops:
        _best_effort(run, stmt, f"drop schema {catalog}.{name}")
    _clear_estate_files(a)
    for stmt in share_drops:
        _best_effort(run, stmt, stmt)
    _delete_pathaccess_notebook(a)
    _delete_maintenance_jobs(a)
    _delete_writer_job(a)
    _delete_schedule_twin_jobs(a)
    for table, sql in control_deletes:
        _best_effort(run, sql, f"clear estate rows from {table}")
    files_note = "kept external files (--keep-files)" if a.keep_files else "cleared external files"
    print(f"[teardown] complete — dropped {len(schema_drops)} estate schemas, {files_note}, "
          f"removed share {SHARE_NAME} + recipient {SHARE_RECIPIENT}, deleted the notebook, cleared "
          f"{len(control_deletes)} control-table row slices. Deployed pipeline_e2e_dlt / "
          f"job_e2e_seed_stream and the e2e_stream / e2e_dlt schemas were left intact.")


def _wipe_control_plane(a: argparse.Namespace) -> None:
    """Full "reset to fresh install": empty EVERY control BASE table, then exit (idempotent).

    Unlike the estate-scoped teardown, this clears the WHOLE control plane so no stale snapshot
    (e.g. a STEP_DONE ``migration_next_actions`` row with NULL ``next_group_key`` — the "83%
    migrated" class) survives; the app then renders its first-run screen. ``migration_control_config``
    (the single-row handshake) is preserved. Gated by :func:`real_estate.is_safe_control_wipe`
    (requires ``--confirm-wipe`` + a real scope). Does NOT drop schemas, remove files, or touch
    deployed bundle assets — it only empties rows.
    """
    catalog = a.catalog
    if not re_.is_safe_control_wipe(catalog, CONTROL_SCHEMA, a.confirm_wipe):
        print(f"[wipe] refusing: --wipe-control-plane needs --confirm-wipe AND a real "
              f"catalog/schema (got catalog={catalog!r}, schema={CONTROL_SCHEMA!r}, "
              f"confirm={a.confirm_wipe}). Nothing changed.")
        raise SystemExit(2)

    wipes = re_.control_plane_wipe_statements(catalog, CONTROL_SCHEMA)
    if a.dry_run:
        print(f"[wipe] dry-run: would empty {len(wipes)} control base tables in "
              f"{catalog}.{CONTROL_SCHEMA} ({re_.CONTROL_CONFIG_TABLE} preserved):")
        for _table, sql in wipes:
            print(sql + ";")
        return

    print(f"[wipe] emptying {len(wipes)} control base tables in {catalog}.{CONTROL_SCHEMA} "
          f"({re_.CONTROL_CONFIG_TABLE} preserved) — the app will render its first-run screen")
    run = _make_runner(a.profile, a.warehouse_id)
    for table, sql in wipes:
        _best_effort(run, sql, f"wipe {table}")
    print(f"[wipe] complete — {len(wipes)} control base tables emptied; "
          f"{re_.CONTROL_CONFIG_TABLE} untouched. Estate schemas/files are NOT dropped by --wipe-"
          f"control-plane (use --teardown / --reset --no-seed for those).")


def _fixture(a: argparse.Namespace) -> None:
    """Dispatch a named L4 app/UI fixture (shapes an already-populated estate; never reseeds)."""
    if a.fixture == FIXTURE_READY_BATCH:
        _fixture_ready_batch(a)
    elif a.fixture == FIXTURE_WRITER_WAVE:
        _fixture_writer_wave(a)
    else:  # unreachable — argparse choices guard it
        raise SystemExit(f"unknown fixture {a.fixture!r}")


def _fixture_ready_batch(a: argparse.Namespace) -> None:
    """Codify the A2 hand-shaping into ONE idempotent command: make a parallel batch READY so the
    /bulk "Recommended next" badge renders (e2e_app_ui.py --a2) and the Convert-Gate is GO-able.

    Recipe (the exact 4-run + manual-resolve we hand-did for the first A2 green, now reproducible):
      0. Pick the target parallel batch (parallel_degree>1) — the one closest to READY, or the
         operator-pinned ``--fixture-batch``.
      1. ``03 dry run`` each member schema (ELIGIBLE -> DRY_RUN_OK). Idempotent (re-check).
      2. ``03c freeze`` (confirm_execute) each member schema — clears the JOB/PO-attributed
         PRE_CONVERT freeze/stop actions the bracket CAN auto-operate. Idempotent (re-freeze = no-op).
      3. Operator-resolve whatever PRE_CONVERT *blocking* action still remains on the batch's waves
         — the manual (USER/NOTEBOOK) ``FREEZE_COMPACTION`` the bracket never auto-operates. This is
         the one control-table write a fixture makes; scoped to the batch's pinned wave actions only.
      4. Verify every member wave is READY (``wave_status.open_blocking`` = pinned OPEN/IN_PROGRESS
         blocking actions -> 0). Fails loud if the batch can't reach READY (e.g. lifecycle-BLOCKED
         members) — try another ``--fixture-batch``.

    Read-mostly + idempotent: re-running on an already-READY batch re-runs the dry-run/freeze as
    no-ops and resolves nothing (the WHERE filters exclude terminal-status actions). Live only.
    """
    catalog = a.catalog
    m = f"{catalog}.{CONTROL_SCHEMA}"

    if a.dry_run:
        print(f"[fixture {FIXTURE_READY_BATCH}] dry-run: would (0) pick a parallel batch, (1) run "
              f"03 dry run per member schema, (2) run 03c freeze per member schema, (3) operator-"
              f"resolve any remaining manual PRE_CONVERT blocking action on the batch's waves, and "
              f"(4) verify the batch is READY. Idempotent; fully offline (no SDK import, no network).")
        return

    run = _make_runner(a.profile, a.warehouse_id)
    query = _make_query(a.profile, a.warehouse_id)

    # 0. Resolve the target parallel batch + its member waves (state, blockers, schema).
    pin = f"AND g.batch_id = '{a.fixture_batch}' " if a.fixture_batch else ""
    rows = query(
        "SELECT g.batch_id, g.batch_order, g.parallel_degree, g.plan_run_id, g.wave_id, "
        "       g.group_key, s.wave_state, s.open_blocking, s.open_pre_convert "
        f"FROM {m}.wave_parallel_groups g "
        f"JOIN {m}.wave_status s "
        "  ON g.plan_run_id = s.plan_run_id AND g.wave_id = s.wave_id "
        f"WHERE g.parallel_degree > 1 {pin}"
        "ORDER BY g.batch_order, g.wave_id")
    if not rows:
        raise SystemExit(
            f"[fixture {FIXTURE_READY_BATCH}] no parallel batch found"
            + (f" for --fixture-batch {a.fixture_batch!r}" if a.fixture_batch else
               " (parallel_degree>1)")
            + " — run `--preset populate` first, or the plan has only serial (single-wave) batches.")

    by_batch: dict[str, list] = {}
    for r in rows:
        by_batch.setdefault(str(r[0]), []).append(r)

    def _score(waves: list) -> tuple:
        non_ready = sum(1 for w in waves if str(w[6]) != "READY")
        blk = sum(int(w[7] or 0) for w in waves)
        return (non_ready, blk, int(waves[0][1] or 0))  # closest-to-READY, then batch_order

    target = a.fixture_batch or min(by_batch, key=lambda b: _score(by_batch[b]))
    waves = by_batch[target]
    plan_run_id = str(waves[0][3])
    schemas: list[str] = []  # ordered-unique member schemas (group_key = catalog.schema)
    for w in waves:
        gk = str(w[5])
        if gk not in schemas:
            schemas.append(gk)

    print(f"[fixture {FIXTURE_READY_BATCH}] target batch {target} (plan_run_id={plan_run_id}) — "
          f"{len(waves)} wave(s) across {len(schemas)} schema(s): {', '.join(schemas)}")
    for w in waves:
        print(f"  - {w[4]}  state={w[6]}  open_blocking={w[7]}  open_pre_convert={w[8]}")

    # 1. 03 dry run each member schema (ELIGIBLE -> DRY_RUN_OK).
    for gk in schemas:
        schema = gk.split(".", 1)[1] if "." in gk else gk
        print(f"\n[fixture] 03 dry run — schema {schema}")
        state = _run_deployed_job(a, DRY_RUN_SUFFIX,
                                  params={"catalog_filter": catalog, "schema_filter": schema})
        print(f"  {'ok  ' if state == 'SUCCESS' else 'WARN'} 03 dry run {schema} -> {state}")

    # 2. 03c freeze each member schema (clears freezable JOB/PO PRE_CONVERT actions).
    for gk in schemas:
        print(f"\n[fixture] 03c freeze — schema {gk}")
        state = _run_deployed_job(a, FREEZE_SUFFIX,
                                  params={"target_schema": gk, "confirm_execute": "true"})
        ok = state in ("SUCCESS", "SUCCESS_WITH_FAILURES")
        print(f"  {'ok  ' if ok else 'WARN'} 03c freeze {gk} -> {state}")

    # 3. Operator-resolve any remaining manual PRE_CONVERT blocking action on the batch's waves.
    #    Freeze cleared the JOB/PO ones; what remains blocking is manual (USER/NOTEBOOK
    #    FREEZE_COMPACTION) the bracket never auto-operates. This is the sole control-table write —
    #    scoped to the batch's PINNED wave actions (the same rows wave_status.open_blocking counts).
    wave_list = ",".join(f"'{w[4]}'" for w in waves)
    remaining = query(
        "SELECT action_id, action_kind, COALESCE(full_name, CONCAT(catalog, '.', schema)), "
        "       target_kind, COALESCE(owner, '') "
        f"FROM {m}.migration_actions "
        f"WHERE plan_run_id = '{plan_run_id}' AND wave_id IN ({wave_list}) "
        "  AND phase = 'PRE_CONVERT' AND blocking = true AND status IN ('OPEN', 'IN_PROGRESS')")
    if remaining:
        print(f"\n[fixture] operator-resolving {len(remaining)} remaining manual PRE_CONVERT "
              "blocking action(s) the freeze bracket cannot auto-operate:")
        for a_id, kind, fn, tk, owner in remaining:
            print(f"  - {kind} on {fn} (target_kind={tk}, owner={owner or '—'})  [{a_id}]")
        ids = ",".join(f"'{r[0]}'" for r in remaining)
        note = (f"L4 {FIXTURE_READY_BATCH} fixture: operator-resolved a manual PRE_CONVERT action to "
                "shape a READY parallel batch for the app/UI e2e (e2e_app_ui.py --a2). Reversible: "
                "re-run the profiler to regenerate the action if the underlying signal persists.")
        run(
            f"UPDATE {m}.migration_actions "
            f"SET status = 'DONE', resolution = '{note}', completed_at = current_timestamp(), "
            "    completed_by = 'e2e-fixture', updated_at = current_timestamp() "
            f"WHERE action_id IN ({ids})",
            "operator-resolve manual PRE_CONVERT actions")
    else:
        print("\n[fixture] no remaining manual PRE_CONVERT blocking actions to resolve.")

    # 4. Verify every member wave is READY (open_blocking -> 0).
    print(f"\n[fixture] verifying batch {target} is READY:")
    verify = query(
        "SELECT s.wave_id, s.wave_state, s.open_blocking "
        f"FROM {m}.wave_parallel_groups g "
        f"JOIN {m}.wave_status s "
        "  ON g.plan_run_id = s.plan_run_id AND g.wave_id = s.wave_id "
        f"WHERE g.batch_id = '{target}' AND g.plan_run_id = '{plan_run_id}' "
        "ORDER BY s.wave_id")
    not_ready = [r for r in verify if str(r[1]) != "READY"]
    for r in verify:
        print(f"  {r[0]}  state={r[1]}  open_blocking={r[2]}")
    if not_ready:
        raise SystemExit(
            f"[fixture {FIXTURE_READY_BATCH}] batch {target} NOT fully READY "
            f"({len(not_ready)}/{len(verify)} wave(s) not READY) — it may contain lifecycle-BLOCKED "
            "members that a dry-run cannot advance. Pick another with --fixture-batch <id>.")
    print(f"\n[fixture {FIXTURE_READY_BATCH}] batch {target} is READY "
          f"({len(verify)}/{len(verify)} waves) — /bulk renders its 'Recommended next' badge (A2). "
          "Verify: python .e2e/e2e_app_ui.py --a2")


def _fixture_writer_wave(a: argparse.Namespace) -> None:
    """Make the isolated ``e2e_real_writer`` wave RUNNABLE so the /waves Convert-Gate opens on it and
    its pause banner names the imminent writer job with a WARN_WINDOW verdict (e2e_app_ui.py --a1).

    Recipe (idempotent; the L4 twin of F-READY-BATCH, one wave instead of a parallel batch):
      0. Resolve the writer wave — the wave whose members are the tiny ``e2e_real_writer`` schema.
      1. Corroborate the writer is attributed (``writer_sources`` SCHEDULED_JOB on the writer table).
         WARN (not fail) on lineage lag — attribution can trail the seed run; the banner is empty
         until ``writer_sources`` catches up.
      2. ``03 dry run`` the writer schema (ELIGIBLE -> DRY_RUN_OK => the wave is RUNNABLE, so the
         inline gate mounts).
      3. ``03c freeze`` the writer schema (confirm_execute) — the freeze bracket auto-pauses the
         SCHEDULED_JOB writer (``PAUSE_WRITER_JOB``), clearing the blocking ``STOP_WRITER_JOB`` action
         so the wave reaches READY. No manual resolve needed (unlike F-READY-BATCH's USER OPTIMIZE).
      4. Verify the wave is RUNNABLE (``dry_run_ok`` > 0) and its rolled-up ``wave_writer_verdict`` is
         ``WARN_WINDOW`` (WARN on schedule/lineage lag).

    Requires an estate seeded WITH the ``e2e_real_writer`` schema (a re-seed after this milestone).
    On an older estate lacking that schema this fails loud with a clear message. Live only.
    """
    catalog = a.catalog
    m = f"{catalog}.{CONTROL_SCHEMA}"
    schema_fq = f"{catalog}.{WRITER_WAVE_SCHEMA}"

    if a.dry_run:
        print(f"[fixture {FIXTURE_WRITER_WAVE}] dry-run: would (0) resolve the {schema_fq} wave, "
              f"(1) corroborate the attributed SCHEDULED_JOB writer, (2) run 03 dry run "
              f"--schema {WRITER_WAVE_SCHEMA} (=> RUNNABLE), (3) run 03c freeze "
              f"--target_schema {schema_fq} (auto-pauses the writer, => READY), and (4) verify the "
              f"wave is runnable + WARN_WINDOW. Idempotent; fully offline (no SDK, no network).")
        return

    query = _make_query(a.profile, a.warehouse_id)

    def _writer_waves() -> list:
        return query(
            "SELECT DISTINCT s.plan_run_id, s.wave_id, s.wave_state, s.dry_run_ok, s.open_blocking, "
            "       s.wave_writer_verdict "
            f"FROM {m}.wave_status s "
            f"JOIN {m}.wave_plan p ON s.plan_run_id = p.plan_run_id AND s.wave_id = p.wave_id "
            f"WHERE p.`schema` = '{WRITER_WAVE_SCHEMA}' "
            "ORDER BY s.wave_id")

    # 0. Resolve the writer wave.
    waves = _writer_waves()
    if not waves:
        raise SystemExit(
            f"[fixture {FIXTURE_WRITER_WAVE}] no wave contains schema {schema_fq} — this estate was "
            f"seeded before the e2e_real_writer schema existed. Re-seed "
            f"(`--reset --preset populate`) to provision it, then re-run this fixture.")
    plan_run_id = str(waves[0][0])
    print(f"[fixture {FIXTURE_WRITER_WAVE}] writer wave(s) for {schema_fq} "
          f"(plan_run_id={plan_run_id}):")
    for w in waves:
        print(f"  - {w[1]}  state={w[2]}  dry_run_ok={w[3]}  open_blocking={w[4]}  "
              f"verdict={w[5]}")

    # 1. Corroborate writer attribution (WARN on lineage lag).
    attributed = query(
        "SELECT full_name, entity_id, writer_class "
        f"FROM {m}.writer_sources "
        f"WHERE full_name LIKE '{schema_fq}.%' AND writer_class IN ('SCHEDULED_JOB', 'EVENT_JOB') "
        "ORDER BY writer_class, entity_id")
    if attributed:
        for fn, jid, wc in attributed:
            print(f"  ok   attributed writer {wc} job {jid} on {fn}")
    else:
        print(f"  WARN no SCHEDULED_JOB/EVENT_JOB attributed on {schema_fq} yet — lineage lag. The "
              f"pause banner stays empty until writer_sources catches up; re-run the profiler + this "
              f"fixture once the write edge lands.")

    # 2. 03 dry run the writer schema (=> RUNNABLE).
    print(f"\n[fixture] 03 dry run — schema {WRITER_WAVE_SCHEMA}")
    state = _run_deployed_job(a, DRY_RUN_SUFFIX,
                              params={"catalog_filter": catalog, "schema_filter": WRITER_WAVE_SCHEMA})
    print(f"  {'ok  ' if state == 'SUCCESS' else 'WARN'} 03 dry run {WRITER_WAVE_SCHEMA} -> {state}")

    # 3. 03c freeze the writer schema (auto-pauses the SCHEDULED_JOB writer => READY).
    print(f"\n[fixture] 03c freeze — schema {schema_fq}")
    state = _run_deployed_job(a, FREEZE_SUFFIX,
                              params={"target_schema": schema_fq, "confirm_execute": "true"})
    ok = state in ("SUCCESS", "SUCCESS_WITH_FAILURES")
    print(f"  {'ok  ' if ok else 'WARN'} 03c freeze {schema_fq} -> {state}")

    # 4. Verify RUNNABLE + WARN_WINDOW.
    print(f"\n[fixture] verifying the {schema_fq} wave is runnable + WARN_WINDOW:")
    verify = _writer_waves()
    for w in verify:
        print(f"  {w[1]}  state={w[2]}  dry_run_ok={w[3]}  open_blocking={w[4]}  verdict={w[5]}")
    runnable = any(int(r[3] or 0) > 0 for r in verify)
    if not runnable:
        raise SystemExit(
            f"[fixture {FIXTURE_WRITER_WAVE}] the {schema_fq} wave is NOT runnable after the dry-run "
            "(no DRY_RUN_OK members) — the tables may be BLOCKED/NEEDS_REVIEW. Inspect wave_members.")
    verdicts = {str(r[5]) for r in verify}
    if "WARN_WINDOW" in verdicts:
        print(f"  ok   wave verdict is WARN_WINDOW — /waves gate shows the pause banner + the "
              f"'Scheduled write approaching' advisory badge (A1).")
    else:
        print(f"  WARN wave verdict is {sorted(verdicts)} (expected WARN_WINDOW) — the writer "
              f"schedule verdict isn't stamped yet (schedule/lineage lag). Re-profile + re-run once "
              f"the imminent write edge lands.")
    print(f"\n[fixture {FIXTURE_WRITER_WAVE}] the {schema_fq} wave is runnable — the /waves "
          f"Convert-Gate opens on it and its pause banner names the writer job. "
          "Verify: python .e2e/e2e_app_ui.py --a1")


def _run_stages(a: argparse.Namespace) -> None:
    """Resolve stages for the requested preset/stages and run the shared dispatch loop."""
    stages = re_.resolve_stages(stages=a.stage, preset=a.preset, no_lineage=a.no_lineage)
    plan = re_.build_estate_plan(table_count=a.table_count, rng_seed=a.rng_seed)
    print(f"[plan] {len(plan)} tables across {len({t['catalog_relative_schema'] for t in plan})} schemas")
    print(f"[stages] {' -> '.join(stages)}  (no_lineage={a.no_lineage}, dry_run={a.dry_run})")
    for s in stages:
        if s == "provision":
            _provision(a, plan)
        elif s == "activity":
            _activity(a, plan)
        elif s == "lineage_wait":
            _lineage_wait(a)
        elif s == "profile":
            _profile(a, plan)
        elif s == "scan":
            _scan(a)
        elif s == "plan":
            _plan(a)
        elif s == "e2e":
            _e2e(a)


def main() -> None:
    a = _parse()
    if a.fixture:
        _fixture(a)   # shape a named L4 UI fixture on an already-populated estate; never reseeds
        return
    if a.wipe_control_plane:
        _wipe_control_plane(a)   # full fresh-install reset; exits after emptying the control plane
        return
    if a.teardown and not a.reset:
        _teardown(a)
        return
    if a.reset:
        _teardown(a)   # remove the old estate, then fall through to seed a brand-new one
        if a.no_seed:
            print("[reset] --no-seed set: teardown done — leaving a fresh, estate-cleared control "
                  "plane (no re-seed). For a guaranteed-empty first-run screen use --wipe-control-plane.")
            return
        if not a.dry_run:
            settle = re_.RESET_SETTLE_SECONDS
            print(f"[reset] settling {settle}s after teardown so async schema/data deletes complete "
                  f"before re-provisioning at the same external paths (provision also retries the "
                  f"transient race)")
            time.sleep(settle)
    _run_stages(a)


if __name__ == "__main__":
    main()
