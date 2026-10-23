"""Unit tests for the pure real-estate builder logic. No Spark required.

Run from the repo root with:  python -m pytest backend/src/tests/test_real_estate.py -q
"""
import os
import pathlib
import sys

import pytest

# Make ``migration`` importable when running pytest from the repo root
# (matches the convention in the other backend/src/tests/test_*.py files).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import bindings as bd  # noqa: E402
from migration import ownership as own  # noqa: E402
from migration import real_estate as re_  # noqa: E402
from migration import writers as wr  # noqa: E402
from migration.real_estate import auto_liquid_optout_outcome  # noqa: E402
from migration.real_estate import enable_optimization_outcome  # noqa: E402
from migration.real_estate import maintenance_restore_outcome  # noqa: E402
from migration.real_estate import rollback_finalize_reversal_outcome  # noqa: E402
from migration.real_estate import rollback_ledger_closeout_outcome  # noqa: E402
from migration.real_estate import schedule_verdict_outcome  # noqa: E402
from migration.real_estate import writer_pause_phase_outcome  # noqa: E402
from migration.real_estate import writer_stop_bracket_outcome  # noqa: E402


def test_size_tiers_are_real_and_small():
    names = [t["name"] for t in re_.SIZE_TIERS]
    assert names == ["XS", "S", "M", "L", "XL"]
    assert all(0 < t["target_bytes"] <= 2 * 1024**3 for t in re_.SIZE_TIERS)  # ≤2GB, affordable


def test_schemas_cover_every_case():
    cases = {s["case"] for s in re_.SCHEMAS}
    assert {"PILOT_SMALL", "MIXED", "LARGE", "STREAMING", "DLT",
            "SHARE", "COMPAT", "NON_DELTA", "PATH_ACCESS", "WRITER_WAVE"} <= cases


def test_build_estate_plan_is_deterministic_and_distributed():
    a = re_.build_estate_plan(table_count=120, rng_seed=42)
    b = re_.build_estate_plan(table_count=120, rng_seed=42)
    assert a == b                                   # deterministic
    assert 108 <= len(a) <= 132                     # ~table_count (±10%)
    schemas = {t["catalog_relative_schema"] for t in a}
    assert {s["name"] for s in re_.SCHEMAS} <= schemas   # every schema populated
    # silver exercises every real size tier
    silver_tiers = {t["size_tier"] for t in a if t["catalog_relative_schema"] == "e2e_real_silver"}
    assert {"XS", "S", "M", "L", "XL"} <= silver_tiers


def test_writer_wave_schema_is_a_small_isolated_delta_slice():
    """The L4 F-WRITER-WAVE fixture rests on e2e_real_writer being a TINY, Delta-only, papercut-free
    schema so the wave planner slices it into its OWN small runnable wave (not buried in bronze).
    seed_real_estate._writer_wave_target wraps compaction_candidates over exactly this slice."""
    plan = re_.build_estate_plan(table_count=120, rng_seed=42)
    rows = [t for t in plan if t["catalog_relative_schema"] == "e2e_real_writer"]
    assert rows, "the isolated writer schema must be populated"
    assert len(rows) <= 5                                 # tiny by design (own small wave)
    assert all(t["is_delta"] for t in rows)               # Delta: convertible + freezable
    assert all(t["papercut_tags"] == [] for t in rows)    # no synthetic tag; the writer is a real job
    # compaction_candidates (which _writer_wave_target wraps) resolves a concrete target table.
    got = re_.compaction_candidates("cat", plan, ("e2e_real_writer",))
    assert got and got[0][0] == "e2e_real_writer" and got[0][1]
    assert got[0][1][0].startswith("cat.e2e_real_writer.")


def test_non_delta_tables_are_marked_unknown_size():
    plan = re_.build_estate_plan(table_count=120, rng_seed=42)
    nondelta = [t for t in plan if t["catalog_relative_schema"] == "e2e_real_nondelta"]
    assert nondelta and all(t["is_delta"] is False for t in nondelta)
    assert all(t["size_source"] == "DESCRIBE_DETAIL" for t in nondelta)  # real, just null bytes at profile time


def test_synthetic_size_rows_cover_tb_bands_and_are_labeled():
    rows = re_.synthetic_size_rows(schema="e2e_real_gold")
    bands = {r["target_bytes"] for r in rows}
    assert re_.TB in bands and 10 * re_.TB in bands and 11 * re_.TB in bands   # ~1TB, ~10TB, >10TB
    assert all(r["size_source"] == "SYNTHETIC" for r in rows)
    assert all(r["full_name_suffix"].startswith("e2e_real_gold.") for r in rows)


def test_rows_pad_for_is_pure_and_monotonic():
    # deterministic + pure: same input -> same output, positive ints
    for tier in re_.SIZE_TIERS:
        r1, p1 = re_.rows_pad_for(tier["target_bytes"])
        r2, p2 = re_.rows_pad_for(tier["target_bytes"])
        assert (r1, p1) == (r2, p2)
        assert isinstance(r1, int) and isinstance(p1, int)
        assert r1 >= 1 and p1 >= re_._MIN_PAD          # payload never narrower than the base width
        assert r1 <= re_._ROW_CAP                       # rows capped so live provisioning stays fast


