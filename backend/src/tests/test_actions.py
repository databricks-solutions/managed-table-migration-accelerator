"""Unit tests for the migration action queue generator + schema. No Spark required."""

import os
import sys
import types as _types
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub pyspark.sql.types so actions.actions_struct_type() runs without a real Spark
# install (the retry tests below exercise merge_action_rows' write path; the struct's
# contents are never inspected). Mirrors test_manifest_retry.py.
if "pyspark" not in sys.modules:
    def _make_type(name):
        # Value-equality by class so ArrayType(StringType()) == ArrayType(StringType())
        # holds under the stub (manifest_struct_type's ARRAY<STRING> mapping is compared
        # by value in test_manifest). Mirrors test_manifest_retry.py.
        return type(name, (), {
            "__init__": lambda self, *a, **k: None,
            "__eq__": lambda self, other: type(self) is type(other),
            "__hash__": lambda self: hash(name),
        })

    _pt = _types.ModuleType("pyspark.sql.types")
    for _n in ("BooleanType", "DoubleType", "IntegerType", "LongType",
               "StringType", "TimestampType"):
        setattr(_pt, _n, _make_type(_n))

    class _ArrayType:
        def __init__(self, elementType=None, containsNull=True):
            self.elementType = elementType
            self.containsNull = containsNull

        def __eq__(self, other):
            return isinstance(other, _ArrayType) and self.elementType == other.elementType

        def __hash__(self):
            return hash(("array", self.elementType))

    _pt.ArrayType = _ArrayType

    class _StructField:
        def __init__(self, name, dataType=None, nullable=True):
            self.name = name
            self.dataType = dataType
            self.nullable = nullable

    class _StructType:
        def __init__(self, fields=None):
            self.fields = list(fields or [])

    _pt.StructField = _StructField
    _pt.StructType = _StructType
    _psql = _types.ModuleType("pyspark.sql")
    _psql.types = _pt
    _pyspark = _types.ModuleType("pyspark")
    _pyspark.sql = _psql
    sys.modules["pyspark"] = _pyspark
    sys.modules["pyspark.sql"] = _psql
    sys.modules["pyspark.sql.types"] = _pt

from migration import actions as ac  # noqa: E402
from migration import path_access as pa  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0)


