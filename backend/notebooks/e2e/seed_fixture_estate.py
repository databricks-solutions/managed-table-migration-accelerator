# Databricks notebook source
# MAGIC %md
# MAGIC # E2E seed — synthetic FIXTURE ESTATE (dev-only control-plane fixturing)
# MAGIC
# MAGIC Builds a few-hundred-table **synthetic control plane** so the dashboard / app / conductor
# MAGIC surfaces that the real 32-table dev estate cannot exercise all light up:
# MAGIC
# MAGIC 1. Convert-window signals (`predicted_next_write_at`, `convert_window_slack_min`,
# MAGIC    `convert_window_ready=true`) non-null/true for several tables, so `wave_status.ready_now_count`
# MAGIC    is non-zero for several waves.
# MAGIC 2. A completed convert cycle: `audit_log` CONVERT/VALIDATE/CLEANUP events staggered across ~2
# MAGIC    weeks, so the burn-down returns a real curve and run history has content.
# MAGIC 3. At least one FAILED gate of every kind in `wave_gate_evaluation` (COMPACTION_COVERAGE,
# MAGIC    STREAM_STOP_COVERAGE, CAMPAIGN_GO_NOGO, WAVE_TARGET_RESOLUTION) with byte-identical reason text.
# MAGIC 4. Path-access rows added under `fx_*` (the existing proven path-access fixture is **never** touched).
# MAGIC 5. A freeze/unfreeze cycle, so `freeze_ledger_open` has rows.
# MAGIC 6. Open actions across every action kind + phase/blocking "workstream" lens.
# MAGIC 7. BLOCKED / NEEDS_REVIEW `fx_*` tables carrying REAL, comma-delimited blocker codes in
# MAGIC    `table_manifest.blockers` (multi-code rows included), so the app's Blocked & review screen
# MAGIC    and the blockers -> `migration_runbook` join (`item_kind` == code, `source_kind='BLOCKER'`)
# MAGIC    light up. Codes with no precise remediation template exercise the runbook's generic branch.
# MAGIC 8. ONE `migration_next_actions` recommendation pinned to the fixture `plan_run_id`, so the
# MAGIC    Overview directive renders the **normal** (non-stale) path. It is built through the
# MAGIC    conductor's own pure builders over the real rollup views (faithful shape/text) with the
# MAGIC    step + wave chosen deterministically (convert of the pilot wave) — the classifier is NOT
# MAGIC    run over the intentionally-inconsistent synthetic `wave_status`.
# MAGIC
# MAGIC ### What is REAL vs SYNTHETIC (read this)
# MAGIC
# MAGIC * **Synthetic** (this notebook): every `fx_*` control-plane row — manifest rows, the
# MAGIC   convert-window columns, the multi-day audit history, all gate rows, the freeze ledger, the
# MAGIC   open actions, the fixture `wave_plan`, and the single fixture `migration_next_actions`
# MAGIC   recommendation. There are **no backing Delta files** for `fx_*`
# MAGIC   tables — they are control-plane rows only. Verdicts/reasons use the engine's *real* report
# MAGIC   formatters (`gate_eval.*_report`, `conductor.schema_go_decision` text) so they are realistic,
# MAGIC   but the underlying "conversions / freezes / gates" never actually ran.
# MAGIC * **Real** (untouched by this notebook): the existing 32-table e2e estate's `table_manifest` /
# MAGIC   `audit_log` rows and anything produced by driving the real stage jobs; the existing proven
# MAGIC   `path_access_locations` fixture. **Exception:** `wave_plan` + `comms_feed` are FULLY replaced
# MAGIC   (see Safety) — wave_plan must hold a single plan_run_id, so the fixture cannot coexist there.
# MAGIC   The real plan is regenerable by re-running `job_02_wave_planner`.
# MAGIC * **Hybrid** (optional, `seed_real_writes=true`): creates a handful of **real** EXTERNAL Delta
# MAGIC   tables under `fx_real` with >=8 real commits so a subsequent `00_profiler` run proves the live
# MAGIC   `DESCRIBE HISTORY` -> windowing plumbing. NOTE: real commit timestamps are all "now", so the
# MAGIC   live path reliably proves *n_writes>=8 + intervals computed*, but `convert_window_ready=true`
# MAGIC   requires an idle gap > downtime+margin that cannot be faked from fresh commits — that state is
# MAGIC   only reproducible synthetically (bulk path above).
# MAGIC
# MAGIC ### Safety
# MAGIC
# MAGIC Dev-only. Writes ONLY into the control schema's tables. Most tables are scoped to `fx_*`
# MAGIC schemas / the fixture `plan_run_id` and are delete-then-written over that slice, so re-seeding
# MAGIC is idempotent and leaves no stale rows. `wave_plan` + `comms_feed` are the deliberate exception:
# MAGIC they are FULLY replaced (like `job_02_wave_planner`) so wave_plan holds a single plan_run_id —
# MAGIC the real plan is regenerable via `job_02`. Runs no `SET MANAGED`, mutates no user tables, runs
# MAGIC no `VACUUM`. `teardown=true` removes every `fx_*` fixture row (and the fixture plan_run_id)
# MAGIC EXCEPT `path_access_locations` (kept permanent per requirement 4); after teardown, re-run
# MAGIC `job_02` to repopulate `wave_plan`.

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "", "Control-plane catalog (required)")
dbutils.widgets.text("manifest_schema", "migration", "Control-plane schema")
dbutils.widgets.text("table_count", "300", "Approx number of synthetic tables to generate")
dbutils.widgets.text("rng_seed", "42", "Deterministic RNG seed (stable keys across re-runs)")
dbutils.widgets.dropdown("seed_real_writes", "false", ["true", "false"],
                         "Hybrid: also create a few REAL external tables with >=8 commits (needs external_base)")
dbutils.widgets.text("external_base", "", "Writable external-location base for the hybrid real-writes path")
dbutils.widgets.dropdown("teardown", "false", ["true", "false"],
                         "Delete every fx_* fixture row (path_access_locations preserved) and exit")