def test_rows_pad_for_real_sizes_are_distinct_and_scale():
    # The payload is INCOMPRESSIBLE random hex, so rows*pad is a faithful proxy
    # for the REAL on-disk size. Assert it is strictly monotonic AND grows by at
    # least ~2x between adjacent tiers (genuinely distinct sizes across XS->XL),
    # while the largest REAL table stays affordable (tens-of-MB, not GB).
    widths = [
        (lambda rp: rp[0] * rp[1])(re_.rows_pad_for(t["target_bytes"]))
        for t in re_.SIZE_TIERS
    ]
    assert all(b < a for b, a in zip(widths, widths[1:]))          # strictly monotonic
    assert all(a >= 2 * b for b, a in zip(widths, widths[1:]))     # >=2x growth per tier
    assert widths[-1] <= 256 * 1024**2                             # top tier <= ~256 MB (affordable)


def test_expected_papercuts_map():
    plan = re_.build_estate_plan(table_count=120, rng_seed=42)
    exp = re_.expected_papercuts(plan)
    assert "FIX_PATH_ACCESS" in exp["e2e_real_pathaccess"]
    assert "STOP_STREAM" in exp["e2e_real_stream"]
    assert "STOP_STREAM" in exp["e2e_real_dlt"]
    assert "RESHARE_D2D" in exp["e2e_real_share"]
    assert "ENABLE_COMPAT" in exp["e2e_real_compat"]
    assert exp.get("e2e_real_bronze", set()) == set()   # clean pilot schema, no papercuts


def test_reduced_wave_cap_forces_real_chunking():
    assert re_.real_chunking_wave_max_bytes() == 1 * re_.GB


def test_compaction_candidates_orders_and_filters():
    # Hand-built plan: ordering preserved, non-Delta excluded, empty schema omitted,
    # fqns catalog-qualified.
    plan = [
        {"full_name_suffix": "e2e_real_bronze.pilot_small_000",
         "catalog_relative_schema": "e2e_real_bronze", "is_delta": True},
        {"full_name_suffix": "e2e_real_bronze.pilot_small_001",
         "catalog_relative_schema": "e2e_real_bronze", "is_delta": True},
        {"full_name_suffix": "e2e_real_silver.mixed_000",
         "catalog_relative_schema": "e2e_real_silver", "is_delta": True},
        # a non-Delta row in silver must be excluded from the candidate list
        {"full_name_suffix": "e2e_real_silver.mixed_001",
         "catalog_relative_schema": "e2e_real_silver", "is_delta": False},
        # gold has ONLY a non-Delta row -> the whole schema is omitted (no empty-list entry)
        {"full_name_suffix": "e2e_real_gold.large_000",
         "catalog_relative_schema": "e2e_real_gold", "is_delta": False},
    ]
    schemas = ("e2e_real_bronze", "e2e_real_silver", "e2e_real_gold")
    got = re_.compaction_candidates("cat", plan, schemas)

    # gold omitted (no Delta table); the requested schema order is preserved
    assert [s for s, _ in got] == ["e2e_real_bronze", "e2e_real_silver"]
    # bronze: both Delta tables in plan order, catalog-qualified
    assert dict(got)["e2e_real_bronze"] == [
        "cat.e2e_real_bronze.pilot_small_000",
        "cat.e2e_real_bronze.pilot_small_001",
    ]
    # silver: only the Delta row (non-Delta excluded)
    assert dict(got)["e2e_real_silver"] == ["cat.e2e_real_silver.mixed_000"]


def test_compaction_candidates_defaults_missing_is_delta_to_delta():
    # A plan row without an explicit is_delta key is treated as Delta (t.get(..., True)).
    plan = [{"full_name_suffix": "e2e_real_bronze.t0",
             "catalog_relative_schema": "e2e_real_bronze"}]
    got = re_.compaction_candidates("cat", plan, ("e2e_real_bronze",))
    assert got == [("e2e_real_bronze", ["cat.e2e_real_bronze.t0"])]


def test_compaction_candidates_over_real_plan():
    # Using the real estate plan, each compaction target schema resolves to a non-empty,
    # plan-ordered, catalog-qualified Delta candidate list.
    plan = re_.build_estate_plan(table_count=120, rng_seed=42)
    schemas = ("e2e_real_bronze", "e2e_real_silver", "e2e_real_gold")
    got = re_.compaction_candidates("mycat", plan, schemas)
    assert [s for s, _ in got] == list(schemas)   # all three have Delta tables
    for schema, fqns in got:
        assert fqns  # non-empty
        assert all(fq.startswith(f"mycat.{schema}.") for fq in fqns)
        # matches plan order for that schema's Delta rows
        expected = [f"mycat.{t['full_name_suffix']}" for t in plan
                    if t.get("is_delta", True) and t["catalog_relative_schema"] == schema]
        assert fqns == expected


def test_preset_populate_excludes_e2e():
    got = re_.resolve_stages(stages=None, preset="populate", no_lineage=False)
    assert got == ["provision", "activity", "lineage_wait", "profile", "scan", "plan"]


def test_full_is_populate_plus_e2e():
    got = re_.resolve_stages(stages=None, preset="full", no_lineage=False)
    assert got[-1] == "e2e" and "plan" in got


def test_no_lineage_drops_lineage_wait():
    got = re_.resolve_stages(stages=None, preset="populate", no_lineage=True)
    assert "lineage_wait" not in got


def test_explicit_stages_sorted_canonically():
    got = re_.resolve_stages(stages=["plan", "provision"], preset=None, no_lineage=False)
    assert got == ["provision", "plan"]


def test_unknown_stage_raises():
    import pytest
    with pytest.raises(ValueError):
        re_.resolve_stages(stages=["bogus"], preset=None, no_lineage=False)


