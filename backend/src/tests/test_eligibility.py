"""Unit tests for the pure eligibility/sizing logic. No Spark required.

Run from the repo root with:  pytest src/tests/test_eligibility.py
"""

import os
import sys

import pytest

# Make ``migration`` importable when running pytest from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import eligibility as e  # noqa: E402
from migration import manifest as m  # noqa: E402
from migration import locations as loc  # noqa: E402


# --------------------------------------------------------------------------- #
# normalize_table_features / is_delta_format
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ([], []),
        (["columnMapping", "deletionVectors"], ["columnMapping", "deletionVectors"]),
        ("columnMapping, deletionVectors", ["columnMapping", "deletionVectors"]),
        ("[columnMapping, deletionVectors]", ["columnMapping", "deletionVectors"]),
        ("  single  ", ["single"]),
    ],
)
def test_normalize_table_features(raw, expected):
    assert e.normalize_table_features(raw) == expected


@pytest.mark.parametrize(
    "fmt,expected",
    [("DELTA", True), ("delta", True), ("Delta", True), ("PARQUET", False), ("CSV", False), (None, False), ("", False)],
)
def test_is_delta_format(fmt, expected):
    assert e.is_delta_format(fmt) is expected


# --------------------------------------------------------------------------- #
# Risk flags
# --------------------------------------------------------------------------- #

def test_uniform_iceberg_enabled():
    assert e.is_uniform_iceberg_enabled({"delta.universalFormat.enabledFormats": "iceberg"}) is True
    assert e.is_uniform_iceberg_enabled({"delta.universalFormat.enabledFormats": "iceberg,hudi"}) is True
    assert e.is_uniform_iceberg_enabled({"delta.universalFormat.enabledFormats": "compatibility"}) is False
    assert e.is_uniform_iceberg_enabled({}) is False
    assert e.is_uniform_iceberg_enabled(None) is False


def test_compatibility_enabled():
    assert e.is_compatibility_enabled({"delta.universalFormat.enabledFormats": "compatibility"}) is True
    assert e.is_compatibility_enabled({"delta.universalFormat.enabledFormats": "iceberg"}) is False


# --------------------------------------------------------------------------- #
# Size banding + estimates
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "size,band",
    [
        (None, "unknown"),
        (50 * e.GB, "<=100GB"),
        (100 * e.GB, "<=100GB"),
        (500 * e.GB, "~1TB"),
        (1 * e.TB, "~1TB"),
        (5 * e.TB, "~10TB"),
        (10 * e.TB, "~10TB"),
        (50 * e.TB, ">10TB"),
    ],
)
def test_size_band(size, band):
    assert e.size_band(size) == band


def test_estimate_data_copy_minutes_matches_doc_anchors():
    # Anchored, piecewise-linear model: copy time passes EXACTLY through the
    # documented downtime knots (100GB->6, 1TB->30, 10TB->90).
    assert e.estimate_data_copy_minutes(100 * e.GB) == 6.0
    assert e.estimate_data_copy_minutes(1 * e.TB) == 30.0
    assert e.estimate_data_copy_minutes(10 * e.TB) == 90.0


def test_estimate_data_copy_minutes_edges():
    assert e.estimate_data_copy_minutes(0) == 0.0
    assert e.estimate_data_copy_minutes(None) == 0.0


def test_estimate_data_copy_minutes_monotonic_across_bands():
    # The anchored model is strictly non-decreasing, so crossing a band boundary
    # no longer drops the estimate (the old per-band-cores model failed this:
    # a 101GB table could estimate LESS than a 100GB one).
    assert e.estimate_data_copy_minutes(101 * e.GB) > e.estimate_data_copy_minutes(100 * e.GB)
    assert e.estimate_data_copy_minutes(2 * e.TB) > e.estimate_data_copy_minutes(1 * e.TB)
    assert e.estimate_data_copy_minutes(50 * e.TB) > e.estimate_data_copy_minutes(10 * e.TB)