dbutils.widgets.text("fixture_control_plane", "", "Extra fixture allowlist entry (catalog.schema)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip() or "migration"
TABLE_COUNT = max(32, int(dbutils.widgets.get("table_count") or 300))
RNG_SEED = int(dbutils.widgets.get("rng_seed") or 42)
SEED_REAL_WRITES = dbutils.widgets.get("seed_real_writes").strip().lower() == "true"
EXTERNAL_BASE = dbutils.widgets.get("external_base").strip().rstrip("/")
TEARDOWN = dbutils.widgets.get("teardown").strip().lower() == "true"
FIXTURE_CONTROL_PLANE = dbutils.widgets.get("fixture_control_plane").strip()

if not MANIFEST_CATALOG:
    raise ValueError("manifest_catalog is required (e.g. mtm_e2e_dev)")

# The fixture FULLY REPLACES wave_plan + comms_feed (like job_02_wave_planner), so it is always
# the single current plan — there is no coexisting real plan to sort against, and the canonical
# app rule ("current plan == the one plan_run_id in wave_plan") holds against the fixture. The id
# is DETERMINISTIC from the seed so every other fixture table keys off a stable plan_run_id.
PLAN_RUN_ID = f"fx_seed{RNG_SEED}"

print(f"control plane   = {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}")
print(f"table_count     = {TABLE_COUNT}")
print(f"plan_run_id     = {PLAN_RUN_ID}  (fixture fully replaces wave_plan + comms_feed)")
print(f"rng_seed        = {RNG_SEED}")
print(f"seed_real_writes= {SEED_REAL_WRITES} (external_base={'set' if EXTERNAL_BASE else 'blank -> skipped'})")
print(f"teardown        = {TEARDOWN}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap: put `src/` on the import path (same pattern as the stage notebooks)

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()
        nb_dir = os.path.dirname(nb_path)          # .../notebooks/e2e
        candidates += [
            "/Workspace" + os.path.dirname(os.path.dirname(nb_dir)),  # repo root (parent of notebooks/)
            "/Workspace" + os.path.dirname(nb_dir),
            "/Workspace" + nb_dir,
        ]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError("Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c))


SRC_ROOT = _bootstrap_src()
print(f"src on path: {SRC_ROOT}")

import json
import random
from datetime import datetime, timedelta, timezone

from migration import actions as ax
from migration import audit as au
from migration import bootstrap as bs
from migration import conductor as cd
from migration import eligibility as elig
from migration import executor as ex
from migration import external_writes as ext
from migration import gate_eval as ge
from migration import lineage as lin
from migration import manifest as mf
from migration import source_scan as ss
from migration import windowing as win
from migration.fixture_safety import require_fixture_dev_control_plane

require_fixture_dev_control_plane(MANIFEST_CATALOG, MANIFEST_SCHEMA, FIXTURE_CONTROL_PLANE)

NOW = datetime.now(timezone.utc)
RNG = random.Random(RNG_SEED)
try:
    OPERATOR = dbutils.notebook.entry_point.getDbutils().notebook().getContext().userName().get()
except Exception:
    OPERATOR = "fixture-seeder"

FIXTURE_SOURCE = "e2e_fixture_estate"   # stamped on gate rows / action source for teardown + provenance

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the control plane exists (idempotent — creates every table + view if absent)

# COMMAND ----------

fqns = bs.ensure_control_plane(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
for k, v in fqns.items():
    print(f"  {k:22s} {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Teardown (optional) — remove every fx_* fixture row, PRESERVING path_access_locations

# COMMAND ----------

def _fqn(name: str) -> str:
    return f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.{name}"


def teardown_fixture() -> None:
    # schema-column tables: everything under an fx_ schema.
    for tbl in ("table_manifest", "migration_actions", "freeze_ledger"):
        spark.sql(f"DELETE FROM {_fqn(tbl)} WHERE schema RLIKE '^fx_'")
    # full_name-only table: match the catalog.fx_<schema>.<table> shape.
    spark.sql(f"DELETE FROM {_fqn('audit_log')} WHERE full_name RLIKE '\\\\.fx_'")
    # plan-scoped tables: the fixture plan generation + its single conductor recommendation.
    for tbl in ("wave_plan", "comms_feed", "migration_next_actions"):
        spark.sql(f"DELETE FROM {_fqn(tbl)} WHERE plan_run_id = '{PLAN_RUN_ID}'")
    # gate rows: stamped with the fixture source.
    spark.sql(f"DELETE FROM {_fqn('wave_gate_evaluation')} WHERE source = '{FIXTURE_SOURCE}'")
    # path_access_locations: DELIBERATELY PRESERVED (requirement 4). Not touched here.
    print("teardown complete — fx_* fixture rows removed; path_access_locations left intact.")


if TEARDOWN:
    teardown_fixture()
    dbutils.notebook.exit("fixture teardown complete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Plan the synthetic estate (buckets mirror the wave-planner v2 order)
# MAGIC First-match bucket order: streaming -> compat -> share -> final_large -> pilot -> normal.
# MAGIC Streaming is always isolated into its own wave(s).

# COMMAND ----------

# Byte ranges per synthetic "size class". The size_band string is NEVER hardcoded — it is
# derived from the generated byte count via the SAME production function the profiler uses
# (``eligibility.size_band``), so the fixture can only ever emit canonical bands
# (<=100GB / ~1TB / ~10TB / >10TB). The class names below are internal generator knobs, not
# stored values. ``over10tb`` deliberately crosses the 25 TB wave byte cap (final_large).
GB, TB = elig.GB, elig.TB
_SIZE_CLASSES = {
    "sub100gb": (10 * 2**20, 95 * GB),   # -> "<=100GB"
    "tb1": (150 * GB, 950 * GB),         # -> "~1TB"
    "tb10": (2 * TB, 9 * TB),            # -> "~10TB"
    "over10tb": (26 * TB, 40 * TB),      # -> ">10TB" (also > WAVE_MAX_BYTES = 25 TB)
}

# Canonical band ordering (for max_size_band). Mirrors eligibility.SIZE_BANDS order exactly.
_BAND_RANK = {b["name"]: i for i, b in enumerate(elig.SIZE_BANDS)}


def _sized(size_class: str) -> tuple:
    """Return (bytes, num_files, copy_min, copy_sec, downtime_min) for a size class.

    Every derived figure comes from the production ``eligibility`` estimators so the numeric
    columns match what ``00_profiler`` would compute for a real table of that size.
    """
    lo, hi = _SIZE_CLASSES[size_class]
    b = RNG.randint(lo, hi)
    files = max(1, b // RNG.randint(64 * 2**20, 256 * 2**20))
    return (
        b,
        files,
        elig.estimate_data_copy_minutes(b),
        elig.estimate_data_copy_seconds(b),
        elig.estimate_downtime_minutes(b),
    )


# Bucket sizing, scaled to TABLE_COUNT (normal soaks up the remainder).
_fixed = {
    "streaming": max(8, TABLE_COUNT * 8 // 100),
    "compat": max(6, TABLE_COUNT * 7 // 100),
    "share": max(8, TABLE_COUNT * 8 // 100),
    "final_large": max(4, TABLE_COUNT * 4 // 100),
    "pilot": 15,
}
_fixed["normal"] = max(1, TABLE_COUNT - sum(_fixed.values()))

# Schema per bucket (all under the fx_ namespace so teardown is a clean RLIKE '^fx_').
_BUCKET_SCHEMA = {
    "streaming": "fx_stream",
    "compat": "fx_compat",
    "share": "fx_share",
    "final_large": "fx_final",
    "pilot": "fx_pilot",
    "normal": None,   # spread across fx_bronze / fx_silver / fx_gold
}
_NORMAL_SCHEMAS = ["fx_bronze", "fx_silver", "fx_gold"]

specs: list[dict] = []
seq = 0
for bucket in ("streaming", "compat", "share", "final_large", "pilot", "normal"):
    for i in range(_fixed[bucket]):
        seq += 1
        if bucket == "normal":
            schema = _NORMAL_SCHEMAS[i % len(_NORMAL_SCHEMAS)]
        else:
            schema = _BUCKET_SCHEMA[bucket]
        # Size CLASS per bucket (the stored size_band is derived from bytes below, never here).
        # pilot must stay in the SMALL band and final_large in the LARGE bands so the fixture is
        # internally consistent with lineage.assign_bucket (pilot == "<=100GB"; final_large in
        # {"~10TB", ">10TB"}); otherwise the planner would re-bucket these rows.
        if bucket == "final_large":
            size_class = "over10tb" if i % 2 == 0 else "tb10"
        elif bucket == "pilot":
            size_class = "sub100gb"
        else:
            size_class = RNG.choice(["sub100gb", "sub100gb", "sub100gb", "tb1", "tb1", "tb10"])
        b, files, cmin, csec, downtime = _sized(size_class)
        band = elig.size_band(b)   # canonical band, straight from the production classifier
        table_name = f"{bucket}_{i:03d}"
        specs.append({
            "bucket": bucket,
            "catalog": MANIFEST_CATALOG,
            "schema": schema,
            "table_name": table_name,
            "full_name": mf.plain_full_name(MANIFEST_CATALOG, schema, table_name),
            "size_band": band,
            "table_size_bytes": b,
            "num_files": files,
            "est_data_copy_minutes": cmin,
            "est_data_copy_seconds": csec,
            "est_downtime_minutes": downtime,
            "recommended_cores": elig.recommended_cores(b),
            "recommended_warehouse": elig.recommended_warehouse(b),
            "exceeds_cap": b > lin.WAVE_MAX_BYTES,
            "is_streaming": bucket == "streaming",
            "needs_compat": bucket == "compat",
            "is_shared": bucket == "share",
        })

print(f"generated {len(specs)} table specs across buckets: "
      + ", ".join(f"{k}={v}" for k, v in _fixed.items()))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Assign waves (streaming isolated; non-streaming grouped by catalog.schema)

# COMMAND ----------

# Wave order mirrors the bucket order. Streaming gets its own isolated wave.
_WAVE_ORDER = ["streaming", "compat", "share", "final_large", "pilot", "normal"]
waves: dict[str, dict] = {}   # wave_id -> {order, bucket, group_key, members[]}


def _wave_id(order: int, bucket: str, group: str) -> str:
    # Mirror the REAL planner (lineage.sequence_waves): a plain `wave_NN` id, with the
    # plan_run_id kept in its OWN column — never composed into wave_id. bucket/group stay
    # human-readable via wave_label. (Was `{PLAN_RUN_ID}::w{NN}_{bucket}_{group}`, a
    # fixture-only shape the planner never emits; keeping it made the fixture teach a false
    # "wave_id embeds plan_run_id" model. See docs/contracts/control-tables.md wave_id.)
    return f"wave_{order:02d}"


order = 0
for bucket in _WAVE_ORDER:
    members = [s for s in specs if s["bucket"] == bucket]
    if not members:
        continue
    # Group non-streaming by catalog.schema (streaming stays one isolated wave for the whole bucket).
    if bucket == "streaming":
        groups = {f"{MANIFEST_CATALOG}.{_BUCKET_SCHEMA['streaming']}": members}
    else:
        groups = {}
        for s in members:
            groups.setdefault(f"{s['catalog']}.{s['schema']}", []).append(s)
    for group_key, gmembers in groups.items():
        order += 1
        wid = _wave_id(order, bucket, group_key)
        waves[wid] = {"order": order, "bucket": bucket, "group_key": group_key, "members": gmembers}
        for s in gmembers:
            s["wave_id"] = wid
            s["wave_order"] = order
            s["group_key"] = group_key

print(f"assigned {len(waves)} waves:")
for wid, w in sorted(waves.items(), key=lambda kv: kv[1]["order"]):
    print(f"  w{w['order']:02d} {w['bucket']:12s} {w['group_key']:45s} n={len(w['members'])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Choose the "ready-now" and "already-converted" subsets
# MAGIC Ready-now tables spread across several normal/pilot waves -> several waves get ready_now_count>0.
# MAGIC Converted tables get terminal state + multi-day audit history -> a real burn-down curve.

# COMMAND ----------

_normal_pilot = [s for s in specs if s["bucket"] in ("normal", "pilot")]
RNG.shuffle(_normal_pilot)

# ~15% ready-now (convert_window_ready=true), spread across waves.
n_ready = max(6, len(_normal_pilot) * 15 // 100)
ready_set = {s["full_name"] for s in _normal_pilot[:n_ready]}

# ~18% already converted (terminal state + audit history). Disjoint from ready_set.
rest = [s for s in _normal_pilot[n_ready:] if s["full_name"] not in ready_set]
n_converted = max(30, len(_normal_pilot) * 18 // 100)
converted_specs = rest[:n_converted]
converted_set = {s["full_name"] for s in converted_specs}

# A "medium" window group: predicted + slack non-null but ready=false (irregular/active writer).
n_medium = max(6, len(rest) * 10 // 100)
medium_set = {s["full_name"] for s in rest[n_converted:n_converted + n_medium]}

print(f"ready-now (convert_window_ready=true) : {len(ready_set)}")
print(f"medium-window (predicted+slack, ready=false): {len(medium_set)}")
print(f"already-converted (terminal state + audit) : {len(converted_set)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1) Manifest rows (base columns) via `manifest.build_manifest_row` + idempotent MERGE

# COMMAND ----------

_ELIGIBILITY_CHOICES = ["ELIGIBLE", "ELIGIBLE", "ELIGIBLE", "NEEDS_REVIEW", "BLOCKED"]

# Random BLOCKED tables carry a REAL blocker code (the manifest serialization is a comma-
# delimited list of codes from the eligibility/locations/external_writes vocabulary — NOT prose,
# which would explode into invalid migration_runbook.item_kind values). Single-code here; the
# deterministic slice below adds the multi-code + generic-remediation cases.
_RANDOM_BLOCKER_CODES = [
    "runtime_below_17_2_uniform",
    "path_based_access_must_be_fixed_first",
    "no_managed_location",
]

# 7) Deterministic BLOCKED / NEEDS_REVIEW slice with REAL, comma-delimited blocker codes so the
# app's "Blocked & review" screen and the blockers -> migration_runbook join (item_kind == code,
# source_kind='BLOCKER') are actually exercised. Includes multi-code rows (the runbook view's
# explode) and codes with NO precise remediation template (external_writer_blocks_managed,
# non_delta_format) that fall to migration_runbook's generic remediation branch. NOTE: a blocker
# code with NO runbook row is not reproducible from the fixture — migration_runbook derives its
# BLOCKER rows from this same CSV with a generic non-null ELSE, so every code yields a row.
_BLOCKED_FIXTURE = [
    ("BLOCKED", au.STATE_BLOCKED, "no_managed_location"),
    ("BLOCKED", au.STATE_BLOCKED, "runtime_below_17_2_uniform"),
    ("BLOCKED", au.STATE_BLOCKED, "path_based_access_must_be_fixed_first,runtime_below_17_2_uniform"),
    ("BLOCKED", au.STATE_BLOCKED, "corrupted_or_inaccessible,external_writer_blocks_managed,no_managed_location"),
    ("BLOCKED", au.STATE_BLOCKED, "external_writer_blocks_managed"),
    ("NEEDS_REVIEW", au.STATE_ASSESSED, "non_delta_format"),
    ("NEEDS_REVIEW", au.STATE_ASSESSED, "managed_location_unresolved"),
    ("NEEDS_REVIEW", au.STATE_ASSESSED, "non_delta_format,managed_location_unresolved"),
    # Assess-time path-access hold-back: path-based access downgrades to NEEDS_REVIEW
    # (token path_based_access_blocks_managed), not BLOCKED, so the app's hold-back path is exercised.
    ("NEEDS_REVIEW", au.STATE_ASSESSED, "path_based_access_blocks_managed"),
]
_blk_candidates = [
    s for s in specs
    if s["bucket"] == "normal"
    and s["full_name"] not in ready_set
    and s["full_name"] not in medium_set
    and s["full_name"] not in converted_set
][-len(_BLOCKED_FIXTURE):]
forced_blockers = {s["full_name"]: _BLOCKED_FIXTURE[i] for i, s in enumerate(_blk_candidates)}
print(f"deterministic blocked/review slice: {len(forced_blockers)} fx_ tables carrying real blocker codes")

manifest_rows = []
for s in specs:
    fn = s["full_name"]
    if fn in forced_blockers:
        eligibility, state, blockers = forced_blockers[fn]
    elif fn in converted_set:
        state = RNG.choice([au.STATE_CONVERTED, au.STATE_VALIDATED, au.STATE_VALIDATED, au.STATE_CLEANED])
        eligibility = "ELIGIBLE"
        blockers = None
    else:
        eligibility = RNG.choice(_ELIGIBILITY_CHOICES)
        if eligibility == "BLOCKED":
            state = au.STATE_BLOCKED
        elif eligibility == "ELIGIBLE" and fn in ready_set:
            state = au.STATE_DRY_RUN_OK
        else:
            state = RNG.choice([au.STATE_DISCOVERED, au.STATE_ASSESSED, au.STATE_ELIGIBLE])
        blockers = RNG.choice(_RANDOM_BLOCKER_CODES) if state == au.STATE_BLOCKED else None
    manifest_rows.append(mf.build_manifest_row(
        catalog=s["catalog"], schema=s["schema"], table_name=s["table_name"],
        table_type="EXTERNAL", data_source_format="DELTA",
        storage_location=f"s3://fixture-estate/{s['schema']}/{s['table_name']}",
        table_size_bytes=s["table_size_bytes"], num_files=s["num_files"],
        created=NOW - timedelta(days=RNG.randint(120, 900)),
        last_altered=NOW - timedelta(days=RNG.randint(0, 40)),
        uniform_iceberg_enabled=(s["bucket"] == "compat" and RNG.random() < 0.3),
        min_reader_version=RNG.choice([1, 2]), min_writer_version=RNG.choice([2, 5, 7]),
        is_delta_shared=s["is_shared"],
        eligibility=eligibility, blockers=blockers,
        size_band=s["size_band"], est_data_copy_minutes=s["est_data_copy_minutes"],
        est_data_copy_seconds=s["est_data_copy_seconds"], est_downtime_minutes=s["est_downtime_minutes"],
        state=state, assessed_at=NOW - timedelta(days=RNG.randint(1, 30)), updated_at=NOW,
    ))

# Replace the fx_ manifest slice wholesale (mirrors teardown's predicate) so a re-seed with a
# different table_count/seed cannot leave stale fx_ rows behind. merge_manifest_rows is an
# UPSERT on full_name, so without this a shrink would strand orphans and inflate every fx_ count.
spark.sql(f"DELETE FROM {_fqn('table_manifest')} WHERE schema RLIKE '^fx_'")
n = mf.merge_manifest_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, manifest_rows)
print(f"merged {n} manifest rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1b) Enrich deferred + windowing columns via UPDATE-only MERGE
# MAGIC Sets the streaming/compat/path/consumer signals AND the convert-window signals
# MAGIC (`predicted_next_write_at`, `convert_window_slack_min`, `convert_window_ready`).

# COMMAND ----------

_WINDOW_COLS = [
    "has_streaming_writer", "needs_compatibility_mode", "has_path_based_access",
    "has_external_non_databricks_writer", "downstream_consumer_count", "path_access_entities",
    "last_write_at", "last_read_at", "write_count", "read_count", "recency_scanned_at",
    "write_interval_p50_s", "write_interval_p90_s", "predicted_next_write_at",
    "convert_window_ready", "convert_window_slack_min", "convert_window_confidence",
    "window_scanned_at", "convert_window_notes",
]

# Path-access fixture tables (a slice of normal tables get name-based remediation actions).
path_fix_specs = [s for s in specs if s["bucket"] == "normal"][:8]
path_fix_set = {s["full_name"] for s in path_fix_specs}

enrich_rows = []
for s in specs:
    fn = s["full_name"]
    row = {"full_name": fn}
    # Deferred access/streaming/compat signals.
    row["has_streaming_writer"] = s["is_streaming"]
    row["needs_compatibility_mode"] = s["needs_compat"]
    row["has_external_non_databricks_writer"] = s["needs_compat"]
    row["has_path_based_access"] = fn in path_fix_set
    row["downstream_consumer_count"] = RNG.randint(0, 25)
    # Canonical contract shape: JSON array of {created_by, entity_id, entity_type, role} with
    # sorted keys, produced by the SAME serializer the profiler uses (external_writes). role is
    # WRITE/READ; entity_type is drawn from the real lineage vocabulary (path-based access is a
    # strong external-writer signal, so external engines feature alongside Databricks entities).
    row["path_access_entities"] = (
        ext.serialize_path_access_entities([
            {
                "entity_type": RNG.choice(
                    ["FLINK", "TRINO", "SPARK_CONNECT_EXTERNAL", "JOB", "NOTEBOOK"]),
                "entity_id": str(RNG.randint(100000, 999999)),
                "created_by": f"svc-{s['table_name']}@example.com",
                "role": RNG.choice([ext.ROLE_WRITE, ext.ROLE_READ]),
            }
            for _ in range(RNG.randint(1, 3))
        ]) if fn in path_fix_set else None
    )
    # Recency (make tables look alive).
    row["last_write_at"] = NOW - timedelta(hours=RNG.randint(1, 240))
    row["last_read_at"] = NOW - timedelta(hours=RNG.randint(1, 72))
    row["write_count"] = RNG.randint(8, 5000)
    row["read_count"] = RNG.randint(0, 20000)
    row["recency_scanned_at"] = NOW
    row["window_scanned_at"] = NOW

    if fn in ready_set:
        # Regular writer with an idle gap wide open -> ready to convert now.
        p50 = float(RNG.randint(3600, 7200))
        p90 = float(RNG.randint(9000, 21600))         # p90 >> downtime+margin
        row["write_interval_p50_s"] = p50
        row["write_interval_p90_s"] = p90
        row["predicted_next_write_at"] = NOW + timedelta(seconds=p90)
        row["convert_window_ready"] = True
        row["convert_window_slack_min"] = round(RNG.uniform(15, 180), 1)
        row["convert_window_confidence"] = win.CONFIDENCE_HIGH
        row["convert_window_notes"] = win.NOTE_READY   # "idle_window_open"
    elif fn in medium_set:
        # Enough history + a prediction, but not ready (slack <= 0 / irregular). Both notes are
        # canonical windowing tokens: irregular cadence vs a recent write / short idle gap.
        p50 = float(RNG.randint(600, 1800))
        p90 = float(RNG.randint(1800, 3000))
        row["write_interval_p50_s"] = p50
        row["write_interval_p90_s"] = p90
        row["predicted_next_write_at"] = NOW + timedelta(seconds=p90)
        row["convert_window_ready"] = False
        row["convert_window_slack_min"] = round(RNG.uniform(-45, -1), 1)
        row["convert_window_confidence"] = win.CONFIDENCE_MEDIUM
        row["convert_window_notes"] = RNG.choice([win.NOTE_IRREGULAR, win.NOTE_ACTIVE])
    else:
        # Insufficient history -> nulls + low confidence (the dashboard omits these).
        row["write_interval_p50_s"] = None
        row["write_interval_p90_s"] = None
        row["predicted_next_write_at"] = None
        row["convert_window_ready"] = False
        row["convert_window_slack_min"] = None
        row["convert_window_confidence"] = win.CONFIDENCE_LOW
        row["convert_window_notes"] = win.NOTE_INSUFFICIENT_WRITES   # "insufficient_write_history"
    enrich_rows.append(row)

n = mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, enrich_rows, _WINDOW_COLS)
print(f"enriched {n} manifest rows (deferred + windowing columns)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2) wave_plan + comms_feed (the fixture plan generation)

# COMMAND ----------

# FULL REPLACE (mirrors job_02_wave_planner, which rewrites the entire wave_plan on every run).
# wave_plan must hold exactly ONE plan_run_id: the app's canonical rule treats the sole plan_run_id
# in wave_plan as the current plan and fails closed on a multi-plan snapshot. Scoping the delete to
# the fixture plan would leave the real plan coexisting and produce exactly that invalid state, so
# the fixture replaces the whole table. The real 32-table plan is regenerable via job_02 (nothing is
# permanently lost); comms_feed is 1:1 with wave_plan and is replaced in lockstep.
spark.sql(f"DELETE FROM {_fqn('wave_plan')}")
spark.sql(f"DELETE FROM {_fqn('comms_feed')}")

wave_rows = []
comms_rows = []
for wid, w in waves.items():
    members = w["members"]
    total_bytes = sum(m["table_size_bytes"] for m in members)
    # Canonical band ordering (never the legacy XS<S<M<L<XL assumption).
    max_band = max((m["size_band"] for m in members), key=lambda b: _BAND_RANK.get(b, -1))
    # Wave-level compute recommendation reflects the largest member (production-style).
    wave_bytes_max = max(m["table_size_bytes"] for m in members)
    wave_cores = elig.recommended_cores(wave_bytes_max)
    wave_warehouse = elig.recommended_warehouse(wave_bytes_max)
    planned_window = lin.wave_window_hint(w["bucket"])
    for i, s in enumerate(members):
        wave_rows.append(lin.build_wave_plan_row(
            full_name=s["full_name"], catalog=s["catalog"], schema=s["schema"],
            plan_run_id=PLAN_RUN_ID, planned_at=NOW, wave_id=wid, wave_order=w["order"],
            wave_label=f"Wave {w['order']:02d} — {w['bucket']}", group_key=w["group_key"],
            wave_seq_in_group=1, wave_count_in_group=1, bucket=w["bucket"],
            blast_radius=round(RNG.uniform(0, 1), 3),
            downstream_consumer_count=RNG.randint(0, 25),
            size_band=s["size_band"], table_size_bytes=s["table_size_bytes"],
            est_data_copy_minutes=s["est_data_copy_minutes"], est_downtime_minutes=s["est_downtime_minutes"],
            planned_table_count=len(members), planned_total_bytes=total_bytes,
            recommended_cores=wave_cores, recommended_warehouse=wave_warehouse,
            max_size_band=max_band, exceeds_wave_byte_cap=s["exceeds_cap"],
            has_streaming=s["is_streaming"], has_external_consumer=s["needs_compat"],
            is_delta_shared=s["is_shared"], needs_compatibility_mode=s["needs_compat"],
            cross_region_risk=False, planned_window=planned_window, updated_at=NOW,
        ))
        comms_rows.append(lin.build_comms_feed_row(
            plan_run_id=PLAN_RUN_ID, wave_id=wid, wave_order=w["order"],
            wave_label=f"Wave {w['order']:02d} — {w['bucket']}", full_name=s["full_name"],
            owners=f"team-{s['schema']}", downstream_consumers=None,
            downstream_consumer_count=RNG.randint(0, 25), blast_radius=round(RNG.uniform(0, 1), 3),
            est_downtime_minutes=s["est_downtime_minutes"],
            recommended_cores=wave_cores, recommended_warehouse=wave_warehouse,
            max_size_band=max_band, planned_window=planned_window, updated_at=NOW,
        ))

spark.createDataFrame(wave_rows, schema=lin.wave_plan_struct_type()) \
    .write.mode("append").saveAsTable(_fqn("wave_plan"))
spark.createDataFrame(comms_rows, schema=lin.comms_feed_struct_type()) \
    .write.mode("append").saveAsTable(_fqn("comms_feed"))
print(f"wrote {len(wave_rows)} wave_plan rows + {len(comms_rows)} comms_feed rows for {PLAN_RUN_ID}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3) audit_log — completed convert cycle staggered across ~14 days (burn-down + run history)

# COMMAND ----------

spark.sql(f"DELETE FROM {_fqn('audit_log')} WHERE full_name RLIKE '\\\\.fx_'")

audit_rows = []
convert_success_dates = set()   # distinct calendar dates a CONVERT/SUCCESS actually lands on
span_days = 14
for idx, s in enumerate(converted_specs):
    # Spread conversions across the window so end_time forms a real per-day curve.
    day = idx * span_days // max(1, len(converted_specs))
    t0 = NOW - timedelta(days=span_days - day, hours=RNG.randint(0, 20), minutes=RNG.randint(0, 59))
    dur = timedelta(minutes=RNG.uniform(1, 45))
    convert_success_dates.add((t0 + dur).date())
    # recommended_cores is band-derived (32/64/256), actual_cores is whatever the run used, and
    # cluster_size_status is the SAME advisory verdict production stamps (INFO/WARN — never the
    # invented OK/UNDERSIZED). A few actuals fall below the recommendation to exercise WARN.
    rec_cores = elig.recommended_cores(s["table_size_bytes"])
    act_cores = RNG.choice([8, 16, 32, 64, 256])
    common = dict(full_name=s["full_name"], operator=OPERATOR, runtime_version="17.2",
                  plan_run_id=PLAN_RUN_ID, wave_id=s.get("wave_id"),
                  actual_cores=act_cores, recommended_cores=rec_cores,
                  cluster_size_status=elig.cluster_size_status(act_cores, rec_cores)[0])
    audit_rows.append(au.build_audit_row(action=au.ACTION_CONVERT, phase=au.PHASE_STARTED,
                                         from_state=au.STATE_DRY_RUN_OK, to_state=au.STATE_CONVERTING,
                                         sql_command=f"ALTER TABLE {s['full_name']} SET MANAGED;",
                                         start_time=t0, created_at=t0, **common))
    audit_rows.append(au.build_audit_row(action=au.ACTION_CONVERT, phase=au.PHASE_SUCCESS,
                                         from_state=au.STATE_CONVERTING, to_state=au.STATE_CONVERTED,
                                         sql_command=f"ALTER TABLE {s['full_name']} SET MANAGED;",
                                         pre_convert_version=RNG.randint(5, 500),
                                         pre_convert_num_files=s["num_files"],
                                         pre_convert_size_bytes=s["table_size_bytes"],
                                         start_time=t0, end_time=t0 + dur, created_at=t0 + dur, **common))
    # Most converted tables also validate; a few later get cleaned (day-14 VACUUM window).
    tv = t0 + dur + timedelta(minutes=RNG.uniform(2, 30))
    audit_rows.append(au.build_audit_row(action=au.ACTION_VALIDATE, phase=au.PHASE_SUCCESS,
                                         from_state=au.STATE_CONVERTED, to_state=au.STATE_VALIDATED,
                                         start_time=tv, end_time=tv, created_at=tv, **common))

# Exact fixture-generated conversion metrics (fx_ only). One CONVERT/SUCCESS per converted table.
expected_convert_success = len(converted_specs)
expected_convert_days = len(convert_success_dates)
n = au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
print(f"appended {n} audit_log rows: {expected_convert_success} CONVERT/SUCCESS across "
      f"{len(converted_specs)} fx_ tables, landing on {expected_convert_days} distinct calendar "
      f"day(s) inside a {span_days}-day window (end-of-run offsets can spill one day past the window)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4) wave_gate_evaluation — one FAILED gate of EVERY kind (+ some PASS), real reason text

# COMMAND ----------

spark.sql(f"DELETE FROM {_fqn('wave_gate_evaluation')} WHERE source = '{FIXTURE_SOURCE}'")

# Pick representative waves to attach gate verdicts to.
_by_bucket = {}
for wid, w in waves.items():
    _by_bucket.setdefault(w["bucket"], (wid, w))
stream_wid, stream_w = _by_bucket.get("streaming", next(iter(waves.items())))
norm_wid, norm_w = _by_bucket.get("normal", next(iter(waves.items())))
compat_wid, compat_w = _by_bucket.get("compat", (norm_wid, norm_w))

gate_rows = []

# --- COMPACTION_COVERAGE = FAIL (uncovered JOB + schema PO), using the real formatter ---
comp_gaps = [
    {"unit": f"JOB:{RNG.randint(100000, 999999)}",
     "remediation_api": "jobs.update_job(job_id=..., new_settings=JobSettings(schedule=PauseStatus.PAUSED))",
     "tables": [m["full_name"] for m in norm_w["members"][:3]]},
    {"unit": f"PREDICTIVE_OPTIMIZATION:{norm_w['group_key']}",
     "remediation_sql": f"ALTER SCHEMA {norm_w['group_key']} DISABLE PREDICTIVE OPTIMIZATION;",
     "tables": []},
]
comp_reason = "\n".join(ge.compaction_gap_report(comp_gaps))
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_COMPACTION_COVERAGE, evaluated_at=NOW - timedelta(hours=6),
    verdict=ge.VERDICT_FAIL, passed=False, reason=comp_reason,
    plan_run_id=PLAN_RUN_ID, wave_id=norm_wid, group_key=norm_w["group_key"],
    catalog=MANIFEST_CATALOG, schema=norm_w["group_key"].split(".")[-1],
    item_count=len(comp_gaps), detail=json.dumps(comp_gaps),
    source=FIXTURE_SOURCE, operator=OPERATOR))

# --- STREAM_STOP_COVERAGE = FAIL (attributed streams not stopped) + unattributed CAUTION ---
stream_gaps = [
    {"unit": f"PIPELINE:{RNG.randint(100000, 999999)}",
     "remediation_api": "pipelines.stop(pipeline_id=...)",
     "tables": [m["full_name"] for m in stream_w["members"][:2]]},
    {"unit": f"JOB:{RNG.randint(100000, 999999)}",
     "remediation_api": "jobs.cancel_all_runs(job_id=...)",
     "tables": [stream_w["members"][2]["full_name"]] if len(stream_w["members"]) > 2 else []},
]
stream_reason_lines = [ge.unattributed_caution([m["full_name"] for m in stream_w["members"][3:5]])]
stream_reason_lines += ge.stream_gap_report(stream_gaps)
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_STREAM_STOP_COVERAGE, evaluated_at=NOW - timedelta(hours=5),
    verdict=ge.VERDICT_FAIL, passed=False, reason="\n".join(stream_reason_lines),
    plan_run_id=PLAN_RUN_ID, wave_id=stream_wid, group_key=stream_w["group_key"],
    catalog=MANIFEST_CATALOG, schema=_BUCKET_SCHEMA["streaming"],
    item_count=len(stream_gaps), detail=json.dumps(stream_gaps),
    include_streaming=True, source=FIXTURE_SOURCE, operator=OPERATOR))

# --- CAMPAIGN_GO_NOGO = NO-GO (reason mirrors conductor.schema_go_decision text) ---
nogo_reason = f"blocked tables in {norm_w['group_key']} — fix blockers before converting"
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_CAMPAIGN_GO_NOGO, evaluated_at=NOW - timedelta(hours=4),
    verdict=ge.VERDICT_NOGO, passed=False, reason=nogo_reason,
    plan_run_id=PLAN_RUN_ID, wave_id=None, group_key=norm_w["group_key"],
    catalog=MANIFEST_CATALOG, schema=norm_w["group_key"].split(".")[-1],
    item_count=0, include_streaming=False, source=FIXTURE_SOURCE, operator=OPERATOR))

# --- WAVE_TARGET_RESOLUTION = EMPTY (a selector that matched no runnable wave) ---
empty_reason = "\n".join(ge.wave_target_report([], 0))
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_WAVE_TARGET_RESOLUTION, evaluated_at=NOW - timedelta(hours=3),
    verdict=ge.VERDICT_EMPTY, passed=False, reason=empty_reason,
    plan_run_id=PLAN_RUN_ID, wave_id=None, selector="bucket=share,include_streaming=false",
    item_count=0, include_streaming=False, source=FIXTURE_SOURCE, operator=OPERATOR))

# --- A few PASS/RESOLVED/GO rows for contrast (latest-view shows a healthy + failing mix) ---
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_COMPACTION_COVERAGE, evaluated_at=NOW - timedelta(hours=2),
    verdict=ge.VERDICT_PASS, passed=True,
    reason="All discovered schedulable compaction sources are covered by a FROZEN freeze_ledger row.",
    plan_run_id=PLAN_RUN_ID, wave_id=compat_wid, group_key=compat_w["group_key"],
    catalog=MANIFEST_CATALOG, schema=_BUCKET_SCHEMA["compat"], item_count=0,
    source=FIXTURE_SOURCE, operator=OPERATOR))
resolved_targets = [{"wave_id": norm_wid, "plan_run_id": PLAN_RUN_ID}]
gate_rows.append(ge.build_gate_eval_row(
    gate_kind=ge.GATE_WAVE_TARGET_RESOLUTION, evaluated_at=NOW - timedelta(hours=1),
    verdict=ge.VERDICT_RESOLVED, passed=True,
    reason="\n".join(ge.wave_target_report(resolved_targets, 12 * 2**40)),
    plan_run_id=PLAN_RUN_ID, wave_id=norm_wid, selector=f"target_schema={norm_w['group_key']}",
    item_count=1, include_streaming=False, source=FIXTURE_SOURCE, operator=OPERATOR))

n = ge.append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, gate_rows)
print(f"appended {n} wave_gate_evaluation rows "
      f"(FAIL: COMPACTION/STREAM, NO-GO: CAMPAIGN, EMPTY: WAVE_TARGET, + PASS/RESOLVED contrast)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5) freeze_ledger — a freeze/unfreeze cycle (open FROZEN/PLANNED + completed UNFROZEN)

# COMMAND ----------

def _op(op_kind, *, scope, catalog=None, schema=None, table_name=None, full_name=None,
        target_kind=ax.TARGET_NONE, target_id=None):
    ref = ax.scope_ref(scope, full_name=full_name, catalog=catalog, schema=schema)
    return {
        "op_kind": op_kind, "ledger_key": ex.ledger_key(op_kind, ref, target_id),
        "action_key": None, "action_id": None, "scope": scope, "catalog": catalog,
        "schema": schema, "table_name": table_name, "full_name": full_name,
        "target_kind": target_kind, "target_id": target_id,
        "plan_run_id": PLAN_RUN_ID, "wave_id": None,
    }


ledger_rows = []
# Open FROZEN: a paused compaction JOB (covers a normal wave) + schema PO disabled.
for i, s in enumerate(norm_w["members"][:2]):
    op = _op(ex.OP_PAUSE_JOB, scope=ax.SCOPE_TABLE, catalog=s["catalog"], schema=s["schema"],
             table_name=s["table_name"], full_name=s["full_name"],
             target_kind=ax.TARGET_JOB, target_id=str(RNG.randint(100000, 999999)))
    r = ex.build_ledger_row(op, status=ex.LEDGER_FROZEN, dry_run=False,
                            prior_pause_status=ex.PAUSE_UNPAUSED, frozen_at=NOW - timedelta(hours=8),
                            frozen_by=OPERATOR, freeze_run_id="fixture", updated_at=NOW,
                            evidence="fixture: job schedule paused")
    ledger_rows.append(r)
po_op = _op(ex.OP_DISABLE_PO, scope=ax.SCOPE_SCHEMA, catalog=MANIFEST_CATALOG,
            schema=norm_w["group_key"].split(".")[-1])
ledger_rows.append(ex.build_ledger_row(po_op, status=ex.LEDGER_FROZEN, dry_run=False,
                                       prior_po_enabled=True, frozen_at=NOW - timedelta(hours=8),
                                       frozen_by=OPERATOR, freeze_run_id="fixture", updated_at=NOW,
                                       evidence="fixture: predictive optimization disabled"))

# Open PLANNED (dry-run preview): a pipeline stop for a streaming wave.
for s in stream_w["members"][:2]:
    op = _op(ex.OP_STOP_PIPELINE, scope=ax.SCOPE_TABLE, catalog=s["catalog"], schema=s["schema"],
             table_name=s["table_name"], full_name=s["full_name"],
             target_kind=ax.TARGET_PIPELINE, target_id=str(RNG.randint(100000, 999999)))
    ledger_rows.append(ex.build_ledger_row(op, status=ex.LEDGER_PLANNED, dry_run=True,
                                           trigger_type="CONTINUOUS", updated_at=NOW,
                                           evidence="fixture: dry-run plan — pipelines.stop(...)"))

# Completed cycle (UNFROZEN): a stream job that was stopped then restarted after convert.
for s in stream_w["members"][2:4]:
    op = _op(ex.OP_STOP_STREAM_JOB, scope=ax.SCOPE_TABLE, catalog=s["catalog"], schema=s["schema"],
             table_name=s["table_name"], full_name=s["full_name"],
             target_kind=ax.TARGET_JOB, target_id=str(RNG.randint(100000, 999999)))
    r = ex.build_ledger_row(op, status=ex.LEDGER_UNFROZEN, dry_run=False,
                            prior_pause_status=ex.PAUSE_UNPAUSED, frozen_at=NOW - timedelta(hours=9),
                            frozen_by=OPERATOR, freeze_run_id="fixture", updated_at=NOW,
                            evidence="fixture: stopped then restarted from checkpoint")
    r["unfrozen_at"] = NOW - timedelta(hours=6)
    r["unfrozen_by"] = OPERATOR
    ledger_rows.append(r)

# One FAILED op (surfaces in freeze_ledger_open sorted first).
fail_op = _op(ex.OP_PAUSE_JOB, scope=ax.SCOPE_TABLE, catalog=norm_w["members"][2]["catalog"],
              schema=norm_w["members"][2]["schema"], table_name=norm_w["members"][2]["table_name"],
              full_name=norm_w["members"][2]["full_name"], target_kind=ax.TARGET_JOB,
              target_id=str(RNG.randint(100000, 999999))) if len(norm_w["members"]) > 2 else None
if fail_op:
    ledger_rows.append(ex.build_ledger_row(fail_op, status=ex.LEDGER_FAILED, dry_run=False,
                                           frozen_by=OPERATOR, freeze_run_id="fixture", updated_at=NOW,
                                           notes="fixture: pause API returned 403 (permissions) — surfaced, not retried"))

# Replace the fx_ ledger slice wholesale (mirrors teardown) so re-seeding with a different seed
# — which changes the RNG target_ids and therefore ledger_key — cannot accumulate stale rows.
spark.sql(f"DELETE FROM {_fqn('freeze_ledger')} WHERE schema RLIKE '^fx_'")
n = ex.merge_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ledger_rows)
print(f"merged {n} freeze_ledger rows (FROZEN + PLANNED + UNFROZEN + FAILED)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6) migration_actions — open actions across every kind + phase/blocking lens

# COMMAND ----------

action_rows = []


def _add_action(kind, scope, phase, title, *, s=None, group=None, blocking=False,
                target_kind=ax.TARGET_NONE, target_id=None, status=ax.STATUS_OPEN,
                remediation_sql=None, remediation_api=None, detail=None, wave_id=None):
    if s is not None:
        cat, sch, tbl, fn = s["catalog"], s["schema"], s["table_name"], s["full_name"]
    elif group is not None:
        cat, sch, tbl, fn = MANIFEST_CATALOG, group.split(".")[-1], None, None
    else:
        cat = sch = tbl = fn = None
    action_rows.append(ax.build_action_row(
        action_kind=kind, scope=scope, phase=phase, title=title,
        catalog=cat, schema=sch, table_name=tbl, full_name=fn,
        target_kind=target_kind, target_id=target_id, blocking=blocking,
        detail=detail, remediation_sql=remediation_sql, remediation_api=remediation_api,
        owner=f"team-{sch}" if sch else None, confidence=ax.CONF_HIGH, source=FIXTURE_SOURCE,
        plan_run_id=PLAN_RUN_ID, wave_id=wave_id, status=status,
        detected_at=NOW - timedelta(hours=RNG.randint(1, 48)), updated_at=NOW,
    ))


# PRE_CONVERT freeze/stop (blocking) + POST_CONVERT restore partners.
for s in norm_w["members"][:4]:
    _add_action(ax.KIND_FREEZE_COMPACTION, ax.SCOPE_TABLE, ax.PHASE_PRE_CONVERT,
                f"Pause compaction JOB on {s['table_name']}", s=s, blocking=True,
                target_kind=ax.TARGET_JOB, target_id=str(RNG.randint(100000, 999999)),
                remediation_api="jobs.update_job(..., PauseStatus.PAUSED)", wave_id=norm_wid)
    _add_action(ax.KIND_UNFREEZE_COMPACTION, ax.SCOPE_TABLE, ax.PHASE_POST_CONVERT,
                f"Resume compaction JOB on {s['table_name']}", s=s, blocking=False,
                target_kind=ax.TARGET_JOB, remediation_api="jobs.update_job(..., PauseStatus.UNPAUSED)",
                wave_id=norm_wid)
_add_action(ax.KIND_FREEZE_COMPACTION, ax.SCOPE_SCHEMA, ax.PHASE_PRE_CONVERT,
            f"Disable Predictive Optimization on {norm_w['group_key']}", group=norm_w["group_key"],
            blocking=True, remediation_sql=f"ALTER SCHEMA {norm_w['group_key']} DISABLE PREDICTIVE OPTIMIZATION;",
            wave_id=norm_wid)

for s in stream_w["members"][:4]:
    _add_action(ax.KIND_STOP_STREAM, ax.SCOPE_TABLE, ax.PHASE_PRE_CONVERT,
                f"Stop streaming writer on {s['table_name']}", s=s, blocking=False,
                target_kind=ax.TARGET_PIPELINE, target_id=str(RNG.randint(100000, 999999)),
                remediation_api="pipelines.stop(pipeline_id=...)", wave_id=stream_wid,
                status=RNG.choice([ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS]))
    _add_action(ax.KIND_RESTART_STREAM, ax.SCOPE_TABLE, ax.PHASE_POST_CONVERT,
                f"Restart streaming writer on {s['table_name']}", s=s, blocking=False,
                target_kind=ax.TARGET_PIPELINE, remediation_api="pipelines.start_update(..., full_refresh=False)",
                wave_id=stream_wid)

# POST_CONVERT papercuts: re-share (D2D) + enable Compatibility Mode.
for s in [m for m in specs if m["bucket"] == "share"][:6]:
    _add_action(ax.KIND_RESHARE_D2D, ax.SCOPE_TABLE, ax.PHASE_POST_CONVERT,
                f"Re-share {s['table_name']} to D2D recipients", s=s, blocking=False,
                target_kind=ax.TARGET_SHARE, target_id="<SHARE_NAME>",
                remediation_sql=(f"ALTER SHARE <SHARE_NAME> REMOVE TABLE {s['full_name']};\n"
                                 f"ALTER SHARE <SHARE_NAME> ADD TABLE {s['full_name']} WITH HISTORY;"),
                status=RNG.choice([ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS]))
for s in [m for m in specs if m["bucket"] == "compat"][:6]:
    _add_action(ax.KIND_ENABLE_COMPAT, ax.SCOPE_TABLE, ax.PHASE_POST_CONVERT,
                f"Enable Compatibility Mode on {s['table_name']}", s=s, blocking=False,
                target_kind=ax.TARGET_TABLE,
                detail="needs an EMPTY external-location path (human storage decision) before it can run")

# PRE_CONVERT advisory: path-based access (blocking) + below-floor consumer DBR.
for s in path_fix_specs:
    _add_action(ax.KIND_FIX_PATH_ACCESS, ax.SCOPE_TABLE, ax.PHASE_PRE_CONVERT,
                f"Switch path-based consumers of {s['table_name']} to name-based", s=s, blocking=True,
                target_kind=ax.TARGET_SOURCE_FILE,
                remediation_api=f"Use name-based access: spark.table('{s['full_name']}') instead of the storage path")
for s in [m for m in specs if m["bucket"] == "normal"][8:14]:
    _add_action(ax.KIND_UPGRADE_CONSUMER_DBR, ax.SCOPE_TABLE, ax.PHASE_PRE_CONVERT,
                f"Upgrade below-floor consumers of {s['table_name']} to DBR 15.4 LTS+", s=s, blocking=False,
                target_kind=ax.TARGET_CLUSTER)

# Dedupe by key (schema-scoped rows can collide) then MERGE.
action_rows = list({r["action_key"]: r for r in action_rows}.values())
# Replace the fx_ action slice wholesale (mirrors teardown) so a re-seed with a different seed —
# which changes RNG target_ids and therefore action_key — cannot strand stale open actions.
spark.sql(f"DELETE FROM {_fqn('migration_actions')} WHERE schema RLIKE '^fx_'")
n = ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, action_rows)
print(f"merged {n} migration_actions rows across kinds: "
      + ", ".join(sorted({r['action_kind'] for r in action_rows})))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6b) migration_next_actions — one conductor recommendation, coherent with the fixture plan
# MAGIC BE-03 makes the **sole** `plan_run_id` in `wave_plan` the current plan; a recommendation whose
# MAGIC `plan_run_id` differs is stale, so the Overview directive would render a permanent
# MAGIC "stale, refresh". After BE-04 fully replaced `wave_plan` with `fx_seed42`, any pre-existing
# MAGIC real-conductor row still carries the old plan id. We close the gap by writing exactly ONE
# MAGIC recommendation pinned to `PLAN_RUN_ID`, assembled through the conductor's OWN pure builders
# MAGIC (`build_implications` / `build_directive` / `build_exact_command` / `build_next_action_row`)
# MAGIC over the REAL rollup views (`migration_progress`, `wave_status`) + `wave_plan` members +
# MAGIC `comms_feed` + `audit_log` — so its shape and text are byte-faithful to production.
# MAGIC
# MAGIC The step + wave are chosen **deterministically** — a convert of the pilot wave, the canonical
# MAGIC "normal path" the Overview screen exists to show — and we deliberately do NOT run the
# MAGIC classifier over the synthetic `wave_status` (it is intentionally inconsistent — converted +
# MAGIC ready-now + open blocking freeze/stop actions + FAILED gates at once — so `classify_next_step`
# MAGIC would land on an unpredictable prep/freeze/blocked verdict). That is why direct-seed (b) beats
# MAGIC run-the-conductor (a) here: same faithful builders, but a stable, testable normal directive.

# COMMAND ----------

na_fqn = cd.ensure_next_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)


def _dict_rows(sql: str) -> list:
    return [r.asDict(recursive=True) for r in spark.sql(sql).collect()]


progress_rows = _dict_rows(f"SELECT * FROM {_fqn('migration_progress')}")
ws_rows = _dict_rows(f"SELECT * FROM {_fqn('wave_status')} WHERE plan_run_id = '{PLAN_RUN_ID}'")
# wave_status carries a decorated wave_label, not the raw group_key the selector needs — join the
# group_key in from wave_plan exactly as 08_conductor does before selecting a wave.
_gk_by_wave = {r["wave_id"]: r["group_key"]
               for r in _dict_rows(f"SELECT DISTINCT wave_id, group_key FROM {_fqn('wave_plan')}")}
for r in ws_rows:
    r["group_key"] = _gk_by_wave.get(r.get("wave_id"))

# Deterministic "normal path": the pilot wave (single catalog.schema, <=100GB, carries ready-now
# members, no forced blockers) — the first mutating wave a fresh migration converts. Fall back to
# the lowest-wave_order non-streaming wave if the pilot bucket is somehow absent.
_pilot_ws = [r for r in ws_rows if r.get("bucket") == "pilot"]
selected_wave = dict((_pilot_ws or sorted(
    [r for r in ws_rows if r.get("bucket") != cd.BUCKET_STREAMING],
    key=lambda r: (r.get("wave_order") or 0)))[0])
selected_wave["resume_needed"] = False
next_step_kind = cd.STEP_CONVERT   # forced, not classified — see the note above.

wid = selected_wave["wave_id"]
# Members from wave_plan (production reads the plan, not the manifest) enriched with the manifest's
# below-floor + convert-window flags — the exact join 08_conductor uses for implications.
wave_members = _dict_rows(f"""
    SELECT p.*, m.has_below_floor_consumer AS has_below_floor_consumer,
           m.convert_window_ready AS convert_window_ready,
           m.convert_window_slack_min AS convert_window_slack_min
    FROM {_fqn('wave_plan')} p
    LEFT JOIN {_fqn('table_manifest')} m USING (full_name)
    WHERE p.plan_run_id = '{PLAN_RUN_ID}' AND p.wave_id = '{wid}'
""")
na_comms_rows = _dict_rows(f"SELECT * FROM {_fqn('comms_feed')} "
                           f"WHERE plan_run_id = '{PLAN_RUN_ID}' AND wave_id = '{wid}'")

estate = cd.summarize_estate(progress_rows)
implications = cd.build_implications(next_step_kind, selected_wave, wave_members, na_comms_rows, parallelism=4)
na_blockers = cd.collect_blockers(ws_rows)
prev_summary = cd.build_activity_summary(progress_rows)
group_key = selected_wave.get("group_key")
exact_command = cd.build_exact_command(
    {"kind": next_step_kind, "target_schema": group_key, "bundle_target": "dev"})
directive = cd.build_directive(estate, next_step_kind, selected_wave, implications, na_blockers)

na_row = cd.build_next_action_row(
    as_of=NOW, plan_run_id=PLAN_RUN_ID, estate=estate, next_step_kind=next_step_kind,
    next_wave=selected_wave, implications=implications, blockers=na_blockers,
    prev_summary=prev_summary, exact_command=exact_command, directive=directive,
)

# Idempotent fx_ slice: drop any prior fixture recommendation (keep real-conductor history intact),
# then append exactly one row pinned to the fixture plan. Its as_of=NOW makes it the LATEST row, so
# the Overview reads a recommendation whose plan_run_id == the sole wave_plan plan_run_id (not stale).
spark.sql(f"DELETE FROM {na_fqn} WHERE plan_run_id = '{PLAN_RUN_ID}'")
cd.append_next_action_row(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, na_row)
print(f"seeded 1 migration_next_actions row: next_step_kind={next_step_kind} wave={wid} "
      f"group_key={group_key} plan_run_id={PLAN_RUN_ID}")
print(f"  directive: {directive[:180]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4b) path_access_locations — ADD fx_* rows (existing proven fixture is PRESERVED)

# COMMAND ----------

# Ensure the table exists (it is NOT created by bootstrap — job_01/source_scan owns its DDL).
spark.sql(ss.create_path_access_locations_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
pa_fqn = ss.path_access_locations_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)

# Only delete the fx_ rows we own; NEVER touch the existing (non-fx) proven fixture.
spark.sql(f"DELETE FROM {pa_fqn} WHERE full_name RLIKE '\\\\.fx_'")

pa_rows = []
for s in path_fix_specs:
    for j in range(RNG.randint(1, 3)):
        loc = f"s3://fixture-estate/{s['schema']}/{s['table_name']}"
        pa_rows.append({
            **ss.build_location_row(
                full_name=s["full_name"], entity_kind=RNG.choice(["NOTEBOOK", "JOB"]),
                entity_id=str(RNG.randint(100000, 999999)),
                entity_name=f"/Workspace/etl/{s['table_name']}_pipeline",
                source_path=f"/Workspace/etl/{s['table_name']}_pipeline",
                cell_index=RNG.randint(0, 12), line_number=RNG.randint(1, 200),
                snippet=f"df = spark.read.format('delta').load('{loc}')",
                matched_path=loc, confidence="high"),
            "updated_at": NOW,
        })

if pa_rows:
    # Mirror notebooks/01_source_scan.py: a DDL-string schema keeps INT/TIMESTAMP columns exact.
    _pal_schema = ", ".join(f"{n} {t}" for n, t in ss.PATH_ACCESS_LOCATIONS_COLUMNS)
    df = spark.createDataFrame(
        [{c: r.get(c) for c in ss.PATH_ACCESS_LOCATIONS_COLUMN_NAMES} for r in pa_rows],
        schema=_pal_schema,
    )
    df.write.mode("append").saveAsTable(pa_fqn)
print(f"added {len(pa_rows)} fx_ path_access_locations rows (existing proven fixture preserved)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Hybrid (optional): a few REAL external tables with >=8 commits (live windowing plumbing)

# COMMAND ----------

if SEED_REAL_WRITES and not EXTERNAL_BASE:
    print("seed_real_writes=true but external_base is blank -> skipping the real-writes path.")
elif SEED_REAL_WRITES:
    from pyspark.sql import functions as F
    import time

    real_schema = "fx_real"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {MANIFEST_CATALOG}.{real_schema}")
    for t in range(3):
        tbl = f"real_writer_{t:02d}"
        fq = f"{MANIFEST_CATALOG}.{real_schema}.{tbl}"
        loc = f"{EXTERNAL_BASE}/{real_schema}/{tbl}"
        spark.sql(f"CREATE TABLE IF NOT EXISTS {fq} (id BIGINT, amount DOUBLE, event_ts TIMESTAMP) "
                  f"USING DELTA LOCATION '{loc}'")
        for c in range(10):   # >=8 commits so windowing has real history to read
            (spark.range(200).withColumn("amount", F.rand() * 1000)
             .withColumn("event_ts", F.current_timestamp())
             .write.format("delta").mode("append").save(loc))
            time.sleep(1)
        ops = spark.sql(f"DESCRIBE HISTORY {fq}").count()
        print(f"  {fq}: {ops} commits (real). Run 00_profiler scoped to schema_filter={real_schema} "
              f"to populate live windowing columns.")
    print("NOTE: real commit timestamps are ~now, so the profiler will compute intervals/predictions "
          "but 'convert_window_ready=true' (needs a wide idle gap) is only reproducible synthetically.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify the acceptance criteria light up

# COMMAND ----------

# Deterministic date bucketing: pin the session TZ to UTC so ``to_date`` matches the UTC-aware
# datetimes the fixture generated (otherwise the distinct-day count is session-TZ dependent).
spark.conf.set("spark.sql.session.timeZone", "UTC")


def _scalar(sql: str):
    return spark.sql(sql).collect()[0][0]


# --------------------------------------------------------------------------------------------- #
# EXPECTED values, computed straight from the in-memory rows this run generated. Because every
# fixture table is now written delete-then-(append|merge) over the fx_ slice, the fx_-scoped
# contents equal exactly what was generated — so each check asserts ACTUAL == EXPECTED. Every
# label/query is scoped to the fixture (schema RLIKE '^fx_' or full_name RLIKE '.fx_'): these
# assertions describe ONLY the fx_ estate this notebook owns, never the co-resident real estate.
# --------------------------------------------------------------------------------------------- #
_CANONICAL_BANDS = {b["name"] for b in elig.SIZE_BANDS} | {"unknown"}
_OPEN_LEDGER_STATUSES = {ex.LEDGER_PLANNED, ex.LEDGER_FROZEN, ex.LEDGER_FAILED}
_OPEN_ACTION_STATUSES = {ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS}
_GENERIC_BLOCKER_CODES = {"external_writer_blocks_managed", "non_delta_format"}
_FAIL_GATE_KINDS = (ge.GATE_COMPACTION_COVERAGE, ge.GATE_STREAM_STOP_COVERAGE,
                    ge.GATE_CAMPAIGN_GO_NOGO, ge.GATE_WAVE_TARGET_RESOLUTION)


def _blocker_codes(row) -> list:
    return [c.strip() for c in (row.get("blockers") or "").split(",") if c.strip()]


_blk_rows = [r for r in manifest_rows
             if r.get("eligibility") in ("BLOCKED", "NEEDS_REVIEW") and _blocker_codes(r)]

exp_total = len(specs)
exp_ready = len(ready_set)
exp_ready_waves = len({s["wave_id"] for s in specs if s["full_name"] in ready_set})
exp_convert_success = expected_convert_success   # from the audit cell (== len(converted_specs))
exp_convert_days = expected_convert_days          # distinct UTC dates a CONVERT/SUCCESS lands on
exp_pa = len(pa_rows)
exp_ledger_open = sum(1 for r in ledger_rows if r["status"] in _OPEN_LEDGER_STATUSES)
exp_open_kinds = len({r["action_kind"] for r in action_rows
                      if r["status"] in _OPEN_ACTION_STATUSES})
exp_blk_review = len(_blk_rows)
exp_multi_code = sum(1 for r in _blk_rows if len(_blocker_codes(r)) >= 2)
exp_rb_blockers = sum(len(_blocker_codes(r)) for r in _blk_rows)
exp_generic = sum(1 for r in _blk_rows for c in _blocker_codes(r) if c in _GENERIC_BLOCKER_CODES)
exp_fail_gates = {k: sum(1 for g in gate_rows if g["gate_kind"] == k and not g["passed"])
                  for k in _FAIL_GATE_KINDS}

# (label, actual, expected). ok := actual == expected.
checks = []


def _add(label, actual, expected):
    checks.append((label, actual, expected))


# 1) windowing + ready_now_count (fx_ only)
_add("1. fx_ convert_window_ready tables (predicted+slack non-null)",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('table_manifest')} "
             f"WHERE convert_window_ready = true AND predicted_next_write_at IS NOT NULL "
             f"AND convert_window_slack_min IS NOT NULL AND schema RLIKE '^fx_'"),
     exp_ready)
_add("1. fx_ waves with ready_now_count > 0",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('wave_status')} "
             f"WHERE plan_run_id = '{PLAN_RUN_ID}' AND ready_now_count > 0"),
     exp_ready_waves)

# 2) burn-down / run history (fx_ only) — one CONVERT/SUCCESS per converted fx_ table
_add("2. fx_ CONVERT/SUCCESS audit events",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('audit_log')} "
             f"WHERE action='CONVERT' AND phase='SUCCESS' AND full_name RLIKE '\\\\.fx_'"),
     exp_convert_success)
_add("2. fx_ distinct calendar days with a conversion",
     _scalar(f"SELECT COUNT(DISTINCT to_date(end_time)) FROM {_fqn('audit_log')} "
             f"WHERE action='CONVERT' AND phase='SUCCESS' AND full_name RLIKE '\\\\.fx_'"),
     exp_convert_days)

# 3) gate kinds (each FAILED kind present exactly as generated)
for kind in _FAIL_GATE_KINDS:
    _add(f"3. fx_ FAILED gate {kind} with reason",
         _scalar(f"SELECT COUNT(*) FROM {_fqn('wave_gate_evaluation')} "
                 f"WHERE gate_kind='{kind}' AND passed = false AND reason IS NOT NULL "
                 f"AND source='{FIXTURE_SOURCE}'"),
         exp_fail_gates[kind])

# 4) path access (fx_ only)
_add("4. fx_ path_access_locations rows",
     _scalar(f"SELECT COUNT(*) FROM {pa_fqn} WHERE full_name RLIKE '\\\\.fx_'"), exp_pa)

# 5) freeze_ledger_open (fx_ only) — PLANNED/FROZEN/FAILED rows this run left open
_add("5. fx_ freeze_ledger_open rows",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('freeze_ledger_open')} WHERE schema RLIKE '^fx_'"),
     exp_ledger_open)

# 6) open actions across kinds (fx_ only)
_add("6. fx_ distinct open action kinds",
     _scalar(f"SELECT COUNT(DISTINCT action_kind) FROM {_fqn('action_queue_open')} "
             f"WHERE source='{FIXTURE_SOURCE}'"),
     exp_open_kinds)

# 7) blocked/review tables carry REAL blocker codes + migration_runbook has matching BLOCKER rows
_add("7. fx_ BLOCKED/NEEDS_REVIEW tables w/ real codes",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('table_manifest')} "
             f"WHERE schema RLIKE '^fx_' AND eligibility IN ('BLOCKED','NEEDS_REVIEW') "
             f"AND blockers IS NOT NULL AND blockers <> ''"),
     exp_blk_review)