def test_path_access_fixtures_are_stable_and_nonempty():
    # A FIXED fixture set (independent of the random plan) so the committed notebook's
    # literal paths always match the tables the builder provisions, regardless of
    # --table-count. Assert the set is non-empty, all-strings, and unique.
    fx = re_.PATH_ACCESS_FIXTURES
    assert isinstance(fx, tuple)
    assert len(fx) >= 3
    assert all(isinstance(n, str) and n for n in fx)
    assert len(set(fx)) == len(fx)                      # unique
    # Stable exact set (the notebook literals are pinned to these names).
    assert fx == ("orders_by_path", "events_by_path", "metrics_by_path")


def test_path_access_fixture_location_composes():
    base = "s3://bucket/mtm_e2e"
    assert (re_.path_access_fixture_location(base, "orders_by_path")
            == "s3://bucket/mtm_e2e/e2e_real_pathaccess/orders_by_path")
    # A trailing slash on the base does not double up the separator.
    assert (re_.path_access_fixture_location("s3://bucket/mtm_e2e/", "events_by_path")
            == "s3://bucket/mtm_e2e/e2e_real_pathaccess/events_by_path")
    # Every fixture composes under the shared e2e_real_pathaccess prefix.
    for name in re_.PATH_ACCESS_FIXTURES:
        loc = re_.path_access_fixture_location(base, name)
        assert loc.endswith(f"/e2e_real_pathaccess/{name}")


def test_freeze_frozen_outcome_conditional():
    # No auto-freezable actor on the pilot (0 applied ledger rows) -> WARN, never a FAIL.
    tag, msg = re_.freeze_frozen_outcome(n_ledger=0, n_frozen=0)
    assert tag == "WARN"
    assert "0" in msg
    # The freeze actually froze -> PASS.
    tag, msg = re_.freeze_frozen_outcome(n_ledger=2, n_frozen=2)
    assert tag == "PASS"
    assert "2" in msg
    # Ledger planned auto-freezable ops but none applied -> FAIL.
    tag, msg = re_.freeze_frozen_outcome(n_ledger=3, n_frozen=0)
    assert tag == "FAIL"
    assert "3" in msg and "0" in msg


def test_unfreeze_outcome_conditional():
    # Nothing was frozen -> 0 UNFROZEN is correct -> WARN.
    tag, msg = re_.unfreeze_outcome(n_frozen=0, n_unfrozen=0)
    assert tag == "WARN"
    assert "0" in msg
    # Ledger flipped to UNFROZEN -> PASS.
    tag, msg = re_.unfreeze_outcome(n_frozen=2, n_unfrozen=2)
    assert tag == "PASS"
    assert "2" in msg
    # Rows were frozen but none flipped -> FAIL.
    tag, msg = re_.unfreeze_outcome(n_frozen=2, n_unfrozen=0)
    assert tag == "FAIL"
    assert "2" in msg and "0" in msg


def test_enable_optimization_outcome_all_effective_on_passes():
    tag, msg = enable_optimization_outcome(
        converted=5, po_effective_on=5, al_success=4, al_manual=1)
    assert tag == "PASS"
    assert "5/5" in msg and "CLUSTER BY AUTO" in msg


def test_enable_optimization_outcome_nothing_converted_warns():
    tag, _ = enable_optimization_outcome(
        converted=0, po_effective_on=0, al_success=0, al_manual=0)
    assert tag == "WARN"


def test_enable_optimization_outcome_po_alter_failed_fails():
    # A real ENABLE PREDICTIVE OPTIMIZATION ALTER errored -> the hard regression.
    tag, msg = enable_optimization_outcome(
        converted=5, po_effective_on=3, al_success=0, al_manual=0, po_failed=2)
    assert tag == "FAIL"
    assert "2" in msg


def test_enable_optimization_outcome_effective_off_fails():
    # A converted table is genuinely PO-OFF (not merely unreadable) -> real gap, hard FAIL.
    tag, msg = enable_optimization_outcome(
        converted=5, po_effective_on=3, al_success=4, al_manual=1, po_unreadable=0)
    assert tag == "FAIL"
    assert "OFF" in msg and "2" in msg


def test_enable_optimization_outcome_unreadable_none_off_warns():
    # None proven OFF, but not all confirmed ON (grants/runtime) -> honest WARN, not PASS/FAIL.
    tag, msg = enable_optimization_outcome(
        converted=5, po_effective_on=3, al_success=4, al_manual=1, po_unreadable=2)
    assert tag == "WARN"
    assert "unreadable" in msg.lower()


def test_enable_optimization_outcome_all_held_back_warns():
    tag, _ = enable_optimization_outcome(
        converted=3, po_effective_on=3, al_success=0, al_manual=3)
    assert tag == "WARN"


def test_enable_optimization_outcome_auto_liquid_silent_fails():
    tag, _ = enable_optimization_outcome(
        converted=3, po_effective_on=3, al_success=0, al_manual=0)
    assert tag == "FAIL"


def test_auto_liquid_optout_outcome_held_back_passes():
    tag, msg = auto_liquid_optout_outcome(seeded=True, held_back=1)
    assert tag == "PASS"
    assert "held back" in msg


def test_auto_liquid_optout_outcome_not_seeded_warns():
    tag, _ = auto_liquid_optout_outcome(seeded=False, held_back=0)
    assert tag == "WARN"


def test_auto_liquid_optout_outcome_seeded_but_not_held_back_warns():
    tag, _ = auto_liquid_optout_outcome(seeded=True, held_back=0)
    assert tag == "WARN"


