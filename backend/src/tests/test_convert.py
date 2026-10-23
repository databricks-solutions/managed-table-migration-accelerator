"""Unit tests for convert + audit pure logic. No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import audit  # noqa: E402
from migration import convert as cv  # noqa: E402
from migration import locations as loc  # noqa: E402


# --------------------------------------------------------------------------- #
# classify_convert_error
# --------------------------------------------------------------------------- #

def test_classify_internal_error_retry():
    info = cv.classify_convert_error(
        "[DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR] something failed"
    )
    assert info["error_class"] == "DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR"
    assert info["disposition"] == cv.DISPOSITION_RETRY
    assert "same DBR" in info["guidance"].lower() or "do not change dbr" in info["guidance"].lower()


def test_classify_file_validation_retry_after_check():
    info = cv.classify_convert_error(
        "DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED: missing files"
    )
    assert info["disposition"] == cv.DISPOSITION_RETRY_AFTER_CHECK
    assert "driver logs" in info["guidance"].lower()


def test_classify_truncated_log_terminal():
    info = cv.classify_convert_error("DELTA_TRUNCATED_TRANSACTION_LOG at version 5")
    assert info["disposition"] == cv.DISPOSITION_TERMINAL
    info2 = cv.classify_convert_error("DELTA_TXN_LOG_FAILED_INTEGRITY")
    assert info2["disposition"] == cv.DISPOSITION_TERMINAL


def test_classify_state_recover_errors_terminal():
    info = cv.classify_convert_error(
        "[DELTA_STATE_RECOVER_ERRORS] failed to recover table state from log"
    )
    assert info["error_class"] == "DELTA_STATE_RECOVER_ERRORS"
    assert info["disposition"] == cv.DISPOSITION_TERMINAL
    assert "do not blind-retry" in info["guidance"].lower()


def test_classify_unknown():
    info = cv.classify_convert_error("some random spark error")
    assert info["error_class"] == "UNKNOWN"
    assert info["disposition"] == cv.DISPOSITION_UNKNOWN


def test_classify_none_message():
    info = cv.classify_convert_error(None)
    assert info["disposition"] == cv.DISPOSITION_UNKNOWN


# --------------------------------------------------------------------------- #
# Type verification
# --------------------------------------------------------------------------- #

def test_extract_and_is_type_managed_from_tuples():
    rows = [("Catalog", "c"), ("Type", "MANAGED"), ("Provider", "delta")]
    assert cv.extract_describe_extended_type(rows) == "MANAGED"
    assert cv.is_type_managed(rows) is True


def test_is_type_managed_external():
    rows = [("Type", "EXTERNAL")]
    assert cv.is_type_managed(rows) is False


def test_is_type_managed_table_type_key():
    rows = [("Table Type", "MANAGED")]
    assert cv.is_type_managed(rows) is True


def test_is_type_managed_missing():
    rows = [("Provider", "delta")]
    assert cv.extract_describe_extended_type(rows) is None
    assert cv.is_type_managed(rows) is False


# --------------------------------------------------------------------------- #
# build_convert_command re-export
# --------------------------------------------------------------------------- #

def test_build_convert_command_reexport():
    assert cv.build_convert_command("c.s.t", False) == "ALTER TABLE c.s.t SET MANAGED;"
    assert cv.build_convert_command("c.s.t", True) == "ALTER TABLE c.s.t SET MANAGED TRUNCATE UNIFORM HISTORY;"


# --------------------------------------------------------------------------- #
# reassert_gating — managed-location hard blocker
# --------------------------------------------------------------------------- #

class _FakeDF:
    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return self._rows


class _FakeSpark:
    """Returns configurable rows for DESCRIBE EXTENDED; truthy rows otherwise."""

    def __init__(self, describe_type=None):
        self._type = describe_type

    def sql(self, q):
        if "DESCRIBE EXTENDED" in q:
            return _FakeDF([("Type", self._type)] if self._type else [("Provider", "delta")])
        return _FakeDF([("ok",)])  # DESCRIBE DETAIL succeeds


def _gate(describe_type=None, **kw):
    base = dict(
        full_name_bt="c.s.t", uniform_iceberg_enabled=False,
        has_path_based_access=False, supports_truncate_uniform=True,
    )
    base.update(kw)
    return cv.reassert_gating(_FakeSpark(describe_type), **base)


def test_reassert_gating_has_no_columnmapping_gate():
    assert _gate()["disposition"] == cv.DISPOSITION_READY
    with pytest.raises(TypeError):
        _gate(has_columnmapping_risk=True)


def test_reassert_gating_blocks_a_native_path_consumer_without_a_valid_opt_in():
    from migration import path_access as pa

    res = _gate(path_redirect_held=True)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert res["blockers"] == [pa.BLOCKER_REDIRECT_CHECK]
    assert _gate()["blockers"] == []


def test_convert_re_check_catches_an_opt_in_that_lapsed_since_the_dry_run():
    from migration import path_access as pa

    consumers = pa.redirect_verdict(pa.native_path_consumers("s3://b/t", [
        {"path": "s3://b/t", "role": "READ", "entity_type": "JOB", "entity_id": "1",
         "last_event_time": None, "dbr_versions": ["18.2.x"]},
    ]))
    row = {"path_redirect_verdict": consumers["verdict"],
           "path_redirect_consumers": pa.serialize_consumers(consumers["consumers"]),
           "path_redirect_optin_status": pa.OPTIN_ACTIVE,
           "path_redirect_optin_fingerprint": pa.consumer_fingerprint(consumers["consumers"])}
    assert _gate(path_redirect_held=pa.native_hold(row))["blockers"] == []
    stale = {**row, "path_redirect_optin_fingerprint": "recorded-before-a-new-job-appeared"}
    assert _gate(path_redirect_held=pa.native_hold(stale))["blockers"] == [pa.BLOCKER_REDIRECT_CHECK]


def test_reassert_gating_ready_when_managed_location_configured():
    res = _gate(managed_location_status=loc.ML_CONFIGURED)
    assert res["disposition"] == cv.DISPOSITION_READY
    assert res["blockers"] == []


def test_reassert_gating_ready_when_status_omitted():
    # None => caller did not check (backward-compatible); does not block on its own.
    res = _gate()
    assert res["disposition"] == cv.DISPOSITION_READY
    assert "no_managed_location" not in res["blockers"]


def test_reassert_gating_blocks_when_no_managed_location():
    res = _gate(managed_location_status=loc.ML_NOT_CONFIGURED)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert loc.NOTE_NO_MANAGED_LOCATION in res["blockers"]


def test_reassert_gating_blocks_when_managed_location_unresolved():
    # Fail-closed: we could not prove a location exists -> block with the DISTINCT blocker.
    res = _gate(managed_location_status=loc.ML_UNRESOLVED)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert loc.NOTE_MANAGED_LOCATION_UNRESOLVED in res["blockers"]
    assert loc.NOTE_NO_MANAGED_LOCATION not in res["blockers"]


def test_reassert_gating_blocks_on_recent_compaction():
    res = _gate(recent_compaction=True)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert "recent_compaction_activity" in res["blockers"]


def test_reassert_gating_blocks_on_recent_write():
    # symmetric fail-safe for opportunistic converts: a live writer blocks conversion.
    res = _gate(recent_write=True)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert "recent_write_activity" in res["blockers"]


def test_reassert_gating_recent_write_defaults_off():
    # default (no recent_write) must not block -> backward compatible.
    res = _gate()
    assert res["disposition"] == cv.DISPOSITION_READY
    assert "recent_write_activity" not in res["blockers"]


def test_reassert_gating_blocks_on_recent_vacuum():
    # a concurrent VACUUM deletes files during SET MANAGED -> hard block.
    res = _gate(recent_vacuum=True)
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert "recent_vacuum_activity" in res["blockers"]


def test_reassert_gating_recent_vacuum_defaults_off():
    # default (no recent_vacuum) must not block -> backward compatible.
    res = _gate()
    assert res["disposition"] == cv.DISPOSITION_READY
    assert "recent_vacuum_activity" not in res["blockers"]


def test_reassert_gating_already_managed():
    res = _gate(describe_type="MANAGED")
    assert res["disposition"] == cv.DISPOSITION_ALREADY_MANAGED
    assert res["blockers"] == []


def test_reassert_gating_unexpected_table_type():
    res = _gate(describe_type="VIEW")
    assert res["disposition"] == cv.DISPOSITION_BLOCKED
    assert cv.BLOCKER_UNEXPECTED_TABLE_TYPE in res["blockers"]


# --------------------------------------------------------------------------- #
# post_conversion_checklist
# --------------------------------------------------------------------------- #

def test_checklist_streaming_compat_share_po():
    items = cv.post_conversion_checklist(
        full_name="c.s.t",
        needs_compatibility_mode=True,
        compat_mode_sql="ALTER TABLE c.s.t SET TBLPROPERTIES(...);",
        is_delta_shared=True,
        streaming_rows=[{"type": "DLT", "role": "WRITE", "job_or_pipeline_id": "p1", "owner": "u@x"}],
        po_enabled=False,
    )
    joined = "\n".join(items)
    assert "Restart 1 streaming" in joined
    assert "Compatibility Mode" in joined
    assert "ALTER SHARE" in joined
    assert "VACUUM after day 14" in joined


def test_checklist_minimal():
    items = cv.post_conversion_checklist(full_name="c.s.t")
    # Only the PO note should appear when nothing else applies.
    assert len(items) == 1
    assert "Predictive Optimization" in items[0]


# --------------------------------------------------------------------------- #
# audit state machine + row builder
# --------------------------------------------------------------------------- #

def test_valid_transitions():
    assert audit.is_valid_transition(audit.STATE_DRY_RUN_OK, audit.STATE_CONVERTING) is True
    assert audit.is_valid_transition(audit.STATE_CONVERTING, audit.STATE_CONVERTED) is True
    assert audit.is_valid_transition(audit.STATE_CONVERTED, audit.STATE_VALIDATED) is True
    assert audit.is_valid_transition(audit.STATE_CONVERTED, audit.STATE_ROLLING_BACK) is True


def test_invalid_transition():
    assert audit.is_valid_transition(audit.STATE_VALIDATED, audit.STATE_CONVERTING) is False
    assert audit.is_valid_transition(audit.STATE_CLEANED, audit.STATE_CONVERTED) is False


def test_self_transition_allowed():
    assert audit.is_valid_transition(audit.STATE_CONVERTING, audit.STATE_CONVERTING) is True


def test_unknown_from_state_allows_any():
    assert audit.is_valid_transition(None, audit.STATE_CONVERTED) is True


# --------------------------------------------------------------------------- #
# Out-of-band reconcile states (CONVERTED_EXTERNAL / DROPPED) + RECONCILE action
# --------------------------------------------------------------------------- #

def test_converted_external_reachable_from_external_side_states():
    # A human can SET MANAGED out from under any of these (incl. ROLLED_BACK).
    for src in (
        audit.STATE_DISCOVERED, audit.STATE_ASSESSED, audit.STATE_ELIGIBLE,
        audit.STATE_BLOCKED, audit.STATE_DRY_RUN_OK, audit.STATE_ROLLED_BACK,
    ):
        assert audit.is_valid_transition(src, audit.STATE_CONVERTED_EXTERNAL) is True, src


def test_converting_is_not_a_converted_external_source():
    # CONVERTING mid-flight reconciliation is owned by job_04_convert, not the profiler.
    assert audit.is_valid_transition(audit.STATE_CONVERTING, audit.STATE_CONVERTED_EXTERNAL) is False


def test_converted_external_forward_downstream_matches_converted_minus_rollback():
    assert audit.is_valid_transition(audit.STATE_CONVERTED_EXTERNAL, audit.STATE_VALIDATED) is True
    assert audit.is_valid_transition(audit.STATE_CONVERTED_EXTERNAL, audit.STATE_BLOCKED) is True
    # No provable 14-day window => no rollback edge for an externally-converted table.
    assert audit.is_valid_transition(audit.STATE_CONVERTED_EXTERNAL, audit.STATE_ROLLING_BACK) is False


def test_dropped_reachable_from_every_non_terminal_state_and_re_enters():
    for src in (
        audit.STATE_DISCOVERED, audit.STATE_ASSESSED, audit.STATE_ELIGIBLE,
        audit.STATE_BLOCKED, audit.STATE_DRY_RUN_OK, audit.STATE_CONVERTING,
        audit.STATE_CONVERTED, audit.STATE_VALIDATED, audit.STATE_ROLLING_BACK,
        audit.STATE_ROLLED_BACK, audit.STATE_CONVERTED_EXTERNAL,
    ):
        assert audit.is_valid_transition(src, audit.STATE_DROPPED) is True, src
    # A re-created table re-enters discovery; DROPPED is never a dead end.
    assert audit.is_valid_transition(audit.STATE_DROPPED, audit.STATE_DISCOVERED) is True
    assert audit.is_valid_transition(audit.STATE_DROPPED, audit.STATE_ASSESSED) is True


def test_dropped_and_converted_external_not_reachable_from_cleaned():
    assert audit.is_valid_transition(audit.STATE_CLEANED, audit.STATE_DROPPED) is False
    assert audit.is_valid_transition(audit.STATE_CLEANED, audit.STATE_CONVERTED_EXTERNAL) is False


def test_out_of_band_rollback_routes_converted_validated_to_assessed():
    assert audit.is_valid_transition(audit.STATE_CONVERTED, audit.STATE_ASSESSED) is True
    assert audit.is_valid_transition(audit.STATE_VALIDATED, audit.STATE_ASSESSED) is True


def test_reconcile_action_defined():
    assert audit.ACTION_RECONCILE == "RECONCILE"


def test_transitions_round_trip_through_is_valid_transition():
    for from_state, targets in audit._TRANSITIONS.items():
        for to_state in targets:
            assert audit.is_valid_transition(from_state, to_state) is True


def test_audit_row_has_all_columns_and_truncates_message():
    row = audit.build_audit_row(
        full_name="c.s.t", action=audit.ACTION_CONVERT, phase=audit.PHASE_FAILED,
        error_message="x" * 5000,
    )
    assert set(row.keys()) == set(audit.AUDIT_COLUMN_NAMES)
    assert len(row["error_message"]) == 4000
    assert row["audit_id"]  # auto-generated uuid


def test_audit_ddl_contains_columns():
    ddl = audit.create_audit_table_sql("main", "migration")
    assert "audit_log" in ddl
    for name, _ in audit.AUDIT_COLUMNS:
        assert name in ddl


def test_audit_row_includes_job_provenance_columns():
    audit.set_run_context(job_id=None, job_run_id=None)  # clean slate
    row = audit.build_audit_row(
        full_name="c.s.t", action=audit.ACTION_CONVERT, phase=audit.PHASE_SUCCESS,
    )
    assert "job_id" in row and "job_run_id" in row
    assert row["job_id"] is None and row["job_run_id"] is None


def test_audit_ambient_run_context_is_stamped_and_overridable():
    try:
        audit.set_run_context(job_id="123", job_run_id="456")
        row = audit.build_audit_row(
            full_name="c.s.t", action=audit.ACTION_CONVERT, phase=audit.PHASE_STARTED,
        )
        assert row["job_id"] == "123" and row["job_run_id"] == "456"
        # Explicit kwarg overrides the ambient context.
        row2 = audit.build_audit_row(
            full_name="c.s.t", action=audit.ACTION_CONVERT, phase=audit.PHASE_STARTED,
            job_run_id="999",
        )
        assert row2["job_run_id"] == "999" and row2["job_id"] == "123"
        # Empty strings normalize to None (a notebook run off a job).
        audit.set_run_context(job_id="", job_run_id="")
        row3 = audit.build_audit_row(
            full_name="c.s.t", action=audit.ACTION_CONVERT, phase=audit.PHASE_STARTED,
        )
        assert row3["job_id"] is None and row3["job_run_id"] is None
    finally:
        audit.set_run_context(job_id=None, job_run_id=None)  # reset for other tests


def test_audit_ddl_and_column_names_include_job_provenance():
    assert audit.AUDIT_COLUMN_NAMES[-2:] == ("job_id", "job_run_id")
    ddl = audit.create_audit_table_sql("main", "migration")
    assert "job_id STRING" in ddl and "job_run_id STRING" in ddl


# --------------------------------------------------------------------------- #
# Rollback (Prompt 7)
# --------------------------------------------------------------------------- #

from datetime import datetime, timedelta, timezone  # noqa: E402


def test_build_unset_command_reexport():
    assert cv.build_unset_command("c.s.t") == "ALTER TABLE c.s.t UNSET MANAGED;"


def test_rollback_window_allowed_within_14_days():
    now = datetime(2026, 1, 20)
    end = now - timedelta(days=10)
    out = cv.rollback_window_status(end, now=now)
    assert out["allowed"] is True
    assert out["days_elapsed"] == pytest.approx(10.0, abs=0.1)


def test_rollback_window_refused_after_14_days():
    now = datetime(2026, 1, 20)
    end = now - timedelta(days=15)
    out = cv.rollback_window_status(end, now=now)
    assert out["allowed"] is False
    assert "no longer safe" in out["reason"]


def test_rollback_window_boundary_exactly_14_days():
    now = datetime(2026, 1, 20)
    end = now - timedelta(days=14)
    assert cv.rollback_window_status(end, now=now)["allowed"] is True


def test_rollback_window_no_end_time_refused():
    out = cv.rollback_window_status(None, now=datetime(2026, 1, 20))
    assert out["allowed"] is False
    assert out["days_elapsed"] is None


def test_rollback_window_negative_elapsed_clamped():
    now = datetime(2026, 1, 20)
    end = now + timedelta(hours=1)  # clock skew
    out = cv.rollback_window_status(end, now=now)
    assert out["allowed"] is True
    assert out["days_elapsed"] == 0.0


def test_rollback_window_mixed_naive_and_aware_datetimes():
    # Regression: audit-log end_time collected from Spark is tz-naive while now is
    # tz-aware UTC. Subtracting them must not raise; naive is treated as UTC.
    now = datetime(2026, 1, 20, tzinfo=timezone.utc)
    end_naive = datetime(2026, 1, 10)  # 10 days earlier, naive
    out = cv.rollback_window_status(end_naive, now=now)
    assert out["allowed"] is True
    assert out["days_elapsed"] == pytest.approx(10.0, abs=0.1)


def test_rollback_window_aware_end_naive_now():
    # The reverse mismatch is also handled.
    now_naive = datetime(2026, 1, 20)
    end_aware = datetime(2026, 1, 18, tzinfo=timezone.utc)
    out = cv.rollback_window_status(end_aware, now=now_naive)
    assert out["allowed"] is True
    assert out["days_elapsed"] == pytest.approx(2.0, abs=0.1)


def test_is_type_external():
    assert cv.is_type_external([("Type", "EXTERNAL")]) is True
    assert cv.is_type_external([("Type", "MANAGED")]) is False
    assert cv.is_type_external([("Provider", "delta")]) is False


def test_post_rollback_checklist():
    items = cv.post_rollback_checklist(
        full_name="c.s.t",
        is_delta_shared=True,
        streaming_rows=[{"type": "DLT", "role": "WRITE", "job_or_pipeline_id": "p1", "owner": "u"}],
    )
    joined = "\n".join(items)
    assert "Restart 1 streaming" in joined
    assert "ALTER SHARE" in joined
    assert "abandoned managed location" in joined
    assert "VERSION" in joined  # version-not-timestamp time-travel note


def test_post_rollback_checklist_minimal():
    items = cv.post_rollback_checklist(full_name="c.s.t")
    assert len(items) == 1
    assert "abandoned managed location" in items[0]


def test_post_rollback_checklist_omits_restored_streams_and_notes_them():
    items = cv.post_rollback_checklist(
        full_name="c.s.t",
        streaming_rows=[
            {"type": "DLT", "role": "WRITE", "job_or_pipeline_id": "p1", "owner": "u"},
            {"type": "STRUCTURED_STREAMING", "role": "WRITE", "job_or_pipeline_id": "j2", "owner": "u"},
        ],
        restored_stream_targets={"p1"},
    )
    joined = "\n".join(items)
    # p1 was restarted by the restore pass -> verified summary line, not an advisory reminder.
    assert "Restarted 1 streaming" in joined
    assert "verified" in joined.lower()
    # j2 had no ledger coverage -> still advisory.
    assert "Restart 1 streaming" in joined
    assert "id=j2" in joined
    assert "id=p1" not in joined  # the restored one is not listed as a manual TODO


def test_post_rollback_checklist_all_restored_has_no_advisory_restart():
    items = cv.post_rollback_checklist(
        full_name="c.s.t",
        streaming_rows=[{"type": "DLT", "role": "WRITE", "job_or_pipeline_id": "p1", "owner": "u"}],
        restored_stream_targets={"p1"},
    )
    joined = "\n".join(items)
    assert "Restarted 1 streaming" in joined
    assert "Restart 1 streaming" not in joined  # no advisory reminder when all were restored


# --------------------------------------------------------------------------- #
# resolve_uniform — live Iceberg-reads re-check before building the convert command
# --------------------------------------------------------------------------- #
_ICEBERG = {"delta.universalFormat.enabledFormats": "iceberg"}


def test_resolve_uniform_live_iceberg_after_profiling_uses_truncate():
    uniform, note = cv.resolve_uniform(False, _ICEBERG)
    assert uniform is True and "TRUNCATE UNIFORM HISTORY" in note
    assert cv.build_convert_command("`c`.`s`.`t`", uniform).endswith(
        "SET MANAGED TRUNCATE UNIFORM HISTORY;")


def test_resolve_uniform_live_off_after_profiling_uses_plain_set_managed():
    uniform, note = cv.resolve_uniform(True, {})
    assert uniform is False and "plain SET MANAGED" in note


def test_resolve_uniform_agreement_has_no_note():
    assert cv.resolve_uniform(True, _ICEBERG) == (True, None)
    assert cv.resolve_uniform(False, {"x": "y"}) == (False, None)
    assert cv.resolve_uniform(None, {}) == (False, None)


def test_resolve_uniform_unreadable_falls_back_to_the_snapshot():
    assert cv.resolve_uniform(True, None) == (True, None)
    assert cv.resolve_uniform(False, None) == (False, None)


class _DetailSpark:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows, fail

    def sql(self, stmt):
        assert stmt.startswith("DESCRIBE DETAIL ")
        if self.fail:
            raise RuntimeError("boom")
        return self

    def collect(self):
        return self.rows


class _Row(dict):
    def asDict(self):
        return dict(self)


def test_read_live_properties_edge():
    assert cv.read_live_properties(_DetailSpark(fail=True), "t") is None
    assert cv.read_live_properties(_DetailSpark(rows=[]), "t") is None
    assert cv.read_live_properties(_DetailSpark(rows=[_Row(properties=None)]), "t") == {}
    assert cv.read_live_properties(_DetailSpark(rows=[_Row(properties=_ICEBERG)]), "t") == _ICEBERG
