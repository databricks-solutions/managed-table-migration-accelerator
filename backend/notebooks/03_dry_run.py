# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Dry Run: pre-flight checks (NOTHING destructive)
# MAGIC
# MAGIC For every `ELIGIBLE` table in the manifest, run the full set of pre-flight checks
# MAGIC that the convert job will re-assert — **but never run `SET MANAGED`**. Tables that
# MAGIC pass every gating check are advanced to `state='DRY_RUN_OK'`; the rest become
# MAGIC `BLOCKED` with reasons. Results are written per-check to `<manifest>.dry_run_results`
# MAGIC and summarised by schema and size band so they can feed the wave + comms plans.
# MAGIC
# MAGIC Reads the control tables produced by `00_profiler.py`:
# MAGIC `table_manifest`, `location_map`, `streaming_plan`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("catalog_filter", "*", "Catalog filter (glob, * = all)")
dbutils.widgets.text("schema_filter", "*", "Schema filter (glob, * = all)")
dbutils.widgets.text("size_band_filter", "*", "Size band filter (* | <=100GB | ~1TB | ~10TB | >10TB)")
dbutils.widgets.text("recent_optimize_minutes", "120", "Window (min) to flag recent OPTIMIZE/compaction")
# This workspace's numeric id (var.workspace_id) so the dry-run can PREDICT the convert
# ownership guard's verdict (fail loud early) instead of letting convert be the first to block.
dbutils.widgets.text("current_workspace_id", "",
                     "This workspace's numeric id (var.workspace_id; blank = runtime probe)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
CATALOG_FILTER = dbutils.widgets.get("catalog_filter").strip()
SCHEMA_FILTER = dbutils.widgets.get("schema_filter").strip()
SIZE_BAND_FILTER = dbutils.widgets.get("size_band_filter").strip()
RECENT_OPTIMIZE_MINUTES = int(dbutils.widgets.get("recent_optimize_minutes"))
CURRENT_WORKSPACE_ID_PARAM = dbutils.widgets.get("current_workspace_id").strip()

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

from datetime import datetime, timezone

from migration import authority as authz
from migration import compat as cm
from migration import consumer_runtime as cr
from migration import coverage as cov
from migration import eligibility as elig
from migration import gate_eval as ge
from migration import job_schedule as js
from migration import locations as loc
from migration import manifest as mf
from migration import path_access as pa
from migration import reconcile as rc
from migration import run_ledger as rl
from migration import scoping as scp
from migration import streaming as st
from migration import writers as wr
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("03_dry_run", dbutils)  # usage telemetry: closed vocabulary only, never raises

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
location_map_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.location_map"
dry_run_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.dry_run_results"
OPERATOR = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
# Ownership prediction (Check 13): resolve THIS workspace deterministically and decide, ONCE,
# whether the estate shows genuine federation (>1 workspace has written the control plane). The
# convert guard uses the exact same evidence + logic, so dry-run now predicts its verdict rather
# than letting convert be the first (opaque) place ownership blocks.
CURRENT_WORKSPACE_ID = rl.resolve_workspace_id(spark, explicit=CURRENT_WORKSPACE_ID_PARAM)
FEDERATION_SEEN = rc.has_federation_evidence(
    rl.distinct_workspace_ids(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA))
print(f"ownership prediction: current_workspace={CURRENT_WORKSPACE_ID} federation_seen={FEDERATION_SEEN}")


def _now():
    return datetime.now(timezone.utc)


def _job_run_id():
    """Best-effort Jobs run id for provenance (None off-cluster / on error)."""
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        opt = ctx.tags().get("jobRunId")
        return opt.get() if opt.isDefined() else None
    except Exception:  # noqa: BLE001
        return None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Check 1 (global) — Runtime gate: DBR 17.0+ or Serverless. **Fail fast.**
# MAGIC The 17.2+ requirement for `TRUNCATE UNIFORM HISTORY` is asserted per-table below.

# COMMAND ----------

def _detect_runtime():
    """Return (version_tuple_or_None, is_serverless, raw_version)."""
    conf = spark.conf
    raw = None
    for key in (
        "spark.databricks.clusterUsageTags.sparkVersion",
        "spark.databricks.clusterUsageTags.effectiveSparkVersion",
    ):
        try:
            raw = conf.get(key)
            if raw:
                break
        except Exception:
            continue
    import os
    # Databricks sets IS_SERVERLESS=TRUE on serverless compute (cluster-usage tags are absent there).
    is_serverless = os.environ.get("IS_SERVERLESS", "").strip().lower() == "true"
    for key in ("spark.databricks.clusterUsageTags.clusterName",
                "spark.databricks.clusterUsageTags.computeType"):
        try:
            val = (conf.get(key) or "").lower()
            if "serverless" in val:
                is_serverless = True
        except Exception:
            pass
    # On serverless there is often no cluster sparkVersion; treat missing+serverless as ok.
    return elig.parse_dbr_version(raw), is_serverless, raw


RUNTIME_VERSION, IS_SERVERLESS, RAW_VERSION = _detect_runtime()
print(f"runtime: raw={RAW_VERSION} parsed={RUNTIME_VERSION} serverless={IS_SERVERLESS}")

if not elig.runtime_supports_set_managed(RUNTIME_VERSION, IS_SERVERLESS):
    raise RuntimeError(
        f"Runtime gate FAILED: SET MANAGED requires DBR {elig.MIN_DBR_SET_MANAGED[0]}."
        f"{elig.MIN_DBR_SET_MANAGED[1]}+ or Serverless. Detected: {RAW_VERSION}. "
        "Attach a qualifying cluster (pin one DBR version for convert/rollback) and re-run."
    )
print("Runtime gate PASSED for SET MANAGED.")
SUPPORTS_TRUNCATE_UNIFORM = elig.runtime_supports_truncate_uniform(RUNTIME_VERSION, IS_SERVERLESS)
print(f"Supports TRUNCATE UNIFORM HISTORY (17.2+/serverless): {SUPPORTS_TRUNCATE_UNIFORM}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select candidate tables (ELIGIBLE + filters)

# COMMAND ----------

from pyspark.sql import functions as F


def _like(col, pattern):
    if pattern in ("", "*"):
        return F.lit(True)
    return F.col(col).like(pattern.replace("*", "%"))


candidates_df = (
    spark.table(manifest_fqn)
    .where(F.col("eligibility") == elig.ELIGIBLE)
    .where(_like("catalog", CATALOG_FILTER))
    .where(_like("schema", SCHEMA_FILTER))
)
if SIZE_BAND_FILTER not in ("", "*"):
    candidates_df = candidates_df.where(F.col("size_band") == SIZE_BAND_FILTER)

candidates = candidates_df.collect()
print(f"Candidate ELIGIBLE tables: {len(candidates)}")

# Streaming plan grouped by table (for check 6).
plan_by_table = {}
try:
    for r in spark.table(streaming_plan_fqn).collect():
        plan_by_table.setdefault(r["full_name"], []).append(r.asDict())
except Exception as ex:  # noqa: BLE001
    print(f"streaming_plan not available: {ex}")

# location_map by schema scope (for check 7).
loc_by_scope = {}
try:
    for r in spark.table(location_map_fqn).collect():
        loc_by_scope[(r["scope_type"], r["scope_name"])] = r.asDict()
except Exception as ex:  # noqa: BLE001
    print(f"location_map not available: {ex}")

# Writer attribution grouped by table (for check 14's named-pause enumeration). Built ONCE
# before the loop; a missing writer_sources table degrades to {} so the dry-run still runs
# (Check 14 then prints the rolled-up reason without naming jobs).
_writer_by_fn = {}
try:
    for r in (spark.table(wr.writer_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
              .select("full_name", "entity_id", "writer_class").collect()):
        _writer_by_fn.setdefault(r["full_name"], []).append(
            {"entity_id": r["entity_id"], "writer_class": r["writer_class"]})
except Exception as ex:  # noqa: BLE001
    print(f"writer_sources not available: {ex}")

# Pre-convert external-reader state (for check 15), scoped by full_name only. A failed view read
# falls back to the manifest flags with no action decision (a flagged table then WARNs).
_reader_by_fn = {}
_READER_VIEW_OK = True
try:
    _reader_fqn = cm.external_reader_readiness_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    for r in (spark.table(_reader_fqn)
              .where(scp.full_name_in_predicate([c["full_name"] for c in candidates]))
              .select("full_name", "reader_readiness").collect()):
        _reader_by_fn[r["full_name"]] = r["reader_readiness"]
except Exception as ex:  # noqa: BLE001
    _READER_VIEW_OK = False
    print(f"external_reader_readiness not available ({str(ex)[:100]}); "
          "Check 15 falls back to the manifest flags")

# Convert authority (for check 4): one batched read AS this job's identity — the same identity
# 04_convert runs as — persisted to convert_authority. Never raises; an unreadable table is UNKNOWN.
_authz_by_fn = {
    r["full_name"]: r
    for r in authz.refresh_authority(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [c["full_name"] for c in candidates],
        source=authz.SOURCE_DRY_RUN, job_run_id=_job_run_id(),
    )
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run pre-flight checks per table (2–10)

# COMMAND ----------

def _describe_detail(full_name_bt):
    """(ok, error text) — the error lets Check 2 tell a refused read from a broken table."""
    try:
        return bool(spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()), None
    except Exception as err:  # noqa: BLE001
        return False, str(err)


def _recent_compaction(full_name_bt):
    """True if an OPTIMIZE/compaction commit happened within the warn window."""
    try:
        rows = spark.sql(f"DESCRIBE HISTORY {full_name_bt} LIMIT 50").select(
            "operation", "timestamp"
        ).collect()
    except Exception:
        return False
    cutoff = _now().timestamp() - RECENT_OPTIMIZE_MINUTES * 60
    for r in rows:
        if elig.is_compaction_operation(r["operation"]):
            ts = r["timestamp"]
            if ts is not None and ts.timestamp() >= cutoff:
                return True
    return False


# Effective managed-location resolution (schema > catalog > metastore), cached per
# (catalog, schema). SET MANAGED needs a managed storage location; we resolve live and
# distinguish CONFIGURED (PASS), NOT_CONFIGURED (FAIL no_managed_location) and UNRESOLVED
# (FAIL managed_location_unresolved — fail-closed: we could not prove one exists). The
# metastore storage root is resolved once and reused.
_METASTORE_ROW = loc.resolve_metastore(spark)
_ml_cache = {}


def _effective_ml(catalog, schema):
    key = (catalog, schema)
    if key not in _ml_cache:
        _ml_cache[key] = loc.effective_managed_location_live(
            spark, catalog, schema, metastore_row=_METASTORE_ROW)
    return _ml_cache[key]


all_check_rows = []
state_updates = []
reader_counts = {}

for r in candidates:
    d = r.asDict()
    full_name = d["full_name"]
    full_name_bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    uniform = bool(d.get("uniform_iceberg_enabled"))
    results = []

    # Check 1b — per-table runtime for TRUNCATE UNIFORM HISTORY.
    if uniform and not SUPPORTS_TRUNCATE_UNIFORM:
        results.append(elig.check_result(
            1, "runtime_truncate_uniform",
            elig.CHECK_FAIL,
            f"UniForm Iceberg table needs DBR {elig.MIN_DBR_TRUNCATE_UNIFORM[0]}."
            f"{elig.MIN_DBR_TRUNCATE_UNIFORM[1]}+/Serverless; detected {RAW_VERSION}.",
            blocker="runtime_below_17_2_uniform",
        ))
    else:
        results.append(elig.check_result(
            1, "runtime", elig.CHECK_PASS,
            f"runtime ok (uniform={uniform}, supports_truncate={SUPPORTS_TRUNCATE_UNIFORM})",
        ))

    # Check 2 — DESCRIBE DETAIL succeeds (not corrupted, and readable by the convert identity).
    results.append(elig.describe_detail_check(*_describe_detail(full_name_bt)))

    # Check 3 — name-based access only (no path-based dependency in lineage). A Databricks job
    # using the path holds the table too, unless the customer opted in to path-based redirect
    # and the engine still finds the table eligible with the same consumers (path_access.native_hold).
    if d.get("has_path_based_access"):
        results.append(elig.check_result(
            3, "name_based_access", elig.CHECK_FAIL,
            "Path-based access detected in lineage; switch consumers to name-based first.",
            blocker="path_based_access_must_be_fixed_first",
        ))
    elif pa.native_hold(d):
        _why = pa.redirect_eligibility(d)
        _exit = ("or opt in to path-based redirect (the table is eligible)" if _why["eligible"]
                 else f"(not eligible for redirect: {', '.join(_why['reasons'])})")
        results.append(elig.check_result(
            3, "name_based_access", elig.CHECK_FAIL,
            f"A Databricks job uses the path; switch it to name-based access first {_exit}.",
            blocker=pa.BLOCKER_REDIRECT_CHECK,
        ))
    elif pa.optin_valid(d):
        results.append(elig.check_result(
            3, "name_based_access", elig.CHECK_PASS,
            f"path-based redirect opted in by {d.get('path_redirect_optin_by')} "
            f"(restart path-based streams after conversion)",
        ))
    else:
        results.append(elig.check_result(
            3, "name_based_access", elig.CHECK_PASS,
            f"no path-based access (confidence={d.get('access_confidence')})",
        ))

    # Check 4 — convert permission (ADVISORY: WARN/INFO, never FAIL; 04_convert is the hard gate).
    _a_status, _a_detail = authz.dry_run_check(_authz_by_fn.get(full_name))
    results.append(elig.check_result(authz.CHECK_NUMBER, authz.CHECK_NAME, _a_status, _a_detail))

    # Check 5 — no active/recent OPTIMIZE/ZORDER/compaction (WARN, advisory).
    if _recent_compaction(full_name_bt):
        results.append(elig.check_result(
            5, "compaction_activity", elig.CHECK_WARN,
            f"OPTIMIZE/compaction within last {RECENT_OPTIMIZE_MINUTES} min. "
            "Pause Predictive Optimization / scheduled OPTIMIZE during the conversion window.",
        ))
    else:
        results.append(elig.check_result(
            5, "compaction_activity", elig.CHECK_WARN,
            "No recent compaction detected, but confirm no scheduled OPTIMIZE/ZORDER/liquid-clustering "
            "and pause Predictive Optimization for the conversion window.",
        ))

    # Check 6 — streaming jobs catalogued + owners identified.
    plan = plan_by_table.get(full_name, [])
    if d.get("has_streaming_writer") or plan:
        missing_owner = [p for p in plan if not p.get("owner")]
        status = elig.CHECK_WARN if (not plan or missing_owner) else elig.CHECK_INFO
        results.append(elig.check_result(
            6, "streaming_catalogued", status,
            f"{len(plan)} streaming relationship(s); {len(missing_owner)} missing owner. "
            "Restart these (stop current, start new) after conversion.",
        ))
    else:
        results.append(elig.check_result(6, "streaming_catalogued", elig.CHECK_INFO, "no streaming detected"))

    # Check 7 — effective managed location & source scope + cross-region advisory.
    eff = _effective_ml(d["catalog"], d["schema"])
    schema_loc = loc_by_scope.get(("SCHEMA", f"{d['catalog']}.{d['schema']}"), {})
    region = schema_loc.get("region")
    results.append(elig.check_result(
        7, "target_location",
        elig.CHECK_INFO if eff["status"] == loc.ML_CONFIGURED else elig.CHECK_WARN,
        f"effective managed_location={eff.get('managed_location')} "
        f"source={eff.get('source_scope')} region={region}. "
        "Confirm external storage region matches to avoid cross-region transfer cost.",
    ))

    # Check 8 — Delta Sharing re-share reminder.
    if d.get("is_delta_shared"):
        cmds = " ".join(elig.build_reshare_commands(full_name))
        results.append(elig.check_result(
            8, "delta_sharing_reshare", elig.CHECK_WARN,
            "Table is Delta-shared (D2D). After conversion, re-share: " + cmds,
        ))
    else:
        results.append(elig.check_result(8, "delta_sharing_reshare", elig.CHECK_INFO, "not Delta-shared"))

    # Check 9 — the exact command that WOULD run.
    cmd = elig.build_convert_command(full_name_bt, uniform)
    results.append(elig.check_result(9, "convert_command", elig.CHECK_INFO, cmd))

    # Check 10 — echo size band, est copy time, est downtime.
    results.append(elig.check_result(
        10, "estimates", elig.CHECK_INFO,
        f"size_band={d.get('size_band')} est_copy_min={d.get('est_data_copy_minutes')} "
        f"est_downtime_min={d.get('est_downtime_minutes')}",
    ))

    # Check 11 — consumer-runtime floor (advisory; WARN never FAIL). Uses d.get() so a
    # manifest that predates the new columns yields INFO instead of erroring.
    cr_status, cr_detail = cr.consumer_runtime_check_status(
        d.get("has_below_floor_consumer"), d.get("consumer_dbr_confidence"),
        d.get("readers_below_zero_downtime"),
        cr.below_floor_versions_from_notes(d.get("consumer_runtime_notes")))
    results.append(elig.check_result(
        11, "consumer_runtime_floor", cr_status,
        f"{cr_detail} min_consumer_dbr={d.get('min_consumer_dbr')}",
    ))

    # Check 12 — managed location configured (HARD BLOCKER). PASS when an effective
    # location is configured; FAIL no_managed_location only when every scope was resolved
    # and absent; FAIL managed_location_unresolved when a lookup error prevents a safe
    # answer (fail-closed — never falsely claims "no location").
    if eff["status"] == loc.ML_CONFIGURED:
        results.append(elig.check_result(
            12, "managed_location_configured", elig.CHECK_PASS, eff["detail"]))
    elif eff["status"] == loc.ML_NOT_CONFIGURED:
        results.append(elig.check_result(
            12, "managed_location_configured", elig.CHECK_FAIL, eff["detail"],
            blocker=loc.NOTE_NO_MANAGED_LOCATION))
    else:  # UNRESOLVED
        results.append(elig.check_result(
            12, "managed_location_configured", elig.CHECK_FAIL, eff["detail"],
            blocker=loc.NOTE_MANAGED_LOCATION_UNRESOLVED))

    # Check 13 — workspace ownership (D4.2), PREDICTING the convert guard. Evidence-based:
    # a blank-provenance row only fails under GENUINE federation (federation_seen); a single-
    # workspace estate owns its unattributed rows (no block). A truly FOREIGN row always fails.
    # Surfacing it here (not just at convert) closes the "green dry-run -> convert fails" gap.
    if not CURRENT_WORKSPACE_ID:
        # We don't know THIS workspace's id — do NOT fabricate a FOREIGN/UNKNOWN verdict (that
        # would falsely block). Degrade honestly: advisory INFO only. The convert guard remains
        # the fail-closed authority if the id is still unknown there.
        results.append(elig.check_result(
            13, "workspace_ownership", elig.CHECK_INFO,
            "current workspace id unknown (set var.workspace_id) — ownership not evaluated"))
        _ownership = None
    else:
        _ownership = rc.classify_ownership(
            discovered_in_workspace_id=d.get("discovered_in_workspace_id"),
            current_workspace_id=CURRENT_WORKSPACE_ID,
            federation_seen=FEDERATION_SEEN)
    if _ownership == rc.OWNERSHIP_UNKNOWN:
        results.append(elig.check_result(
            13, "workspace_ownership", elig.CHECK_FAIL,
            "Unknown owning workspace (federated estate, provenance missing) — set owning_workspace "
            "or run the profiler where the table lives.", blocker=rc.BLOCKER_UNKNOWN_OWNER))
    elif _ownership == rc.OWNERSHIP_FOREIGN:
        results.append(elig.check_result(
            13, "workspace_ownership", elig.CHECK_FAIL,
            f"Discovered by a DIFFERENT workspace ({d.get('discovered_in_workspace_id')}) — convert "
            "from the owning workspace or set owning_workspace to override.",
            blocker=rc.BLOCKER_FOREIGN_WORKSPACE))
    elif _ownership is not None:  # OWN / OVERRIDE — the unknown-id case already emitted INFO
        results.append(elig.check_result(
            13, "workspace_ownership", elig.CHECK_PASS,
            f"ownership={_ownership} (current_workspace={CURRENT_WORKSPACE_ID}, "
            f"federation_seen={FEDERATION_SEEN})"))

    # Check 14 — writer schedule verdict (ADVISORY: INFO/WARN, never FAIL). SR7: UNKNOWN is
    # honest ("couldn't read the schedule"), not a green light. The reactive gates (recent-write,
    # recent-compaction, recent-vacuum, writer-stop coverage) remain the authority. Only appended
    # when a verdict exists on the row, so a pre-Milestone-C manifest (NULL verdict) stays silent.
    _wsv = (d.get("writer_schedule_verdict") or "").upper()
    if _wsv in ("MUST_STOP", "WARN_WINDOW"):
        _wr_rows = [{**_w, "full_name": full_name,
                     "next_scheduled_write_at": d.get("next_scheduled_write_at")}
                    for _w in _writer_by_fn.get(full_name, [])]
        _named = js.format_pause_warning(_wr_rows, dry_run=True)
        _detail = d.get("writer_schedule_reason") or _wsv
        if _named:
            _detail = _detail + "\n" + "\n".join(_named)
        results.append(elig.check_result(
            14, "Writer schedule verdict", elig.CHECK_WARN, _detail))
    elif _wsv in ("SAFE_NOW", "UNKNOWN"):
        results.append(elig.check_result(
            14, "Writer schedule verdict", elig.CHECK_INFO,
            d.get("writer_schedule_reason") or _wsv))

    # Check 15 — external readers before convert (ADVISORY: WARN/INFO, never FAIL or a blocker).
    _reader_state = (_reader_by_fn.get(full_name, cm.READER_NONE) if _READER_VIEW_OK
                     else cm.reader_readiness(d.get("needs_compatibility_mode"),
                                              d.get("access_notes"), None, None))
    _r_status, _r_detail = cm.reader_check(_reader_state, from_view=_READER_VIEW_OK)
    reader_counts[_reader_state] = reader_counts.get(_reader_state, 0) + 1
    results.append(elig.check_result(
        cm.READER_CHECK_NUMBER, cm.READER_CHECK_NAME, _r_status, _r_detail))

    # Finalize state for this table. Replace only dry-run-OWNED blockers so a repaired
    # table returns to DRY_RUN_OK without retaining stale ones, while blockers owned by
    # other assessment stages (external-writer/streaming/profiler) are preserved.
    new_state, fail_blockers = elig.finalize_dry_run_state(results)
    state_updates.append({
        "full_name": full_name,
        "state": new_state,
        "blockers": loc.replace_owned_blockers(
            d.get("blockers"), loc.DRY_RUN_OWNED_BLOCKERS, fail_blockers),
        "updated_at": _now(),
    })

    checked_at = _now()
    for res in results:
        all_check_rows.append({
            "full_name": full_name,
            "catalog": d["catalog"],
            "schema": d["schema"],
            "size_band": d.get("size_band"),
            "check_num": res["check_num"],
            "check_name": res["check_name"],
            "status": res["status"],
            "detail": res["detail"],
            "dry_run_state": new_state,
            "checked_at": checked_at,
        })

print(f"Ran checks on {len(candidates)} tables -> {len(all_check_rows)} check rows")
passed = sum(1 for s in state_updates if s["state"] == elig.STATE_DRY_RUN_OK)
print(f"DRY_RUN_OK: {passed}   BLOCKED: {len(state_updates) - passed}")
_reader_warning = cm.preconvert_reader_warning(
    reader_counts.get(cm.READER_UNDECIDED, 0), reader_counts.get(cm.READER_FGAC_REQUIRED, 0))
if _reader_warning:
    print(f"WARN external readers (Check 15, advisory): {_reader_warning}")
_authz_sum = authz.summarize(_authz_by_fn.values())
if _authz_sum["not_authorized"]:
    print(f"WARN convert permission (Check 4, advisory): {_authz_sum['counts']} — run "
          "job_03d_lease_ownership (Authorize) or send the grant script before converting.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist results + advance manifest state

# COMMAND ----------

DRY_RUN_DDL = f"""
CREATE TABLE IF NOT EXISTS {dry_run_fqn} (
  full_name STRING, catalog STRING, schema STRING, size_band STRING,
  check_num INT, check_name STRING, status STRING, detail STRING,
  dry_run_state STRING, checked_at TIMESTAMP
) USING DELTA
"""
spark.sql(DRY_RUN_DDL)

if all_check_rows:
    processed = {row["full_name"] for row in all_check_rows}
    # Idempotent for this scope: clear prior rows for processed tables, then append.
    spark.createDataFrame([(fn,) for fn in processed], "full_name STRING").createOrReplaceTempView("_dr_processed")
    spark.sql(f"DELETE FROM {dry_run_fqn} WHERE full_name IN (SELECT full_name FROM _dr_processed)")
    # Explicit schema: a column can be all-NULL (e.g. detail when every check passes),
    # which would break Spark type inference (CANNOT_DETERMINE_TYPE).
    _dr_cols = ["full_name", "catalog", "schema", "size_band", "check_num",
                "check_name", "status", "detail", "dry_run_state", "checked_at"]
    _dr_schema = ("full_name STRING, catalog STRING, schema STRING, size_band STRING, "
                  "check_num INT, check_name STRING, status STRING, detail STRING, "
                  "dry_run_state STRING, checked_at TIMESTAMP")
    (
        spark.createDataFrame(
            [{c: r.get(c) for c in _dr_cols} for r in all_check_rows], schema=_dr_schema)
        .write.mode("append").saveAsTable(dry_run_fqn)
    )

updated = mf.merge_manifest_columns(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, state_updates,
    ["state", "blockers", "updated_at"],
)
print(f"Wrote {len(all_check_rows)} rows to {dry_run_fqn}; advanced state on {updated} tables")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wave-grain freeze-bracket coverage (advisory preview — job_04 re-checks at mutation)
# MAGIC
# MAGIC Dry-run's per-table checks answer "is each table ready". Freeze-bracket coverage
# MAGIC (is every compaction source paused / every attributed stream stopped) is a **wave-grain**
# MAGIC fact, and until now it was evaluated NOWHERE until `job_04_convert` at mutation time — so
# MAGIC the app could only show UNEVALUATED before an attempt. Here we evaluate the SAME two
# MAGIC gates job_04 asserts (`COMPACTION_COVERAGE`, `STREAM_STOP_COVERAGE`) for every wave in the
# MAGIC current plan, via the shared `migration.coverage` helper — identical gap math + reason
# MAGIC text, no second implementation. These rows are **advisory** (`source=03_dry_run`);
# MAGIC `job_04_convert` remains the authoritative, fail-closed evaluation and is unchanged.
# MAGIC Coverage is point-in-time (a stream can start after this runs) — the app shows the source
# MAGIC job and "evaluated N ago", and marks the verdict STALE past a threshold; it never claims a
# MAGIC bare green. `03c_freeze` re-writes these rows right after it freezes so the board flips
# MAGIC without a re-run here.

# COMMAND ----------

try:
    ge.ensure_gate_eval_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
except Exception as _ge_ex:  # noqa: BLE001
    print(f"WARN: could not ensure {ge.GATE_EVAL_TABLE} (coverage preview skipped): {str(_ge_ex)[:200]}")

wave_plan_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.wave_plan"
try:
    _wave_plan_rows = [r.asDict() for r in spark.table(wave_plan_fqn).collect()]
except Exception as _wp_ex:  # noqa: BLE001
    _wave_plan_rows = []
    print(f"coverage preview skipped: {wave_plan_fqn} unavailable — run the wave planner first "
          f"({str(_wp_ex)[:120]}).")

if _wave_plan_rows:
    from collections import defaultdict

    _waves = defaultdict(list)
    for _wr in _wave_plan_rows:
        _waves[(_wr.get("plan_run_id"), _wr.get("wave_id"))].append(_wr)
    _cov_rows = []
    _cov_eval_at = _now()
    _cov_run_id = _job_run_id()
    for _wkey in sorted(_waves, key=lambda k: (str(k[0]), str(k[1]))):
        _cov_rows += cov.evaluate_wave_coverage(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _waves[_wkey],
            evaluated_at=_cov_eval_at, source=cov.SOURCE_DRY_RUN,
            operator=OPERATOR, job_run_id=_cov_run_id)
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _cov_rows)
    print(f"coverage preview: wrote {len(_cov_rows)} advisory gate row(s) for {len(_waves)} "
          f"wave(s) (source={cov.SOURCE_DRY_RUN}; job_04_convert re-checks at mutation).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Human-readable report — grouped by schema and by size band

# COMMAND ----------

report = spark.table(dry_run_fqn)

print("=== Dry-run outcome by schema ===")
(
    report.groupBy("catalog", "schema", "dry_run_state")
    .agg(F.countDistinct("full_name").alias("tables"))
    .orderBy("catalog", "schema", "dry_run_state")
    .show(truncate=False)
)

print("=== Dry-run outcome by size band ===")
(
    report.groupBy("size_band", "dry_run_state")
    .agg(F.countDistinct("full_name").alias("tables"))
    .orderBy("size_band", "dry_run_state")
    .show(truncate=False)
)

print("=== Failed checks (gating) ===")
(
    report.where(F.col("status") == elig.CHECK_FAIL)
    .groupBy("check_name", "detail")
    .agg(F.countDistinct("full_name").alias("tables"))
    .orderBy(F.desc("tables"))
    .show(truncate=False)
)

display(report.orderBy("dry_run_state", "catalog", "schema", "full_name", "check_num"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping: banded outcome counts, FAIL/WARN per check number, the blocker and
# MAGIC reader-readiness mix. Never names, paths, details or identities. Never raises.

# COMMAND ----------

TM.finish(
    tm.OUTCOME_OK if tm.size(state_updates) else tm.OUTCOME_NOOP,
    mode=tm.MODE_DRY_RUN,
    selector="none",
    counts={"targets": tm.size(state_updates), "verified": passed},
    tokens={
        "chk": tm.tally(all_check_rows, fn=lambda c: (
            f"{c['check_num']}.{c['status']}"
            if c["status"] in (elig.CHECK_FAIL, elig.CHECK_WARN) else None)),
        "st": tm.tally(state_updates, "state"),
        "blk": tm.blocker_histogram((s.get("blockers"), 1) for s in state_updates),
        "rdr": reader_counts,
    },
)