def test_rollback_finalize_reversal_pass():
    tag, msg = rollback_finalize_reversal_outcome(
        po_enabled=5, po_reversed=5, al_enabled=4, al_reversed=4, finalize_obsolete=9)
    assert tag == "PASS"
    assert "PO on 5" in msg
    assert "CLUSTER BY AUTO on 4" in msg


def test_rollback_finalize_reversal_nothing_enabled_warns():
    # PO inherited-on AND no clean-eligible table -> nothing to reverse.
    tag, _ = rollback_finalize_reversal_outcome(
        po_enabled=0, po_reversed=0, al_enabled=0, al_reversed=0, finalize_obsolete=0)
    assert tag == "WARN"


def test_rollback_finalize_reversal_enabled_but_not_reversed_fails():
    tag, _ = rollback_finalize_reversal_outcome(
        po_enabled=5, po_reversed=0, al_enabled=0, al_reversed=0, finalize_obsolete=0)
    assert tag == "FAIL"


def test_rollback_finalize_reversal_inherited_po_still_asserts_auto_liquid():
    # RB-M2 refinement: 08c enabled PO by inheritance (po_enabled==0) but applied CLUSTER BY AUTO
    # to 34 tables -> the auto-liquid reversal is still asserted (PASS when reversed), not masked.
    tag, msg = rollback_finalize_reversal_outcome(
        po_enabled=0, po_reversed=0, al_enabled=34, al_reversed=34, finalize_obsolete=34)
    assert tag == "PASS"
    assert "CLUSTER BY AUTO on 34" in msg
    assert "inheritance" in msg


def test_rollback_finalize_reversal_auto_liquid_applied_but_not_reversed_fails():
    # RB-M2 refinement: auto-liquid applied yet none reversed is now a real FAIL, even with
    # PO inherited-on (previously masked as a WARN on the PO branch alone).
    tag, msg = rollback_finalize_reversal_outcome(
        po_enabled=0, po_reversed=0, al_enabled=34, al_reversed=0, finalize_obsolete=0)
    assert tag == "FAIL"
    assert "CLUSTER BY AUTO on 34" in msg


def test_rollback_ledger_closeout_pass():
    tag, msg = rollback_ledger_closeout_outcome(
        applied_freeze=3, manifest_rolled_back=8, ledger_rolled_back=3)
    assert tag == "PASS"
    assert "ROLLED_BACK" in msg


def test_rollback_ledger_closeout_no_rollback_warns():
    tag, _ = rollback_ledger_closeout_outcome(
        applied_freeze=3, manifest_rolled_back=0, ledger_rolled_back=0)
    assert tag == "WARN"


def test_rollback_ledger_closeout_no_freeze_warns():
    tag, _ = rollback_ledger_closeout_outcome(
        applied_freeze=0, manifest_rolled_back=8, ledger_rolled_back=0)
    assert tag == "WARN"


def test_rollback_ledger_closeout_not_stamped_fails():
    tag, _ = rollback_ledger_closeout_outcome(
        applied_freeze=3, manifest_rolled_back=8, ledger_rolled_back=0)
    assert tag == "FAIL"


def test_is_retryable_provision_error_matches_only_transient_race():
    # The exact race signatures seen during --reset re-provisioning are retryable.
    assert re_.is_retryable_provision_error("[TABLE_OR_VIEW_NOT_FOUND] Table 'abc-uuid' does not exist")
    assert re_.is_retryable_provision_error("DELTA_TABLE_NOT_FOUND: ...")
    assert re_.is_retryable_provision_error("PATH_ALREADY_EXISTS: s3://.../mtm_e2e/...")
    assert re_.is_retryable_provision_error("java.io.FileNotFoundException: No such file")
    assert re_.is_retryable_provision_error("Concurrent modification detected")
    # Genuine, non-transient failures must FAIL fast (not be retried).
    assert not re_.is_retryable_provision_error("PERMISSION_DENIED: user lacks CREATE")
    assert not re_.is_retryable_provision_error("PARSE_SYNTAX_ERROR near 'CREAT'")
    assert not re_.is_retryable_provision_error(None)
    assert not re_.is_retryable_provision_error("")


def test_retry_backoff_seconds_is_exponential_and_capped():
    assert re_.retry_backoff_seconds(0, base_s=5.0, cap_s=60.0) == 5.0
    assert re_.retry_backoff_seconds(1, base_s=5.0, cap_s=60.0) == 10.0
    assert re_.retry_backoff_seconds(3, base_s=5.0, cap_s=60.0) == 40.0
    # Capped once the doubling exceeds cap_s.
    assert re_.retry_backoff_seconds(4, base_s=5.0, cap_s=60.0) == 60.0
    assert re_.retry_backoff_seconds(10, base_s=5.0, cap_s=60.0) == 60.0
    with pytest.raises(ValueError):
        re_.retry_backoff_seconds(-1)


def test_provision_retry_delays_default_schedule():
    delays = re_.provision_retry_delays()
    assert len(delays) == re_.PROVISION_MAX_RETRIES
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    # Non-decreasing (each attempt waits at least as long as the prior).
    assert all(b >= a for a, b in zip(delays, delays[1:]))
    assert re_.provision_retry_delays(0) == []
    with pytest.raises(ValueError):
        re_.provision_retry_delays(-1)