def _clean_row(**overrides) -> dict:
    """A manifest row with every papercut signal OFF."""
    row = {
        "catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t",
        "is_delta_shared": False, "needs_compatibility_mode": False, "compat_mode_sql": None,
        "has_path_based_access": False, "has_below_floor_consumer": False,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Identity / key determinism
# --------------------------------------------------------------------------- #

def test_action_key_deterministic_and_readable():
    k = ac.action_key(ac.KIND_FREEZE_COMPACTION, "c.s.t", "job-1")
    assert k == "FREEZE_COMPACTION|c.s.t|job-1"


def test_action_key_none_target_uses_dash():
    assert ac.action_key(ac.KIND_RESHARE_D2D, "c.s.t", None) == "RESHARE_D2D|c.s.t|-"


def test_scope_ref_table_vs_schema():
    assert ac.scope_ref(ac.SCOPE_TABLE, full_name="c.s.t") == "c.s.t"
    assert ac.scope_ref(ac.SCOPE_SCHEMA, catalog="c", schema="s") == "c.s"


def test_build_row_key_and_id_are_stable_inputs():
    r = ac.build_action_row(
        action_kind=ac.KIND_ENABLE_COMPAT, scope=ac.SCOPE_TABLE, phase=ac.PHASE_POST_CONVERT,
        title="x", full_name="c.s.t", catalog="c", schema="s", table_name="t", action_id="fixed",
    )
    assert r["action_key"] == "ENABLE_COMPAT|c.s.t|-"
    assert r["action_id"] == "fixed"
    assert r["status"] == ac.STATUS_OPEN
    # Every declared column is present exactly once.
    assert set(r.keys()) == set(ac.ACTION_COLUMN_NAMES)


# --------------------------------------------------------------------------- #
# Column-ownership partition
# --------------------------------------------------------------------------- #

def test_ownership_partition_covers_every_column_exactly_once():
    union = ac.IDENTITY_COLUMNS | ac.GENERATOR_OWNED_COLUMNS | ac.OPERATOR_OWNED_COLUMNS
    assert union == set(ac.ACTION_COLUMN_NAMES)
    # Disjoint: no column appears in two groups.
    total = len(ac.IDENTITY_COLUMNS) + len(ac.GENERATOR_OWNED_COLUMNS) + len(ac.OPERATOR_OWNED_COLUMNS)
    assert total == len(ac.ACTION_COLUMN_NAMES)


def test_operator_owned_never_overlaps_generator():
    assert ac.OPERATOR_OWNED_COLUMNS.isdisjoint(ac.GENERATOR_OWNED_COLUMNS)
    assert ac.OPERATOR_OWNED_COLUMNS.isdisjoint(ac.IDENTITY_COLUMNS)


# --------------------------------------------------------------------------- #
# DDL / column-name parity
# --------------------------------------------------------------------------- #

def test_actions_fqn():
    assert ac.actions_fqn("main", "migration") == "main.migration.migration_actions"


def test_create_actions_table_sql_mentions_every_column():
    sql = ac.create_actions_table_sql("main", "migration")
    assert "CREATE TABLE IF NOT EXISTS main.migration.migration_actions" in sql
    assert "USING DELTA" in sql
    for name, _ in ac.ACTION_COLUMNS:
        assert name in sql


def test_view_sql_builders_reference_the_table():
    for builder in (
        ac.create_action_queue_open_view_sql,
        ac.create_action_summary_view_sql,
        ac.create_wave_action_status_view_sql,
    ):
        sql = builder("main", "migration")
        assert "CREATE OR REPLACE VIEW" in sql
        assert "main.migration.migration_actions" in sql


def test_action_summary_counts_every_status_so_total_is_their_sum():
    sql = ac.create_action_summary_view_sql("main", "migration")
    for status, col in ((ac.STATUS_OPEN, "open"), (ac.STATUS_IN_PROGRESS, "in_progress"),
                        (ac.STATUS_DONE, "done"), (ac.STATUS_OBSOLETE, "obsolete"),
                        (ac.STATUS_DEFERRED, "deferred")):
        assert f"COUNT_IF(status = '{status}') AS {col}," in sql
    # DEFERRED never holds: open_blocking stays OPEN/IN_PROGRESS only
    assert (f"COUNT_IF(status IN ('{ac.STATUS_OPEN}', '{ac.STATUS_IN_PROGRESS}') AND blocking) "
            "AS open_blocking") in sql


def test_wave_action_status_exposes_per_kind_reshare_compat_counts():
    sql = ac.create_wave_action_status_view_sql("main", "migration")
    # per-kind counts so the conductor can dispatch STEP_RESHARE / STEP_COMPAT separately
    # from the freeze/unfreeze (restore) bucket.
    assert "open_reshare" in sql
    assert "open_compat" in sql
    assert ac.KIND_RESHARE_D2D in sql
    assert ac.KIND_ENABLE_COMPAT in sql


def test_wave_action_status_splits_pre_convert_auto_vs_manual():
    from migration import actions as ac

    sql = ac.create_wave_action_status_view_sql("c", "s")

    # The two new counts exist.
    assert "AS open_pre_convert_auto" in sql
    assert "AS open_pre_convert_manual" in sql

    # The legacy total is preserved (wave_state depends on it).
    assert "AS open_pre_convert" in sql

    # The scoped CTEs must carry the columns the auto predicate reads.
    for col in ("a.scope", "a.target_kind", "a.target_id"):
        assert col in sql, f"scoped CTE missing {col} needed by the auto predicate"

    # Auto predicate mirrors executor.freeze_op_for_action: attributed job/pipeline or schema PO.
    assert "FREEZE_COMPACTION" in sql and "STOP_STREAM" in sql and "STOP_WRITER_JOB" in sql
    # Manual bucket excludes lease (it has its own open_lease / STEP_LEASE path).
    assert "LEASE_OWNERSHIP" in sql


def test_wave_action_status_auto_predicate_shape():
    from migration import actions as ac

    sql = ac.create_wave_action_status_view_sql("c", "s")
    # attributed job/pipeline with non-empty target_id, or schema-scoped PO.
    assert (
        f"action_kind = '{ac.KIND_FREEZE_COMPACTION}' "
        f"  AND (scope = '{ac.SCOPE_SCHEMA}' OR "
        f"(target_kind = '{ac.TARGET_JOB}' AND target_id IS NOT NULL AND target_id <> ''))"
    ) in sql
    assert (
        f"action_kind = '{ac.KIND_STOP_STREAM}' "
        f"  AND ((target_kind = '{ac.TARGET_PIPELINE}' OR target_kind = '{ac.TARGET_JOB}') "
        f"AND target_id IS NOT NULL AND target_id <> '')"
    ) in sql
    assert (
        f"action_kind = '{ac.KIND_STOP_WRITER_JOB}' "
        f"  AND target_kind = '{ac.TARGET_JOB}' AND target_id IS NOT NULL AND target_id <> ''"
    ) in sql
    assert f"scope = '{ac.SCOPE_SCHEMA}' OR (target_kind" in sql
    assert (
        f"NOT ("
        f"((action_kind = '{ac.KIND_FREEZE_COMPACTION}'"
    ) in sql
    assert f"AND action_kind <> '{ac.KIND_LEASE_OWNERSHIP}') AS open_pre_convert_manual" in sql


def test_wave_action_status_counts_applied_but_unverified_leases():
    from migration import actions as ac

    sql = ac.create_wave_action_status_view_sql("c", "s")
    assert (f"COUNT_IF(status = '{ac.STATUS_IN_PROGRESS}' AND phase = '{ac.PHASE_PRE_CONVERT}'\n"
            f"           AND action_kind = '{ac.KIND_LEASE_OWNERSHIP}') AS open_pre_convert_lease_unverified"
            ) in sql


def test_wave_action_status_is_member_scoped_not_plan_wave_filtered():
    # Regression: actions carry NULL plan_run_id/wave_id, so the rollup MUST join wave_plan
    # on member identity (full_name for TABLE scope, catalog/schema for SCHEMA scope) rather
    # than filter the actions table by its own (always-NULL) plan/wave — which matched nothing.
    sql = ac.create_wave_action_status_view_sql("main", "migration")
    assert "main.migration.wave_plan" in sql
    assert "JOIN" in sql and "a.full_name = wp.full_name" in sql
    # The old, broken predicate must be gone.
    assert "WHERE plan_run_id IS NOT NULL" not in sql
    # they remain subsets of the overall post-convert count (unchanged)
    assert "open_post_convert" in sql


# --------------------------------------------------------------------------- #
# Generator — clean row emits nothing
# --------------------------------------------------------------------------- #

def test_clean_row_emits_no_actions():
    assert ac.actions_from_manifest_row(_clean_row(), now=NOW) == []


# --------------------------------------------------------------------------- #
# Generator — per signal
# --------------------------------------------------------------------------- #

def test_reshare_action_generated():
    rows = ac.actions_from_manifest_row(_clean_row(is_delta_shared=True), now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["action_kind"] == ac.KIND_RESHARE_D2D
    assert r["target_kind"] == ac.TARGET_SHARE
    assert r["phase"] == ac.PHASE_POST_CONVERT
    assert "ALTER SHARE" in r["remediation_sql"]
    assert r["blocking"] is False


def test_compat_action_requires_sql():
    # Flag set but no SQL -> nothing (cannot act).
    assert ac.actions_from_manifest_row(
        _clean_row(needs_compatibility_mode=True), now=NOW) == []
    rows = ac.actions_from_manifest_row(
        _clean_row(needs_compatibility_mode=True, compat_mode_sql="ALTER TABLE ..."), now=NOW)
    assert len(rows) == 1
    assert rows[0]["action_kind"] == ac.KIND_ENABLE_COMPAT
    assert rows[0]["remediation_sql"] == "ALTER TABLE ..."


def test_compat_action_leads_with_open_apis_then_compat_fallback():
    from migration import compat as cm
    [r] = ac.actions_from_manifest_row(
        _clean_row(needs_compatibility_mode=True, compat_mode_sql="ALTER TABLE ..."), now=NOW)
    assert r["title"].startswith("Decide how external readers connect to ")
    detail = r["detail"]
    assert detail.index("open APIs") < detail.index("enable Compatibility Mode")
    assert cm.OPEN_APIS_REASON in detail
    assert "GRANT EXTERNAL USE SCHEMA ON SCHEMA" in detail
    assert "second, read-only copy" in detail


def test_path_access_is_blocking_pre_convert():
    rows = ac.actions_from_manifest_row(_clean_row(has_path_based_access=True), now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["action_kind"] == ac.KIND_FIX_PATH_ACCESS
    assert r["blocking"] is True
    assert r["phase"] == ac.PHASE_PRE_CONVERT
    assert r["confidence"] == ac.CONF_HIGH


def test_path_access_with_tier2_locations_counts_them():
    rows = ac.actions_from_manifest_row(
        _clean_row(has_path_based_access=True),
        path_locations=[{"source_path": "a"}, {"source_path": "b"}],
        now=NOW,
    )
    assert rows[0]["target_kind"] == ac.TARGET_SOURCE_FILE
    assert "2 source location" in rows[0]["detail"]


def test_build_path_fix_remediation_no_locations_mentions_entities_fallback():
    rem = ac.build_path_fix_remediation("c.s.t", None)
    assert "c.s.t" in rem and "path_access_entities" in rem
    # always surfaces the name-based rewrite guidance
    assert "spark.read.table" in rem and "SET MANAGED" in rem


def test_path_fix_remediation_states_redirect_version_gate():
    # Name-based stays the recommendation; redirect is named as the alternative, with its
    # 18.1/18.2 runtime gate and the shared doc link.
    rem = ac.build_path_fix_remediation("c.s.t", None)
    assert "18.1" in rem and "18.2" in rem
    assert "Can't update the code?" in rem
    assert pa.PATH_REDIRECT_DOC_URL in rem
    assert "rather than rely on redirect" not in rem
    assert ac.PATH_REDIRECT_READ_DBR == pa.PATH_REDIRECT_READ_DBR == (18, 1)
    assert ac.PATH_REDIRECT_WRITE_DBR == pa.PATH_REDIRECT_WRITE_DBR == (18, 2)


def _native_row(verdict, status, *, dbr="15.4", floor="18.2", **extra):
    consumers = [{
        "entity_type": "JOB", "entity_id": "42", "role": "WRITE", "dbr_version": dbr,
        "floor": floor, "status": status, "last_event_time": None, "serverless": False,
    }]
    return _clean_row(path_redirect_verdict=verdict,
                      path_redirect_consumers=pa.serialize_consumers(consumers), **extra), consumers


def test_redirect_below_floor_emits_a_blocking_fix_leading_with_name_based():
    row, _ = _native_row(pa.REDIRECT_BELOW_FLOOR, pa.CONSUMER_BELOW_FLOOR)
    rows = ac.actions_from_manifest_row(row, now=NOW)
    assert len(rows) == 1
    fix = rows[0]
    assert fix["action_kind"] == ac.KIND_FIX_PATH_ACCESS
    assert fix["blocking"] is True and fix["phase"] == ac.PHASE_PRE_CONVERT
    assert "JOB 42 WRITE on DBR 15.4 (needs 18.2+)" in fix["remediation_api"]
    rem = fix["remediation_api"]
    assert rem.index("name-based") < rem.index("upgrade")
    assert "not eligible" in fix["detail"] and "name-based" in fix["detail"]
    assert pa.PATH_REDIRECT_DOC_URL in rem


def test_an_eligible_native_consumer_without_an_opt_in_still_emits_the_blocking_fix():
    row, _ = _native_row(pa.REDIRECT_OK, pa.CONSUMER_OK, dbr="18.2")
    rows = ac.actions_from_manifest_row(row, now=NOW)
    assert len(rows) == 1
    fix = rows[0]
    assert fix["blocking"] is True
    assert fix["detail"].index("name-based") < fix["detail"].index("opt in")
    assert "eligible" in fix["detail"]
    assert "opt in" in fix["remediation_api"]


def test_an_unknown_runtime_consumer_emits_the_fix_and_says_why_it_is_not_eligible():
    row, _ = _native_row(pa.REDIRECT_UNKNOWN, pa.CONSUMER_UNKNOWN, dbr=None)
    fix = ac.actions_from_manifest_row(row, now=NOW)[0]
    assert "not eligible" in fix["detail"]
    assert "unknown_runtime:JOB:42:WRITE" in fix["remediation_api"]


def test_a_valid_opt_in_or_no_native_consumer_emits_no_fix_action():
    row, consumers = _native_row(pa.REDIRECT_OK, pa.CONSUMER_OK, dbr="18.2")
    row.update(path_redirect_optin_status=pa.OPTIN_ACTIVE,
               path_redirect_optin_fingerprint=pa.consumer_fingerprint(consumers))
    assert ac.actions_from_manifest_row(row, now=NOW) == []
    assert ac.actions_from_manifest_row(_clean_row(path_redirect_verdict=pa.REDIRECT_NONE), now=NOW) == []
    assert ac.actions_from_manifest_row(_clean_row(path_redirect_verdict=None), now=NOW) == []


def test_fix_path_access_detail_states_redirect_gate_and_keeps_per_consumer():
    rows = ac.actions_from_manifest_row(
        _clean_row(has_path_based_access=True),
        path_locations=[{"entity_kind": "NOTEBOOK", "entity_name": "/nb",
                         "matched_path": "s3://bkt/t"}],
        now=NOW,
    )
    fix = rows[0]
    # detail carries the redirect gate...
    assert "18.1" in fix["detail"] and "18.2" in fix["detail"]
    # ...and the remediation still carries the existing per-consumer WHO/WHERE/WHICH line.
    assert "NOTEBOOK /nb" in fix["remediation_api"] and "s3://bkt/t" in fix["remediation_api"]
    assert fix["blocking"] is True and fix["phase"] == ac.PHASE_PRE_CONVERT


def test_build_path_fix_remediation_lists_per_consumer_who_where_which():
    locs = [
        {"entity_kind": "NOTEBOOK", "entity_name": "/Repos/etl/load",
         "source_path": "/Repos/etl/load.py", "cell_index": 3, "line_number": 42,
         "matched_path": "s3://bkt/data/t"},
        {"entity_kind": "job", "entity_id": "9911", "line_number": 7,
         "matched_path": "abfss://c@a.dfs/x"},
    ]
    rem = ac.build_path_fix_remediation("c.s.t", locs)
    assert "Per-consumer fixes" in rem
    assert "NOTEBOOK /Repos/etl/load" in rem and "cell 3, line 42" in rem
    assert "s3://bkt/data/t" in rem
    # entity_kind is upper-cased; entity_id used when name missing
    assert "JOB 9911" in rem and "abfss://c@a.dfs/x" in rem


def test_build_path_fix_remediation_truncates_after_ten():
    locs = [{"entity_kind": "FILE", "entity_name": f"f{i}"} for i in range(13)]
    rem = ac.build_path_fix_remediation("c.s.t", locs)
    assert "+3 more in path_access_locations" in rem


def test_fix_path_access_action_carries_per_consumer_detail():
    rows = ac.actions_from_manifest_row(
        _clean_row(has_path_based_access=True),
        path_locations=[{"entity_kind": "NOTEBOOK", "entity_name": "/nb",
                         "matched_path": "s3://bkt/t"}],
        now=NOW,
    )
    fix = rows[0]
    assert fix["action_kind"] == ac.KIND_FIX_PATH_ACCESS
    assert "NOTEBOOK /nb" in fix["remediation_api"] and "s3://bkt/t" in fix["remediation_api"]


def test_consumer_dbr_action_is_advisory():
    rows = ac.actions_from_manifest_row(
        _clean_row(has_below_floor_consumer=True, min_consumer_dbr="14.3"), now=NOW)
    assert len(rows) == 1
    assert rows[0]["action_kind"] == ac.KIND_UPGRADE_CONSUMER_DBR
    assert rows[0]["blocking"] is False
    assert "14.3" in rows[0]["detail"]


def test_consumer_dbr_reader_only_no_incommit_sql():
    # A below-floor READER only: advisory upgrade text, NO executable DROP FEATURE SQL.
    rows = ac.actions_from_manifest_row(
        _clean_row(has_below_floor_consumer=True, min_consumer_dbr="14.3"),
        consumer_rows=[{"role": "READ", "entity_id": "job-r", "dbr_version": "13.3.x",
                        "below_floor": True}],
        now=NOW,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["remediation_sql"] is None
    assert "inCommitTimestamp" not in (r["remediation_sql"] or "")
    # per-consumer enumeration names the reader + version
    assert "READ job-r on DBR 13.3.x" in r["remediation_api"]


def test_consumer_dbr_writer_gets_drop_feature_sql():
    # A below-floor WRITER: executable DROP FEATURE inCommitTimestamp remediation.
    rows = ac.actions_from_manifest_row(
        _clean_row(has_below_floor_consumer=True, min_consumer_dbr="14.3"),
        consumer_rows=[{"role": "WRITE", "entity_id": "job-w", "dbr_version": "14.3.x",
                        "below_floor": True}],
        now=NOW,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["remediation_sql"] == "ALTER TABLE c.s.t DROP FEATURE inCommitTimestamp;"
    assert "WRITER" in r["detail"]
    assert "WRITE job-w on DBR 14.3.x" in r["remediation_api"]


def test_consumer_dbr_mixed_roles_writer_triggers_sql():
    rows = ac.actions_from_manifest_row(
        _clean_row(has_below_floor_consumer=True, min_consumer_dbr="13.3"),
        consumer_rows=[
            {"role": "READ", "entity_id": "job-r", "dbr_version": "13.3.x", "below_floor": True},
            {"role": "WRITE", "entity_id": "job-w", "dbr_version": "14.3.x", "below_floor": True},
        ],
        now=NOW,
    )
    r = rows[0]
    assert "DROP FEATURE inCommitTimestamp" in r["remediation_sql"]
    assert "READ job-r on DBR 13.3.x" in r["remediation_api"]
    assert "WRITE job-w on DBR 14.3.x" in r["remediation_api"]


def test_consumer_dbr_no_rows_keeps_recall_caveat():
    # Flag set but no per-consumer rows resolved: advisory, no SQL, explicit recall gap.
    rows = ac.actions_from_manifest_row(
        _clean_row(has_below_floor_consumer=True, min_consumer_dbr="14.3"), now=NOW)
    r = rows[0]
    assert r["remediation_sql"] is None
    assert "not proof of safety" in r["remediation_api"]


def test_build_consumer_upgrade_remediation_pure():
    # Reader-only: no DROP FEATURE line.
    txt = ac.build_consumer_upgrade_remediation(
        "c.s.t",
        [{"role": "READ", "entity_id": "j1", "dbr_version": "13.3.x", "below_floor": True}],
        "13.3",
    )
    assert "DROP FEATURE" not in txt
    assert "READ j1 on DBR 13.3.x" in txt
    # Empty rows: recall caveat.
    empty = ac.build_consumer_upgrade_remediation("c.s.t", [], "14.3")
    assert "not proof of safety" in empty


# --------------------------------------------------------------------------- #
# Generator — streaming
# --------------------------------------------------------------------------- #

def test_stop_and_restart_stream_pair_per_relationship():
    streaming = [
        {"job_or_pipeline_id": "j1", "type": "STRUCTURED_STREAMING", "role": "WRITE",
         "owner": "u@x", "confidence": "high"},
        {"job_or_pipeline_id": "p1", "type": "DLT", "role": "READ",
         "owner": "v@x", "confidence": "high"},
    ]
    rows = ac.actions_from_manifest_row(_clean_row(), streaming_rows=streaming, now=NOW)
    # Each relationship now yields a STOP_STREAM (pre) + RESTART_STREAM (post) pair.
    assert len(rows) == 4
    kinds = {r["action_kind"] for r in rows}
    assert kinds == {ac.KIND_STOP_STREAM, ac.KIND_RESTART_STREAM}
    stops = [r for r in rows if r["action_kind"] == ac.KIND_STOP_STREAM]
    restarts = [r for r in rows if r["action_kind"] == ac.KIND_RESTART_STREAM]
    assert all(r["phase"] == ac.PHASE_PRE_CONVERT for r in stops)
    assert all(r["phase"] == ac.PHASE_POST_CONVERT for r in restarts)
    by_target = {r["target_id"]: r for r in restarts}
    assert by_target["j1"]["target_kind"] == ac.TARGET_JOB
    assert by_target["p1"]["target_kind"] == ac.TARGET_PIPELINE


def test_stop_restart_stream_unattributed_still_emits_pair():
    streaming = [{"job_or_pipeline_id": None, "type": "STRUCTURED_STREAMING", "role": "WRITE"}]
    rows = ac.actions_from_manifest_row(_clean_row(), streaming_rows=streaming, now=NOW)
    assert len(rows) == 2
    assert {r["action_kind"] for r in rows} == {ac.KIND_STOP_STREAM, ac.KIND_RESTART_STREAM}
    for r in rows:
        assert r["target_kind"] == ac.TARGET_NONE
        assert r["confidence"] == ac.CONF_LOW
        assert r["notes"] is not None


# --------------------------------------------------------------------------- #
# Generator — compaction freeze/unfreeze
# --------------------------------------------------------------------------- #

def test_compaction_job_actor_pairs_freeze_and_unfreeze():
    comp = [{"actor_kind": "JOB", "actor_id": "job-9", "actor_name": "u@x", "confidence": "high"}]
    rows = ac.actions_from_manifest_row(_clean_row(), compaction_rows=comp, now=NOW)
    assert len(rows) == 2
    kinds = {r["action_kind"] for r in rows}
    assert kinds == {ac.KIND_FREEZE_COMPACTION, ac.KIND_UNFREEZE_COMPACTION}
    freeze = next(r for r in rows if r["action_kind"] == ac.KIND_FREEZE_COMPACTION)
    assert freeze["scope"] == ac.SCOPE_TABLE
    assert freeze["target_kind"] == ac.TARGET_JOB
    assert freeze["target_id"] == "job-9"
    assert freeze["blocking"] is True
    assert freeze["phase"] == ac.PHASE_PRE_CONVERT
    unfreeze = next(r for r in rows if r["action_kind"] == ac.KIND_UNFREEZE_COMPACTION)
    assert unfreeze["phase"] == ac.PHASE_POST_CONVERT
    assert unfreeze["blocking"] is False


def test_vacuum_job_actor_pairs_freeze_and_unfreeze():
    # A scheduled VACUUM job reuses the same PAUSE_JOB/RESUME_JOB freeze bracket as compaction;
    # operation_class=VACUUM only refines the wording.
    comp = [{"actor_kind": "JOB", "actor_id": "vac-9", "actor_name": "nightly-vacuum",
             "confidence": "high", "operation_class": "VACUUM"}]
    rows = ac.actions_from_manifest_row(_clean_row(), compaction_rows=comp, now=NOW)
    assert len(rows) == 2
    freeze = next(r for r in rows if r["action_kind"] == ac.KIND_FREEZE_COMPACTION)
    unfreeze = next(r for r in rows if r["action_kind"] == ac.KIND_UNFREEZE_COMPACTION)
    assert freeze["scope"] == ac.SCOPE_TABLE
    assert freeze["target_kind"] == ac.TARGET_JOB
    assert freeze["target_id"] == "vac-9"
    assert freeze["blocking"] is True
    assert freeze["phase"] == ac.PHASE_PRE_CONVERT
    assert "VACUUM" in freeze["title"]
    assert "FILE_VALIDATION_FAILED" in freeze["detail"]
    assert unfreeze["phase"] == ac.PHASE_POST_CONVERT
    assert unfreeze["blocking"] is False
    assert "VACUUM" in unfreeze["title"]


def _pairs(comp):
    rows = ac.dedupe_by_key(ac.actions_from_manifest_row(_clean_row(), compaction_rows=comp, now=NOW))
    freezes = [r for r in rows if r["action_kind"] == ac.KIND_FREEZE_COMPACTION]
    unfreezes = [r for r in rows if r["action_kind"] == ac.KIND_UNFREEZE_COMPACTION]
    return freezes, unfreezes


def test_job_running_optimize_and_vacuum_gets_one_pair_naming_both():
    comp = [
        {"actor_kind": "JOB", "actor_id": "9", "actor_name": "nightly-maint",
         "confidence": "high", "operation_class": "COMPACTION"},
        {"actor_kind": "JOB", "actor_id": "9", "actor_name": "nightly-maint",
         "confidence": "high", "operation_class": "VACUUM"},
    ]
    for ordered in (comp, list(reversed(comp))):
        freezes, unfreezes = _pairs(ordered)
        assert len(freezes) == 1 and len(unfreezes) == 1
        assert "compaction (OPTIMIZE/ZORDER) and VACUUM" in freezes[0]["title"]
        assert "compaction (OPTIMIZE/ZORDER) and VACUUM" in unfreezes[0]["title"]
        assert "FILE_VALIDATION_FAILED" in freezes[0]["detail"]
        assert "OPTIMIZE/ZORDER/liquid-clustering" in freezes[0]["detail"]
        assert freezes[0]["target_id"] == "9"


def test_distinct_job_actors_keep_separate_pairs():
    comp = [
        {"actor_kind": "JOB", "actor_id": "9", "operation_class": "COMPACTION"},
        {"actor_kind": "JOB", "actor_id": "10", "operation_class": "VACUUM"},
    ]
    freezes, unfreezes = _pairs(comp)
    assert sorted(r["target_id"] for r in freezes) == ["10", "9"]
    assert len(unfreezes) == 2
    by_id = {r["target_id"]: r["title"] for r in freezes}
    assert "Pause compaction (OPTIMIZE/ZORDER) on" in by_id["9"]
    assert "Pause VACUUM on" in by_id["10"]


def test_null_operation_class_alongside_vacuum_reads_as_both():
    comp = [
        {"actor_kind": "JOB", "actor_id": "9", "operation_class": None},
        {"actor_kind": "JOB", "actor_id": "9", "operation_class": "VACUUM"},
    ]
    freezes, _ = _pairs(comp)
    assert len(freezes) == 1
    assert "compaction (OPTIMIZE/ZORDER) and VACUUM" in freezes[0]["title"]


def test_grouped_actor_keeps_highest_confidence():
    comp = [
        {"actor_kind": "JOB", "actor_id": "9", "confidence": "low", "operation_class": "COMPACTION"},
        {"actor_kind": "JOB", "actor_id": "9", "confidence": "high", "operation_class": "VACUUM"},
        {"actor_kind": "JOB", "actor_id": "9", "confidence": "medium", "operation_class": "COMPACTION"},
    ]
    freezes, unfreezes = _pairs(comp)
    assert freezes[0]["confidence"] == "high"
    assert unfreezes[0]["confidence"] == "high"


def test_freeze_maintenance_label():
    assert ac.freeze_maintenance_label(["VACUUM"]) == "VACUUM"
    assert ac.freeze_maintenance_label(["COMPACTION"]) == "compaction (OPTIMIZE/ZORDER)"
    assert ac.freeze_maintenance_label([None]) == "compaction (OPTIMIZE/ZORDER)"
    assert ac.freeze_maintenance_label([]) == "compaction (OPTIMIZE/ZORDER)"
    assert ac.freeze_maintenance_label(["vacuum", "compaction"]) == (
        "compaction (OPTIMIZE/ZORDER) and VACUUM")


def test_predictive_optimization_is_schema_scoped():
    comp = [{"actor_kind": "PREDICTIVE_OPTIMIZATION", "actor_id": None, "confidence": "high"}]
    rows = ac.actions_from_manifest_row(_clean_row(), compaction_rows=comp, now=NOW)
    freeze = next(r for r in rows if r["action_kind"] == ac.KIND_FREEZE_COMPACTION)
    assert freeze["scope"] == ac.SCOPE_SCHEMA
    assert freeze["full_name"] is None
    assert freeze["action_key"] == "FREEZE_COMPACTION|c.s|-"
    assert "DISABLE PREDICTIVE OPTIMIZATION" in freeze["remediation_sql"]


def test_manual_user_compaction_actor_has_no_schedulable_target():
    comp = [{"actor_kind": "USER", "actor_id": None, "actor_name": "human@x"}]
    rows = ac.actions_from_manifest_row(_clean_row(), compaction_rows=comp, now=NOW)
    freeze = next(r for r in rows if r["action_kind"] == ac.KIND_FREEZE_COMPACTION)
    assert freeze["target_kind"] == ac.TARGET_NONE
    assert "human@x" in freeze["remediation_api"]


# --------------------------------------------------------------------------- #
# State-aware bracket obsolescence (orphaned freeze bracket -> stale STEP_ACTIONS)
# --------------------------------------------------------------------------- #

def test_migrated_row_suppresses_pre_convert_freeze_keeps_post_unfreeze():
    """Once a table has migrated, the PRE_CONVERT freeze is moot and must not be emitted;
    the POST_CONVERT unfreeze is still generated (it may be genuine restore work)."""
    comp = [{"actor_kind": "JOB", "actor_id": "job-9", "confidence": "high"}]
    rows = ac.actions_from_manifest_row(
        _clean_row(state="VALIDATED"), compaction_rows=comp, now=NOW)
    kinds = {r["action_kind"] for r in rows}
    assert ac.KIND_FREEZE_COMPACTION not in kinds
    assert ac.KIND_UNFREEZE_COMPACTION in kinds


def test_migrated_row_suppresses_stream_and_writer_pre_stops():
    streaming = [{"job_or_pipeline_id": "s-1", "type": "STRUCTURED_STREAMING",
                  "role": "WRITE", "confidence": "high"}]
    writers = [{"entity_type": "JOB", "entity_id": "w-1",
                "writer_class": "SCHEDULED_JOB", "confidence": "high"}]
    rows = ac.actions_from_manifest_row(
        _clean_row(state="CONVERTED"), streaming_rows=streaming,
        writer_rows=writers, now=NOW)
    kinds = {r["action_kind"] for r in rows}
    assert ac.KIND_STOP_STREAM not in kinds
    assert ac.KIND_STOP_WRITER_JOB not in kinds
    assert ac.KIND_RESTART_STREAM in kinds
    assert ac.KIND_RESTART_WRITER_JOB in kinds


def test_migrated_row_does_not_suppress_ownership_lease():
    """The convert-permission pair has its own ledger + 03d/07 lifecycle; state-aware suppression
    must never drop it (no regression)."""
    rows = ac.actions_from_manifest_row(
        _clean_row(state="CONVERTED"), authority=_authority("c14_table_manage_before_self_grant"),
        now=NOW)
    kinds = {r["action_kind"] for r in rows}
    assert ac.KIND_LEASE_OWNERSHIP in kinds


def test_non_migrated_row_still_emits_pre_convert_freeze():
    comp = [{"actor_kind": "JOB", "actor_id": "job-9", "confidence": "high"}]
    for st in (None, "ELIGIBLE", "DRY_RUN_OK", "CONVERTING", "ROLLED_BACK"):
        rows = ac.actions_from_manifest_row(
            _clean_row(state=st), compaction_rows=comp, now=NOW)
        kinds = {r["action_kind"] for r in rows}
        assert ac.KIND_FREEZE_COMPACTION in kinds, st


# --------------------------------------------------------------------------- #
# bracket_keys_to_obsolete — pure ledger-aware skipped-bracket closer
# --------------------------------------------------------------------------- #

def _bracket_action(kind, full_name="c.s.t", target_id=None, scope=None):
    """A minimal open bracket action row as read from migration_actions."""
    scope = scope or ac.SCOPE_TABLE
    ref = full_name if scope == ac.SCOPE_TABLE else "c.s"
    return {"action_kind": kind, "scope": scope, "full_name": full_name,
            "target_id": target_id, "action_key": ac.action_key(kind, ref, target_id)}


def test_bracket_keys_to_obsolete_closes_skipped_manual_bracket_both_halves():
    """The mixed_000 case: a migrated table with a never-frozen (manual/USER) freeze bracket.
    Both the PRE freeze and its POST partner are obsoleted."""
    pre = _bracket_action(ac.KIND_FREEZE_COMPACTION, target_id=None)
    post = _bracket_action(ac.KIND_UNFREEZE_COMPACTION, target_id=None)
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "VALIDATED"},
        open_bracket_actions=[pre, post],
        frozen_target_ids=set())
    assert keys == sorted(["FREEZE_COMPACTION|c.s.t|-", "UNFREEZE_COMPACTION|c.s.t|-"])


def test_bracket_keys_to_obsolete_from_post_half_only_still_closes_pair():
    post = _bracket_action(ac.KIND_UNFREEZE_COMPACTION, target_id=None)
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "CONVERTED"},
        open_bracket_actions=[post], frozen_target_ids=set())
    assert set(keys) == {"UNFREEZE_COMPACTION|c.s.t|-", "FREEZE_COMPACTION|c.s.t|-"}


def test_bracket_keys_to_obsolete_leaves_genuinely_frozen_bracket_open():
    """A bracket whose target was FROZEN for real awaits 05b unfreeze — never obsoleted."""
    pre = _bracket_action(ac.KIND_FREEZE_COMPACTION, target_id="job-9")
    post = _bracket_action(ac.KIND_UNFREEZE_COMPACTION, target_id="job-9")
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "CONVERTED"},
        open_bracket_actions=[pre, post],
        frozen_target_ids={"job-9"})
    assert keys == []


