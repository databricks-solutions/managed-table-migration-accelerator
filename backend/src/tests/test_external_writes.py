"""Unit tests for external/non-Databricks access classification. No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import external_writes as ew  # noqa: E402
from migration import locations as loc  # noqa: E402


# --------------------------------------------------------------------------- #
# classify_entity_type
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "entity_type,expected",
    [
        ("NOTEBOOK", "databricks"),
        ("job", "databricks"),
        ("DLT_PIPELINE", "databricks"),
        ("FLINK", "external"),
        ("trino", "external"),
        ("ZEROBUS", "external"),
        ("SOMETHING_NEW", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_entity_type(entity_type, expected):
    assert ew.classify_entity_type(entity_type) == expected


# --------------------------------------------------------------------------- #
# is_path_based_access
# --------------------------------------------------------------------------- #

def test_path_based_access_matches_prefix():
    loc = "s3://bucket/warehouse/db/table"
    assert ew.is_path_based_access("s3://bucket/warehouse/db/table", loc) is True
    assert ew.is_path_based_access("s3://bucket/warehouse/db/table/part=1", loc) is True
    assert ew.is_path_based_access("s3://bucket/warehouse/db/table/", loc) is True


def test_path_based_access_negative():
    loc = "s3://bucket/warehouse/db/table"
    assert ew.is_path_based_access("s3://bucket/warehouse/db/other", loc) is False
    assert ew.is_path_based_access(None, loc) is False
    assert ew.is_path_based_access("s3://x/y", None) is False


# --------------------------------------------------------------------------- #
# collect_path_access_entities / serialize_path_access_entities
# --------------------------------------------------------------------------- #

def test_collect_path_access_entities_matches_and_tags_role():
    loc = "s3://bucket/warehouse/db/table"
    write_paths = {
        loc + "/part=1": [{"entity_type": "FLINK", "entity_id": "f1", "created_by": "svc@x"}],
        "s3://bucket/other": [{"entity_type": "JOB", "entity_id": "j9", "created_by": "z@x"}],
    }
    read_paths = {
        loc: [{"entity_type": "TRINO", "entity_id": "t2", "created_by": "a@x"}],
    }
    out = ew.collect_path_access_entities(
        storage_location=loc,
        write_path_entities=write_paths,
        read_path_entities=read_paths,
    )
    assert {"entity_type": "FLINK", "entity_id": "f1", "created_by": "svc@x", "role": "WRITE"} in out
    assert {"entity_type": "TRINO", "entity_id": "t2", "created_by": "a@x", "role": "READ"} in out
    # The non-matching path (s3://bucket/other) must be excluded.
    assert all(e["entity_id"] != "j9" for e in out)


def test_collect_path_access_entities_dedupes_and_caps():
    loc = "s3://b/t"
    dup = {loc: [
        {"entity_type": "FLINK", "entity_id": "f1", "created_by": "u"},
        {"entity_type": "FLINK", "entity_id": "f1", "created_by": "u"},
    ]}
    assert len(ew.collect_path_access_entities(storage_location=loc, write_path_entities=dup)) == 1

    many = {loc: [{"entity_type": "FLINK", "entity_id": f"f{i}", "created_by": "u"} for i in range(50)]}
    capped = ew.collect_path_access_entities(storage_location=loc, write_path_entities=many, max_entities=10)
    assert len(capped) == 10


def test_collect_path_access_entities_no_location_or_no_match():
    assert ew.collect_path_access_entities(storage_location=None, write_path_entities={"s3://b/t": [{}]}) == []
    assert ew.collect_path_access_entities(
        storage_location="s3://b/t", write_path_entities={"s3://b/other": [{"entity_id": "x"}]}
    ) == []


def test_serialize_path_access_entities():
    assert ew.serialize_path_access_entities(None) is None
    assert ew.serialize_path_access_entities([]) is None
    s = ew.serialize_path_access_entities([{"entity_type": "FLINK", "entity_id": "f1", "role": "WRITE"}])
    assert '"entity_type": "FLINK"' in s
    assert '"role": "WRITE"' in s
    # sort_keys=True gives stable ordering for diff-friendly storage.
    assert s.index('"entity_id"') < s.index('"entity_type"') < s.index('"role"')


# --------------------------------------------------------------------------- #
# build_compat_mode_sql
# --------------------------------------------------------------------------- #

def test_build_compat_mode_sql():
    sql = ew.build_compat_mode_sql("cat.sch.tbl")
    assert "ALTER TABLE cat.sch.tbl SET TBLPROPERTIES" in sql
    assert "'delta.universalFormat.enabledFormats' = 'compatibility'" in sql
    assert ew.COMPAT_LOCATION_PLACEHOLDER in sql
    assert "EMPTY path" in sql  # the operator guidance comment


# --------------------------------------------------------------------------- #
# derive_access_flags
# --------------------------------------------------------------------------- #

def test_external_writer_blocks_and_no_compat():
    out = ew.derive_access_flags(
        full_name="c.s.t",
        has_path_write=True,
        has_path_read=True,  # reader exists but writer dominates
    )
    assert out["has_external_non_databricks_writer"] is True
    assert out["needs_compatibility_mode"] is False
    assert out["compat_mode_sql"] is None
    assert ew.BLOCKER_EXTERNAL_WRITER in out["blockers"]
    assert out["confidence"] == "high"


def test_external_reader_only_needs_compat():
    out = ew.derive_access_flags(
        full_name="c.s.t",
        reader_entity_types=["TRINO"],
        has_path_read=False,
    )
    assert out["has_external_non_databricks_writer"] is False
    assert out["needs_compatibility_mode"] is True
    assert out["compat_mode_sql"] is not None
    assert out["blockers"] == []


def test_rls_cls_disables_compat():
    out = ew.derive_access_flags(
        full_name="c.s.t",
        has_path_read=True,
        has_rls_cls=True,
    )
    assert out["needs_compatibility_mode"] is False
    assert out["compat_mode_sql"] is None
    assert ew.NOTE_COMPAT_UNAVAILABLE_RLS_CLS in out["notes"]


def test_databricks_only_no_flags():
    out = ew.derive_access_flags(
        full_name="c.s.t",
        writer_entity_types=["NOTEBOOK", "JOB"],
        reader_entity_types=["DASHBOARD"],
    )
    assert out["has_external_non_databricks_writer"] is False
    assert out["needs_compatibility_mode"] is False
    assert out["confidence"] == "low"


def test_no_lineage_note():
    out = ew.derive_access_flags(full_name="c.s.t", had_any_lineage=False)
    assert ew.NOTE_NO_LINEAGE in out["notes"]


# --------------------------------------------------------------------------- #
# path_access_is_databricks_only + Databricks-native path access does not block
# --------------------------------------------------------------------------- #

def test_path_access_is_databricks_only_true_for_job_writer():
    loc = "s3://bucket/warehouse/db/table"
    write_paths = {loc + "/part=1": [{"entity_type": "JOB", "entity_id": "j1", "created_by": "u"}]}
    assert ew.path_access_is_databricks_only(loc, write_paths) is True


def test_path_access_is_databricks_only_false_for_external():
    loc = "s3://bucket/warehouse/db/table"
    paths = {loc: [{"entity_type": "FLINK", "entity_id": "f1"}]}
    assert ew.path_access_is_databricks_only(loc, paths) is False


def test_path_access_is_databricks_only_false_for_unknown_or_mixed():
    loc = "s3://bucket/warehouse/db/table"
    # Unknown/null entity_type stays conservative (could be an external client).
    assert ew.path_access_is_databricks_only(loc, {loc: [{"entity_type": None}]}) is False
    # Mixed: a Databricks job AND an external engine on the same location.
    mixed = {loc: [{"entity_type": "JOB"}, {"entity_type": "TRINO"}]}
    assert ew.path_access_is_databricks_only(loc, mixed) is False


def test_path_access_is_databricks_only_false_when_no_match_or_no_location():
    loc = "s3://bucket/warehouse/db/table"
    assert ew.path_access_is_databricks_only(loc, {"s3://other/loc": [{"entity_type": "JOB"}]}) is False
    assert ew.path_access_is_databricks_only(None, {loc: [{"entity_type": "JOB"}]}) is False
    assert ew.path_access_is_databricks_only(loc, None) is False


def test_databricks_only_path_write_does_not_block():
    # A Databricks JOB's path-based Delta write must NOT flag an external writer, even
    # though has_path_write is True (path access exists). This is the streaming-to-own-
    # external-table false positive the fix addresses.
    out = ew.derive_access_flags(
        full_name="c.s.t",
        has_path_write=True,
        path_write_databricks_only=True,
    )
    assert out["has_external_non_databricks_writer"] is False
    assert out["blockers"] == []
    # Path access is still a high-confidence signal.
    assert out["confidence"] == "high"


def test_databricks_only_path_read_does_not_need_compat():
    out = ew.derive_access_flags(
        full_name="c.s.t",
        has_path_read=True,
        path_read_databricks_only=True,
    )
    assert out["needs_compatibility_mode"] is False
    assert out["compat_mode_sql"] is None


def test_external_path_write_still_blocks_when_not_databricks_only():
    # Regression guard: an external/unknown path writer (databricks_only=False) still blocks.
    out = ew.derive_access_flags(
        full_name="c.s.t",
        has_path_write=True,
        path_write_databricks_only=False,
    )
    assert out["has_external_non_databricks_writer"] is True
    assert ew.BLOCKER_EXTERNAL_WRITER in out["blockers"]


# --------------------------------------------------------------------------- #
# apply_external_writer_to_eligibility / merge_notes
# --------------------------------------------------------------------------- #

def test_apply_external_writer_downgrades_to_needs_review():
    elig, blockers = ew.apply_external_writer_to_eligibility("ELIGIBLE", None, True)
    assert elig == "NEEDS_REVIEW"
    assert ew.BLOCKER_EXTERNAL_WRITER in blockers


def test_apply_external_writer_keeps_blocked():
    elig, blockers = ew.apply_external_writer_to_eligibility(
        "BLOCKED", "corrupted_or_inaccessible", True
    )
    assert elig == "BLOCKED"
    assert "corrupted_or_inaccessible" in blockers
    assert ew.BLOCKER_EXTERNAL_WRITER in blockers


def test_apply_external_writer_noop_when_none():
    elig, blockers = ew.apply_external_writer_to_eligibility("ELIGIBLE", None, False)
    assert elig == "ELIGIBLE"
    assert blockers is None


def test_apply_external_writer_clears_stale_blocker_and_restores_eligible():
    # Self-healing: a previously-flagged table whose signal cleared returns to ELIGIBLE
    # when the external writer was the ONLY blocker.
    elig, blockers = ew.apply_external_writer_to_eligibility(
        "NEEDS_REVIEW", ew.BLOCKER_EXTERNAL_WRITER, False
    )
    assert elig == "ELIGIBLE"
    assert blockers is None


def test_apply_external_writer_clear_keeps_other_blockers_and_review():
    elig, blockers = ew.apply_external_writer_to_eligibility(
        "NEEDS_REVIEW", f"non_delta_format, {ew.BLOCKER_EXTERNAL_WRITER}", False
    )
    assert elig == "NEEDS_REVIEW"
    assert "non_delta_format" in blockers
    assert ew.BLOCKER_EXTERNAL_WRITER not in blockers


def test_apply_external_writer_clear_never_upgrades_blocked():
    # A hard blocker keeps the table BLOCKED even after the external-writer token clears.
    elig, blockers = ew.apply_external_writer_to_eligibility(
        "BLOCKED", f"corrupted_or_inaccessible, {ew.BLOCKER_EXTERNAL_WRITER}", False
    )
    assert elig == "BLOCKED"
    assert "corrupted_or_inaccessible" in blockers
    assert ew.BLOCKER_EXTERNAL_WRITER not in blockers


def test_merge_notes_dedupes():
    assert ew.merge_notes(None, ["a", "b"]) == "a, b"
    assert ew.merge_notes("a", ["a", "c"]) == "a, c"
    assert ew.merge_notes("x, y", []) == "x, y"
    assert ew.merge_notes(None, []) is None


# --------------------------------------------------------------------------- #
# apply_path_access_to_eligibility
# --------------------------------------------------------------------------- #

def test_apply_path_access_downgrades_to_needs_review():
    elig, blockers = ew.apply_path_access_to_eligibility("ELIGIBLE", None, True)
    assert elig == "NEEDS_REVIEW"
    assert ew.BLOCKER_PATH_ACCESS in blockers


def test_apply_path_access_keeps_blocked():
    # BLOCKED is stronger than NEEDS_REVIEW — path access must never upgrade it.
    elig, blockers = ew.apply_path_access_to_eligibility(
        "BLOCKED", "corrupted_or_inaccessible", True
    )
    assert elig == "BLOCKED"
    assert "corrupted_or_inaccessible" in blockers
    assert ew.BLOCKER_PATH_ACCESS in blockers


def test_apply_path_access_noop_when_none():
    elig, blockers = ew.apply_path_access_to_eligibility("ELIGIBLE", None, False)
    assert elig == "ELIGIBLE"
    assert blockers is None


def test_apply_path_access_clears_stale_blocker_and_restores_eligible():
    # Self-healing: lineage false positive cleared -> back to ELIGIBLE when it was
    # the ONLY blocker.
    elig, blockers = ew.apply_path_access_to_eligibility(
        "NEEDS_REVIEW", ew.BLOCKER_PATH_ACCESS, False
    )
    assert elig == "ELIGIBLE"
    assert blockers is None


def test_apply_path_access_clear_keeps_other_blockers_and_review():
    elig, blockers = ew.apply_path_access_to_eligibility(
        "NEEDS_REVIEW", f"non_delta_format, {ew.BLOCKER_PATH_ACCESS}", False
    )
    assert elig == "NEEDS_REVIEW"
    assert "non_delta_format" in blockers
    assert ew.BLOCKER_PATH_ACCESS not in blockers


def test_apply_path_access_clear_never_upgrades_blocked():
    elig, blockers = ew.apply_path_access_to_eligibility(
        "BLOCKED", f"corrupted_or_inaccessible, {ew.BLOCKER_PATH_ACCESS}", False
    )
    assert elig == "BLOCKED"
    assert "corrupted_or_inaccessible" in blockers
    assert ew.BLOCKER_PATH_ACCESS not in blockers


def test_apply_path_access_idempotent():
    once = ew.apply_path_access_to_eligibility("ELIGIBLE", None, True)
    twice = ew.apply_path_access_to_eligibility(once[0], once[1], True)
    assert once == twice
    assert once[0] == "NEEDS_REVIEW"


# --------------------------------------------------------------------------- #
# ownership split + reconcile composition
# --------------------------------------------------------------------------- #

def test_path_access_token_not_dry_run_owned():
    # Ownership split: the profiler access pass owns path_based_access_blocks_managed.
    # The dry-run must NOT clear it via replace_owned_blockers — it persists until a
    # re-profile re-evaluates the lineage flag.
    assert ew.BLOCKER_PATH_ACCESS not in loc.DRY_RUN_OWNED_BLOCKERS


def test_redirect_hold_token_is_profiler_owned_and_its_check_token_is_dry_run_owned():
    from migration import path_access as pa

    assert ew.BLOCKER_PATH_REDIRECT_HOLD not in loc.DRY_RUN_OWNED_BLOCKERS
    assert pa.BLOCKER_REDIRECT_CHECK in loc.DRY_RUN_OWNED_BLOCKERS


def test_external_writer_and_path_access_compose():
    # Both signals set -> NEEDS_REVIEW carrying BOTH tokens; each reconcile owns only its
    # own token, so neither erases the other. Clearing both -> ELIGIBLE.
    e1, b1 = ew.apply_external_writer_to_eligibility("ELIGIBLE", None, True)
    e2, b2 = ew.apply_path_access_to_eligibility(e1, b1, True)
    assert e2 == "NEEDS_REVIEW"
    assert ew.BLOCKER_EXTERNAL_WRITER in b2
    assert ew.BLOCKER_PATH_ACCESS in b2

    e3, b3 = ew.apply_external_writer_to_eligibility(e2, b2, False)
    e4, b4 = ew.apply_path_access_to_eligibility(e3, b3, False)
    assert e4 == "ELIGIBLE"
    assert b4 is None


def test_path_access_clear_preserves_external_writer_token():
    # Path access clears but the external-writer signal persists -> stays NEEDS_REVIEW
    # with only the external-writer token remaining.
    elig, blockers = ew.apply_path_access_to_eligibility(
        "NEEDS_REVIEW", f"{ew.BLOCKER_EXTERNAL_WRITER}, {ew.BLOCKER_PATH_ACCESS}", False
    )
    assert elig == "NEEDS_REVIEW"
    assert ew.BLOCKER_EXTERNAL_WRITER in blockers
    assert ew.BLOCKER_PATH_ACCESS not in blockers