def test_is_safe_estate_base_accepts_only_the_dedicated_test_prefix():
    # Accept: the stable default base and any run/case subpath under the mtm_e2e segment.
    assert re_.is_safe_estate_base("s3://my-bucket/managed_migration/mtm_e2e")
    assert re_.is_safe_estate_base("s3://my-bucket/managed_migration/mtm_e2e/")
    assert re_.is_safe_estate_base("s3://my-bucket/mtm_e2e/run3")
    assert re_.is_safe_estate_base("abfss://c@acct.dfs.core.windows.net/prefix/mtm_e2e")
    # Reject: bucket roots, bare schemes, non-cloud paths, and any base missing the marker.
    assert not re_.is_safe_estate_base("s3://my-bucket")
    assert not re_.is_safe_estate_base("s3://my-bucket/")
    assert not re_.is_safe_estate_base("s3://")
    assert not re_.is_safe_estate_base("/mnt/data/mtm_e2e")   # not a scheme://bucket URI
    assert not re_.is_safe_estate_base("s3://my-bucket/prod/warehouse")
    # Reject substring / bucket-name lookalikes (marker must be an exact PATH segment under bucket).
    assert not re_.is_safe_estate_base("s3://my-bucket/mtm_e2e_other")
    assert not re_.is_safe_estate_base("s3://mtm_e2e")        # marker as bucket name only
    assert not re_.is_safe_estate_base(None)
    assert not re_.is_safe_estate_base("")


def test_estate_file_clear_path_strips_slash_and_refuses_unsafe():
    assert re_.estate_file_clear_path("s3://b/x/mtm_e2e/") == "s3://b/x/mtm_e2e"
    assert re_.estate_file_clear_path("s3://b/mtm_e2e/run3") == "s3://b/mtm_e2e/run3"
    with pytest.raises(ValueError):
        re_.estate_file_clear_path("s3://b")           # bucket root
    with pytest.raises(ValueError):
        re_.estate_file_clear_path("s3://b/prod/data")  # missing marker


def test_estate_file_clear_paths_are_per_schema_disjoint_from_reused_assets():
    paths = re_.estate_file_clear_paths("s3://b/x/mtm_e2e/")   # trailing slash normalized
    schema_names = [s["name"] for s in re_.SCHEMAS]
    # One target per estate schema, each the base + that e2e_real_* dir (slash normalized once).
    assert paths == [f"s3://b/x/mtm_e2e/{name}" for name in schema_names]
    assert len(paths) == len(schema_names) == 10
    # Every target carries the e2e_real_ marker the in-cluster guard requires...
    assert all("/e2e_real_" in p for p in paths)
    # ...and NONE targets a preserved reused-asset dir (e2e_stream/ e2e_dlt/) or the base root.
    assert not any(p.endswith("/e2e_stream") or p.endswith("/e2e_dlt") for p in paths)
    assert "s3://b/x/mtm_e2e" not in paths
    # Unsafe bases can never be turned into removal targets (reuses estate_file_clear_path's gate).
    with pytest.raises(ValueError):
        re_.estate_file_clear_paths("s3://b")            # bucket root
    with pytest.raises(ValueError):
        re_.estate_file_clear_paths("s3://b/prod/data")  # missing marker


def test_is_safe_control_wipe_requires_confirm_and_real_scope():
    assert re_.is_safe_control_wipe("cat", "migration", confirm=True)
    # Missing confirm never wipes, even with a valid scope.
    assert not re_.is_safe_control_wipe("cat", "migration", confirm=False)
    # Empty / whitespace scope never wipes, even when confirmed.
    assert not re_.is_safe_control_wipe("", "migration", confirm=True)
    assert not re_.is_safe_control_wipe("cat", "   ", confirm=True)
    assert not re_.is_safe_control_wipe(None, "migration", confirm=True)
    assert not re_.is_safe_control_wipe("cat", None, confirm=True)


def test_control_plane_wipe_statements_cover_base_tables_unscoped_and_spare_config():
    stmts = re_.control_plane_wipe_statements("cat", "migration")
    tables = [t for t, _ in stmts]
    # Every base table is wiped, exactly once, and none is the single-row handshake or a view.
    assert tables == list(re_.CONTROL_BASE_TABLES)
    assert re_.CONTROL_CONFIG_TABLE not in tables
    assert "migration_progress" not in tables and "wave_status" not in tables  # views, never wiped
    assert "coverage_report" not in tables                                     # view over the ledger
    assert "migration_next_actions" in tables                                  # clears the stale snapshot
    # wave_parallel_groups is a derived-but-persisted base table (not a view): the planner rewrites
    # it per plan_run_id. It MUST be wiped, else an orphaned batch snapshot survives a wipe that
    # emptied wave_plan and the Bulk Convert screen renders a ghost plan (batches with no wave_plan).
    assert "wave_parallel_groups" in tables
    # profiler_run_ledger MUST be wiped: the Overview landing reads NEVER_PROFILED vs
    # ZERO_DISCOVERY off coverage_report (a view over this ledger). Leaving it unwiped strands the
    # app on ZERO_DISCOVERY after a fresh-install wipe instead of the true first-run card.
    assert "profiler_run_ledger" in tables
    # stage_run_ledger MUST be wiped: it is append-only stage-run history (one row per
    # convert/campaign run). Leaving it unwiped means a "brand new install" shows prior runs.
    assert "stage_run_ledger" in tables
    # ownership_ledger / writer_sources / coverage_matrix are base tables too: a 2026-10-02 e2e
    # wipe left a LEASED ledger row and 7191 writer attributions from the old estate behind.
    assert {own.OWNERSHIP_LEDGER_TABLE, wr.WRITER_SOURCES_TABLE, bd.COVERAGE_MATRIX_TABLE} <= set(tables)
    # Fully-qualified, UNSCOPED DELETEs (no WHERE) — a true full wipe.
    for t, sql in stmts:
        assert sql == f"DELETE FROM cat.migration.{t}"
        assert "WHERE" not in sql.upper()
    # Empty scope refuses to build a wipe target.
    with pytest.raises(ValueError):
        re_.control_plane_wipe_statements("", "migration")
    with pytest.raises(ValueError):
        re_.control_plane_wipe_statements("cat", "  ")


