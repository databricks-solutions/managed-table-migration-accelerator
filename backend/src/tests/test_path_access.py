"""Unit tests for the pure path-access CUJ logic (scan verify loop + path-based redirect). No Spark."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import external_writes as ew  # noqa: E402
from migration import path_access as pa  # noqa: E402

LOC = "s3://bkt/db/orders"
T0 = datetime(2026, 10, 1, 12, 0, 0)
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Doc link + redirect caveat
# --------------------------------------------------------------------------- #

def test_redirect_doc_url_points_at_the_path_based_redirect_section():
    assert pa.PATH_REDIRECT_DOC_URL == (
        "https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed"
        "#path-based-redirect"
    )


def test_redirect_floors_are_the_documented_runtimes():
    assert pa.PATH_REDIRECT_READ_DBR == (18, 1)
    assert pa.PATH_REDIRECT_WRITE_DBR == (18, 2)


def test_redirect_caveat_recommends_name_based_and_offers_redirect_as_the_alternative():
    text = pa.redirect_caveat()
    assert "name-based" in text
    assert "Can't update the code?" in text
    assert "18.1" in text and "18.2" in text
    assert "latency" in text
    assert "non-Databricks" in text
    assert pa.PATH_REDIRECT_DOC_URL in text
    assert "rather than rely on redirect" not in text


def test_redirect_caveat_makes_redirect_an_eligible_opt_in_not_a_default():
    text = pa.redirect_caveat()
    assert text.index("name-based") < text.index("redirect")
    assert "opt in" in text
    assert "eligible" in text
    assert "restart" in text


# --------------------------------------------------------------------------- #
# scan_verdict — the engine-owned Verify step
# --------------------------------------------------------------------------- #

def _verdict(**kw):
    base = dict(
        hit_sources=[], exported_sources=[], located_sources=[], entities=[],
        prior_status=None, prior_clean_at=None, last_seen_at=None, now=NOW,
    )
    base.update(kw)
    return pa.scan_verdict(**base)


def test_hits_mean_still_found_and_remember_where():
    v = _verdict(hit_sources=["/nb/b", "/nb/a", "/nb/a"], exported_sources=["/nb/a", "/nb/b"])
    assert v["status"] == pa.SCAN_STILL_FOUND
    assert v["clean_at"] is None
    assert v["located_sources"] == ["/nb/a", "/nb/b"]


def test_hits_keep_remembering_a_located_source_that_failed_to_re_export():
    # /nb/old held the literal last time but could not be exported now: it is unproven, so it
    # stays located. A located source that WAS re-exported without a hit is dropped (fixed).
    v = _verdict(
        hit_sources=["/nb/a"], exported_sources=["/nb/a", "/nb/fixed"],
        located_sources=["/nb/old", "/nb/fixed"],
    )
    assert v["status"] == pa.SCAN_STILL_FOUND
    assert v["located_sources"] == ["/nb/a", "/nb/old"]


def test_located_then_rescanned_with_no_hits_is_clean_and_stamps_the_watermark():
    v = _verdict(located_sources=["/nb/a"], exported_sources=["/nb/a", "/nb/z"])
    assert v["status"] == pa.SCAN_CLEAN
    assert v["clean_at"] == NOW
    assert v["located_sources"] == ["/nb/a"]


def test_zero_hits_on_a_table_never_located_is_not_coverable():
    # A dynamically built path (or code outside the scan) also yields zero hits, so the scan
    # can only vouch for a fix it saw the literal for first.
    v = _verdict(exported_sources=["/nb/a"])
    assert v["status"] == pa.SCAN_NOT_COVERABLE
    assert v["clean_at"] is None
    assert pa.NOTE_NEVER_LOCATED in v["notes"]


def test_a_located_source_that_failed_to_export_is_not_coverable_and_keeps_the_memory():
    v = _verdict(located_sources=["/nb/a", "/nb/b"], exported_sources=["/nb/a"])
    assert v["status"] == pa.SCAN_NOT_COVERABLE
    assert v["clean_at"] is None
    assert v["located_sources"] == ["/nb/a", "/nb/b"]
    assert any(n == pa.NOTE_NOT_RESCANNED_PREFIX + "/nb/b" for n in v["notes"])


def test_an_external_engine_consumer_is_never_coverable():
    ents = [{"entity_type": "TRINO", "entity_id": "t1", "role": "READ"},
            {"entity_type": "JOB", "entity_id": "9", "role": "READ"}]
    v = _verdict(located_sources=["/nb/a"], exported_sources=["/nb/a"], entities=ents)
    assert v["status"] == pa.SCAN_NOT_COVERABLE
    assert pa.NOTE_EXTERNAL_ENGINE_PREFIX + "TRINO:t1" in v["notes"]


def test_a_prior_clean_stays_clean_and_keeps_its_watermark():
    v = _verdict(prior_status=pa.SCAN_CLEAN, prior_clean_at=T0, last_seen_at=T0 - timedelta(days=1))
    assert v["status"] == pa.SCAN_CLEAN
    assert v["clean_at"] == T0


def test_path_access_seen_after_the_watermark_contradicts_the_scan():
    v = _verdict(prior_status=pa.SCAN_CLEAN, prior_clean_at=T0, last_seen_at=T0 + timedelta(hours=3),
                 located_sources=["/nb/a"], exported_sources=["/nb/a"])
    assert v["status"] == pa.SCAN_CONTRADICTED
    assert v["clean_at"] == T0
    assert any(n.startswith(pa.NOTE_SEEN_AFTER_CLEAN_PREFIX) for n in v["notes"])


def test_contradicted_never_re_clears_without_new_hits():
    v = _verdict(prior_status=pa.SCAN_CONTRADICTED, prior_clean_at=T0,
                 last_seen_at=T0 + timedelta(hours=3), located_sources=["/nb/a"],
                 exported_sources=["/nb/a"])
    assert v["status"] == pa.SCAN_CONTRADICTED


def test_new_hits_after_a_contradiction_reset_the_watermark():
    v = _verdict(hit_sources=["/nb/c"], exported_sources=["/nb/c"], prior_status=pa.SCAN_CONTRADICTED,
                 prior_clean_at=T0, last_seen_at=T0 + timedelta(hours=3))
    assert v["status"] == pa.SCAN_STILL_FOUND
    assert v["clean_at"] is None


def test_watermark_comparison_tolerates_mixed_naive_and_aware_timestamps():
    aware_after = (T0 + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    v = _verdict(prior_status=pa.SCAN_CLEAN, prior_clean_at=T0, last_seen_at=aware_after)
    assert v["status"] == pa.SCAN_CONTRADICTED


def test_located_sources_round_trip_through_json():
    assert pa.parse_located_sources(None) == []
    assert pa.parse_located_sources("not json") == []
    s = pa.serialize_located_sources(["/b", "/a", "/a"])
    assert json.loads(s) == ["/a", "/b"]
    assert pa.parse_located_sources(s) == ["/a", "/b"]
    assert pa.serialize_located_sources([]) is None


def test_parse_entities_is_safe_on_bad_input():
    assert pa.parse_entities(None) == []
    assert pa.parse_entities("{") == []
    assert pa.parse_entities('{"a": 1}') == []
    assert pa.parse_entities('[{"entity_type": "JOB"}, 3]') == [{"entity_type": "JOB"}]


# --------------------------------------------------------------------------- #
# clean_reconcile — what a CLEAN verdict writes to the manifest
# --------------------------------------------------------------------------- #

def test_clean_reconcile_clears_both_path_tokens_and_restores_eligible():
    blockers = f"{ew.BLOCKER_PATH_ACCESS}, {ew.BLOCKER_PATH_REDIRECT_HOLD}"
    out = pa.clean_reconcile("NEEDS_REVIEW", blockers, pa.REDIRECT_BELOW_FLOOR)
    assert out == {
        "has_path_based_access": False,
        "path_redirect_verdict": pa.REDIRECT_NONE,
        "path_redirect_eligible": None,
        "eligibility": "ELIGIBLE",
        "blockers": None,
    }


def test_clean_reconcile_keeps_other_blockers_and_clears_any_native_verdict():
    for verdict in (pa.REDIRECT_UNKNOWN, pa.REDIRECT_OK):
        out = pa.clean_reconcile("NEEDS_REVIEW", f"{ew.BLOCKER_PATH_ACCESS}, other_token", verdict)
        assert out["eligibility"] == "NEEDS_REVIEW"
        assert out["blockers"] == "other_token"
        assert out["path_redirect_verdict"] == pa.REDIRECT_NONE


def test_clean_reconcile_leaves_a_never_resolved_verdict_null():
    assert pa.clean_reconcile("NEEDS_REVIEW", ew.BLOCKER_PATH_ACCESS, None)["path_redirect_verdict"] is None


def test_clean_reconcile_never_upgrades_blocked():
    out = pa.clean_reconcile("BLOCKED", ew.BLOCKER_PATH_ACCESS, None)
    assert out["eligibility"] == "BLOCKED"


# --------------------------------------------------------------------------- #
# actionable_path_access — the profiler's watermark-aware flag
# --------------------------------------------------------------------------- #

def _ent(etype, eid="e", t=T0):
    return {"entity_type": etype, "entity_id": eid, "created_by": "u", "last_event_time": t}


def test_actionable_access_matches_the_legacy_rule_without_a_watermark():
    # Legacy: a direction counts when it has any non-Databricks (external or unknown) accessor.
    cases = [
        ({LOC: [_ent("JOB")]}, {}, False),                      # Databricks-only write
        ({LOC: [_ent("JOB"), _ent("TRINO")]}, {}, True),        # mixed
        ({}, {LOC + "/part=1": [_ent(None)]}, True),            # unknown reader
        ({"s3://other/t": [_ent("FLINK")]}, {}, False),         # different location
    ]
    for writes, reads, expected in cases:
        out = pa.actionable_path_access(LOC, writes, reads)
        assert out["has_path_based_access"] is expected, (writes, reads)


def test_actionable_access_ignores_events_at_or_before_the_watermark():
    writes = {LOC: [_ent("FLINK", t=T0 - timedelta(days=2))]}
    out = pa.actionable_path_access(LOC, writes, {}, clean_at=T0)
    assert out["has_path_based_access"] is False
    assert out["last_seen_at"] == T0 - timedelta(days=2)


def test_actionable_access_after_the_watermark_re_flags():
    reads = {LOC: [_ent(None, t=T0 + timedelta(minutes=5))]}
    out = pa.actionable_path_access(LOC, {}, reads, clean_at=T0)
    assert out["has_path_based_access"] is True
    assert out["last_seen_at"] == T0 + timedelta(minutes=5)


def test_actionable_access_without_an_event_time_counts_conservatively():
    reads = {LOC: [{"entity_type": None, "entity_id": "x"}]}
    out = pa.actionable_path_access(LOC, {}, reads, clean_at=T0)
    assert out["has_path_based_access"] is True
    assert out["last_seen_at"] is None


def test_last_seen_ignores_databricks_native_accessors():
    writes = {LOC: [_ent("JOB", t=T0 + timedelta(days=1)), _ent("TRINO", t=T0)]}
    out = pa.actionable_path_access(LOC, writes, {})
    assert out["last_seen_at"] == T0


# --------------------------------------------------------------------------- #
# Path-based redirect — Databricks-native path consumers
# --------------------------------------------------------------------------- #

def _row(etype="JOB", eid="1", role="READ", t=T0, dbrs=("17.3.x-scala2.13",), path=LOC, serverless=None):
    r = {"path": path, "role": role, "entity_type": etype, "entity_id": eid,
         "last_event_time": t, "dbr_versions": list(dbrs)}
    if serverless is not None:
        r["is_serverless"] = serverless
    return r


def test_native_consumers_keep_only_databricks_entities_under_the_location():
    rows = [_row(), _row(etype="TRINO", eid="t"), _row(eid="2", path="s3://other/x"), _row(etype=None, eid="n")]
    got = pa.native_path_consumers(LOC, rows)
    assert [(c["entity_type"], c["entity_id"]) for c in got] == [("JOB", "1")]


def test_native_consumers_dedupe_per_entity_and_role_keeping_the_latest_run():
    rows = [_row(t=T0, dbrs=["15.4.x-scala2.12"]), _row(t=T0 + timedelta(days=1), dbrs=["18.1.x-scala2.13"])]
    got = pa.native_path_consumers(LOC, rows)
    assert len(got) == 1
    assert got[0]["dbr_version"] == "18.1"


def test_native_consumers_pick_the_lowest_runtime_of_a_multi_cluster_run():
    got = pa.native_path_consumers(LOC, [_row(dbrs=["18.2.x-scala2.13", "16.4.x-scala2.12"])])
    assert got[0]["dbr_version"] == "16.4"


def test_native_consumers_drop_entities_last_seen_at_or_before_the_watermark():
    rows = [_row(eid="old", t=T0 - timedelta(hours=1)), _row(eid="new", t=T0 + timedelta(hours=1))]
    got = pa.native_path_consumers(LOC, rows, clean_at=T0)
    assert [c["entity_id"] for c in got] == ["new"]


def test_redirect_verdict_none_when_no_native_path_consumer():
    assert pa.redirect_verdict([])["verdict"] == pa.REDIRECT_NONE


def test_redirect_verdict_ok_when_readers_and_writers_meet_their_floors():
    consumers = pa.native_path_consumers(LOC, [
        _row(eid="r", role="READ", dbrs=["18.1.x-scala2.13"]),
        _row(eid="w", role="WRITE", dbrs=["18.2.x-scala2.13"]),
    ])
    v = pa.redirect_verdict(consumers)
    assert v["verdict"] == pa.REDIRECT_OK
    assert {c["status"] for c in v["consumers"]} == {pa.CONSUMER_OK}


def test_a_writer_on_18_1_is_below_the_write_floor():
    consumers = pa.native_path_consumers(LOC, [_row(eid="w", role="WRITE", dbrs=["18.1.x-scala2.13"])])
    v = pa.redirect_verdict(consumers)
    assert v["verdict"] == pa.REDIRECT_BELOW_FLOOR
    assert v["consumers"][0]["status"] == pa.CONSUMER_BELOW_FLOOR
    assert v["consumers"][0]["floor"] == "18.2"


def test_below_floor_outranks_unknown():
    consumers = pa.native_path_consumers(LOC, [
        _row(eid="a", dbrs=["15.4.x-scala2.12"]),
        _row(etype="NOTEBOOK", eid="b", dbrs=[]),
    ])
    assert pa.redirect_verdict(consumers)["verdict"] == pa.REDIRECT_BELOW_FLOOR


def test_unresolved_runtime_is_unknown_never_ok():
    consumers = pa.native_path_consumers(LOC, [_row(dbrs=["18.2.x"]), _row(eid="s", dbrs=["serverless"])])
    v = pa.redirect_verdict(consumers)
    assert v["verdict"] == pa.REDIRECT_UNKNOWN
    statuses = {c["entity_id"]: c["status"] for c in v["consumers"]}
    assert statuses == {"1": pa.CONSUMER_OK, "s": pa.CONSUMER_UNKNOWN}


def test_serialize_consumers_is_json_with_iso_times():
    consumers = pa.redirect_verdict(pa.native_path_consumers(LOC, [_row()]))["consumers"]
    s = pa.serialize_consumers(consumers)
    parsed = json.loads(s)
    assert parsed[0]["last_event_time"] == T0.isoformat()
    assert pa.serialize_consumers([]) is None


def test_fix_last_seen_counts_every_native_event_without_a_valid_opt_in():
    below = datetime(2026, 10, 3, 9, 0, 0)
    ok = datetime(2026, 10, 4)
    redirect = pa.redirect_verdict(pa.native_path_consumers(LOC, [
        _row(eid="old", t=below, dbrs=["15.4.x"]),   # below the read floor
        _row(eid="new", t=ok, dbrs=["18.1.x"]),      # at the floor: still needs a fix or an opt-in
    ]))
    assert pa.fix_last_seen_at(T0, redirect) == ok
    assert pa.fix_last_seen_at(None, redirect) == ok
    assert pa.fix_last_seen_at(T0, None) == T0
    assert pa.fix_last_seen_at(None, None) is None


def test_fix_last_seen_ignores_native_events_covered_by_a_valid_opt_in():
    redirect = pa.redirect_verdict(pa.native_path_consumers(LOC, [_row(t=datetime(2026, 10, 4), dbrs=["18.2.x"])]))
    assert pa.fix_last_seen_at(T0, redirect, optin_ok=True) == T0
    assert pa.fix_last_seen_at(None, redirect, optin_ok=True) is None


def test_has_native_path_consumer_reads_the_manifest_row():
    assert pa.has_native_path_consumer({"path_redirect_verdict": pa.REDIRECT_OK}) is True
    assert pa.has_native_path_consumer({"path_redirect_verdict": pa.REDIRECT_NONE}) is False
    assert pa.has_native_path_consumer({}) is False


def test_needs_path_fix_covers_the_external_flag_and_every_native_hold():
    assert pa.needs_path_fix({"has_path_based_access": True}) is True
    for verdict in (pa.REDIRECT_BELOW_FLOOR, pa.REDIRECT_UNKNOWN, pa.REDIRECT_OK):
        assert pa.needs_path_fix(_mrow(verdict)) is True, verdict
    assert pa.needs_path_fix(_mrow(pa.REDIRECT_NONE, consumers=[])) is False
    assert pa.needs_path_fix({}) is False


def test_needs_path_fix_is_false_once_a_valid_opt_in_covers_the_native_consumers():
    row = _opted_in_row()
    assert pa.native_hold(row) is False
    assert pa.needs_path_fix(row) is False


def test_scan_target_predicate_covers_every_native_verdict():
    assert pa.SCAN_TARGET_PREDICATE == (
        "has_path_based_access = true OR coalesce(path_redirect_verdict, 'NONE') <> 'NONE'"
    )


# --------------------------------------------------------------------------- #
# Serverless consumers (positive billing evidence only)
# --------------------------------------------------------------------------- #

def test_serverless_counts_as_eligible_by_named_product_decision():
    assert pa.SERVERLESS_ELIGIBLE is True


def test_a_confirmed_serverless_job_is_eligible_without_a_cluster_runtime():
    consumers = pa.native_path_consumers(LOC, [_row(dbrs=[], serverless=True, role="WRITE")])
    v = pa.redirect_verdict(consumers)
    assert v["consumers"][0]["status"] == pa.CONSUMER_SERVERLESS
    assert v["consumers"][0]["serverless"] is True
    assert v["verdict"] == pa.REDIRECT_OK


def test_no_cluster_row_without_serverless_evidence_stays_unknown():
    for flag in (None, False):
        v = pa.redirect_verdict(pa.native_path_consumers(LOC, [_row(dbrs=[], serverless=flag)]))
        assert v["consumers"][0]["status"] == pa.CONSUMER_UNKNOWN
        assert v["verdict"] == pa.REDIRECT_UNKNOWN


def test_a_resolved_cluster_runtime_wins_over_a_serverless_flag():
    v = pa.redirect_verdict(pa.native_path_consumers(LOC, [_row(dbrs=["15.4.x"], serverless=True)]))
    assert v["consumers"][0]["status"] == pa.CONSUMER_BELOW_FLOOR


def test_serverless_is_kept_from_the_latest_run_only():
    rows = [_row(t=T0, dbrs=[], serverless=True), _row(t=T0 + timedelta(days=1), dbrs=[], serverless=False)]
    v = pa.redirect_verdict(pa.native_path_consumers(LOC, rows))
    assert v["consumers"][0]["status"] == pa.CONSUMER_UNKNOWN


# --------------------------------------------------------------------------- #
# Eligibility, fingerprint, opt-in validity, and the native hold
# --------------------------------------------------------------------------- #

def _consumers(*rows):
    return pa.redirect_verdict(pa.native_path_consumers(LOC, list(rows)))


def _mrow(verdict, consumers=None, *, external=False, status=None, fingerprint=None):
    if consumers is None:
        consumers = _consumers(_row(dbrs=["18.2.x"]))["consumers"]
    return {
        "has_path_based_access": external,
        "path_redirect_verdict": verdict,
        "path_redirect_consumers": pa.serialize_consumers(consumers),
        "path_redirect_optin_status": status,
        "path_redirect_optin_fingerprint": fingerprint,
    }


def _eligible_row(**kw):
    rv = _consumers(_row(eid="r", dbrs=["18.1.x"]), _row(eid="w", role="WRITE", dbrs=[], serverless=True))
    assert rv["verdict"] == pa.REDIRECT_OK
    return _mrow(rv["verdict"], rv["consumers"], **kw)


def _opted_in_row():
    row = _eligible_row()
    row["path_redirect_optin_status"] = pa.OPTIN_ACTIVE
    row["path_redirect_optin_fingerprint"] = pa.consumer_fingerprint(pa.parse_entities(row["path_redirect_consumers"]))
    return row


def test_eligible_when_every_native_consumer_is_at_floor_or_serverless_and_no_external_engine():
    out = pa.redirect_eligibility(_eligible_row())
    assert out == {"eligible": True, "reasons": []}


def test_not_eligible_with_an_external_engine_even_if_native_consumers_are_fine():
    out = pa.redirect_eligibility(_eligible_row(external=True))
    assert out["eligible"] is False
    assert pa.REASON_EXTERNAL_ENGINE in out["reasons"]


def test_not_eligible_lists_each_below_floor_and_unknown_consumer():
    rv = _consumers(_row(eid="old", dbrs=["15.4.x"]), _row(etype="NOTEBOOK", eid="nb", dbrs=[]))
    out = pa.redirect_eligibility(_mrow(rv["verdict"], rv["consumers"]))
    assert out["eligible"] is False
    assert out["reasons"] == [
        "below_floor:JOB:old:READ:15.4<18.1",
        "unknown_runtime:NOTEBOOK:nb:READ",
    ]


def test_nothing_to_opt_in_when_there_is_no_native_path_consumer():
    out = pa.redirect_eligibility(_mrow(pa.REDIRECT_NONE, consumers=[]))
    assert out == {"eligible": False, "reasons": [pa.REASON_NO_NATIVE_CONSUMER]}
    assert pa.redirect_eligibility({})["eligible"] is False


def test_fingerprint_is_stable_across_order_and_runtime_changes():
    a = _consumers(_row(eid="1", dbrs=["18.1.x"]), _row(eid="2", role="WRITE", dbrs=["18.2.x"]))["consumers"]
    b = _consumers(_row(eid="2", role="WRITE", dbrs=["18.3.x"]), _row(eid="1", dbrs=["18.4.x"]))["consumers"]
    assert pa.consumer_fingerprint(a) == pa.consumer_fingerprint(b)
    assert pa.consumer_fingerprint([]) is None


def test_fingerprint_changes_when_a_consumer_or_role_is_added():
    base = _consumers(_row(eid="1", dbrs=["18.2.x"]))["consumers"]
    more = _consumers(_row(eid="1", dbrs=["18.2.x"]), _row(eid="9", dbrs=["18.2.x"]))["consumers"]
    write = _consumers(_row(eid="1", dbrs=["18.2.x"]), _row(eid="1", role="WRITE", dbrs=["18.2.x"]))["consumers"]
    fps = {pa.consumer_fingerprint(c) for c in (base, more, write)}
    assert len(fps) == 3


def test_opt_in_is_valid_only_when_active_eligible_and_the_fingerprint_matches():
    row = _opted_in_row()
    assert pa.optin_valid(row) is True
    assert pa.optin_valid({**row, "path_redirect_optin_status": pa.OPTIN_LAPSED}) is False
    assert pa.optin_valid({**row, "path_redirect_optin_status": pa.OPTIN_REVOKED}) is False
    assert pa.optin_valid({**row, "path_redirect_optin_fingerprint": "different"}) is False
    assert pa.optin_valid({**row, "has_path_based_access": True}) is False


def test_native_hold_applies_to_every_native_verdict_without_a_valid_opt_in():
    assert pa.native_hold(_mrow(pa.REDIRECT_NONE, consumers=[])) is False
    assert pa.native_hold({}) is False
    assert pa.native_hold(_eligible_row()) is True
    rv = _consumers(_row(dbrs=[]))
    assert pa.native_hold(_mrow(rv["verdict"], rv["consumers"])) is True
    assert pa.native_hold(_opted_in_row()) is False


def test_an_active_opt_in_on_a_now_ineligible_table_still_holds():
    row = _opted_in_row()
    rv = _consumers(_row(eid="r", dbrs=["15.4.x"]), _row(eid="w", role="WRITE", dbrs=[], serverless=True))
    row.update(path_redirect_verdict=rv["verdict"], path_redirect_consumers=pa.serialize_consumers(rv["consumers"]))
    assert pa.native_hold(row) is True


def test_optin_lapsed_only_for_an_active_opt_in_with_native_consumers_that_no_longer_match():
    row = _opted_in_row()
    assert pa.optin_lapsed(row) is False
    assert pa.optin_lapsed({**row, "path_redirect_optin_fingerprint": "stale"}) is True
    assert pa.optin_lapsed({**row, "path_redirect_optin_status": pa.OPTIN_REVOKED,
                            "path_redirect_optin_fingerprint": "stale"}) is False
    # Code moved to name-based (no native consumers left): the opt-in is moot, not lapsed.
    moot = {**row, "path_redirect_verdict": pa.REDIRECT_NONE, "path_redirect_consumers": None}
    assert pa.optin_lapsed(moot) is False


# --------------------------------------------------------------------------- #
# optin_transition — the pure accept / revoke rules the opt-in task applies
# --------------------------------------------------------------------------- #

def test_accept_records_who_when_why_and_the_fingerprint():
    row = _eligible_row()
    out = pa.optin_transition(pa.OPTIN_ACCEPT, row, reason="vendor code, cannot change",
                              operator="ops@x.com", now=NOW)
    assert out["ok"] is True and out["noop"] is False
    assert out["audit_action"] == pa.AUDIT_OPT_IN
    fp = pa.consumer_fingerprint(pa.parse_entities(row["path_redirect_consumers"]))
    assert out["updates"] == {
        "path_redirect_optin_status": pa.OPTIN_ACTIVE,
        "path_redirect_optin_at": NOW,
        "path_redirect_optin_by": "ops@x.com",
        "path_redirect_optin_reason": "vendor code, cannot change",
        "path_redirect_optin_fingerprint": fp,
    }
    assert pa.native_hold({**row, **out["updates"]}) is False


def test_accept_requires_a_reason():
    for reason in (None, "", "   "):
        out = pa.optin_transition(pa.OPTIN_ACCEPT, _eligible_row(), reason=reason, operator="o", now=NOW)
        assert out["ok"] is False
        assert out["refusal"] == pa.REFUSE_REASON_REQUIRED
        assert out["updates"] == {}


def test_accept_refuses_an_ineligible_table_with_the_engine_reasons():
    rv = _consumers(_row(eid="old", dbrs=["15.4.x"]))
    out = pa.optin_transition(pa.OPTIN_ACCEPT, _mrow(rv["verdict"], rv["consumers"]),
                              reason="r", operator="o", now=NOW)
    assert out["ok"] is False
    assert out["refusal"] == pa.REFUSE_NOT_ELIGIBLE
    assert out["reasons"] == ["below_floor:JOB:old:READ:15.4<18.1"]


def test_accept_is_idempotent_when_the_same_opt_in_is_already_valid():
    out = pa.optin_transition(pa.OPTIN_ACCEPT, _opted_in_row(), reason="r", operator="o", now=NOW)
    assert out["ok"] is True and out["noop"] is True and out["updates"] == {}


def test_accept_after_a_lapse_re_records_the_new_fingerprint():
    row = {**_opted_in_row(), "path_redirect_optin_status": pa.OPTIN_LAPSED,
           "path_redirect_optin_fingerprint": "old"}
    out = pa.optin_transition(pa.OPTIN_ACCEPT, row, reason="r", operator="o", now=NOW)
    assert out["ok"] is True and out["noop"] is False
    assert out["updates"]["path_redirect_optin_fingerprint"] != "old"


def test_revoke_marks_an_active_or_lapsed_opt_in_revoked():
    for status in (pa.OPTIN_ACTIVE, pa.OPTIN_LAPSED):
        row = {**_opted_in_row(), "path_redirect_optin_status": status}
        out = pa.optin_transition(pa.OPTIN_REVOKE, row, reason=None, operator="o", now=NOW)
        assert out["ok"] is True and out["audit_action"] == pa.AUDIT_REVOKE
        assert out["updates"] == {"path_redirect_optin_status": pa.OPTIN_REVOKED,
                                  "path_redirect_optin_at": NOW, "path_redirect_optin_by": "o"}
        assert pa.native_hold({**row, **out["updates"]}) is True


def test_revoke_with_nothing_to_revoke_is_refused():
    for status in (None, pa.OPTIN_REVOKED):
        out = pa.optin_transition(pa.OPTIN_REVOKE, {**_eligible_row(), "path_redirect_optin_status": status},
                                  reason=None, operator="o", now=NOW)
        assert out["ok"] is False and out["refusal"] == pa.REFUSE_NOTHING_TO_REVOKE


def test_an_unknown_action_is_refused():
    out = pa.optin_transition("enable", _eligible_row(), reason="r", operator="o", now=NOW)
    assert out["ok"] is False and out["refusal"] == pa.REFUSE_UNKNOWN_ACTION


def test_parse_optin_action_normalizes_and_blank_means_no_action():
    assert pa.parse_optin_action(" Accept ") == pa.OPTIN_ACCEPT
    assert pa.parse_optin_action("REVOKE") == pa.OPTIN_REVOKE
    assert pa.parse_optin_action("") is None
    assert pa.parse_optin_action(None) is None
    assert pa.parse_optin_action("none") is None
    try:
        pa.parse_optin_action("enable")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_parse_optin_tables_splits_on_commas_semicolons_and_whitespace():
    assert pa.parse_optin_tables(" a.b.c; a.b.d ,a.b.c\n") == ["a.b.c", "a.b.d"]
    assert pa.parse_optin_tables("") == []
    assert pa.parse_optin_tables(None) == []


# --------------------------------------------------------------------------- #
# The redirect hold-back token (external_writes owns eligibility tokens)
# --------------------------------------------------------------------------- #

def test_native_hold_token_holds_back_and_self_heals():
    assert ew.BLOCKER_PATH_REDIRECT_HOLD == "path_redirect_requires_name_based_or_opt_in"
    elig, blockers = ew.apply_path_redirect_to_eligibility("ELIGIBLE", None, True)
    assert elig == "NEEDS_REVIEW" and blockers == ew.BLOCKER_PATH_REDIRECT_HOLD
    elig, blockers = ew.apply_path_redirect_to_eligibility(elig, blockers, False)
    assert elig == "ELIGIBLE" and blockers is None


def test_native_hold_token_never_upgrades_blocked_and_keeps_other_tokens():
    elig, blockers = ew.apply_path_redirect_to_eligibility("BLOCKED", "x", True)
    assert elig == "BLOCKED" and blockers == f"x, {ew.BLOCKER_PATH_REDIRECT_HOLD}"
    elig, blockers = ew.apply_path_redirect_to_eligibility("NEEDS_REVIEW", f"x, {ew.BLOCKER_PATH_REDIRECT_HOLD}", False)
    assert elig == "NEEDS_REVIEW" and blockers == "x"


def test_dry_run_blocker_names_the_two_exits():
    assert pa.BLOCKER_REDIRECT_CHECK == "path_redirect_opt_in_required"


def test_opt_in_audit_actions_match_the_audit_vocabulary():
    from migration import audit as au
    assert pa.AUDIT_OPT_IN == au.ACTION_PATH_REDIRECT_OPT_IN
    assert pa.AUDIT_REVOKE == au.ACTION_PATH_REDIRECT_REVOKE
    assert pa.AUDIT_LAPSED == au.ACTION_PATH_REDIRECT_OPT_IN_LAPSED


def test_opt_in_columns_are_declared_on_the_manifest_with_null_defaults():
    from migration import manifest as mf
    names = {n for n, _ in mf.MANIFEST_COLUMNS}
    for col in pa.OPTIN_MANIFEST_COLUMNS:
        assert col in names, col
        assert col in mf._DEFERRED_DEFAULTS and mf._DEFERRED_DEFAULTS[col] is None, col