def test_estimate_data_copy_seconds():
    assert e.estimate_data_copy_seconds(0) == 0.0
    assert e.estimate_data_copy_seconds(None) == 0.0
    # 100GB knot = 6.0 min -> 360.0 s (locks the x60 conversion).
    assert e.estimate_data_copy_seconds(100 * e.GB) == 360.0
    # MB-scale: minutes rounds to 0.0 but seconds is non-zero — the whole reason
    # the seconds column exists (so aggregates of sub-minute tables aren't 0).
    mb = 50 * 1024 * 1024
    assert e.estimate_data_copy_minutes(mb) == 0.0
    assert e.estimate_data_copy_seconds(mb) > 0.0


def test_estimate_downtime_minutes():
    assert e.estimate_downtime_minutes(50 * e.GB) == 1.5
    assert e.estimate_downtime_minutes(5 * e.TB) == 3.0
    assert e.estimate_downtime_minutes(50 * e.TB) == 5.0


@pytest.mark.parametrize(
    "size,cores,warehouse_substr",
    [
        (50 * e.GB, 32, "X-Large"),
        (1 * e.TB, 64, "2X-Large"),
        (10 * e.TB, 256, "4X-Large"),
        (50 * e.TB, 256, "4X-Large"),
    ],
)
def test_recommended_cores_and_warehouse(size, cores, warehouse_substr):
    assert e.recommended_cores(size) == cores
    assert warehouse_substr in e.recommended_warehouse(size)


def test_cluster_size_status_warns_when_undersized():
    status, note = e.cluster_size_status(32, 256)
    assert status == e.CHECK_WARN
    assert "32 cores" in note


def test_cluster_size_status_ok_when_adequate():
    status, _ = e.cluster_size_status(256, 256)
    assert status == e.CHECK_INFO


def test_cluster_size_status_serverless_or_unknown_is_info():
    status, note = e.cluster_size_status(None, 256)
    assert status == e.CHECK_INFO
    assert "skipped" in note
    assert e.cluster_size_status(0, 64)[0] == e.CHECK_INFO


# --------------------------------------------------------------------------- #
# assess_eligibility
# --------------------------------------------------------------------------- #

def test_eligible_clean_delta():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=True,
        data_source_format="DELTA",
        min_reader_version=1,
        min_writer_version=2,
        table_features=["appendOnly"],
    )
    assert elig == e.ELIGIBLE
    assert blockers == []


def test_blocked_corrupted_takes_precedence():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=False,
        data_source_format="DELTA",
        min_reader_version=1,
        min_writer_version=2,
        table_features=[],
    )
    assert elig == e.BLOCKED
    assert "corrupted_or_inaccessible" in blockers


_DENIED = "PERMISSION_DENIED: User does not have SELECT on Table 'c.s.t'."


def test_describe_detail_denied_reads_only_privilege_refusals():
    assert e.describe_detail_denied(_DENIED)
    assert e.describe_detail_denied("[INSUFFICIENT_PERMISSIONS] Insufficient privileges")
    assert not e.describe_detail_denied("DELTA_TABLE_NOT_FOUND: _delta_log is missing")
    assert not e.describe_detail_denied(None)


def test_unreadable_table_is_held_for_review_not_called_corrupt():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=False,
        describe_detail_denied=True,
        data_source_format="DELTA",
        min_reader_version=None,
        min_writer_version=None,
        table_features=[],
    )
    assert elig == e.NEEDS_REVIEW
    assert blockers == [e.BLOCKER_CANNOT_READ]


def test_describe_detail_check_names_the_refused_read():
    assert e.describe_detail_check(True)["status"] == e.CHECK_PASS
    denied = e.describe_detail_check(False, _DENIED)
    assert denied["status"] == e.CHECK_FAIL and denied["blocker"] == e.BLOCKER_CANNOT_READ
    assert "Authorize" in denied["detail"]
    broken = e.describe_detail_check(False, "DELTA_TABLE_NOT_FOUND")
    assert broken["blocker"] == e.BLOCKER_CORRUPTED
    assert e.describe_detail_check(False)["blocker"] == e.BLOCKER_CORRUPTED