def test_driver_and_notebooks_never_run_set_managed_or_vacuum_directly():
    # The builder driver and its e2e notebooks must NEVER convert/reverse a table: the real
    # SET MANAGED / UNSET MANAGED only ever happen inside the invoked deployed stage jobs.
    root = pathlib.Path(__file__).resolve().parents[2]  # backend/
    driver = root / ".e2e" / "seed_real_estate.py"
    bypath = root / "notebooks" / "e2e" / "read_by_path_real.py"
    maint = root / "notebooks" / "e2e" / "seed_maintenance_jobs.py"

    for p in (driver, bypath, maint):
        src = p.read_text().upper()
        assert "SET MANAGED" not in src, f"{p} must not run SET MANAGED directly"
        assert "UNSET MANAGED" not in src, f"{p} must not run UNSET MANAGED directly"

    # The by-path read notebook never touches VACUUM at all.
    assert "VACUUM" not in bypath.read_text().upper()

    # The driver never *issues* a VACUUM SQL statement itself (``VACUUM <table>``). The ONLY
    # sanctioned VACUUM is the seeded maintenance job's notebook (seed_maintenance_jobs.py), which
    # runs a real VACUUM on freshly-seeded ESTATE tables (RETAIN 0 HOURS, removing only the tombstones
    # it just created via fragment+OPTIMIZE — the latest snapshot is untouched) to attribute a scheduled
    # JOB for the PAUSE_JOB/RESUME_JOB freeze bracket — never a source-of-truth hard-delete and never
    # inside a real conversion's 14-day window. The driver only *orchestrates* that job.
    assert "VACUUM {" not in driver.read_text(), f"{driver} must not build a VACUUM SQL statement"
    assert "VACUUM {" in maint.read_text(), "the sanctioned VACUUM statement must live in the maintenance notebook"


def test_maintenance_restore_outcome_retired_stays_paused_passes():
    tag, msg = maintenance_restore_outcome(
        job_name="opt", resolved=True, actual_pause_status="PAUSED", retired=True)
    assert tag == "PASS"
    assert "retired" in msg


def test_maintenance_restore_outcome_retired_but_unpaused_fails():
    tag, _ = maintenance_restore_outcome(
        job_name="opt", resolved=True, actual_pause_status="UNPAUSED", retired=True)
    assert tag == "FAIL"


def test_maintenance_restore_outcome_resumed_unpaused_passes():
    tag, msg = maintenance_restore_outcome(
        job_name="vac", resolved=True, actual_pause_status="unpaused", retired=False)
    assert tag == "PASS"  # case-insensitive normalization
    assert "resumed" in msg


def test_maintenance_restore_outcome_resumed_but_paused_fails():
    tag, _ = maintenance_restore_outcome(
        job_name="vac", resolved=True, actual_pause_status="PAUSED", retired=False)
    assert tag == "FAIL"


def test_maintenance_restore_outcome_unresolved_warns():
    tag, _ = maintenance_restore_outcome(
        job_name="opt", resolved=False, actual_pause_status=None, retired=False)
    assert tag == "WARN"


def test_maintenance_restore_outcome_no_readable_status_warns():
    tag, _ = maintenance_restore_outcome(
        job_name="opt", resolved=True, actual_pause_status=None, retired=False)
    assert tag == "WARN"


def test_writer_stop_bracket_pass():
    tag, msg = writer_stop_bracket_outcome(attributed=1, stop_actions_blocking=1)
    assert tag == "PASS"
    assert "STOP_WRITER_JOB" in msg


def test_writer_stop_bracket_not_attributed_warns():
    tag, _ = writer_stop_bracket_outcome(attributed=0, stop_actions_blocking=0)
    assert tag == "WARN"


def test_writer_stop_bracket_attributed_but_no_action_fails():
    tag, _ = writer_stop_bracket_outcome(attributed=2, stop_actions_blocking=0)
    assert tag == "FAIL"


def test_writer_pause_phase_pass():
    tag, _ = writer_pause_phase_outcome(
        bracketed=True, resolved=True, expected="PAUSED", actual="PAUSED")
    assert tag == "PASS"


def test_writer_pause_phase_not_bracketed_warns():
    tag, _ = writer_pause_phase_outcome(
        bracketed=False, resolved=True, expected="PAUSED", actual="UNPAUSED")
    assert tag == "WARN"


def test_writer_pause_phase_unresolved_warns():
    tag, _ = writer_pause_phase_outcome(
        bracketed=True, resolved=False, expected="UNPAUSED", actual=None)
    assert tag == "WARN"


def test_writer_pause_phase_mismatch_fails():
    tag, _ = writer_pause_phase_outcome(
        bracketed=True, resolved=True, expected="UNPAUSED", actual="PAUSED")
    assert tag == "FAIL"


def test_schedule_verdict_safe_now_pass():
    tag, msg = schedule_verdict_outcome(
        label="far-out", expected="SAFE_NOW", actual="SAFE_NOW", next_write_present=True)
    assert tag == "PASS"
    assert "SAFE_NOW" in msg


