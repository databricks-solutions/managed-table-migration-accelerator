"""Control-plane bootstrap: make the estate resilient for deploy + operator UX.

Two concerns live here:

1. **Deploy resilience.** ``ensure_control_plane`` idempotently creates every control
   table + view the dashboard and Genie space read from, EMPTY, so those read-only
   resources never fail on "table/view does not exist". It reuses the existing
   per-module creators (no duplicated DDL) and is safe to re-run (``CREATE ... IF NOT
   EXISTS`` / ``CREATE OR REPLACE VIEW`` on empty tables never clobbers real data).

2. **Config handshake + smart resolution.** The profiler writes the chosen
   ``catalog.schema`` (and warehouse/dashboard ids) to a tiny ``migration_control_config``
   Delta table. The cockpit reads it when its widgets are left at defaults, then falls
   back to an information-schema search, then fails fast with guidance. The *decision*
   rules are pure (``pick_candidate`` / ``decide_control_schema``) so they unit-test with
   no Spark; the Spark probes/writes are thin edges at the bottom.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# Widget defaults shared by the profiler + cockpit — "left at defaults" means the
# operator did not point us at a specific control schema, so we may resolve one.
WIDGET_DEFAULT_CATALOG = "main"
WIDGET_DEFAULT_SCHEMA = "migration"

# --------------------------------------------------------------------------- #
# migration_control_config — the deploy-time handshake table (single row).
# --------------------------------------------------------------------------- #

CONTROL_CONFIG_TABLE = "migration_control_config"

CONTROL_CONFIG_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("warehouse_id", "STRING"),
    ("dashboard_id", "STRING"),
    ("genie_space_id", "STRING"),
    ("updated_at", "TIMESTAMP"),
)
CONTROL_CONFIG_COLUMN_NAMES: Tuple[str, ...] = tuple(n for n, _ in CONTROL_CONFIG_COLUMNS)

# The control tables that, when present in a schema, prove it holds the control plane.
CONTROL_MARKER_TABLES: Tuple[str, ...] = ("migration_next_actions", "table_manifest")


def control_config_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{CONTROL_CONFIG_TABLE}"


def create_genie_volume_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Idempotent DDL for the Genie knowledge volume (fresh-deploy safe).

    Created here (post-schema) rather than as a DABs ``volumes`` resource so a fresh
    workspace never fails deploy-time object validation, mirroring the SDK-managed Agent.
    """
    from . import genie_space as gs

    return f"CREATE VOLUME IF NOT EXISTS {gs.volume_fqn(manifest_catalog, manifest_schema)}"


def genie_volume_comment_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """``COMMENT ON VOLUME`` describing the knowledge library (the Genie Agent reads it to pick files).

    Run best-effort by ``job_99_setup`` only, never by ``ensure_control_plane`` (the profiler
    calls that, and a COMMENT permission error must never fail it).
    """
    from . import genie_space as gs

    docs = "; ".join(f"{name}.md: {purpose}" for name, purpose in gs.KNOWLEDGE_DOC_PURPOSES)
    text = ("Operator knowledge for the External to Managed table migration. Each topic is "
            "written once, in its home doc; the others link to it. Read "
            "overview_and_glossary.md first for terms and states, and troubleshooting.md for "
            f"symptoms. Docs: {docs}.")
    return (f"COMMENT ON VOLUME {gs.volume_fqn(manifest_catalog, manifest_schema)} IS "
            f"'{text.replace(chr(39), chr(39) * 2)}'")