def test_cannot_read_is_dry_run_owned_and_has_remediation():
    from migration import locations as loc
    from migration import runbook as rb

    assert e.BLOCKER_CANNOT_READ in loc.DRY_RUN_OWNED_BLOCKERS
    hint = rb.blocker_remediation(e.BLOCKER_CANNOT_READ, catalog="c", schema="s", full_name="c.s.t")
    assert "not corrupted" in hint and "target_schema=c.s" in hint


def test_needs_review_non_delta():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=True,
        data_source_format="PARQUET",
        min_reader_version=1,
        min_writer_version=1,
        table_features=[],
    )
    assert elig == e.NEEDS_REVIEW
    assert "non_delta_format" in blockers


def test_columnmapping_protocol_is_eligible():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=True,
        data_source_format="DELTA",
        min_reader_version=2,
        min_writer_version=7,
        table_features=["columnMapping"],
    )
    assert elig == e.ELIGIBLE
    assert blockers == []


def test_columnmapping_protocol_still_blocked_when_corrupted():
    elig, blockers = e.assess_eligibility(
        describe_detail_ok=False,
        data_source_format="DELTA",
        min_reader_version=2,
        min_writer_version=7,
        table_features=["columnMapping"],
    )
    assert elig == e.BLOCKED
    assert blockers == ["corrupted_or_inaccessible"]


# --------------------------------------------------------------------------- #
# manifest helpers
# --------------------------------------------------------------------------- #

def test_manifest_row_has_all_columns():
    row = m.build_manifest_row(catalog="c", schema="s", table_name="t")
    assert set(row.keys()) == set(m.MANIFEST_COLUMN_NAMES)
    assert row["full_name"] == "c.s.t"
    assert row["state"] == m.STATE_DISCOVERED


def test_manifest_fqn_and_ddl():
    assert m.manifest_fqn("ops", "migration") == "ops.migration.table_manifest"
    ddl = m.create_manifest_table_sql("ops", "migration")
    assert "CREATE TABLE IF NOT EXISTS ops.migration.table_manifest" in ddl
    for name, _ in m.MANIFEST_COLUMNS:
        assert name in ddl


def test_quoting_handles_special_chars():
    assert m.make_full_name("cat", "sch", "weird-name") == "`cat`.`sch`.`weird-name`"


# --------------------------------------------------------------------------- #
# Dry-run helpers (Prompt 4)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("17.2.x-scala2.12", (17, 2)),
        ("15.4.x-photon-scala2.12", (15, 4)),
        ("14.3", (14, 3)),
        ("17.0", (17, 0)),
        ("  17.2 ", (17, 2)),
        (None, None),
        ("", None),
        ("serverless", None),
    ],
)
def test_parse_dbr_version(raw, expected):
    assert e.parse_dbr_version(raw) == expected


def test_runtime_supports_set_managed():
    # Floor is DBR 17.3 LTS (convert-external-managed doc); below 17.3 is unsupported.
    assert e.runtime_supports_set_managed((17, 3)) is True
    assert e.runtime_supports_set_managed((17, 4)) is True
    assert e.runtime_supports_set_managed((18, 0)) is True
    assert e.runtime_supports_set_managed((17, 2)) is False
    assert e.runtime_supports_set_managed((17, 0)) is False
    assert e.runtime_supports_set_managed((16, 4)) is False
    assert e.runtime_supports_set_managed((15, 4)) is False
    assert e.runtime_supports_set_managed(None, is_serverless=True) is True
    assert e.runtime_supports_set_managed(None, is_serverless=False) is False


def test_runtime_supports_truncate_uniform():
    # TRUNCATE UNIFORM HISTORY shares the 17.3 floor.
    assert e.runtime_supports_truncate_uniform((17, 3)) is True
    assert e.runtime_supports_truncate_uniform((17, 4)) is True
    assert e.runtime_supports_truncate_uniform((17, 2)) is False
    assert e.runtime_supports_truncate_uniform((17, 1)) is False
    assert e.runtime_supports_truncate_uniform((17, 0)) is False
    assert e.runtime_supports_truncate_uniform(None, is_serverless=True) is True