def test_schedule_verdict_warn_window_pass():
    tag, _ = schedule_verdict_outcome(
        label="imminent", expected="WARN_WINDOW", actual="WARN_WINDOW", next_write_present=True)
    assert tag == "PASS"


def test_schedule_verdict_none_warns():
    tag, _ = schedule_verdict_outcome(
        label="far-out", expected="SAFE_NOW", actual=None, next_write_present=False)
    assert tag == "WARN"


def test_schedule_verdict_unknown_warns():
    tag, _ = schedule_verdict_outcome(
        label="imminent", expected="WARN_WINDOW", actual="UNKNOWN", next_write_present=False)
    assert tag == "WARN"


def test_schedule_verdict_wrong_fails():
    tag, _ = schedule_verdict_outcome(
        label="far-out", expected="SAFE_NOW", actual="WARN_WINDOW", next_write_present=True)
    assert tag == "FAIL"


def test_schedule_verdict_match_but_no_next_write_fails():
    tag, _ = schedule_verdict_outcome(
        label="imminent", expected="WARN_WINDOW", actual="WARN_WINDOW", next_write_present=False)
    assert tag == "FAIL"


from datetime import datetime, timezone

from migration.real_estate import far_out_cron
from migration import job_schedule as _js


def _slack_min(cron: str, now: datetime) -> float:
    nf = _js.next_fire(cron, "UTC", now)                      # tz-naive UTC
    now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    return (nf - now_naive).total_seconds() / 60.0


def test_far_out_cron_shape_same_day():
    now = datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)
    cron = far_out_cron(now)
    assert cron == "0 30 18 ? * *"                            # 10 + 8 = 18, same day
    assert 15 < _slack_min(cron, now) <= 8 * 60 + 1           # ~8h out, well past the 15-min margin


def test_far_out_cron_hour_wraps_next_day():
    now = datetime(2026, 1, 15, 20, 5, tzinfo=timezone.utc)
    cron = far_out_cron(now)
    assert cron == "0 5 4 ? * *"                              # (20 + 8) % 24 = 4, next day
    assert 15 < _slack_min(cron, now) <= 8 * 60 + 1           # ~8h out (next-day fire)


# --------------------------------------------------------------------------- #
# L5 notify/comms surfaces (comms_feed / digest / runbook export oracles)
# --------------------------------------------------------------------------- #

from migration.real_estate import comms_feed_outcome  # noqa: E402
from migration.real_estate import digest_reflects_estate  # noqa: E402
from migration.real_estate import phase_ranks_nondecreasing  # noqa: E402
from migration.real_estate import runbook_reflects_estate  # noqa: E402


# --- N2: comms_feed -------------------------------------------------------- #

def test_comms_feed_empty_warns():
    tag, msg = comms_feed_outcome(total_rows=0, with_notice=0, scope_label="e2e_real_bronze")
    assert tag == "WARN"
    assert "empty" in msg


def test_comms_feed_rows_without_notice_warn():
    tag, _ = comms_feed_outcome(total_rows=5, with_notice=0)
    assert tag == "WARN"


def test_comms_feed_rows_with_notice_pass():
    tag, msg = comms_feed_outcome(total_rows=5, with_notice=3, scope_label="e2e_real_bronze")
    assert tag == "PASS"
    assert "3 carry an owner_notice" in msg


def test_comms_feed_never_fails():
    # A quiet / half-rendered feed is WARN, never FAIL (SR: absence is not a defect).
    for tr, wn in ((0, 0), (10, 0)):
        assert comms_feed_outcome(total_rows=tr, with_notice=wn)[0] != "FAIL"


# --- N1: digest ------------------------------------------------------------ #

def test_digest_text_not_rendered_fails():
    tag, _ = digest_reflects_estate(
        blocking_count=2, stale_freeze_count=0, open_blocking_actions=2, text_rendered=False)
    assert tag == "FAIL"


def test_digest_underreports_blockers_fails():
    # Estate has open blocking actions but the digest surfaced 0 — under-reporting.
    tag, msg = digest_reflects_estate(
        blocking_count=0, stale_freeze_count=0, open_blocking_actions=3, text_rendered=True)
    assert tag == "FAIL"
    assert "under-reports" in msg


def test_digest_quiet_estate_warns():
    tag, _ = digest_reflects_estate(
        blocking_count=0, stale_freeze_count=0, open_blocking_actions=0, text_rendered=True)
    assert tag == "WARN"


def test_digest_reflects_blockers_pass():
    tag, msg = digest_reflects_estate(
        blocking_count=3, stale_freeze_count=1, open_blocking_actions=3, text_rendered=True)
    assert tag == "PASS"
    assert "3 blocking" in msg


def test_digest_stale_freeze_only_pass():
    # No blocking actions, but a stale freeze — still attention-worthy, PASS.
    tag, _ = digest_reflects_estate(
        blocking_count=0, stale_freeze_count=2, open_blocking_actions=0, text_rendered=True)
    assert tag == "PASS"


# --- N3: runbook export ---------------------------------------------------- #

def test_runbook_empty_warns():
    tag, _ = runbook_reflects_estate(builder_entries=0, view_rows=0, phase_ordered=True)
    assert tag == "WARN"


def test_runbook_not_phase_ordered_fails():
    tag, msg = runbook_reflects_estate(builder_entries=5, view_rows=5, phase_ordered=False)
    assert tag == "FAIL"
    assert "phase-ordered" in msg


def test_runbook_view_unavailable_warns():
    tag, _ = runbook_reflects_estate(builder_entries=5, view_rows=None, phase_ordered=True)
    assert tag == "WARN"