def test_bracket_keys_to_obsolete_skips_non_migrated_table():
    pre = _bracket_action(ac.KIND_FREEZE_COMPACTION, target_id=None)
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "DRY_RUN_OK"},
        open_bracket_actions=[pre], frozen_target_ids=set())
    assert keys == []


def test_bracket_keys_to_obsolete_never_touches_schema_scoped_po():
    """Schema-wide PO cannot be proven moot from one table's state."""
    pre = _bracket_action(ac.KIND_FREEZE_COMPACTION, full_name=None, scope=ac.SCOPE_SCHEMA)
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "VALIDATED"},
        open_bracket_actions=[pre], frozen_target_ids=set())
    assert keys == []


def test_bracket_keys_to_obsolete_ignores_non_bracket_kinds():
    reshare = {"action_kind": ac.KIND_RESHARE_D2D, "scope": ac.SCOPE_TABLE,
               "full_name": "c.s.t", "target_id": None,
               "action_key": "RESHARE_D2D|c.s.t|-"}
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "VALIDATED"},
        open_bracket_actions=[reshare], frozen_target_ids=set())
    assert keys == []


def test_bracket_keys_to_obsolete_stream_and_writer_brackets():
    stop_stream = _bracket_action(ac.KIND_STOP_STREAM, target_id="s-1")
    stop_writer = _bracket_action(ac.KIND_STOP_WRITER_JOB, target_id="w-1")
    keys = ac.bracket_keys_to_obsolete(
        state_by_full_name={"c.s.t": "CONVERTED"},
        open_bracket_actions=[stop_stream, stop_writer],
        frozen_target_ids=set())
    assert set(keys) == {
        "STOP_STREAM|c.s.t|s-1", "RESTART_STREAM|c.s.t|s-1",
        "STOP_WRITER_JOB|c.s.t|w-1", "RESTART_WRITER_JOB|c.s.t|w-1",
    }