def test_build_convert_command():
    assert e.build_convert_command("c.s.t", False) == "ALTER TABLE c.s.t SET MANAGED;"
    assert (
        e.build_convert_command("c.s.t", True)
        == "ALTER TABLE c.s.t SET MANAGED TRUNCATE UNIFORM HISTORY;"
    )


def test_build_unset_command():
    assert e.build_unset_command("c.s.t") == "ALTER TABLE c.s.t UNSET MANAGED;"


def test_build_reshare_commands():
    cmds = e.build_reshare_commands("cat.sch.tbl", share_name="my_share")
    assert cmds[0] == "ALTER SHARE my_share REMOVE TABLE cat.sch.tbl;"
    assert cmds[1] == "ALTER SHARE my_share ADD TABLE cat.sch.tbl AS tbl WITH HISTORY;"


@pytest.mark.parametrize(
    "op,expected",
    [
        ("OPTIMIZE", True),
        ("optimize", True),
        ("ZORDER BY", True),
        ("CLUSTER BY", True),
        ("REORG", True),
        ("WRITE", False),
        ("MERGE", False),
        ("STREAMING UPDATE", False),
        (None, False),
    ],
)
def test_is_compaction_operation(op, expected):
    assert e.is_compaction_operation(op) is expected


@pytest.mark.parametrize(
    "op,expected",
    [
        ("VACUUM START", True),
        ("VACUUM END", True),
        ("vacuum start", True),
        ("VACUUM END (with retry)", True),  # startswith match tolerates trailing detail
        ("VACUUM", False),  # bare VACUUM is not one of the recorded ops
        ("OPTIMIZE", False),  # compaction is not vacuum
        ("WRITE", False),
        ("DELETE", False),
        (None, False),
    ],
)
def test_is_vacuum_operation(op, expected):
    assert e.is_vacuum_operation(op) is expected


def test_gating_passed_and_finalize_ok():
    results = [
        e.check_result(1, "runtime", e.CHECK_PASS),
        e.check_result(5, "compaction", e.CHECK_WARN),
        e.check_result(9, "cmd", e.CHECK_INFO),
    ]
    assert e.gating_passed(results) is True
    state, blockers = e.finalize_dry_run_state(results)
    assert state == e.STATE_DRY_RUN_OK
    assert blockers == []


def test_finalize_blocked_collects_blockers():
    results = [
        e.check_result(2, "describe_detail", e.CHECK_FAIL, blocker="corrupted_or_inaccessible"),
        e.check_result(3, "name_based", e.CHECK_FAIL, blocker="path_based_access_must_be_fixed_first"),
        e.check_result(3, "name_based_dup", e.CHECK_FAIL, blocker="path_based_access_must_be_fixed_first"),
    ]
    assert e.gating_passed(results) is False
    state, blockers = e.finalize_dry_run_state(results)
    assert state == e.BLOCKED
    assert blockers == ["corrupted_or_inaccessible", "path_based_access_must_be_fixed_first"]


# --------------------------------------------------------------------------- #
# locations helpers
# --------------------------------------------------------------------------- #

def test_container_key():
    assert loc.container_key("s3://my-bucket/path/to/x") == "my-bucket"
    assert loc.container_key("abfss://cont@acct.dfs.core.windows.net/x") == "acct.dfs.core.windows.net"
    assert loc.container_key(None) is None


def test_cross_region_risk():
    assert loc.cross_region_risk("us-east-1", "us-west-2") is True
    assert loc.cross_region_risk("us-east-1", "us-east-1") is False
    assert loc.cross_region_risk(None, "us-east-1") is None
    assert loc.cross_region_risk("us-east-1", None) is None


def test_parse_desc_extended_tuples():
    rows = [("Region", "us-east-1"), ("Storage Root", "s3://b/x")]
    parsed = loc.parse_desc_extended(rows)
    assert parsed["Region"] == "us-east-1"
    assert parsed["Storage Root"] == "s3://b/x"