def create_control_config_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in CONTROL_CONFIG_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {control_config_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Pure resolution logic (no Spark) — unit-tested.
# --------------------------------------------------------------------------- #

def pick_candidate(
    widget: Tuple[str, str],
    is_default: bool,
    config_hit: Optional[Tuple[str, str]],
    defaults: Tuple[str, str] = (WIDGET_DEFAULT_CATALOG, WIDGET_DEFAULT_SCHEMA),
) -> Tuple[str, str, str]:
    """Choose the catalog.schema to PROBE first.

    Layer 1 (config handshake): if the operator left the widgets at defaults and a
    ``migration_control_config`` row was found at the default location, adopt the
    catalog.schema it points to. Otherwise honour the widget values verbatim.

    Returns ``(catalog, schema, source)`` where source is ``"config"`` or ``"widget"``.
    """
    if is_default and config_hit and config_hit[0] and config_hit[1]:
        return (config_hit[0], config_hit[1], "config")
    return (widget[0], widget[1], "widget")


def _fail_fast_message(
    widget: Tuple[str, str],
    defaults: Tuple[str, str],
    search_hits: List[Tuple[str, str]],
) -> str:
    wc, ws = widget
    dc, ds = defaults
    lead = (
        f"No control tables at {wc}.{ws} — set manifest_catalog/manifest_schema to match "
        f"your databricks.yml deploy (var manifest_catalog default '{dc}'; your workspace "
        f"may use a different catalog, e.g. my_catalog)."
    )
    if not search_hits:
        return (
            lead + " No schema in this workspace contains "
            + " or ".join(CONTROL_MARKER_TABLES)
            + " — run job_00_profiler first to create the control plane."
        )
    cand = ", ".join(f"{c}.{s}" for c, s in search_hits)
    return lead + f" Ambiguous — control tables were found in multiple schemas: {cand}."


def pick_config_location(
    default_present: bool,
    searched_locations: Optional[List[Tuple[str, str]]],
) -> Optional[str]:
    """Choose WHERE to read ``migration_control_config`` from (pure).

    The config handshake is deterministic only when there is exactly one config table:
    prefer the default location if a row is present there; otherwise adopt the single
    schema a cross-catalog search turned up. Zero or multiple candidates -> ``None``
    (defer to the control-table search / fail-fast).

    Returns ``"default"``, a ``(catalog, schema)`` tuple, or ``None``.
    """
    if default_present:
        return "default"
    uniq = sorted({(c, s) for c, s in (searched_locations or [])})
    return uniq[0] if len(uniq) == 1 else None


def decide_control_schema(
    widget: Tuple[str, str],
    candidate: Tuple[str, str],
    candidate_source: str,
    found_at_candidate: bool,
    search_hits: List[Tuple[str, str]],
    defaults: Tuple[str, str] = (WIDGET_DEFAULT_CATALOG, WIDGET_DEFAULT_SCHEMA),
) -> Tuple[str, str, str, Optional[str]]:
    """Resolve the effective control ``catalog.schema``.

    Given the candidate (from :func:`pick_candidate`) and the results of the Spark
    probes, apply: found-at-candidate -> single search hit (auto-resolve) ->
    zero/ambiguous (fail-fast with guidance).

    Returns ``(catalog, schema, source, error_msg)``. ``error_msg`` is non-None ONLY on
    the fail-fast branch; the caller should raise it.
    """
    if found_at_candidate:
        return (candidate[0], candidate[1], candidate_source, None)

    uniq: List[Tuple[str, str]] = sorted({(c, s) for c, s in (search_hits or [])})
    if len(uniq) == 1:
        c, s = uniq[0]
        return (c, s, "auto-resolve", None)

    return (candidate[0], candidate[1], "fail-fast", _fail_fast_message(widget, defaults, uniq))


# --------------------------------------------------------------------------- #
# Spark edges (thin) — import pyspark-adjacent siblings lazily so the pure logic
# above stays importable with no cluster / no Spark session.
# --------------------------------------------------------------------------- #

def ensure_control_plane(spark, manifest_catalog: str, manifest_schema: str) -> dict:
    """Create EVERY control table + view empty (idempotent). Returns their FQNs.

    Ordering matters: the manifest ensure also creates the schema; the two progress
    views JOIN ``wave_plan`` + ``table_manifest`` so those tables must exist first
    (they do — created just above).
    """
    from . import actions as ax
    from . import audit as au
    from . import authority as authz
    from . import bindings as bd
    from . import compat as cm
    from . import compaction as cp
    from . import concurrency as cc
    from . import conductor as cd
    from . import focus as fx
    from . import coverage_report as cvr
    from . import executor as ex
    from . import freeze_views as _fv
    from . import gate_eval as ge
    from . import genie_space as gs
    from . import lineage as lin
    from . import locations as loc
    from . import manifest as mf
    from . import ownership as own
    from . import parallelism as par
    from . import progress as pg
    from . import reconcile as rec
    from . import run_ledger as rl
    from . import runbook as rb
    from . import stage_ledger as sl
    from . import streaming as st
    from . import validate as vd
    from . import writers as wr

    manifest_fqn = mf.ensure_manifest_table(spark, manifest_catalog, manifest_schema)
    # Row-level concurrency for the shared MERGE target: parallel bulk-convert waves write
    # DISJOINT manifest rows at once. Deletion vectors on this UNPARTITIONED table auto-enable
    # row-level concurrency (disjoint-row MERGEs cannot conflict under WriteSerializable). The
    # append-only audit_log / gate_eval tables need no change (INSERT+INSERT never conflicts).
    spark.sql(cc.enable_row_level_concurrency_sql(manifest_fqn))
    spark.sql(loc.create_location_map_table_sql(manifest_catalog, manifest_schema))
    spark.sql(lin.create_wave_plan_table_sql(manifest_catalog, manifest_schema))
    spark.sql(lin.create_comms_feed_table_sql(manifest_catalog, manifest_schema))
    # Parallel-safe batch grouping over the current plan (WS: bulk convert). Empty on deploy so
    # App/dashboard reads never fail; rewritten by 02_wave_planner after each wave_plan write.
    spark.sql(par.create_wave_parallel_groups_table_sql(manifest_catalog, manifest_schema))
    au.ensure_audit_table(spark, manifest_catalog, manifest_schema)
    cd.ensure_next_actions_table(spark, manifest_catalog, manifest_schema)
    # Focus mode: per-scope recommendation batches + the team pin event log (both append-only),
    # created empty on deploy so App/MCP/Genie reads never fail before the first conductor run.
    fx.ensure_focus_tables(spark, manifest_catalog, manifest_schema)
    # Row-level concurrency on the tables that parallel bulk-convert waves write at once
    # (freeze_ledger, migration_actions, validation_results), mirroring the manifest above.
    # Deletion vectors on these UNPARTITIONED tables auto-enable row-level concurrency so
    # disjoint-row MERGE/UPDATE from different waves cannot conflict. Each ALTER runs AFTER
    # its table is ensured (created empty on deploy) so the property change is always valid.
    actions_fqn = ax.ensure_actions_table(spark, manifest_catalog, manifest_schema)
    spark.sql(cc.enable_row_level_concurrency_sql(actions_fqn))
    cp.ensure_compaction_sources_table(spark, manifest_catalog, manifest_schema)
    # streaming_plan is otherwise created lazily by the profiler; ensure it empty here so the
    # freeze_plan_by_scope view (which UNIONs its WRITE-role rows) can be created on a fresh
    # deploy. Auto-evolves its columns via the module's ADD COLUMNS reconcile.
    st.ensure_streaming_plan_table(spark, manifest_catalog, manifest_schema)
    # writer_sources feeds the freeze_plan_by_scope view's writers CTE (Milestone A). Ensure it
    # empty AND column-reconciled here (adds next_scheduled_run_at on estates provisioned before
    # that column) BEFORE the view is created below — otherwise CREATE VIEW references a missing
    # table (fresh deploy) or a missing column (older estate) and dies with TABLE_OR_VIEW_NOT_FOUND
    # / a resolution error. Mirrors the streaming_plan/compaction_sources ensures above.
    wr.ensure_writer_sources_table(spark, manifest_catalog, manifest_schema)
    freeze_ledger_fqn = ex.ensure_freeze_ledger_table(spark, manifest_catalog, manifest_schema)
    spark.sql(cc.enable_row_level_concurrency_sql(freeze_ledger_fqn))
    # Authorize ledger (03d grants / 07 revokes, plus legacy lease restores) — the permission twin
    # of freeze_ledger. Ensured here (before the runbook view, which UNIONs it) and given the same
    # row-level concurrency so parallel bulk waves authorizing disjoint tables don't conflict.
    ownership_ledger_fqn = own.ensure_ownership_ledger_table(spark, manifest_catalog, manifest_schema)
    spark.sql(cc.enable_row_level_concurrency_sql(ownership_ledger_fqn))
    # convert_authority: the latest "can the convert identity SET MANAGED this table" verdict,
    # MERGEd by the profiler / dry run / authorize / convert / conductor writers — several of
    # which run in parallel under bulk fan-out, hence row-level concurrency. Ensured BEFORE
    # wave_status, which reads it for open_lease.
    authority_fqn = authz.ensure_convert_authority_table(spark, manifest_catalog, manifest_schema)
    spark.sql(cc.enable_row_level_concurrency_sql(authority_fqn))
    # validation_results is otherwise created lazily by 05_validate; ensure it empty here so
    # the deletion-vectors ALTER is valid on a fresh deploy.
    spark.sql(vd.create_validation_results_sql(manifest_catalog, manifest_schema))
    spark.sql(
        cc.enable_row_level_concurrency_sql(
            vd.validation_results_fqn(manifest_catalog, manifest_schema)
        )
    )
    ge.ensure_gate_eval_table(spark, manifest_catalog, manifest_schema)
    rl.ensure_run_ledger_table(spark, manifest_catalog, manifest_schema)
    # Stage-run ledger (one row per convert/campaign run): created empty on deploy so the App's
    # "Campaign & convert runs" read shows a first-run empty state instead of a missing-table
    # error before any stage has run. Append-only; written by 04_convert / 08_campaign_gate.
    sl.ensure_stage_run_ledger_table(spark, manifest_catalog, manifest_schema)
    # Blind-spot enumeration snapshot (WS3/D6): created empty on deploy so App/dashboard
    # reads never fail; populated only by the opt-in profiler bindings scan.
    bd.ensure_coverage_matrix_table(spark, manifest_catalog, manifest_schema)
    spark.sql(create_control_config_table_sql(manifest_catalog, manifest_schema))
    # Views last — they depend on their base tables AND on each other existing. The action
    # views (incl. ``wave_action_status``) MUST precede the progress ``wave_status`` view,
    # which LEFT JOINs ``wave_action_status``; Databricks validates referenced objects at
    # CREATE VIEW time, so an out-of-order create fails with TABLE_OR_VIEW_NOT_FOUND.
    ax.ensure_action_views(spark, manifest_catalog, manifest_schema)
    # Per-entity planned-freeze rows + safety class (UNIONs compaction_sources + WRITE-role
    # streaming_plan + writer_sources, JOINs table_manifest for the stream's scope, LEFT JOINs
    # action_queue_open for the open-freeze flag). All sources are ensured above; safety_class
    # mirrors the pure freeze_downtime.classify_freeze_entity. Read-only; App/dashboard/MCP.
    spark.sql(_fv.create_freeze_plan_by_scope_view_sql(manifest_catalog, manifest_schema))
    ex.ensure_freeze_ledger_view(spark, manifest_catalog, manifest_schema)
    ge.ensure_gate_eval_view(spark, manifest_catalog, manifest_schema)
    spark.sql(pg.create_migration_progress_view_sql(manifest_catalog, manifest_schema))
    spark.sql(pg.create_wave_status_view_sql(manifest_catalog, manifest_schema))
    # Per-table pre-convert external-reader state (table_manifest + ENABLE_COMPAT actions).
    # Read-only; app preflight, Genie. wave_status inlines the same CASE (no view dependency).
    spark.sql(cm.create_external_reader_readiness_view_sql(manifest_catalog, manifest_schema))
    rb.ensure_runbook_view(spark, manifest_catalog, manifest_schema)
    # Coverage self-diagnostic (WS2/D6): latest run per workspace + a manifest rollup over
    # profiler_run_ledger + table_manifest (both ensured above). Read-only; App/dashboard.
    spark.sql(cvr.create_coverage_report_view_sql(manifest_catalog, manifest_schema))
    # Honest staleness (WS5/D1): manifest rows not re-seen by their workspace's latest
    # covering run. Read-only; selection only (never deletes). App/dashboard surface it.
    spark.sql(rec.create_stale_manifest_view_sql(manifest_catalog, manifest_schema))
    # Out-of-band reconcile surface (out-of-band reconciliation): CONVERTED_EXTERNAL / DROPPED
    # rows + still-unexplained stale rows, with disposition + last RECONCILE audit. Depends on
    # stale_manifest (above) and audit_log; read-only. App/dashboard surface it.
    spark.sql(rec.create_reconciled_manifest_view_sql(manifest_catalog, manifest_schema))
    # Genie knowledge volume (unstructured docs) — idempotent, after the schema exists.
    spark.sql(create_genie_volume_sql(manifest_catalog, manifest_schema))

    return {
        "manifest": manifest_fqn,
        "location_map": loc.location_map_fqn(manifest_catalog, manifest_schema),
        "wave_plan": lin.wave_plan_fqn(manifest_catalog, manifest_schema),
        "comms_feed": lin.comms_feed_fqn(manifest_catalog, manifest_schema),
        "wave_parallel_groups": par.wave_parallel_groups_fqn(manifest_catalog, manifest_schema),
        "audit_log": au.audit_log_fqn(manifest_catalog, manifest_schema),
        "next_actions": cd.next_actions_fqn(manifest_catalog, manifest_schema),
        "next_actions_by_scope": fx.by_scope_fqn(manifest_catalog, manifest_schema),
        "focus_pins": fx.pins_fqn(manifest_catalog, manifest_schema),
        "migration_actions": ax.actions_fqn(manifest_catalog, manifest_schema),
        "compaction_sources": cp.compaction_sources_fqn(manifest_catalog, manifest_schema),
        "freeze_ledger": ex.freeze_ledger_fqn(manifest_catalog, manifest_schema),
        "ownership_ledger": own.ownership_ledger_fqn(manifest_catalog, manifest_schema),
        "convert_authority": authz.convert_authority_fqn(manifest_catalog, manifest_schema),
        "validation_results": vd.validation_results_fqn(manifest_catalog, manifest_schema),
        "wave_gate_evaluation": ge.gate_eval_fqn(manifest_catalog, manifest_schema),
        "control_config": control_config_fqn(manifest_catalog, manifest_schema),
        "profiler_run_ledger": rl.run_ledger_fqn(manifest_catalog, manifest_schema),
        "stage_run_ledger": sl.stage_run_ledger_fqn(manifest_catalog, manifest_schema),
        "coverage_matrix": bd.coverage_matrix_fqn(manifest_catalog, manifest_schema),
        "migration_progress": pg.migration_progress_view_fqn(manifest_catalog, manifest_schema),
        "wave_status": pg.wave_status_view_fqn(manifest_catalog, manifest_schema),
        "external_reader_readiness": cm.external_reader_readiness_view_fqn(
            manifest_catalog, manifest_schema),
        "action_queue_open": ax.action_queue_open_view_fqn(manifest_catalog, manifest_schema),
        "action_summary": ax.action_summary_view_fqn(manifest_catalog, manifest_schema),
        "wave_action_status": ax.wave_action_status_view_fqn(manifest_catalog, manifest_schema),
        "freeze_ledger_open": ex.freeze_ledger_open_view_fqn(manifest_catalog, manifest_schema),
        "freeze_plan_by_scope": _fv.freeze_plan_by_scope_view_fqn(manifest_catalog, manifest_schema),
        "wave_gate_latest": ge.gate_eval_latest_view_fqn(manifest_catalog, manifest_schema),
        "migration_runbook": rb.runbook_view_fqn(manifest_catalog, manifest_schema),
        "coverage_report": cvr.coverage_report_view_fqn(manifest_catalog, manifest_schema),
        "stale_manifest": rec.stale_manifest_view_fqn(manifest_catalog, manifest_schema),
        "reconciled_manifest": rec.reconciled_manifest_view_fqn(manifest_catalog, manifest_schema),
        "genie_knowledge": gs.volume_fqn(manifest_catalog, manifest_schema),
    }


def _control_config_struct_type():
    from pyspark.sql.types import StringType, StructField, StructType, TimestampType

    sql_to_spark = {"STRING": StringType(), "TIMESTAMP": TimestampType()}
    return StructType([StructField(n, sql_to_spark[t], True) for n, t in CONTROL_CONFIG_COLUMNS])


def merge_control_config_row(
    manifest_catalog: str,
    manifest_schema: str,
    existing: Optional[dict],
    *,
    warehouse_id: Optional[str] = None,
    dashboard_id: Optional[str] = None,
    genie_space_id: Optional[str] = None,
    updated_at=None,
) -> dict:
    """Compute the merged single-row config (pure; no Spark).

    Partial upsert: set only the fields PASSED (non-``None``) and preserve the rest from
    ``existing``. ``catalog``/``schema`` always identify the row (the caller supplies the
    control-plane location). This is what lets the profiler and the setup job each write
    their own fields into the same row without blanking each other's — e.g. the profiler
    writes ``warehouse_id`` while setup writes ``genie_space_id``/``dashboard_id``, and
    neither run wipes the other's field regardless of which ran last.
    """
    prior = existing or {}

    def _keep(passed, key):
        return passed if passed is not None else prior.get(key)

    return {
        "catalog": manifest_catalog,
        "schema": manifest_schema,
        "warehouse_id": _keep(warehouse_id, "warehouse_id"),
        "dashboard_id": _keep(dashboard_id, "dashboard_id"),
        "genie_space_id": _keep(genie_space_id, "genie_space_id"),
        "updated_at": updated_at,
    }


def write_control_config(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    warehouse_id: Optional[str] = None,
    dashboard_id: Optional[str] = None,
    genie_space_id: Optional[str] = None,
) -> str:
    """Partial-upsert the single-row config handshake (tiny table; latest wins per field).

    Only the fields passed (non-``None``) are written; the rest are preserved from the
    existing row. This is deliberately NOT a whole-row overwrite: the profiler (which
    writes ``catalog``/``schema``/``warehouse_id``) and the setup job (which writes
    ``genie_space_id``/``dashboard_id`` after the Genie space is created via the SDK) run
    as separate jobs in either order, and a whole-row overwrite would let the later run
    silently blank the earlier run's fields (e.g. the profiler wiping ``genie_space_id``).
    Reading the prior row + merging keeps every field owned by whichever job set it.
    """
    from datetime import datetime, timezone

    fqn = control_config_fqn(manifest_catalog, manifest_schema)
    spark.sql(create_control_config_table_sql(manifest_catalog, manifest_schema))
    existing = read_control_config(spark, manifest_catalog, manifest_schema)
    row = merge_control_config_row(
        manifest_catalog,
        manifest_schema,
        existing,
        warehouse_id=warehouse_id,
        dashboard_id=dashboard_id,
        genie_space_id=genie_space_id,
        updated_at=datetime.now(timezone.utc),
    )
    df = spark.createDataFrame([row], schema=_control_config_struct_type())
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(fqn)
    return fqn


def read_control_config(spark, manifest_catalog: str, manifest_schema: str) -> Optional[dict]:
    """Return the latest config row as a dict, or None if the table is absent/empty."""
    fqn = control_config_fqn(manifest_catalog, manifest_schema)
    try:
        rows = spark.sql(
            f"SELECT * FROM {fqn} ORDER BY updated_at DESC LIMIT 1"
        ).collect()
    except Exception:  # noqa: BLE001 — table missing / no access -> treat as no config
        return None
    return rows[0].asDict() if rows else None


def table_exists(spark, fqn: str) -> bool:
    """True if a table/view exists at ``fqn`` (thin edge)."""
    try:
        spark.sql(f"DESCRIBE TABLE {fqn}").collect()
        return True
    except Exception:  # noqa: BLE001
        return False


def _search_schemas_with_tables(
    spark, table_names: Tuple[str, ...], catalog_filter: Optional[str] = None
) -> List[Tuple[str, str]]:
    """Find schemas that contain any of ``table_names`` (thin edge).

    Prefers ``system.information_schema.tables``; falls back to a per-catalog scan when
    the system schema isn't granted (mirrors the profiler's discovery fallback). Returns
    the distinct ``(catalog, schema)`` pairs, sorted.
    """
    names = ", ".join(f"'{t}'" for t in table_names)
    hits: set = set()

    def _rows_to_hits(rows):
        for r in rows:
            d = r.asDict()
            hits.add((d["table_catalog"], d["table_schema"]))

    try:
        sql = (
            "SELECT DISTINCT table_catalog, table_schema "
            "FROM system.information_schema.tables "
            f"WHERE table_name IN ({names})"
        )
        _rows_to_hits(spark.sql(sql).collect())
        return sorted(hits)
    except Exception:  # noqa: BLE001 — fall back to per-catalog information_schema
        pass

    try:
        catalogs = [r[0] for r in spark.sql("SHOW CATALOGS").collect()]
    except Exception:  # noqa: BLE001
        return sorted(hits)
    skip = {"system", "samples", "__databricks_internal"}
    for cat in catalogs:
        if cat in skip:
            continue
        if catalog_filter and catalog_filter not in ("", "*") and catalog_filter != cat:
            continue
        try:
            rows = spark.sql(
                f"SELECT DISTINCT table_catalog, table_schema "
                f"FROM `{cat}`.information_schema.tables "
                f"WHERE table_name IN ({names})"
            ).collect()
            _rows_to_hits(rows)
        except Exception:  # noqa: BLE001
            continue
    return sorted(hits)


def search_control_schemas(spark, catalog_filter: Optional[str] = None) -> List[Tuple[str, str]]:
    """Distinct ``(catalog, schema)`` pairs holding the control plane (a marker table)."""
    return _search_schemas_with_tables(spark, CONTROL_MARKER_TABLES, catalog_filter)


def search_config_schemas(spark, catalog_filter: Optional[str] = None) -> List[Tuple[str, str]]:
    """Distinct ``(catalog, schema)`` pairs holding a ``migration_control_config`` table."""
    return _search_schemas_with_tables(spark, (CONTROL_CONFIG_TABLE,), catalog_filter)


def resolve_control_schema(
    spark,
    widget_catalog: str,
    widget_schema: str,
    defaults: Tuple[str, str] = (WIDGET_DEFAULT_CATALOG, WIDGET_DEFAULT_SCHEMA),
) -> Tuple[str, str, str]:
    """Orchestrate the layered resolution (thin edge over the pure decision logic).

    Returns ``(catalog, schema, source)``. Raises ``RuntimeError`` with a guidance
    message on the fail-fast branch.
    """
    widget = (widget_catalog, widget_schema)
    is_default = widget == tuple(defaults)

    # Config handshake (deterministic). Only when the operator left the widgets at
    # defaults: prefer a config row at the default location, else look for a single
    # migration_control_config elsewhere in the metastore (so a non-`main` deploy still
    # resolves deterministically). A stale config self-heals below via the table search.
    config_hit = None
    if is_default:
        default_cfg = read_control_config(spark, defaults[0], defaults[1])
        default_present = bool(
            default_cfg and default_cfg.get("catalog") and default_cfg.get("schema")
        )
        searched = [] if default_present else search_config_schemas(spark)
        choice = pick_config_location(default_present, searched)
        cfg = default_cfg if choice == "default" else (
            read_control_config(spark, choice[0], choice[1]) if choice else None
        )
        if cfg and cfg.get("catalog") and cfg.get("schema"):
            config_hit = (cfg["catalog"], cfg["schema"])

    cand_c, cand_s, source = pick_candidate(widget, is_default, config_hit, defaults)

    found = any(
        table_exists(spark, f"{cand_c}.{cand_s}.{t}") for t in CONTROL_MARKER_TABLES
    )
    search_hits: List[Tuple[str, str]] = [] if found else search_control_schemas(spark)

    catalog, schema, source, err = decide_control_schema(
        widget, (cand_c, cand_s), source, found, search_hits, defaults
    )
    if err:
        raise RuntimeError(err)
    return catalog, schema, source