# --------------------------------------------------------------------------- #
# generate_actions — full backlog, dedupe, wave stamping
# --------------------------------------------------------------------------- #

def test_generate_actions_dedupes_schema_scoped_po_across_tables():
    rows_in = [
        _clean_row(full_name="c.s.a", table_name="a"),
        _clean_row(full_name="c.s.b", table_name="b"),
    ]
    comp_by = {
        "c.s.a": [{"actor_kind": "PREDICTIVE_OPTIMIZATION"}],
        "c.s.b": [{"actor_kind": "PREDICTIVE_OPTIMIZATION"}],
    }
    out = ac.generate_actions(rows_in, compaction_by_table=comp_by, source="00_profiler", now=NOW)
    keys = [r["action_key"] for r in out]
    # Exactly one freeze + one unfreeze for the schema despite two tables.
    assert keys.count("FREEZE_COMPACTION|c.s|-") == 1
    assert keys.count("UNFREEZE_COMPACTION|c.s|-") == 1


def test_generate_actions_threads_writer_by_table():
    # P0 regression guard: 00_profiler / 03b MUST pass writer_by_table so the blocking pre-convert
    # writer-stop bracket is actually generated. The shipped bug omitted it, so 0 STOP_WRITER_JOB
    # actions were ever created while 04_convert's WRITER_STOP_COVERAGE gate still demanded them
    # (an unsatisfiable-convert deadlock). This locks the generate_actions -> writer contract.
    rows_in = [_clean_row(full_name="c.s.a", table_name="a")]
    writer_by = {"c.s.a": [{"writer_class": "SCHEDULED_JOB", "entity_id": "job-1"}]}
    out = ac.generate_actions(rows_in, writer_by_table=writer_by, source="00_profiler", now=NOW)
    kinds = [r["action_kind"] for r in out]
    assert ac.KIND_STOP_WRITER_JOB in kinds
    assert ac.KIND_RESTART_WRITER_JOB in kinds
    stop = next(r for r in out if r["action_kind"] == ac.KIND_STOP_WRITER_JOB)
    assert stop["target_id"] == "job-1" and stop["blocking"] is True
    # Omitting writer_by_table (the shipped bug) generates NO writer-stop bracket.
    out_omitted = ac.generate_actions(rows_in, source="00_profiler", now=NOW)
    assert ac.KIND_STOP_WRITER_JOB not in [r["action_kind"] for r in out_omitted]


