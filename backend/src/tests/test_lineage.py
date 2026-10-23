"""Unit tests for wave-planner pure logic (lineage.py). No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import lineage as lin  # noqa: E402


# --------------------------------------------------------------------------- #
# classify_consumer_kind
# --------------------------------------------------------------------------- #

def test_classify_kinds():
    assert lin.classify_consumer_kind("JOB") == "job"
    assert lin.classify_consumer_kind("DLT_PIPELINE") == "pipeline"
    assert lin.classify_consumer_kind("DBSQL_DASHBOARD") == "dashboard"
    assert lin.classify_consumer_kind("NOTEBOOK") == "notebook"
    assert lin.classify_consumer_kind("TRINO") == "external"
    assert lin.classify_consumer_kind("") == "other"
    assert lin.classify_consumer_kind(None) == "other"
    assert lin.classify_consumer_kind("SOMETHING_NEW") == "other"


# --------------------------------------------------------------------------- #
# downstream_summary
# --------------------------------------------------------------------------- #

def test_downstream_summary_counts_distinct():
    events = [
        {"entity_type": "JOB", "entity_id": "j1", "created_by": "a@x.com", "downstream_table": "c.s.t2"},
        {"entity_type": "JOB", "entity_id": "j1", "created_by": "a@x.com", "downstream_table": "c.s.t2"},  # dup
        {"entity_type": "DASHBOARD", "entity_id": "d1", "created_by": "b@x.com", "downstream_table": None},
        {"entity_type": "TRINO", "entity_id": "ext1", "created_by": None, "downstream_table": None},
    ]
    s = lin.downstream_summary(events)
    assert s["downstream_tables"] == ["c.s.t2"]
    # distinct consumers: (job,j1),(dashboard,d1),(external,ext1) = 3 ; +1 downstream table = 4
    assert s["downstream_consumer_count"] == 4
    assert s["has_external_consumer"] is True
    assert s["owners"] == ["a@x.com", "b@x.com"]
    assert any(lbl.startswith("table:") for lbl in s["consumer_labels"])


def test_downstream_summary_empty():
    s = lin.downstream_summary(None)
    assert s["downstream_consumer_count"] == 0
    assert s["has_external_consumer"] is False
    assert s["owners"] == []


# --------------------------------------------------------------------------- #
# blast_radius_score
# --------------------------------------------------------------------------- #

def test_blast_radius_base():
    assert lin.blast_radius_score(3) == 3.0


def test_blast_radius_weights_add():
    score = lin.blast_radius_score(
        2, has_streaming=True, has_external_consumer=True, cross_region_risk=True
    )
    assert score == 2 + lin.STREAMING_WEIGHT + lin.EXTERNAL_CONSUMER_WEIGHT + lin.CROSS_REGION_WEIGHT


def test_blast_radius_handles_none_count():
    assert lin.blast_radius_score(None) == 0.0


# --------------------------------------------------------------------------- #
# assign_bucket (precedence)
# --------------------------------------------------------------------------- #

def _bucket(**kw):
    base = dict(
        size_band="<=100GB", blast_radius=0.0, has_streaming=False,
        has_external_consumer=False, is_delta_shared=False, needs_compatibility_mode=False,
    )
    base.update(kw)
    return lin.assign_bucket(**base)


def test_bucket_pilot_simple_small():
    assert _bucket() == lin.BUCKET_PILOT


def test_bucket_streaming_wins_over_everything():
    # Streaming is always isolated, even for large / compat / shared tables.
    assert _bucket(has_streaming=True) == lin.BUCKET_STREAMING
    assert _bucket(has_streaming=True, size_band=">10TB", needs_compatibility_mode=True) == lin.BUCKET_STREAMING


def test_bucket_compat_and_share_beat_large():
    # New precedence: streaming > compat > share > final_large.
    assert _bucket(size_band=">10TB", needs_compatibility_mode=True) == lin.BUCKET_COMPAT
    assert _bucket(size_band=">10TB", is_delta_shared=True) == lin.BUCKET_SHARE
    assert _bucket(size_band="~10TB") == lin.BUCKET_FINAL_LARGE


def test_bucket_compat_before_share():
    assert _bucket(needs_compatibility_mode=True, is_delta_shared=True) == lin.BUCKET_COMPAT


def test_bucket_share():
    assert _bucket(is_delta_shared=True) == lin.BUCKET_SHARE


def test_bucket_streaming_isolated():
    assert _bucket(has_streaming=True) == lin.BUCKET_STREAMING


def test_bucket_external_consumer_disqualifies_pilot():
    assert _bucket(has_external_consumer=True) == lin.BUCKET_NORMAL


def test_bucket_high_blast_disqualifies_pilot():
    assert _bucket(blast_radius=lin.PILOT_MAX_SCORE + 1) == lin.BUCKET_NORMAL


def test_bucket_midsize_is_normal():
    assert _bucket(size_band="~1TB") == lin.BUCKET_NORMAL


# --------------------------------------------------------------------------- #
# grain_key
# --------------------------------------------------------------------------- #

def test_grain_key_variants():
    t = {"catalog": "c", "schema": "s"}
    assert lin.grain_key(t, lin.GRAIN_CATALOG_SCHEMA) == "c.s"
    assert lin.grain_key(t, lin.GRAIN_SCHEMA) == "s"
    assert lin.grain_key(t, lin.GRAIN_CATALOG) == "c"
    # falls back to schema when catalog is absent
    assert lin.grain_key({"schema": "s"}, lin.GRAIN_CATALOG_SCHEMA) == "s"


# --------------------------------------------------------------------------- #
# pick_pilot (auto-pick, single schema, capped)
# --------------------------------------------------------------------------- #

def _p(full_name, catalog, schema, blast=0.0, size=0):
    return {"full_name": full_name, "catalog": catalog, "schema": schema,
            "bucket": lin.BUCKET_PILOT, "blast_radius": blast, "table_size_bytes": size}


def test_pick_pilot_auto_picks_densest_schema_and_caps():
    pool = [_p(f"c.a.t{i}", "c", "a") for i in range(20)]  # 20 in schema a
    pool += [_p(f"c.b.t{i}", "c", "b") for i in range(3)]   # 3 in schema b
    chosen, demoted = lin.pick_pilot(pool, pilot_max_tables=15)
    assert len(chosen) == 15
    assert all(t["schema"] == "a" for t in chosen)          # densest schema wins
    # 5 leftover from a + all 3 from b are demoted
    assert len(demoted) == 8


def test_pick_pilot_honours_explicit_schema():
    pool = [_p(f"c.a.t{i}", "c", "a") for i in range(5)]
    pool += [_p(f"c.b.t{i}", "c", "b") for i in range(2)]
    chosen, _ = lin.pick_pilot(pool, pilot_schema="c.b")
    assert {t["schema"] for t in chosen} == {"b"}


def test_pick_pilot_empty():
    assert lin.pick_pilot([]) == ([], [])


# --------------------------------------------------------------------------- #
# sequence_waves
# --------------------------------------------------------------------------- #

def _t(full_name, schema, bucket, blast, size=0, catalog="c"):
    return {"full_name": full_name, "catalog": catalog, "schema": schema, "bucket": bucket,
            "blast_radius": blast, "table_size_bytes": size}


def test_sequence_pilot_first_final_last():
    tables = [
        _t("c.fin.big", "fin", lin.BUCKET_FINAL_LARGE, 50, 20 * 1024**4),
        _t("c.fin.p1", "fin", lin.BUCKET_PILOT, 0, 10),
        _t("c.sales.n1", "sales", lin.BUCKET_NORMAL, 4, 100),
        _t("c.fin.share1", "fin", lin.BUCKET_SHARE, 8, 100),
        _t("c.fin.compat1", "fin", lin.BUCKET_COMPAT, 6, 100),
    ]
    waves, assignments = lin.sequence_waves(tables)
    order = [w["bucket"] for w in waves]
    assert order[0] == lin.BUCKET_PILOT
    assert order[-1] == lin.BUCKET_FINAL_LARGE
    # compat appears before share
    assert order.index(lin.BUCKET_COMPAT) < order.index(lin.BUCKET_SHARE)
    # every table assigned, wave_ids are zero-padded + ordered
    assert len(assignments) == len(tables)
    assert assignments["c.fin.p1"]["wave_id"] == "wave_00"


def test_sequence_streaming_isolated_after_normal_before_compat():
    tables = [
        _t("c.s.n1", "s", lin.BUCKET_NORMAL, 1, 100),
        _t("c.s.st1", "s", lin.BUCKET_STREAMING, 5, 100),
        _t("c.s.cm1", "s", lin.BUCKET_COMPAT, 6, 100),
    ]
    waves, _ = lin.sequence_waves(tables)
    order = [w["bucket"] for w in waves]
    assert order.index(lin.BUCKET_NORMAL) < order.index(lin.BUCKET_STREAMING) < order.index(lin.BUCKET_COMPAT)


def test_sequence_normal_grouped_by_catalog_schema_ascending():
    tables = [
        _t("c.high.t", "high", lin.BUCKET_NORMAL, 20, 100),
        _t("c.low.t", "low", lin.BUCKET_NORMAL, 1, 100),
    ]
    waves, _ = lin.sequence_waves(tables)
    # lower average blast-radius slice should come first; label uses catalog.schema grain
    assert waves[0]["wave_label"].startswith("c.low")


def test_sequence_within_wave_sorted_by_blast():
    tables = [
        _t("c.s.b", "s", lin.BUCKET_NORMAL, 9, 100),
        _t("c.s.a", "s", lin.BUCKET_NORMAL, 1, 100),
    ]
    waves, _ = lin.sequence_waves(tables)
    assert waves[0]["members"] == ["c.s.a", "c.s.b"]


def test_sequence_pilot_capped_and_demotes_rest_to_normal():
    tables = [_t(f"c.a.t{i}", "a", lin.BUCKET_PILOT, 0, 10) for i in range(20)]
    waves, assignments = lin.sequence_waves(tables, pilot_max_tables=15)
    pilot_waves = [w for w in waves if w["bucket"] == lin.BUCKET_PILOT]
    assert len(pilot_waves) == 1
    assert len(pilot_waves[0]["members"]) == 15
    # the 5 leftovers are re-bucketed to normal
    normal_members = [m for w in waves if w["bucket"] == lin.BUCKET_NORMAL for m in w["members"]]
    assert len(normal_members) == 5


def test_sequence_pilot_group_key_is_real_schema():
    tables = [
        _t("c.fin.p1", "fin", lin.BUCKET_PILOT, 0, 10),
        _t("c.fin.p2", "fin", lin.BUCKET_PILOT, 1, 10),
    ]
    waves, _ = lin.sequence_waves(tables)
    pilot = next(w for w in waves if w["bucket"] == lin.BUCKET_PILOT)
    # group_key is now the pilot's real catalog.schema, not the literal "pilot".
    assert pilot["group_key"] == "c.fin"
    # ...but the operator-facing label is unchanged.
    assert pilot["wave_label"] == "Pilot — prove the tooling"


def test_sequence_pilot_and_demoted_normal_share_group_key():
    # 17 pilot-eligible tables in one schema, cap 15 -> 2 demote to normal, same schema.
    tables = [_t(f"c.fin.t{i:02d}", "fin", lin.BUCKET_PILOT, 0, 10) for i in range(17)]
    waves, _ = lin.sequence_waves(tables, pilot_max_tables=15)
    pilot = next(w for w in waves if w["bucket"] == lin.BUCKET_PILOT)
    normal = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL]
    assert pilot["group_key"] == "c.fin"
    # Documents the convert-scope ripple that Task A3 handles: the pilot's schema also
    # hosts normal waves with the same group_key.
    assert normal and all(w["group_key"] == "c.fin" for w in normal)


def test_sequence_chunks_by_count():
    tables = [_t(f"c.s.t{i}", "s", lin.BUCKET_NORMAL, 1, 100) for i in range(7)]
    waves, _ = lin.sequence_waves(tables, wave_max_tables=3, wave_max_bytes=None)
    normal = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL]
    assert [len(w["members"]) for w in normal] == [3, 3, 1]
    # chunk labels carry (seq/n)
    assert normal[0]["wave_label"].endswith("(1/3) (domain)")


def test_sequence_chunks_by_bytes():
    tb = 1024 ** 4
    tables = [_t(f"c.s.t{i}", "s", lin.BUCKET_NORMAL, 1, 10 * tb) for i in range(3)]
    waves, _ = lin.sequence_waves(tables, wave_max_tables=1000, wave_max_bytes=25 * tb)
    normal = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL]
    # 3 x 10TB with 25TB cap -> [2, 1]
    assert [len(w["members"]) for w in normal] == [2, 1]


def test_sequence_sizing_rollup_uses_largest_member():
    tb = 1024 ** 4
    tables = [
        _t("c.s.small", "s", lin.BUCKET_NORMAL, 1, 50 * 1024**3),   # 50GB
        _t("c.s.big", "s", lin.BUCKET_NORMAL, 1, 5 * tb),            # 5TB -> ~10TB band
    ]
    waves, _ = lin.sequence_waves(tables)
    w = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL][0]
    assert w["max_size_band"] == "~10TB"
    assert w["recommended_cores"] == 256
    assert "256-core" in w["recommended_warehouse"]


def test_sequence_oversized_singleton_flagged():
    tb = 1024 ** 4
    tables = [_t("c.s.huge", "s", lin.BUCKET_FINAL_LARGE, 1, 30 * tb)]  # > 25TB cap
    waves, _ = lin.sequence_waves(tables, wave_max_bytes=25 * tb)
    w = waves[0]
    assert w["exceeds_wave_byte_cap"] is True
    assert w["planned_table_count"] == 1


def test_sequence_group_metadata():
    tables = [_t(f"c.s.t{i}", "s", lin.BUCKET_NORMAL, 1, 100) for i in range(5)]
    waves, assignments = lin.sequence_waves(tables, wave_max_tables=2, wave_max_bytes=None)
    normal = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL]
    assert [w["wave_count_in_group"] for w in normal] == [3, 3, 3]
    assert [w["planned_table_count"] for w in normal] == [2, 2, 1]
    assert all(w["group_key"] == "c.s" for w in normal)


def test_sequence_grain_catalog():
    tables = [
        _t("c.a.t", "a", lin.BUCKET_NORMAL, 1, 100),
        _t("c.b.t", "b", lin.BUCKET_NORMAL, 1, 100),
    ]
    waves, _ = lin.sequence_waves(tables, grain=lin.GRAIN_CATALOG)
    normal = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL]
    # both schemas collapse into one catalog-grain slice
    assert len(normal) == 1
    assert normal[0]["group_key"] == "c"


def test_sequence_invalid_grain_raises():
    with pytest.raises(ValueError):
        lin.sequence_waves([], grain="table")


def test_sequence_bad_pilot_schema_raises():
    with pytest.raises(ValueError):
        lin.sequence_waves([], pilot_schema="just_schema")


def test_pick_pilot_missing_explicit_schema_raises():
    pool = [_p("c.a.t", "c", "a")]
    with pytest.raises(ValueError):
        lin.pick_pilot(pool, pilot_schema="c.zzz")


# --------------------------------------------------------------------------- #
# resolve_wave_targets (selector)
# --------------------------------------------------------------------------- #

def _wr(wave_id, order, bucket, group_key, state, plan="p1"):
    return {"plan_run_id": plan, "wave_id": wave_id, "wave_order": order,
            "bucket": bucket, "group_key": group_key, "state": state}


def test_resolve_targets_by_schema_orders_and_filters_runnable():
    rows = [
        _wr("wave_01", 1, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK"),
        _wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "CONVERTED"),   # nothing runnable
        _wr("wave_02", 2, lin.BUCKET_NORMAL, "c.s", "CONVERTING"),  # resume
    ]
    out = lin.resolve_wave_targets(rows, target_schema="c.s")
    assert [o["wave_id"] for o in out] == ["wave_01", "wave_02"]
    assert all(o["plan_run_id"] == "p1" for o in out)


def test_resolve_targets_excludes_streaming_unless_opted_in():
    rows = [
        _wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK"),
        _wr("wave_01", 1, lin.BUCKET_STREAMING, "c.s", "DRY_RUN_OK"),
    ]
    assert [o["wave_id"] for o in lin.resolve_wave_targets(rows, target_schema="c.s")] == ["wave_00"]
    both = lin.resolve_wave_targets(rows, target_schema="c.s", include_streaming=True)
    assert [o["wave_id"] for o in both] == ["wave_00", "wave_01"]
    stream_only = lin.resolve_wave_targets(rows, bucket=lin.BUCKET_STREAMING)
    assert [o["wave_id"] for o in stream_only] == ["wave_01"]


def test_resolve_targets_explicit_ids_and_unknown_raises():
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    assert [o["wave_id"] for o in lin.resolve_wave_targets(rows, wave_ids=["wave_00"])] == ["wave_00"]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows, wave_ids=["wave_99"])


def test_resolve_targets_requires_a_selector():
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows)


def test_resolve_targets_exclusive_selectors():
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows, wave_ids=["wave_00"], target_schema="c.s")


def test_resolve_targets_multiple_plan_ids_raises():
    rows = [
        _wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK", plan="p1"),
        _wr("wave_01", 1, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK", plan="p2"),
    ]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows, target_schema="c.s")


def test_resolve_targets_empty_scope_is_noop():
    # Scope MATCHES a wave (group_key c.s) but nothing is runnable -> idempotent [].
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "CONVERTED")]
    assert lin.resolve_wave_targets(rows, target_schema="c.s") == []


def test_resolve_targets_zero_match_scope_raises():
    # Scope matches NO wave at all (typo) -> fail loudly (not a silent no-op).
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows, target_schema="c.typo")
    # A VALID bucket that filters the schema down to nothing -> zero-match error.
    # (bucket must accompany a target_schema now — see the filter rule below.)
    with pytest.raises(ValueError, match="matched no waves"):
        lin.resolve_wave_targets(rows, target_schema="c.s", bucket=lin.BUCKET_COMPAT)


def test_resolve_targets_unknown_bucket_raises():
    # An unknown bucket is a typo -> distinct, clearer error than "matched no waves".
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    with pytest.raises(ValueError, match="unknown bucket"):
        lin.resolve_wave_targets(rows, bucket="nonexistent_bucket")
    # Every canonical bucket value is accepted by the validation (no false rejects).
    # bucket is a filter, so pair it with a target_schema; a bucket absent from the
    # scoped waves -> zero-match (not "unknown bucket").
    for b in lin.ALL_BUCKETS:
        if b == lin.BUCKET_NORMAL:
            continue
        with pytest.raises(ValueError, match="matched no waves"):
            lin.resolve_wave_targets(rows, target_schema="c.s", bucket=b)


def test_resolve_targets_non_streaming_bucket_alone_raises():
    # A non-streaming bucket ALONE (no schema, no explicit ids) would convert that bucket
    # across EVERY schema in the plan -> rejected. Must pair with a target_schema.
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    with pytest.raises(ValueError, match="bucket is a filter"):
        lin.resolve_wave_targets(rows, bucket=lin.BUCKET_NORMAL)
    with pytest.raises(ValueError, match="bucket is a filter"):
        lin.resolve_wave_targets(rows, bucket=lin.BUCKET_COMPAT)


def test_resolve_targets_bucket_with_schema_resolves():
    # target_schema + bucket is a valid narrowing (bucket matches the scoped wave).
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    out = lin.resolve_wave_targets(rows, target_schema="c.s", bucket=lin.BUCKET_NORMAL)
    assert [o["wave_id"] for o in out] == ["wave_00"]


def test_resolve_targets_streaming_bucket_alone_resolves():
    # streaming is the one bucket a target_schema scope excludes, so it stays a valid
    # standalone selector even without a target_schema.
    rows = [_wr("wave_00", 0, lin.BUCKET_STREAMING, "c.s", "DRY_RUN_OK")]
    out = lin.resolve_wave_targets(rows, bucket=lin.BUCKET_STREAMING)
    assert [o["wave_id"] for o in out] == ["wave_00"]


def test_resolve_targets_dedupes_explicit_ids():
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK")]
    out = lin.resolve_wave_targets(rows, wave_ids=["wave_00", "wave_00", " wave_00 "])
    assert [o["wave_id"] for o in out] == ["wave_00"]


def test_resolve_targets_null_plan_run_id_raises():
    rows = [_wr("wave_00", 0, lin.BUCKET_NORMAL, "c.s", "DRY_RUN_OK", plan=None)]
    with pytest.raises(ValueError):
        lin.resolve_wave_targets(rows, target_schema="c.s")


# --------------------------------------------------------------------------- #
# _sizing_rollup — unknown size is conservative (not silently 32 cores)
# --------------------------------------------------------------------------- #

def test_sequence_all_unknown_size_is_conservative():
    tables = [
        _t("c.s.a", "s", lin.BUCKET_NORMAL, 1, None),
        _t("c.s.b", "s", lin.BUCKET_NORMAL, 1, None),
    ]
    waves, _ = lin.sequence_waves(tables)
    w = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL][0]
    # None-size members must not be treated as 0-byte (32 cores); use the top band.
    assert w["recommended_cores"] == 256


def test_sequence_unknown_member_not_downsized_by_small_known():
    tables = [
        _t("c.s.small", "s", lin.BUCKET_NORMAL, 1, 10 * 1024**3),  # 10GB -> 32 cores
        _t("c.s.unknown", "s", lin.BUCKET_NORMAL, 1, None),         # unknown -> 256 cores
    ]
    waves, _ = lin.sequence_waves(tables, wave_max_tables=10, wave_max_bytes=None)
    w = [w for w in waves if w["bucket"] == lin.BUCKET_NORMAL][0]
    assert w["recommended_cores"] == 256


# --------------------------------------------------------------------------- #
# build_plan_rows
# --------------------------------------------------------------------------- #

def test_build_plan_rows_empty_returns_typed_empty():
    wr, cr = lin.build_plan_rows([], {}, plan_run_id="p1", planned_at=None, updated_at=None)
    assert wr == [] and cr == []


# --------------------------------------------------------------------------- #
# wave_summary
# --------------------------------------------------------------------------- #

def test_wave_summary_rollup():
    rows = [
        {"table_size_bytes": 100, "est_data_copy_minutes": 10.0, "est_downtime_minutes": 2.0, "owners": ["a"]},
        {"table_size_bytes": 200, "est_data_copy_minutes": 30.0, "est_downtime_minutes": 5.0, "owners": ["b", "a"]},
    ]
    s = lin.wave_summary(rows, parallelism=4)
    assert s["total_tables"] == 2
    assert s["total_bytes"] == 300
    assert s["sum_est_data_copy_minutes"] == 40.0
    assert s["parallel_est_data_copy_minutes"] == 10.0
    assert s["max_est_downtime_minutes"] == 5.0
    assert s["owners"] == ["a", "b"]


def test_wave_summary_empty():
    s = lin.wave_summary([], parallelism=1)
    assert s["total_tables"] == 0
    assert s["max_est_downtime_minutes"] == 0.0


# --------------------------------------------------------------------------- #
# DDL helpers
# --------------------------------------------------------------------------- #

def test_create_sql_contains_table_and_columns():
    sql = lin.create_wave_plan_table_sql("cat", "mig")
    assert "cat.mig.wave_plan" in sql
    assert "wave_id STRING" in sql
    assert "blast_radius DOUBLE" in sql
    comms = lin.create_comms_feed_table_sql("cat", "mig")
    assert "cat.mig.comms_feed" in comms
    assert "downstream_consumers STRING" in comms


def test_build_rows_have_all_columns():
    wr = lin.build_wave_plan_row(full_name="c.s.t", wave_id="wave_00")
    assert set(wr.keys()) == set(lin.WAVE_PLAN_COLUMN_NAMES)
    cr = lin.build_comms_feed_row(full_name="c.s.t", wave_id="wave_00")
    assert set(cr.keys()) == set(lin.COMMS_FEED_COLUMN_NAMES)


# --------------------------------------------------------------------------- #
# bucket precedence predicate (flag/bucket coherence)
# --------------------------------------------------------------------------- #

def test_bucket_rank_orders_specials_before_pilot():
    assert lin.bucket_rank("streaming") < lin.bucket_rank("compat") < lin.bucket_rank("share")
    assert lin.bucket_rank("share") < lin.bucket_rank("final_large") < lin.bucket_rank("pilot")
    assert lin.bucket_rank("pilot") <= lin.bucket_rank("normal")
    assert lin.bucket_rank("nonsense") >= lin.bucket_rank("normal")  # unknown sorts last


def test_live_flags_outrank_bucket():
    # a live streaming writer outranks any non-streaming bucket (the reported bug)
    assert lin.live_flags_outrank_bucket("pilot", has_streaming_writer=True,
                                         needs_compatibility_mode=False, is_delta_shared=False)
    assert lin.live_flags_outrank_bucket("compat", has_streaming_writer=True,
                                         needs_compatibility_mode=False, is_delta_shared=False)
    # compat flag outranks share/final_large/pilot/normal but NOT streaming
    assert lin.live_flags_outrank_bucket("pilot", has_streaming_writer=False,
                                         needs_compatibility_mode=True, is_delta_shared=False)
    assert not lin.live_flags_outrank_bucket("streaming", has_streaming_writer=False,
                                             needs_compatibility_mode=True, is_delta_shared=False)
    # a table already IN its highest-precedence bucket is coherent → no outrank
    assert not lin.live_flags_outrank_bucket("streaming", has_streaming_writer=True,
                                             needs_compatibility_mode=False, is_delta_shared=False)
    # path is NOT an input → never causes outrank (pilot+path is legitimate)
    assert not lin.live_flags_outrank_bucket("pilot", has_streaming_writer=False,
                                             needs_compatibility_mode=False, is_delta_shared=False)


def test_build_plan_rows_are_internally_coherent():
    """Fixture invariant: because the planner derives each row's bucket FROM its flags via
    assign_bucket, no wave_plan row may have flags that outrank its own stored bucket. Pins
    the internal-consistency guarantee at the pure layer (the wave_plan_integrity probe is the
    runtime mirror). If this ever fires, the planner is emitting self-contradictory rows."""
    def _scored(full_name, schema, *, size_band, blast, size,
                streaming=False, ext=False, shared=False, compat=False):
        bucket = lin.assign_bucket(
            size_band=size_band, blast_radius=blast, has_streaming=streaming,
            has_external_consumer=ext, is_delta_shared=shared, needs_compatibility_mode=compat,
        )
        return {
            "full_name": full_name, "catalog": "c", "schema": schema, "bucket": bucket,
            "blast_radius": blast, "table_size_bytes": size, "size_band": size_band,
            "downstream_consumer_count": 0,
            "has_streaming": streaming, "has_external_consumer": ext,
            "is_delta_shared": shared, "needs_compatibility_mode": compat,
        }

    scored = [
        _scored("c.s.stream", "s", size_band="<=100GB", blast=5, size=100, streaming=True),
        _scored("c.s.compat", "s", size_band="<=100GB", blast=6, size=100, compat=True),
        _scored("c.s.share", "s", size_band="<=100GB", blast=8, size=100, shared=True),
        _scored("c.fin.big", "fin", size_band=">10TB", blast=50, size=20 * 1024**4),
        _scored("c.a.pilot", "a", size_band="<=100GB", blast=0, size=10),
        _scored("c.sales.norm", "sales", size_band="1-10TB", blast=4, size=5 * 1024**4),
    ]
    _waves, assignments = lin.sequence_waves(scored)
    wave_rows, _ = lin.build_plan_rows(
        scored, assignments, plan_run_id="p1", planned_at=None, updated_at=None,
    )
    assert wave_rows, "expected wave_plan rows for a non-empty plan"
    for row in wave_rows:
        assert not lin.live_flags_outrank_bucket(
            row["bucket"],
            has_streaming_writer=bool(row["has_streaming"]),
            needs_compatibility_mode=bool(row["needs_compatibility_mode"]),
            is_delta_shared=bool(row["is_delta_shared"]),
        ), f"{row['full_name']} bucket {row['bucket']} contradicts its own flags"


# --------------------------------------------------------------------------- #
# build_plan_rows — ownership classification + owner notice (Plan 3)
# --------------------------------------------------------------------------- #

def _scored_owner(full_name, table_owner, *, schema="s", catalog="c"):
    return {
        "full_name": full_name, "catalog": catalog, "schema": schema,
        "size_band": "<=100GB", "table_size_bytes": 100,
        "est_data_copy_minutes": 1.0, "est_downtime_minutes": 1.0,
        "blast_radius": 0.0, "bucket": lin.BUCKET_PILOT,
        "downstream_consumer_count": 0, "owners": [], "consumer_labels": [],
        "has_streaming": False, "has_external_consumer": False,
        "is_delta_shared": False, "needs_compatibility_mode": False,
        "cross_region_risk": False, "table_owner": table_owner,
    }


def _comms_by_name(scored, **plan_kw):
    _waves, assignments = lin.sequence_waves(scored)
    _wr, comms = lin.build_plan_rows(
        scored, assignments, plan_run_id="p1", planned_at=None, updated_at=None, **plan_kw
    )
    return {c["full_name"]: c for c in comms}


def test_comms_feed_has_ownership_columns():
    for col in ("prior_owner", "ownership_change", "owner_notice"):
        assert col in lin.COMMS_FEED_COLUMN_NAMES


def test_build_plan_rows_sp_owned_table_gets_no_notice():
    scored = [_scored_owner("c.s.a", "sp-123")]
    c = _comms_by_name(scored, sp_client_id="sp-123")["c.s.a"]
    assert c["prior_owner"] == "sp-123"
    assert c["ownership_change"] == "NONE"
    assert c["owner_notice"] is None


def test_build_plan_rows_never_classifies_a_lease():
    # Ownership never changes: every new plan writes NONE, whoever owns the table.
    for owner in ("alice@x.com", "data-eng", None):
        c = _comms_by_name([_scored_owner("c.s.a", owner)], sp_client_id="sp-123")["c.s.a"]
        assert c["prior_owner"] == owner
        assert c["ownership_change"] == "NONE"
        assert "ownership does not change" in c["owner_notice"]


def test_build_plan_rows_unresolved_sp_still_drafts_a_notice():
    c = _comms_by_name([_scored_owner("c.s.a", "alice@x.com")])["c.s.a"]
    assert c["ownership_change"] == "NONE"
    assert c["owner_notice"] is not None


# --------------------------------------------------------------------------- #
# needs_coordination (ad-hoc writer coordination flag; Open Question A = flag)
# --------------------------------------------------------------------------- #

def test_streaming_isolation_ignores_adhoc_flag():
    # An ad-hoc writer must NEVER change a table's bucket; streaming stays isolated.
    b = lin.assign_bucket(
        size_band="<=100GB", blast_radius=0.0, has_streaming=True,
        has_external_consumer=False, is_delta_shared=False, needs_compatibility_mode=False,
    )
    assert b == lin.BUCKET_STREAMING


def test_needs_coordination_flags_routine_tables_with_adhoc_writers():
    assert lin.needs_coordination(lin.BUCKET_NORMAL, has_adhoc_writer=True) is True
    assert lin.needs_coordination(lin.BUCKET_PILOT, has_adhoc_writer=True) is True
    assert lin.needs_coordination(lin.BUCKET_NORMAL, has_adhoc_writer=False) is False
    # Already-coordinated buckets add nothing from the flag.
    assert lin.needs_coordination(lin.BUCKET_STREAMING, has_adhoc_writer=True) is False


def test_wave_plan_has_adhoc_writer_column():
    assert "has_adhoc_writer" in lin.WAVE_PLAN_COLUMN_NAMES
