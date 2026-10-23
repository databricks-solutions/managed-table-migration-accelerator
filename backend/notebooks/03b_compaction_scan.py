# Databricks notebook source
# MAGIC %md
# MAGIC # 03b — Targeted compaction rescan + action-backlog refresh (READ-ONLY)
# MAGIC
# MAGIC The dedicated operator workflow to run against ONE `catalog.schema` immediately
# MAGIC before converting it. It re-scans `DESCRIBE HISTORY` for that scope only (with its
# MAGIC own deeper `history_limit`, ignoring the profiler's `history_scanned_at` marker),
# MAGIC attributes OPTIMIZE/compaction to job/pipeline/notebook/user/Predictive-Optimization
# MAGIC actors, rewrites `<manifest>.compaction_sources` + the manifest compaction columns,
# MAGIC then regenerates `<manifest>.migration_actions` for the scope.
# MAGIC
# MAGIC Read-only: it NEVER pauses/optimizes/converts anything. Safe to re-run; a scope with
# MAGIC nothing to do is an idempotent no-op. A failed `DESCRIBE HISTORY` leaves that table's
# MAGIC prior values untouched (recorded as `history_unavailable`, not "no compaction").

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("target_schema", "", "Target catalog.schema to rescan (preferred)")
dbutils.widgets.text("catalog_filter", "*", "Catalog filter (glob, * = all) — used when target_schema empty")
dbutils.widgets.text("schema_filter", "*", "Schema filter (glob, * = all) — used when target_schema empty")
dbutils.widgets.text("history_limit", "200", "DESCRIBE HISTORY LIMIT per table")
dbutils.widgets.text("recent_optimize_minutes", "120", "Window (min) to flag recent OPTIMIZE/compaction")
dbutils.widgets.text("parallelism", "8", "Parallel DESCRIBE HISTORY workers")
dbutils.widgets.dropdown("refresh_actions", "true", ["true", "false"], "Regenerate migration_actions for the scope")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
CATALOG_FILTER = dbutils.widgets.get("catalog_filter").strip()
SCHEMA_FILTER = dbutils.widgets.get("schema_filter").strip()
HISTORY_LIMIT = max(1, int(dbutils.widgets.get("history_limit") or 200))
RECENT_OPTIMIZE_MINUTES = int(dbutils.widgets.get("recent_optimize_minutes") or 120)
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism") or 8))
REFRESH_ACTIONS = dbutils.widgets.get("refresh_actions") == "true"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap `src/` on the path

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_dir = os.path.dirname(ctx.notebookPath().get())
        candidates += ["/Workspace" + os.path.dirname(nb_dir), "/Workspace" + nb_dir]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError("Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c))


_bootstrap_src()

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from migration import actions as ax
from migration import compaction as cp
from migration import executor as ex
from migration import manifest as mf
from migration import scoping as sc
from migration import streaming as st
from migration import writers as wr

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
compaction_sources_fqn = cp.ensure_compaction_sources_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _q(value: str) -> str:
    return value.replace("'", "''")


# Resolve the scope predicate. target_schema (catalog.schema) is the precise, preferred
# selector; catalog_filter/schema_filter globs are the fallback.
if TARGET_SCHEMA:
    parts = TARGET_SCHEMA.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"target_schema must be 'catalog.schema'; got {TARGET_SCHEMA!r}")
    _cat, _sch = parts
    SCOPE_PRED = f"catalog = '{_q(_cat)}' AND schema = '{_q(_sch)}'"
else:
    def _glob(col: str, pat: str) -> str:
        if pat in ("", "*"):
            return "TRUE"
        return f"{col} LIKE '{_q(pat.replace('*', '%'))}'"

    SCOPE_PRED = f"{_glob('catalog', CATALOG_FILTER)} AND {_glob('schema', SCHEMA_FILTER)}"

print(f"Scope predicate: {SCOPE_PRED}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rescan history + attribute compaction for the scope

# COMMAND ----------

in_scope = [
    r.asDict()
    for r in spark.table(manifest_fqn).where(SCOPE_PRED)
    .select("full_name", "catalog", "schema", "table_name").collect()
]
print(f"Tables in scope: {len(in_scope)}")


def _scan_table(d: dict) -> dict:
    """One DESCRIBE HISTORY read -> compaction summary (thread-safe, read-only)."""
    full_name_bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    try:
        df = spark.sql(f"DESCRIBE HISTORY {full_name_bt} LIMIT {HISTORY_LIMIT}")
        cols = cp.select_history_columns(df.columns)
        rows = df.select(*cols).collect()
    except Exception:  # noqa: BLE001
        return {**d, "summary": cp.degraded_summary(cp.NOTE_HISTORY_UNAVAILABLE)}
    parsed = [cp.parse_history_row(r) for r in rows]
    summary = cp.summarize_compaction(
        parsed, now=_now(), recent_window_minutes=RECENT_OPTIMIZE_MINUTES,
        attribution_available=cp.has_attribution(cols),
    )
    return {**d, "summary": summary}


results = []
if in_scope:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_scan_table, d) for d in in_scope]
        for f in as_completed(futures):
            results.append(f.result())

# Only rewrite tables that produced a REAL summary (a failed read yields a degraded
# summary with compaction_count=None — leave those tables' prior values untouched).
real = [r for r in results if r["summary"].get("compaction_count") is not None]
degraded = [r for r in results if r["summary"].get("compaction_count") is None]