def test_profiler_notebooks_feed_writer_by_table_to_generate_actions():
    # Source guard for the EXACT shipped bug: 00_profiler and 03b_compaction_scan populate
    # writer_sources but must ALSO pass writer_by_table into generate_actions — otherwise the
    # blocking writer-stop bracket is silently never generated (and 03b's obsolete-detection would
    # even delete it), while 04_convert's WRITER_STOP_COVERAGE gate keeps demanding it. Pin the wiring.
    import pathlib
    nb_dir = pathlib.Path(__file__).resolve().parents[2] / "notebooks"
    for name in ("00_profiler.py", "03b_compaction_scan.py"):
        src = (nb_dir / name).read_text()
        assert "ax.generate_actions(" in src, f"{name}: expected an ax.generate_actions(...) call"
        call_head = src.split("ax.generate_actions(", 1)[1][:800]
        assert "writer_by_table=writer_by_table" in call_head, (
            f"{name}: generate_actions must be fed writer_by_table (regression: the writer-stop "
            f"bracket is never generated when this kwarg is omitted)")


def test_scan_notebooks_wire_orphaned_bracket_reconcile():
    # Contract: the profiler + 03b rescan must ledger-aware-close orphaned freeze/stop brackets
    # on migrated tables (the wave_00 bug). Pin that each notebook (a) reads the freeze ledger
    # via the executor adapter, (b) reads open brackets, (c) computes stale keys with the pure
    # helper, and (d) obsoletes them — so a converted wave leaves AWAITING_ACTIONS on rescan.
    import pathlib
    nb_dir = pathlib.Path(__file__).resolve().parents[2] / "notebooks"
    for name in ("00_profiler.py", "03b_compaction_scan.py"):
        src = (nb_dir / name).read_text()
        assert "ex.frozen_target_ids(" in src, f"{name}: must derive frozen target ids from the ledger"
        assert "ax.read_open_bracket_actions(" in src, f"{name}: must read open bracket actions"
        assert "ax.bracket_keys_to_obsolete(" in src, f"{name}: must compute orphaned bracket keys"
        assert "bracket_stale" in src and "ax.mark_obsolete(" in src, (
            f"{name}: must obsolete the orphaned bracket keys")


def test_convert_notebook_retires_orphaned_brackets_on_success():
    # Contract: 04_convert closes SKIPPED (never-frozen) brackets on the tables it just
    # converted, keyed off the freeze ledger, so the conductor never lands on the stale
    # "clear the open items before converting" step for an already-converted wave.
    import pathlib
    nb = (pathlib.Path(__file__).resolve().parents[2] / "notebooks" / "04_convert.py").read_text()
    assert "ex.frozen_target_ids(" in nb
    assert "ax.read_open_bracket_actions(" in nb
    assert "ax.bracket_keys_to_obsolete(" in nb
    assert "ax.mark_obsolete(" in nb


def test_generate_actions_stamps_wave_and_source():
    rows_in = [_clean_row(is_delta_shared=True)]
    wave_by = {"c.s.t": {"plan_run_id": "run-1", "wave_id": "wave_03"}}
    out = ac.generate_actions(rows_in, wave_by_table=wave_by, source="03b", now=NOW)
    assert out[0]["plan_run_id"] == "run-1"
    assert out[0]["wave_id"] == "wave_03"
    assert out[0]["source"] == "03b"
    assert out[0]["detected_at"] == NOW


def test_dedupe_by_key_keeps_last():
    a = ac.build_action_row(action_kind=ac.KIND_RESHARE_D2D, scope=ac.SCOPE_TABLE,
                            phase=ac.PHASE_POST_CONVERT, title="a", full_name="c.s.t")
    b = ac.build_action_row(action_kind=ac.KIND_RESHARE_D2D, scope=ac.SCOPE_TABLE,
                            phase=ac.PHASE_POST_CONVERT, title="b", full_name="c.s.t")
    out = ac.dedupe_by_key([a, b])
    assert len(out) == 1
    assert out[0]["title"] == "b"


# --------------------------------------------------------------------------- #
# obsolete_keys
# --------------------------------------------------------------------------- #

def test_obsolete_keys_finds_cleared_signals():
    existing = ["k1", "k2", "k3"]
    generated = ["k2", "k3", "k4"]
    assert ac.obsolete_keys(existing, generated) == ["k1"]


def test_obsolete_keys_empty_when_all_still_generated():
    assert ac.obsolete_keys(["k1", "k2"], ["k1", "k2", "k3"]) == []


def test_obsolete_keys_dedupes_and_sorts():
    assert ac.obsolete_keys(["b", "a", "b"], []) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Freeze/stop pairing (slice 2)
# --------------------------------------------------------------------------- #

def test_freeze_kind_pairs_map():
    assert ac.FREEZE_KIND_PAIRS[ac.KIND_FREEZE_COMPACTION] == ac.KIND_UNFREEZE_COMPACTION
    assert ac.FREEZE_KIND_PAIRS[ac.KIND_STOP_STREAM] == ac.KIND_RESTART_STREAM


def test_stop_stream_is_a_declared_kind():
    assert ac.KIND_STOP_STREAM in ac.ALL_KINDS


def test_stop_stream_is_pre_convert_partner_of_restart():
    streaming = [{"job_or_pipeline_id": "j1", "type": "STRUCTURED_STREAMING", "role": "WRITE"}]
    rows = ac.actions_from_manifest_row(_clean_row(), streaming_rows=streaming, now=NOW)
    stop = next(r for r in rows if r["action_kind"] == ac.KIND_STOP_STREAM)
    restart = next(r for r in rows if r["action_kind"] == ac.KIND_RESTART_STREAM)
    assert stop["phase"] == ac.PHASE_PRE_CONVERT
    assert restart["phase"] == ac.PHASE_POST_CONVERT
    # Same target so freeze/unfreeze pair up on identity.
    assert stop["target_id"] == restart["target_id"] == "j1"


# --------------------------------------------------------------------------- #
# resolve_assignee — operator-owned routing (slice 3)
# --------------------------------------------------------------------------- #

def test_resolve_assignee_exact_owner_match():
    assert ac.resolve_assignee("team-a", {"team-a": "alice", "team-b": "bob"}) == "alice"


def test_resolve_assignee_falls_back_to_star_default():
    assert ac.resolve_assignee("team-z", {"team-a": "alice", "*": "oncall"}) == "oncall"


def test_resolve_assignee_none_when_no_match_and_no_default():
    assert ac.resolve_assignee("team-z", {"team-a": "alice"}) is None


def test_resolve_assignee_none_on_empty_or_missing_map():
    assert ac.resolve_assignee("team-a", None) is None
    assert ac.resolve_assignee("team-a", {}) is None


def test_resolve_assignee_blank_owner_uses_star_only():
    assert ac.resolve_assignee(None, {"*": "oncall"}) == "oncall"
    assert ac.resolve_assignee("", {"team-a": "alice"}) is None


def test_resolve_assignee_is_operator_owned_column():
    # The generator must never overwrite assignee — it lives in OPERATOR_OWNED_COLUMNS.
    assert "assignee" in ac.OPERATOR_OWNED_COLUMNS
    assert "assignee" not in ac.GENERATOR_OWNED_COLUMNS


# --------------------------------------------------------------------------- #
# Remediation contract (slice 4): every supported kind emits a manual statement
# --------------------------------------------------------------------------- #

def _rows_for_every_kind():
    """One representative manifest row + detail rows that trigger EVERY action kind."""
    row = _clean_row(
        is_delta_shared=True,
        needs_compatibility_mode=True, compat_mode_sql="ALTER TABLE c.s.t SET TBLPROPERTIES (...);",
        has_path_based_access=True,
        has_below_floor_consumer=True, min_consumer_dbr="13.3",
    )
    streaming = [{"job_or_pipeline_id": "j1", "type": "STRUCTURED_STREAMING", "role": "WRITE"}]
    compaction = [
        {"actor_kind": "JOB", "actor_id": "job-9", "actor_name": "nightly-optimize"},
        {"actor_kind": "PREDICTIVE_OPTIMIZATION"},
    ]
    writers = [{"writer_class": "SCHEDULED_JOB", "entity_id": "812", "confidence": "high"}]
    return ac.actions_from_manifest_row(
        row, streaming_rows=streaming, compaction_rows=compaction, writer_rows=writers,
        authority=_authority("c14_table_manage_before_self_grant"), now=NOW,
    )


def test_every_supported_kind_emits_nonempty_remediation():
    rows = _rows_for_every_kind()
    kinds_seen = {r["action_kind"] for r in rows}
    # every declared GENERATOR kind is represented by the fixture (executor-emitted kinds
    # like RETIRE_MAINTENANCE_JOB are raised at unfreeze, not by the manifest generator).
    assert kinds_seen == set(ac.ALL_KINDS) - ac.EXECUTOR_EMITTED_KINDS
    for r in rows:
        sql = (r.get("remediation_sql") or "").strip()
        api = (r.get("remediation_api") or "").strip()
        assert sql or api, f"{r['action_kind']} emitted empty remediation_sql AND remediation_api"


def test_fix_path_access_remediation_uses_name_based_identifier():
    rows = _rows_for_every_kind()
    fix = next(r for r in rows if r["action_kind"] == ac.KIND_FIX_PATH_ACCESS)
    assert "c.s.t" in (fix["remediation_api"] or "")


def test_upgrade_consumer_dbr_remediation_mentions_floor():
    rows = _rows_for_every_kind()
    up = next(r for r in rows if r["action_kind"] == ac.KIND_UPGRADE_CONSUMER_DBR)
    assert "15.4" in (up["remediation_api"] or "")


def test_retire_maintenance_job_kind_is_registered():
    assert ac.KIND_RETIRE_MAINTENANCE_JOB == "RETIRE_MAINTENANCE_JOB"
    assert ac.KIND_RETIRE_MAINTENANCE_JOB in ac.ALL_KINDS
    # It is emitted by the unfreeze executor, NOT the manifest generator.
    assert ac.KIND_RETIRE_MAINTENANCE_JOB in ac.EXECUTOR_EMITTED_KINDS