_add("7. fx_ tables with >=2 blocker codes (explode)",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('table_manifest')} "
             f"WHERE schema RLIKE '^fx_' AND blockers IS NOT NULL AND blockers <> '' "
             f"AND size(split(blockers, ',')) >= 2"),
     exp_multi_code)
_add("7. fx_ migration_runbook BLOCKER rows (one per code)",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('migration_runbook')} "
             f"WHERE source_kind='BLOCKER' AND full_name RLIKE '\\\\.fx_'"),
     exp_rb_blockers)
_add("7. fx_ generic-remediation codes surfaced in runbook",
     _scalar(f"SELECT COUNT(*) FROM {_fqn('migration_runbook')} "
             f"WHERE source_kind='BLOCKER' AND full_name RLIKE '\\\\.fx_' "
             f"AND item_kind IN ('external_writer_blocks_managed','non_delta_format')"),
     exp_generic)

# scale — total fx_ manifest tables equals exactly what was generated (delete-then-merge)
_add("scale. fx_ manifest tables", _scalar(
    f"SELECT COUNT(*) FROM {_fqn('table_manifest')} WHERE schema RLIKE '^fx_'"), exp_total)

# coherence (BE-05) — the current plan is the SINGLE plan_run_id in wave_plan, and the latest
# migration_next_actions recommendation must carry THAT plan_run_id (else the Overview directive
# renders a permanent "stale, refresh"). Assert the full chain: one plan in wave_plan, it equals
# the fixture plan, and the newest recommendation matches it.
_n_wave_plans = _scalar(f"SELECT COUNT(DISTINCT plan_run_id) FROM {_fqn('wave_plan')}")
_sole_wave_plan = _scalar(f"SELECT MAX(plan_run_id) FROM {_fqn('wave_plan')}")
_latest_na_plan = _scalar(
    f"SELECT plan_run_id FROM {_fqn('migration_next_actions')} ORDER BY as_of DESC LIMIT 1")