ts = _now()
if real:
    comp_names = [r["full_name"] for r in real]
    del_sql = cp.delete_compaction_rows_sql(compaction_sources_fqn, comp_names)
    if del_sql:
        spark.sql(del_sql)
    detail_rows = []
    manifest_updates = []
    for r in real:
        detail_rows.extend(
            cp.compaction_source_rows(r["full_name"], r["catalog"], r["schema"], r["summary"], ts)
        )
        upd = cp.build_manifest_compaction_update(r["full_name"], r["summary"], ts)
        upd["updated_at"] = ts
        manifest_updates.append(upd)
    if detail_rows:
        spark.createDataFrame(
            detail_rows, schema=cp.compaction_sources_struct_type()
        ).write.mode("append").saveAsTable(compaction_sources_fqn)
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, manifest_updates,
        [*cp.MANIFEST_COMPACTION_COLUMNS, "updated_at"],
    )

with_actors = sum(1 for r in real if r["summary"].get("actor_count"))
print(f"Compaction rescan: {len(real)} table(s) rewritten "
      f"({with_actors} with attributed compaction actors); "
      f"{len(degraded)} left untouched (history_unavailable).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Regenerate the action backlog for the scope

# COMMAND ----------

if not REFRESH_ACTIONS:
    print("refresh_actions=false -> skipping migration_actions refresh.")
elif not in_scope:
    print("No tables in scope -> nothing to regenerate.")
else:
    manifest_rows = [r.asDict() for r in spark.table(manifest_fqn).where(SCOPE_PRED).collect()]

    # Scope the detail tables by full_name membership, NOT the manifest SCOPE_PRED: detail
    # tables like ``streaming_plan`` carry only ``full_name`` (no catalog/schema columns), so a
    # catalog/schema predicate throws there and would silently drop every streaming action.
    # ``generate_actions`` only emits for in-scope manifest rows, so keying the detail reads off
    # those same full_names is both correct and precise (see migration.scoping).
    scope_names = [d["full_name"] for d in in_scope if d.get("full_name")]

    streaming_by_table = sc.read_detail_by_full_name(
        spark, streaming_plan_fqn,
        ["type", "role", "job_or_pipeline_id", "owner", "confidence"], scope_names,
    )
    compaction_by_table = sc.read_detail_by_full_name(
        spark, compaction_sources_fqn,
        ["actor_kind", "actor_id", "actor_name", "cluster_id", "confidence"], scope_names,
    )
    # writer_sources -> STOP_WRITER_JOB / RESTART_WRITER_JOB (feature F). MUST be regenerated here
    # too: 03b's obsolete-detection below marks every OPEN in-scope action NOT in this run's
    # generated set as OBSOLETE, so omitting writer_by_table would DELETE the writer-stop bracket
    # 00_profiler created. Degrades to {} when writer_sources is absent (older control plane).
    writer_by_table = sc.read_detail_by_full_name(
        spark, wr.writer_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA),
        ["entity_type", "entity_id", "writer_class", "confidence"], scope_names,
    )
    # Re-apply the compaction-actor exclusion (parity with 00_profiler): 03b re-scans compaction
    # fresh above, so drop any writer row whose JOB id is a compaction actor for the same table
    # before regenerating actions — otherwise a maintenance OPTIMIZE/VACUUM job re-enters the
    # writer-stop bracket (double-bracket -> 05b "retired but UNPAUSED" conflict).
    writer_by_table = {
        _fn: wr.exclude_compaction_actor_writers(
            _rows, wr.compaction_job_actor_ids(compaction_by_table.get(_fn, [])))
        for _fn, _rows in writer_by_table.items()
    }

    wave_by_table: dict = {}
    try:
        wp = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.wave_plan"
        for r in spark.table(wp).where(SCOPE_PRED).select(
            "full_name", "plan_run_id", "wave_id"
        ).collect():
            wave_by_table[r["full_name"]] = {"plan_run_id": r["plan_run_id"], "wave_id": r["wave_id"]}
    except Exception:  # noqa: BLE001
        pass

    now = _now()
    generated = ax.generate_actions(
        manifest_rows,
        streaming_by_table=streaming_by_table,
        compaction_by_table=compaction_by_table,
        writer_by_table=writer_by_table,
        wave_by_table=wave_by_table,
        source="03b_compaction_scan",
        now=now,
    )
    generated_keys = [r["action_key"] for r in generated]
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, generated)

    open_in_scope = ax.read_open_action_keys(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=SCOPE_PRED
    )
    stale = ax.obsolete_keys(open_in_scope, generated_keys)
    ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, stale, updated_at=now)

    # Ledger-aware self-heal (parity with 00_profiler): retire orphaned freeze/stop brackets on
    # tables that have already migrated. The state-aware generator drops the PRE half; this
    # closes the never-run POST partner too. A genuinely-frozen bracket is preserved (05b owns
    # its restore). Keeps a converted wave from re-entering AWAITING_ACTIONS after a rescan.
    _state_by_fn = {r.get("full_name"): r.get("state") for r in manifest_rows}
    _frozen_tids = ex.frozen_target_ids(
        ex.read_frozen_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=SCOPE_PRED))
    _open_brackets = ax.read_open_bracket_actions(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=SCOPE_PRED)
    bracket_stale = ax.bracket_keys_to_obsolete(_state_by_fn, _open_brackets, _frozen_tids)
    if bracket_stale:
        ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, bracket_stale, updated_at=now)

    print(f"migration_actions: generated/refreshed {len(generated)} action(s); "
          f"marked {len(stale)} obsolete (+{len(bracket_stale)} orphaned bracket(s)) for the scope.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Open actions for the scope (quick look)

# COMMAND ----------

try:
    display(
        spark.table(ax.action_queue_open_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
        .where(SCOPE_PRED)
    )
except Exception as err:  # noqa: BLE001
    print(f"action_queue_open view unavailable ({str(err)[:120]})")