def test_build_retire_maintenance_job_action_row():
    row = ac.build_retire_maintenance_job_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t", job_id="912",
        plan_run_id="p1", wave_id="w1",
    )
    assert row["action_kind"] == ac.KIND_RETIRE_MAINTENANCE_JOB
    assert row["scope"] == ac.SCOPE_TABLE
    assert row["phase"] == ac.PHASE_POST_CONVERT
    assert row["blocking"] is False
    assert row["target_kind"] == ac.TARGET_JOB
    assert row["target_id"] == "912"
    assert row["status"] == ac.STATUS_OPEN
    assert row["confidence"] == ac.CONF_HIGH
    assert row["source"] == "05b_unfreeze_verify"
    # A non-blocking recommendation must still carry a copy-pasteable manual statement.
    assert (row["remediation_api"] or "").strip()
    assert "912" in row["remediation_api"]
    # Deterministic identity from build_action_row.
    assert row["action_key"] == "RETIRE_MAINTENANCE_JOB|c.s.t|912"


def test_build_retire_maintenance_job_action_rejects_empty_job_id():
    with pytest.raises(ValueError):
        ac.build_retire_maintenance_job_action(
            full_name="c.s.t", catalog="c", schema="s", table_name="t", job_id="")


def test_maintenance_job_label_names_each_class():
    assert ac.maintenance_job_label(["COMPACTION"]) == "OPTIMIZE job"
    assert ac.maintenance_job_label(["VACUUM"]) == "VACUUM job"
    assert ac.maintenance_job_label(["VACUUM", "COMPACTION"]) == "OPTIMIZE + VACUUM job"
    assert ac.maintenance_job_label([" vacuum "]) == "VACUUM job"


def test_maintenance_job_label_is_generic_without_known_classes():
    assert ac.GENERIC_MAINTENANCE_JOB_LABEL == "OPTIMIZE/VACUUM maintenance job"
    for classes in (None, [], [""], ["SOMETHING_ELSE"]):
        assert ac.maintenance_job_label(classes) == ac.GENERIC_MAINTENANCE_JOB_LABEL


def test_maintenance_classes_by_job_merges_classes_per_table_and_job():
    rows = {
        "c.s.a": [
            {"actor_kind": "JOB", "actor_id": "9", "operation_class": "COMPACTION"},
            {"actor_kind": "JOB", "actor_id": "9", "operation_class": "VACUUM"},
            {"actor_kind": "JOB", "actor_id": 12, "operation_class": None},
            {"actor_kind": "PREDICTIVE_OPTIMIZATION", "actor_id": None, "operation_class": "VACUUM"},
            {"actor_kind": "USER", "actor_id": "u@x", "operation_class": "VACUUM"},
        ],
        "c.s.b": [{"actor_kind": "job", "actor_id": "9", "operation_class": "vacuum"}],
    }
    assert ac.maintenance_classes_by_job(rows) == {
        ("c.s.a", "9"): ["COMPACTION", "VACUUM"],
        ("c.s.a", "12"): ["COMPACTION"],
        ("c.s.b", "9"): ["VACUUM"],
    }
    assert ac.maintenance_classes_by_job(None) == {}


def test_retire_action_names_a_vacuum_job():
    row = ac.build_retire_maintenance_job_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t", job_id="77",
        operation_classes=["VACUUM"])
    assert row["title"] == "Retire redundant VACUUM job 77 — c.s.t is PO-managed"
    assert "VACUUM job" in row["detail"]
    assert "VACUUM job is redundant" in row["remediation_api"]
    assert row["action_key"] == "RETIRE_MAINTENANCE_JOB|c.s.t|77"


def test_retire_action_names_a_job_that_does_both():
    row = ac.build_retire_maintenance_job_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t", job_id="5",
        operation_classes=["COMPACTION", "VACUUM"])
    assert row["title"].startswith("Retire redundant OPTIMIZE + VACUUM job 5")


def test_retire_action_wording_is_generic_without_classes():
    row = ac.build_retire_maintenance_job_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t", job_id="912")
    assert row["title"] == "Retire redundant OPTIMIZE/VACUUM maintenance job 912 — c.s.t is PO-managed"
    assert "OPTIMIZE and VACUUM" in row["detail"]


def test_enable_po_kind_is_registered_and_executor_emitted():
    assert ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION == "ENABLE_PREDICTIVE_OPTIMIZATION"
    assert ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION in ac.ALL_KINDS
    # 08c self-discovers + MERGEs it (not the manifest generator).
    assert ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION in ac.EXECUTOR_EMITTED_KINDS


def test_build_enable_po_action_rejects_empty_full_name():
    with pytest.raises(ValueError):
        ac.build_enable_po_action(
            full_name="", catalog="c", schema="s", table_name="t")


def test_build_enable_po_action_row():
    row = ac.build_enable_po_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t",
        plan_run_id="p1", wave_id="w1")
    assert row["action_kind"] == ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION
    assert row["scope"] == ac.SCOPE_TABLE
    assert row["phase"] == ac.PHASE_POST_CONVERT
    assert row["blocking"] is False
    assert row["target_kind"] == ac.TARGET_TABLE
    assert row["status"] == ac.STATUS_OPEN
    # Carries the exact, copy-pasteable statement.
    assert (row["remediation_sql"] or "").strip() == (
        "ALTER TABLE c.s.t ENABLE PREDICTIVE OPTIMIZATION;")
    # Deterministic identity from build_action_row (TABLE target -> target ref == full_name).
    assert row["action_key"] == "ENABLE_PREDICTIVE_OPTIMIZATION|c.s.t|c.s.t"


def test_enable_auto_liquid_kind_is_registered_and_executor_emitted():
    assert ac.KIND_ENABLE_AUTO_LIQUID == "ENABLE_AUTO_LIQUID"
    assert ac.KIND_ENABLE_AUTO_LIQUID in ac.ALL_KINDS
    assert ac.KIND_ENABLE_AUTO_LIQUID in ac.EXECUTOR_EMITTED_KINDS


def test_build_enable_auto_liquid_action_row():
    row = ac.build_enable_auto_liquid_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t",
        plan_run_id="p1", wave_id="w1")
    assert row["action_kind"] == ac.KIND_ENABLE_AUTO_LIQUID
    assert row["scope"] == ac.SCOPE_TABLE
    assert row["phase"] == ac.PHASE_POST_CONVERT
    assert row["blocking"] is False
    assert row["target_kind"] == ac.TARGET_TABLE
    assert row["status"] == ac.STATUS_OPEN
    assert (row["remediation_sql"] or "").strip() == "ALTER TABLE c.s.t CLUSTER BY AUTO;"
    assert row["action_key"] == "ENABLE_AUTO_LIQUID|c.s.t|c.s.t"


def test_build_enable_auto_liquid_action_honors_remediation_override():
    row = ac.build_enable_auto_liquid_action(
        full_name="c.s.t", catalog="c", schema="s", table_name="t",
        remediation_sql="ALTER TABLE c.s.t REPLACE PARTITIONED BY WITH CLUSTER BY AUTO;")
    assert "REPLACE PARTITIONED BY WITH CLUSTER BY AUTO" in row["remediation_sql"]


def test_build_enable_auto_liquid_action_rejects_empty_full_name():
    with pytest.raises(ValueError):
        ac.build_enable_auto_liquid_action(
            full_name="", catalog="c", schema="s", table_name="t")


# --------------------------------------------------------------------------- #
# Conflict-retry on the Delta write sites (parallel bulk-convert waves).
# The write helpers wrap their MERGE/UPDATE in concurrency.run_with_conflict_retry:
# transient row-level conflicts retry; metadata/protocol drift fails loud. No Spark.
# --------------------------------------------------------------------------- #

class _FakeSpark:
    """Raises a conflict on the first `fail_times` sql() calls, then succeeds.

    Reads (SELECT) never fail here — only the write MERGE/UPDATE is exercised.
    createDataFrame is a no-op stub so merge_action_rows runs Spark-free.
    """

    def __init__(self, fail_times: int, error: str):
        self.fail_times = fail_times
        self.error = error
        self.sql_calls = 0

    def createDataFrame(self, data, schema=None):
        class _DF:
            def createOrReplaceTempView(self, _name):
                return None
        return _DF()

    def sql(self, _query):
        self.sql_calls += 1
        if self.sql_calls <= self.fail_times:
            raise RuntimeError(self.error)
        class _R:
            def collect(self_inner):
                return []
        return _R()


_MERGE_ROWS = [ac.build_action_row(
    action_kind=ac.KIND_RESHARE_D2D, scope=ac.SCOPE_TABLE,
    phase=ac.PHASE_POST_CONVERT, title="x", full_name="c.s.t", action_id="fixed",
)]


def test_merge_action_rows_retries_then_succeeds():
    spark = _FakeSpark(fail_times=1, error="ConcurrentAppendException: files added")
    sleeps = []
    n = ac.merge_action_rows(spark, "c", "s", _MERGE_ROWS, max_attempts=5, sleep=sleeps.append)
    assert n == 1                      # normal return value preserved
    assert spark.sql_calls == 2        # 1 conflict + 1 success
    assert sleeps == [0.5]             # slept once before the retry


def test_merge_action_rows_reraises_non_retryable_without_retry():
    spark = _FakeSpark(fail_times=1, error="MetadataChangedException: schema updated")
    try:
        ac.merge_action_rows(spark, "c", "s", _MERGE_ROWS, sleep=lambda _s: None)
        assert False, "expected the metadata conflict to propagate"
    except RuntimeError as ex:
        assert "MetadataChangedException" in str(ex)
    assert spark.sql_calls == 1        # failed once, no retry


def test_mark_obsolete_retries_then_succeeds():
    spark = _FakeSpark(fail_times=1, error="ConcurrentDeleteReadException")
    sleeps = []
    n = ac.mark_obsolete(spark, "c", "s", ["FREEZE_COMPACTION|c.s.t|job-9"],
                         updated_at=NOW, max_attempts=5, sleep=sleeps.append)
    assert n == 1                      # normal return value preserved
    assert spark.sql_calls == 2        # 1 conflict + 1 success
    assert sleeps == [0.5]


def test_set_action_progress_retries_then_succeeds():
    spark = _FakeSpark(fail_times=1, error="ConcurrentAppendException")
    sleeps = []
    n = ac.set_action_progress(
        spark, "c", "s", ["FREEZE_COMPACTION|c.s.t|job-9"],
        status=ac.STATUS_DONE, updated_at=NOW, max_attempts=5, sleep=sleeps.append,
    )
    assert n == 1                      # normal return value preserved
    assert spark.sql_calls == 2        # 1 conflict + 1 success
    assert sleeps == [0.5]