_add("coherence. wave_plan holds exactly one plan_run_id", _n_wave_plans, 1)
_add("coherence. wave_plan plan_run_id == fixture plan", _sole_wave_plan, PLAN_RUN_ID)
_add("coherence. latest migration_next_actions.plan_run_id == wave_plan",
     _latest_na_plan, _sole_wave_plan)

# --------------------------------------------------------------------------------------------- #
# Canonical size-band domain guard (the fixture must never emit XS/S/M/L/XL). This is the exact
# check the request asks to confirm: estate_rollup_by_size_band (the dashboard `size_band` GROUP
# BY over table_manifest) can only return canonical bands.
# --------------------------------------------------------------------------------------------- #
fx_bands = sorted({r[0] for r in spark.sql(
    f"SELECT DISTINCT COALESCE(size_band, 'unknown') AS b "
    f"FROM {_fqn('table_manifest')} WHERE schema RLIKE '^fx_'").collect()})
noncanonical_bands = [b for b in fx_bands if b not in _CANONICAL_BANDS]

print(f"\n{'ACCEPTANCE CHECK':52s} {'ACTUAL':>8s} {'EXPECT':>8s}  OK")
print("-" * 80)
all_ok = True
for label, actual, expected in checks:
    ok = (actual == expected)
    all_ok = all_ok and ok
    print(f"{label:52s} {str(actual):>8s} {str(expected):>8s}  {'✓' if ok else '✗'}")
bands_ok = not noncanonical_bands
all_ok = all_ok and bands_ok
print(f"{'bands. fx_ size_band values ⊆ canonical domain':52s} "
      f"{('|'.join(fx_bands)):>8s} {'canon':>8s}  {'✓' if bands_ok else '✗'}")
print("-" * 80)
print(f"canonical size bands = {sorted(_CANONICAL_BANDS)}")
print(f"fx_ size bands seen  = {fx_bands}")
if noncanonical_bands:
    print(f"NON-CANONICAL BANDS PRESENT: {noncanonical_bands}")
print(f"plan_run_id = {PLAN_RUN_ID}")
if not all_ok:
    raise RuntimeError("one or more acceptance checks failed (ACTUAL != EXPECTED) — see the table above")
print("\nAll acceptance checks passed (every ACTUAL == EXPECTED). Fixture estate is live.")