def test_runbook_view_empty_but_builder_nonempty_fails():
    tag, msg = runbook_reflects_estate(builder_entries=5, view_rows=0, phase_ordered=True)
    assert tag == "FAIL"
    assert "disagree" in msg


def test_runbook_parity_pass():
    tag, msg = runbook_reflects_estate(builder_entries=7, view_rows=7, phase_ordered=True)
    assert tag == "PASS"
    assert "phase-ordered" in msg


def test_phase_ranks_nondecreasing():
    assert phase_ranks_nondecreasing([0, 0, 1, 2, 2]) is True
    assert phase_ranks_nondecreasing([0, 2, 1]) is False
    assert phase_ranks_nondecreasing([]) is True
    assert phase_ranks_nondecreasing([0, None]) is True       # None -> 9 (sorts last)
    assert phase_ranks_nondecreasing([None, 0]) is False      # 9 then 0 decreases


# --- convert-permission owner variants (a foreign-owned and a group-owned table) ---

def test_owner_variants_come_only_from_the_flags():
    assert re_.owner_variants(None, None) == []
    assert re_.owner_variants("  ", "") == []
    assert re_.owner_variants("alice@example.com", "data-owners") == [
        (re_.FOREIGN_OWNED_TABLE, "alice@example.com"),
        (re_.GROUP_OWNED_TABLE, "data-owners"),
    ]
    assert re_.owner_variants(None, " data-owners ") == [(re_.GROUP_OWNED_TABLE, "data-owners")]


def test_owner_variants_reject_a_name_that_breaks_the_quoted_identifier():
    with pytest.raises(ValueError, match="backtick"):
        re_.owner_variants("a`b", None)


def test_owner_variant_outcome_without_a_verdict_warns():
    tag, msg = re_.owner_variant_outcome(table="t", owner="g", verdict=None)
    assert tag == "WARN" and "no stored convert permission" in msg


def test_owner_variant_outcome_owner_mismatch_warns():
    tag, msg = re_.owner_variant_outcome(
        table="t", owner="g", verdict={"status": "AUTHORIZED", "table_owner": "someone-else"})
    assert tag == "WARN" and "someone-else" in msg


def test_owner_variant_outcome_matching_owner_passes_with_the_verdict():
    tag, msg = re_.owner_variant_outcome(
        table="t", owner="Data-Owners",
        verdict={"status": "NEEDS_GRANT", "basis": None, "table_owner": "data-owners"})
    assert tag == "PASS"
    assert "NEEDS_GRANT" in msg and "data-owners" in msg


# --------------------------------------------------------------------------- #
# Liquid-clustering step M7: the seeded ZORDER and manual-key tables, checked live
# --------------------------------------------------------------------------- #

from migration.real_estate import clustering_layout_outcome  # noqa: E402
from migration.real_estate import layout_from_extended_json  # noqa: E402


def test_layout_from_extended_json_reads_the_string_properties():
    import json
    on = json.dumps({"table_properties": {"clusterByAuto": "true",
                                          "clusteringColumns": '["event_date","customer_id"]'}})
    assert layout_from_extended_json(on) == {
        "cluster_by_auto": True, "clustering_columns": ["event_date", "customer_id"]}
    keys = json.dumps({"table_properties": {"clusteringColumns": '["region"]'}})
    assert layout_from_extended_json(keys) == {"cluster_by_auto": False, "clustering_columns": ["region"]}
    never = json.dumps({"table_properties": {"delta.minReaderVersion": "1"}})
    assert layout_from_extended_json(never) == {"cluster_by_auto": False, "clustering_columns": []}


def test_layout_from_extended_json_unreadable_is_unknown():
    assert layout_from_extended_json(None) == {"cluster_by_auto": None, "clustering_columns": None}
    assert layout_from_extended_json("not json") == {"cluster_by_auto": None, "clustering_columns": None}
    bad = '{"table_properties": {"clusterByAuto": "true", "clusteringColumns": "oops"}}'
    assert layout_from_extended_json(bad) == {"cluster_by_auto": True, "clustering_columns": None}


def test_clustering_layout_outcome_passes_on_the_expected_layout():
    tag, msg = clustering_layout_outcome(
        "ZORDER hints", layout={"cluster_by_auto": True, "clustering_columns": ["event_date", "customer_id"]},
        expect_auto=True, expect_keys=["event_date", "customer_id"])
    assert tag == "PASS" and "event_date" in msg


def test_clustering_layout_outcome_fails_on_the_wrong_layout():
    tag, _ = clustering_layout_outcome(
        "ZORDER hints", layout={"cluster_by_auto": True, "clustering_columns": []},
        expect_auto=True, expect_keys=["event_date", "customer_id"])
    assert tag == "FAIL"
    tag, _ = clustering_layout_outcome(
        "restore", layout={"cluster_by_auto": True, "clustering_columns": ["region"]},
        expect_auto=False, expect_keys=["region"])
    assert tag == "FAIL"


def test_clustering_layout_outcome_degrades_honestly():
    assert clustering_layout_outcome("x", layout=None, expect_auto=True, expect_keys=[], seeded=False)[0] == "WARN"
    assert clustering_layout_outcome("x", layout=None, expect_auto=True, expect_keys=[], converted=False)[0] == "WARN"
    unread = {"cluster_by_auto": None, "clustering_columns": None}
    assert clustering_layout_outcome("x", layout=unread, expect_auto=True, expect_keys=[])[0] == "WARN"