def test_ownership_kinds_registered_and_paired():
    assert ac.KIND_LEASE_OWNERSHIP == "LEASE_OWNERSHIP"
    assert ac.KIND_RESTORE_OWNERSHIP == "RESTORE_OWNERSHIP"
    assert ac.KIND_LEASE_OWNERSHIP in ac.ALL_KINDS
    assert ac.KIND_RESTORE_OWNERSHIP in ac.ALL_KINDS
    assert ac.OWNERSHIP_KIND_PAIRS == {ac.KIND_LEASE_OWNERSHIP: ac.KIND_RESTORE_OWNERSHIP}


def _authority(case_id, fn=None):
    """A convert_authority verdict evaluated from the shared golden fixture (never hand-built)."""
    import json
    from pathlib import Path

    from migration import authority as authz

    cases = json.loads((Path(__file__).parent / "fixtures" / "convert_authority_cases.json")
                       .read_text())["cases"]
    facts = dict(next(c for c in cases if c["id"] == case_id)["facts"])
    if fn:
        facts["full_name"] = fn
    return authz.evaluate(authz.facts_from_dict(facts))


def test_generate_ownership_actions_self_grantable_emits_grants_and_a_revoke():
    v = _authority("c14_table_manage_before_self_grant", "c.s.t")
    assert v["status"] == "SELF_AUTHORIZABLE"
    rows = ac.generate_ownership_actions([dict(v, plan_run_id="p1", wave_id="w1")])
    kinds = {r["action_kind"]: r for r in rows}
    assert set(kinds) == {ac.KIND_LEASE_OWNERSHIP, ac.KIND_RESTORE_OWNERSHIP}
    lease = kinds[ac.KIND_LEASE_OWNERSHIP]
    restore = kinds[ac.KIND_RESTORE_OWNERSHIP]
    assert lease["phase"] == ac.PHASE_PRE_CONVERT and lease["blocking"] is True
    assert restore["phase"] == ac.PHASE_POST_CONVERT and restore["blocking"] is False
    assert lease["remediation_sql"] == ";\n".join(v["grant_sql"]) + ";"
    assert "OWNER TO" not in lease["remediation_sql"] + restore["remediation_sql"]
    assert restore["remediation_sql"].startswith("REVOKE ")
    assert "job_03d_lease_ownership" in lease["remediation_api"]
    assert v["reason"] in lease["detail"]
    # Same keys as the ownership-lease actions they replace, so MATCH refreshes them in place.
    assert lease["action_key"] == "LEASE_OWNERSHIP|c.s.t|-"
    assert restore["action_key"] == "RESTORE_OWNERSHIP|c.s.t|-"
    assert lease["plan_run_id"] == "p1" and lease["wave_id"] == "w1"


def test_generate_ownership_actions_accepts_the_stored_row_shape():
    """03d hands the persisted convert_authority rows (requests without a grantee) to the generator."""
    from datetime import datetime, timezone

    from migration import authority as authz

    v = _authority("c12_schema_owner_no_table_grants", "c.s.t")
    stored = authz.authority_row(v, source=authz.SOURCE_AUTHORIZE, evaluated_at=datetime.now(timezone.utc))
    assert stored["status"] == "SELF_AUTHORIZABLE"
    assert all("grantee" not in r for r in stored["grant_requests"])
    rows = ac.generate_ownership_actions([stored])
    restore = next(r for r in rows if r["action_kind"] == ac.KIND_RESTORE_OWNERSHIP)
    actor = authz._q(v["actor"])
    assert restore["remediation_sql"] == (
        f"REVOKE MODIFY ON TABLE c.s.t FROM {actor};\nREVOKE SELECT ON TABLE c.s.t FROM {actor};")


def test_generate_ownership_actions_needs_grant_names_who_can_grant_and_promises_no_revoke():
    v = _authority("c0_data_only", "c.s.t")
    assert v["status"] == "NEEDS_GRANT"
    rows = ac.generate_ownership_actions([v])
    assert [r["action_kind"] for r in rows] == [ac.KIND_LEASE_OWNERSHIP]
    lease = rows[0]
    assert lease["remediation_api"] is None
    for req in v["grant_requests"]:
        assert req["ask_text"] in lease["detail"]
    # Routed to the first named grantor (owner -> assignee map routing), never guessed.
    from migration import authority as authz
    assert lease["owner"] == authz.first_grantor(v)
    assert lease["owner"] in next(r["grantors"] for r in v["grant_requests"] if not r["self_grantable"])


def test_self_grantable_lease_action_has_no_owner_to_route_to():
    rows = ac.generate_ownership_actions([_authority("c14_table_manage_before_self_grant", "c.s.t")])
    assert next(r for r in rows if r["action_kind"] == ac.KIND_LEASE_OWNERSHIP)["owner"] is None


def test_generate_ownership_actions_skips_authorized_and_unknown():
    assert ac.generate_ownership_actions([_authority("c4_actor_owns_table")]) == []
    assert ac.generate_ownership_actions([_authority("e1_reads_failed")]) == []


def test_actions_from_manifest_row_emits_ownership_pair_from_authority():
    row = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t"}
    out = ac.actions_from_manifest_row(
        row, authority=_authority("c14_table_manage_before_self_grant", "c.s.t"))
    assert {r["action_kind"] for r in out} == {ac.KIND_LEASE_OWNERSHIP, ac.KIND_RESTORE_OWNERSHIP}


def test_actions_from_manifest_row_no_ownership_by_default():
    row = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t"}
    assert ac.actions_from_manifest_row(row) == []  # clean row, no ownership info -> nothing


def test_writer_stop_pair_for_scheduled_and_event_jobs():
    common = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t",
              "source": "profiler", "plan_run_id": None, "wave_id": None,
              "detected_at": None, "updated_at": None}
    for wc in ("SCHEDULED_JOB", "EVENT_JOB"):
        rows = ac._writer_stop_actions(
            {}, [{"writer_class": wc, "entity_id": "812", "confidence": "high"}], common)
        assert [r["action_kind"] for r in rows] == [ac.KIND_STOP_WRITER_JOB, ac.KIND_RESTART_WRITER_JOB]
        pre, post = rows
        assert pre["target_kind"] == ac.TARGET_JOB and pre["target_id"] == "812"
        assert pre["phase"] == ac.PHASE_PRE_CONVERT and pre["blocking"] is True
        assert pre["remediation_api"]  # trust surface: never empty
        assert post["phase"] == ac.PHASE_POST_CONVERT and post["blocking"] is False
        assert post["remediation_api"]


def test_writer_stop_noop_for_clean_and_adhoc_and_unattributed():
    common = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t",
              "source": None, "plan_run_id": None, "wave_id": None,
              "detected_at": None, "updated_at": None}
    assert ac._writer_stop_actions({}, [], common) == []                                  # clean
    assert ac._writer_stop_actions({}, [{"writer_class": "NOTEBOOK", "entity_id": "n1"}], common) == []
    assert ac._writer_stop_actions({}, [{"writer_class": "SCHEDULED_JOB", "entity_id": None}], common) == []


def test_writer_stop_dedupes_same_job_across_writer_rows():
    common = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t",
              "source": None, "plan_run_id": None, "wave_id": None,
              "detected_at": None, "updated_at": None}
    rows = ac._writer_stop_actions({}, [
        {"writer_class": "SCHEDULED_JOB", "entity_id": "812"},
        {"writer_class": "EVENT_JOB", "entity_id": "812"},
    ], common)
    assert [r["action_kind"] for r in rows] == [ac.KIND_STOP_WRITER_JOB, ac.KIND_RESTART_WRITER_JOB]


def test_freeze_kind_pairs_includes_writer():
    assert ac.FREEZE_KIND_PAIRS[ac.KIND_STOP_WRITER_JOB] == ac.KIND_RESTART_WRITER_JOB


def test_column_partition_still_exact_after_writer_kinds():
    union = ac.IDENTITY_COLUMNS | ac.GENERATOR_OWNED_COLUMNS | ac.OPERATOR_OWNED_COLUMNS
    assert union == set(ac.ACTION_COLUMN_NAMES)
    assert (len(ac.IDENTITY_COLUMNS) + len(ac.GENERATOR_OWNED_COLUMNS)
            + len(ac.OPERATOR_OWNED_COLUMNS)) == len(ac.ACTION_COLUMN_NAMES)


# --------------------------------------------------------------------------- #
# select_wave_freeze_actions — wave scoping by MEMBER identity (regression: actions
# carry NULL plan_run_id/wave_id, so a plan+wave predicate silently selects nothing).
# --------------------------------------------------------------------------- #

def _freeze_action(kind, *, scope, full_name=None, catalog="c", schema="s",
                   target_kind=None, target_id=None):
    # Deliberately NULL plan_run_id/wave_id — that is exactly the estate condition
    # (actions are generated table/schema-scoped, never stamped with a plan or wave).
    return {"action_kind": kind, "scope": scope, "plan_run_id": None, "wave_id": None,
            "catalog": catalog, "schema": schema, "full_name": full_name,
            "target_kind": target_kind, "target_id": target_id}


def test_select_wave_freeze_actions_selects_null_plan_wave_member():
    """A STOP_STREAM action with NULL plan_run_id/wave_id is STILL selected for its wave
    (the whole point of the fix: scope by member full_name, not by the never-populated
    plan/wave columns). Schema-scoped PO for the wave's schema is included; non-member
    tables and other schemas are excluded."""
    stop_member = _freeze_action(ac.KIND_STOP_STREAM, scope=ac.SCOPE_TABLE,
                                 full_name="c.s.t1", target_kind="JOB", target_id="j1")
    po_member = _freeze_action(ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA,
                               full_name=None, catalog="c", schema="s")
    stop_nonmember = _freeze_action(ac.KIND_STOP_STREAM, scope=ac.SCOPE_TABLE,
                                    full_name="c.s.OTHER", target_kind="JOB", target_id="j2")
    po_otherschema = _freeze_action(ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA,
                                    full_name=None, catalog="c", schema="OTHER")

    selected = ac.select_wave_freeze_actions(
        [stop_member, po_member, stop_nonmember, po_otherschema],
        member_full_names=["c.s.t1"], wave_schemas=[("c", "s")])

    picked = {(r["action_kind"], r.get("full_name"), r["scope"]) for r in selected}
    assert (ac.KIND_STOP_STREAM, "c.s.t1", ac.SCOPE_TABLE) in picked       # NULL plan/wave, still selected
    assert (ac.KIND_FREEZE_COMPACTION, None, ac.SCOPE_SCHEMA) in picked    # schema PO for wave's schema
    assert (ac.KIND_STOP_STREAM, "c.s.OTHER", ac.SCOPE_TABLE) not in picked  # non-member table excluded
    assert len(selected) == 2


def test_select_wave_freeze_actions_empty_scope_selects_nothing():
    stop = _freeze_action(ac.KIND_STOP_STREAM, scope=ac.SCOPE_TABLE, full_name="c.s.t1")
    po = _freeze_action(ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA, catalog="c", schema="s")
    # No members and no schemas -> a freeze that matches nothing (never freeze-all).
    assert ac.select_wave_freeze_actions([stop, po], member_full_names=[], wave_schemas=[]) == []
    # Table-scoped rows are never matched by schema membership alone.
    assert ac.select_wave_freeze_actions([stop], member_full_names=[], wave_schemas=[("c", "s")]) == []


def test_post_convert_deferred_kinds_are_exactly_the_four_non_holding_kinds():
    assert set(ac.POST_CONVERT_DEFERRED_KINDS) == {
        ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION,
        ac.KIND_ENABLE_AUTO_LIQUID,
        ac.KIND_RETIRE_MAINTENANCE_JOB,
        ac.KIND_RESTORE_OWNERSHIP,
    }
    assert set(ac.POST_CONVERT_DEFERRED_KINDS) <= set(ac.ALL_KINDS)


def test_wave_action_status_splits_holding_from_deferred_post_convert():
    sql = ac.create_wave_action_status_view_sql("c", "s")
    for col in ("open_optimize", "open_retire", "open_restore_ownership", "open_post_convert_holding"):
        assert f"AS {col}" in sql, col
    assert (
        f"action_kind IN ('{ac.KIND_ENABLE_PREDICTIVE_OPTIMIZATION}', '{ac.KIND_ENABLE_AUTO_LIQUID}')) AS open_optimize"
    ) in sql
    assert f"action_kind = '{ac.KIND_RETIRE_MAINTENANCE_JOB}') AS open_retire" in sql
    assert f"action_kind = '{ac.KIND_RESTORE_OWNERSHIP}') AS open_restore_ownership" in sql
    deferred = ", ".join(f"'{k}'" for k in ac.POST_CONVERT_DEFERRED_KINDS)
    assert (
        f"AND phase = '{ac.PHASE_POST_CONVERT}' AND action_kind NOT IN ({deferred})) AS open_post_convert_holding"
    ) in sql
    # The lumped total is unchanged — other readers depend on it.
    assert "AS open_post_convert," in sql


def _fa(key, kind, status, full_name="c.s.t"):
    return {"action_key": key, "action_kind": kind, "status": status, "full_name": full_name,
            "plan_run_id": "p1", "wave_id": "w1"}


def test_deferred_status_and_deferrable_kinds():
    assert ac.STATUS_DEFERRED == "DEFERRED"
    assert ac.DEFERRABLE_KINDS == (ac.KIND_RESHARE_D2D, ac.KIND_ENABLE_COMPAT)
    assert (ac.FINISH_RUN, ac.FINISH_DEFER, ac.FINISH_REOPEN) == ("run", "defer", "reopen")


def test_finish_transition_defer_picks_open_rows_of_kind_and_requires_reason():
    rows = [_fa("k1", ac.KIND_RESHARE_D2D, ac.STATUS_OPEN),
            _fa("k2", ac.KIND_RESHARE_D2D, ac.STATUS_IN_PROGRESS),
            _fa("k3", ac.KIND_RESHARE_D2D, ac.STATUS_DONE),
            _fa("k4", ac.KIND_ENABLE_COMPAT, ac.STATUS_OPEN)]
    got = ac.finish_transition(rows, mode="defer", kind=ac.KIND_RESHARE_D2D,
                               reason="  share owner away  ")
    assert [t["action_key"] for t in got] == ["k1", "k2"]
    assert {t["status"] for t in got} == {ac.STATUS_DEFERRED}
    assert got[0]["resolution"] == "deferred: share owner away"
    assert got[0]["full_name"] == "c.s.t" and got[0]["plan_run_id"] == "p1"
    with pytest.raises(ValueError, match="defer_reason"):
        ac.finish_transition(rows, mode="defer", kind=ac.KIND_RESHARE_D2D, reason="  ")


def test_finish_transition_reopen_picks_deferred_rows_only():
    rows = [_fa("k1", ac.KIND_ENABLE_COMPAT, ac.STATUS_DEFERRED),
            _fa("k2", ac.KIND_ENABLE_COMPAT, ac.STATUS_OPEN)]
    got = ac.finish_transition(rows, mode="reopen", kind=ac.KIND_ENABLE_COMPAT, reason=None)
    assert [(t["action_key"], t["status"], t["resolution"]) for t in got] == [
        ("k1", ac.STATUS_OPEN, "reopened")]
    got = ac.finish_transition(rows, mode="reopen", kind=ac.KIND_ENABLE_COMPAT, reason="ready")
    assert got[0]["resolution"] == "reopened: ready"


def test_finish_transition_rejects_non_deferrable_kind_and_bad_mode():
    with pytest.raises(ValueError, match="not deferrable"):
        ac.finish_transition([], mode="defer", kind=ac.KIND_RESTART_STREAM, reason="x")
    with pytest.raises(ValueError, match="finish_action"):
        ac.finish_transition([], mode="run", kind=ac.KIND_RESHARE_D2D, reason="x")


def test_obsolete_detection_and_marking_include_deferred():
    import inspect
    assert "STATUS_DEFERRED" in inspect.getsource(ac.read_open_action_keys)
    assert "STATUS_DEFERRED" in inspect.getsource(ac.mark_obsolete)


def test_read_open_actions_of_kind_delegates_to_read_actions_of_kind():
    import inspect
    src = inspect.getsource(ac.read_open_actions_of_kind)
    assert "read_actions_of_kind(" in src
    assert "statuses=(STATUS_OPEN, STATUS_IN_PROGRESS)" in src


def test_audit_defer_and_reopen_actions():
    from migration import audit
    assert audit.ACTION_DEFER_ACTION == "DEFER_ACTION"
    assert audit.ACTION_REOPEN_ACTION == "REOPEN_ACTION"


def test_wave_action_status_counts_deferred_post_convert():
    sql = ac.create_wave_action_status_view_sql("c", "s")
    assert (f"COUNT_IF(status = '{ac.STATUS_DEFERRED}' AND phase = '{ac.PHASE_POST_CONVERT}') "
            "AS deferred_post_convert") in sql


def test_runbook_action_statuses_include_deferred():
    assert ac.RUNBOOK_ACTION_STATUSES == (ac.STATUS_OPEN, ac.STATUS_IN_PROGRESS, ac.STATUS_DEFERRED)


def test_parse_action_keys_splits_trims_dedupes_and_drops_blanks():
    assert ac.parse_action_keys(None) == []
    assert ac.parse_action_keys("") == []
    assert ac.parse_action_keys(" k1 , k2,,k1 ,") == ["k1", "k2"]


def test_require_action_keys_blank_means_no_narrowing():
    assert ac.require_action_keys(None) == []
    assert ac.require_action_keys("") == []


def test_require_action_keys_parses_named_keys():
    assert ac.require_action_keys("k1, k2") == ["k1", "k2"]


@pytest.mark.parametrize("raw", [",", " , ", "  ", ",,"])
def test_require_action_keys_raises_when_set_but_names_no_key(raw):
    with pytest.raises(ValueError, match="action_keys"):
        ac.require_action_keys(raw)


def test_select_action_keys_narrows_in_requested_order():
    rows = [_fa("k1", ac.KIND_RESHARE_D2D, ac.STATUS_OPEN),
            _fa("k2", ac.KIND_RESHARE_D2D, ac.STATUS_OPEN),
            _fa("k3", ac.KIND_RESHARE_D2D, ac.STATUS_IN_PROGRESS)]
    assert [r["action_key"] for r in ac.select_action_keys(rows, ["k3", "k1"])] == ["k3", "k1"]


def test_select_action_keys_empty_keys_keeps_every_row():
    rows = [_fa("k1", ac.KIND_ENABLE_COMPAT, ac.STATUS_OPEN)]
    assert ac.select_action_keys(rows, []) == rows


def test_select_action_keys_raises_naming_every_unknown_key_before_any_write():
    rows = [_fa("k1", ac.KIND_RESHARE_D2D, ac.STATUS_OPEN)]
    with pytest.raises(ValueError, match="nope, typo"):
        ac.select_action_keys(rows, ["k1", "nope", "typo"])


_CLEAN_DETAIL = (
    "Converted MANAGED table eligible for automatic liquid clustering (CLUSTER BY AUTO). "
    "Databricks recommends auto-liquid + Predictive Optimization for all managed tables; "
    "enabling is metadata-only and reversible (CLUSTER BY NONE). PO clusters asynchronously "
    "and may select no keys. Gated + verified via DESCRIBE TABLE EXTENDED AS JSON."
)


def test_build_enable_auto_liquid_action_clean_detail_is_unchanged():
    for reason in (None, "clean"):
        row = ac.build_enable_auto_liquid_action(
            full_name="c.s.t", catalog="c", schema="s", table_name="t", reason=reason)
        assert row["detail"] == _CLEAN_DETAIL


def test_build_enable_auto_liquid_action_detail_names_why_it_is_held():
    held = {
        "partitioned_needs_reorg": "REPLACE PARTITIONED BY",
        "po_off": "Predictive Optimization is off",
        "layout_unknown": "could not be read",
        "iceberg_needs_confirm": "Iceberg",
        "uniform_needs_confirm": "UniForm",
    }
    for reason, needle in held.items():
        row = ac.build_enable_auto_liquid_action(
            full_name="c.s.t", catalog="c", schema="s", table_name="t", reason=reason)
        assert needle in row["detail"], reason
        assert "reversible (CLUSTER BY NONE)" not in row["detail"], reason


def test_build_enable_auto_liquid_action_detail_for_the_m2_reasons():
    expect = {
        "manual_keys_to_auto": "keeps them as the starting keys",
        "zorder_hints": "ZORDER columns become the clustering keys",
        "zorder_no_valid_hints": "plain CLUSTER BY AUTO",
        "manual_keys_auto_unknown": "could not be read",
    }
    for reason, needle in expect.items():
        row = ac.build_enable_auto_liquid_action(
            full_name="c.s.t", catalog="c", schema="s", table_name="t", reason=reason)
        assert needle in row["detail"], reason
