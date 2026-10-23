"""Pure bulk-journey oracle for the M6.4 live proof. No Spark, no network."""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import bulk_journey_oracle as bjo  # noqa: E402
from migration import parallelism as pl  # noqa: E402

APP_SRC = Path(__file__).resolve().parents[3] / "app" / "client" / "src"
PLAN = "p-1"
NOW = 1_790_000_000_000
DAY = 24 * 3600 * 1000
KW = dict(plan_run_id=PLAN, batch_id="batch_01", member_schemas={"c.a"},
          deferrable_keys={"RESHARE_D2D|c.a.t|SHARE"})


def _src(rel: str) -> str:
    path = APP_SRC / rel
    if not path.exists():
        pytest.skip(f"{rel} is not in this checkout (a tooling-only checkout has no app/)")
    return path.read_text()


def joined(batch_id, wave_id, order, **kw):
    base = {"plan_run_id": PLAN, "batch_id": batch_id, "batch_order": int(batch_id[-2:]),
            "parallel_degree": 2, "blocked_from_parallel": False, "conflict_reason": None,
            "wave_id": wave_id, "wave_order": order, "bucket": "normal",
            "group_key": f"cat.s_{wave_id}", "table_count": 3, "size_bytes": 100,
            "est_copy_min": 1.0, "max_downtime_min": 2.0, "recommended_warehouse": "MEDIUM",
            "wave_state": "READY", "fully_migrated": False, "open_pre_convert": 0,
            "open_post_convert": 0, "open_blocking": 0, "open_lease": 0, "total_tables": 3,
            "dry_run_ok": 3, "converting": 0, "converted": 0, "validated": 0, "cleaned": 0,
            "converted_external": 0, "open_post_convert_holding": 0, "open_reshare": 0,
            "open_compat": 0, "optimize_pending": False, "wave_writer_verdict": None,
            "wave_next_scheduled_write_at": None, "convert_ready": True}
    return {**base, **kw}


def rollups(rows, frozen=None):
    return pl.batch_readiness(rows, rows, frozen or {})


TWO = [joined("batch_01", "wave_01", 1), joined("batch_01", "wave_02", 2),
       joined("batch_02", "wave_03", 3), joined("batch_02", "wave_04", 4)]
FINISHED = dict(wave_state="DONE", fully_migrated=True, validated=3, dry_run_ok=0, convert_ready=False)
CONVERTED = dict(wave_state="IN_PROGRESS", converted=3, dry_run_ok=0, convert_ready=False)


# ---- constants pinned to the app and the rollup -----------------------------------------------
def test_bulk_steps_follow_the_finish_order():
    assert bjo.BULK_STEPS[5:9] == pl.BULK_FINISH_ORDER
    assert bjo.BULK_STEPS[0] == pl.STEP_BULK_DRY_RUN
    assert [bjo.BULK_RANK[s] for s in bjo.BULK_STEPS] == list(range(len(bjo.BULK_STEPS)))


def test_card_labels_match_batchcard():
    found = dict(re.findall(r"\[BulkStep\.(\w+)\]: '([^']+)'", _src("pages/bulk/BatchCard.tsx")))
    assert {getattr(pl, f"STEP_BULK_{k}"): v for k, v in found.items()} == bjo.CARD_LABEL


def test_precheck_labels_match_convert_prechecks():
    src = _src("lib/convertPrechecks.pure.ts")
    assert tuple(re.findall(r"\{ id: '\w+', label: '([^']+)'", src)) == bjo.PRECHECK_LABELS


def test_precheck_copy_matches_the_bulk_prechecks_source():
    src = _src("pages/bulk/bulkPrechecks.pure.ts")
    for literal in ("'No wave to check.'", "`All ${n} wave${n === 1 ? '' : 's'} pass.`",
                    "`Blocked: ${", "`Unknown: ${", "join(' · ')", " — ${r.detail}`"):
        assert literal in src, literal


def test_lease_step_note_matches_the_app_text():
    note = pl.lease_step_note({"open_pre_convert_freeze": 0, "open_pre_convert_lease": 1,
                               "open_pre_convert_lease_unverified": 7, "rollup": "AWAITING_ACTIONS"})
    assert note and note.startswith("7 ")
    assert f"`${{n}}{note[1:]}`" in _src("lib/leaseNote.pure.ts")
    assert "return unverifiedLeaseText(n);" in _src("pages/bulk/bulkBatches.pure.ts")


def test_rolled_back_step_note_matches_the_app_text():
    pieces = ("${ids.join(', ')} ", "${one ? 'was' : 'were'}", " rolled back; re-run ",
              "${one ? 'its dry run' : 'their dry runs'}",
              " (job_03_dry_run, one per schema) before ", "this batch can convert again.")
    body = _src("pages/bulk/bulkBatches.pure.ts").split("export function rolledBackStepNote", 1)[1]
    for p in pieces:
        assert p in body, p
    for ids, verb, run in ((["wave_08"], "was", "its dry run"),
                           (["wave_08", "wave_09"], "were", "their dry runs")):
        want = (f"{', '.join(ids)} {verb}{pieces[2]}{run}{pieces[4]}{pieces[5]}")
        assert pl.rolled_back_step_note({"rolled_back_wave_ids": ids}) == want
    assert "DRY_RUN: 'bulk_dry_run'" in _src("pages/bulk/bulkBatches.pure.ts")


def test_n1_matches_the_batchcard_bulk_convert_params():
    src = _src("pages/bulk/BatchCard.tsx")
    block = src.split('alias="bulk_convert"', 1)[1].split("params={{", 1)[1].split("}}", 1)[0]
    assert set(re.findall(r"(\w+):", block)) == bjo.N1_CONVERT_PARAMS


# ---- statement-API typing ---------------------------------------------------------------------------
def test_coerce_types_statement_strings():
    row = {"blocked_from_parallel": "false", "batch_order": "2", "est_copy_min": "1.5",
           "fully_migrated": "true", "wave_next_scheduled_write_at": "2026-10-01T12:00:00.000Z",
           "wave_state": "READY", "group_key": None}
    got = bjo.coerce_joined_rows([row])[0]
    assert got["blocked_from_parallel"] is False and got["fully_migrated"] is True
    assert got["batch_order"] == 2 and got["est_copy_min"] == 1.5
    assert got["wave_next_scheduled_write_at"] == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert got["wave_state"] == "READY" and got["group_key"] is None


def test_coerced_string_rows_roll_up_like_typed_rows():
    as_strings = [{k: (None if v is None else str(v).lower() if isinstance(v, bool) else str(v))
                   for k, v in r.items()} for r in TWO]
    assert pl.batch_readiness(as_strings, as_strings, {}) == []
    assert rollups(bjo.coerce_joined_rows(as_strings)) == rollups(TWO)


# ---- survey ---------------------------------------------------------------------------------------
def test_pick_batch_skips_a_rolled_back_member():
    pick, cands, why = bjo.pick_batch(rollups(TWO), [{"wave_id": "wave_03", "rolled_back": 3}])
    assert (pick, cands) == ("batch_01", ["batch_01"]) and "rolled-back member(s) wave_03" in why


def test_pick_batch_ambiguous_proposes_the_recommended_batch():
    pick, cands, why = bjo.pick_batch(rollups(TWO), [])
    assert pick == "batch_01" and cands == ["batch_01", "batch_02"] and why.startswith("AMBIGUOUS")


def test_pick_batch_none_when_every_batch_is_started_or_done():
    rows = [joined("batch_01", "wave_01", 1, **FINISHED), joined("batch_01", "wave_02", 2, **FINISHED),
            joined("batch_02", "wave_03", 3, **CONVERTED), joined("batch_02", "wave_04", 4)]
    pick, cands, why = bjo.pick_batch(rollups(rows), [])
    assert pick is None and cands == [] and "batch_01: done" in why and "batch_02: started" in why


def test_prep_targets_names_the_campaign_scope_and_shared_schemas():
    members = [joined("batch_01", "wave_01", 1),
               joined("batch_01", "wave_02", 2, wave_state="PLANNED", dry_run_ok=0)]
    scopes = {"wave_01": {"cat.s_wave_01"}, "wave_02": {"cat.s_wave_02"}, "wave_09": {"cat.s_wave_02"}}
    got = bjo.prep_targets(members, scopes)
    assert got[0] == ("wave_01", "cat.s_wave_01", False, "READY")
    assert got[1][:3] == ("wave_02", "cat.s_wave_02", True) and "also plans wave_09" in got[1][3]


def test_prep_targets_awaiting_member_with_a_dry_run_is_not_a_campaign_target():
    members = [joined("batch_01", "wave_01", 1, wave_state="AWAITING_ACTIONS", dry_run_ok=3, open_pre_convert=1),
               joined("batch_01", "wave_02", 2, wave_state="AWAITING_ACTIONS", dry_run_ok=0)]
    got = bjo.prep_targets(members, {})
    assert got[0][2] is False and "owes freeze/lease" in got[0][3]
    assert got[1][2] is True  # no dry run yet: still needs the campaign


def test_prep_outcome():
    assert bjo.prep_outcome({"wave_state": "READY"}, "SUCCESS")[0] == bjo.PASS
    assert bjo.prep_outcome({"wave_state": "BLOCKED"}, "FAILED")[0] == bjo.WARN
    assert bjo.prep_outcome({"wave_state": "PLANNED"}, "FAILED")[0] == bjo.FAIL
    assert bjo.prep_outcome({"wave_state": "PLANNED"}, "SUCCESS")[0] == bjo.WARN
    assert bjo.prep_outcome({"wave_state": "PLANNED"}, "SUCCESS_WITH_FAILURES")[0] == bjo.WARN


def test_pick_batch_excludes_streaming():
    rows = [joined("batch_01", "wave_01", 1), joined("batch_02", "wave_02", 2, bucket="streaming")]
    pick, cands, why = bjo.pick_batch(rollups(rows), [])
    assert pick == "batch_01" and cands == ["batch_01"] and "streaming" in why


# ---- the three surfaces agree ---------------------------------------------------------------------
def test_card_label_outcome():
    assert bjo.card_label_outcome("Bulk convert", pl.STEP_BULK_CONVERT)[0] == bjo.PASS
    assert bjo.card_label_outcome("Bulk validate", pl.STEP_BULK_CONVERT)[0] == bjo.FAIL
    # Both surfaces read wave_status.open_lease (the stored convert_authority verdicts): a lease
    # disagreement is a real one, not a run_as mismatch to excuse.
    assert bjo.card_label_outcome(bjo.CARD_LABEL[pl.STEP_BULK_LEASE], pl.STEP_BULK_CONVERT)[0] == bjo.FAIL
    assert bjo.card_label_outcome("", pl.STEP_BULK_CONVERT)[0] == bjo.WARN


def test_cockpit_outcome_agrees_and_disagrees():
    rs = rollups(TWO)
    ok = pl.cockpit_summary(PLAN, rs, batch_id="batch_01")
    assert [v for v, _ in bjo.cockpit_outcome(ok, rs, "batch_01", plan_run_id=PLAN)] == \
        [bjo.PASS, bjo.PASS, bjo.PASS]
    assert bjo.cockpit_outcome({**ok, "next_step": pl.STEP_BULK_VALIDATE}, rs, "batch_01",
                               plan_run_id=PLAN)[0][0] == bjo.FAIL
    assert bjo.cockpit_outcome({**ok, "next_step": pl.STEP_BULK_LEASE}, rs, "batch_01",
                               plan_run_id=PLAN)[0][0] == bjo.WARN
    assert bjo.cockpit_outcome(None, rs, "batch_01", plan_run_id=PLAN)[0][0] == bjo.WARN
    assert bjo.cockpit_outcome(ok, rs, "batch_01", plan_run_id="other")[0][0] == bjo.FAIL


def test_cockpit_outcome_compares_the_reader_warning_count():
    rs = rollups(TWO)
    ok = pl.cockpit_summary(PLAN, rs, batch_id="batch_01")
    bumped = {**ok, "batches": [{**b, "readers_attention": 3} if b["batch_id"] == "batch_01" else b
                                for b in ok["batches"]]}
    assert bjo.cockpit_outcome(bumped, rs, "batch_01", plan_run_id=PLAN)[1][0] == bjo.FAIL
    old = {**ok, "batches": [{k: v for k, v in b.items() if k != "readers_attention"}
                             for b in ok["batches"]]}
    assert bjo.cockpit_outcome(old, rs, "batch_01", plan_run_id=PLAN)[1][0] == bjo.WARN


def test_recommended_batch_outcome():
    rs = rollups(TWO)
    assert bjo.recommended_batch_outcome(rs, "batch_01")[0] == bjo.PASS
    assert bjo.recommended_batch_outcome(rs, "batch_02")[0] == bjo.WARN
    assert bjo.recommended_batch_outcome(rs, "batch_09")[0] == bjo.FAIL
    started = rollups(TWO[:2] + [joined("batch_02", "wave_03", 3, **CONVERTED),
                                 joined("batch_02", "wave_04", 4, **CONVERTED)])
    assert bjo.recommended_batch_outcome(started, "batch_02")[0] == bjo.PASS
    assert bjo.recommended_batch_outcome(started, "batch_01")[0] == bjo.WARN
    both = rollups([joined("batch_01", "wave_01", 1, **CONVERTED), joined("batch_01", "wave_02", 2, **CONVERTED),
                    joined("batch_02", "wave_03", 3, **CONVERTED), joined("batch_02", "wave_04", 4, **CONVERTED)])
    # batch_01 holds the recommendation; batch_02 is also started, which is correct, not a D6 breach
    verdict, msg = bjo.recommended_batch_outcome(both, "batch_02")
    assert verdict == bjo.WARN and "batch_01" in msg
    done = rollups([joined("batch_01", "wave_01", 1, **FINISHED),
                    joined("batch_01", "wave_02", 2, **FINISHED)] + TWO[2:])
    assert bjo.recommended_batch_outcome(done, "batch_01") == (
        bjo.PASS, "batch_01 is done; the recommendation moved on to batch_02")
    rolled = rollups(TWO[:2] + [joined("batch_02", "wave_03", 3, **FINISHED),
                                joined("batch_02", "wave_04", 4, wave_state="ROLLED_BACK", rolled_back=3,
                                       dry_run_ok=0, convert_ready=False)])
    assert bjo.recommended_batch_outcome(rolled, "batch_02") == (
        bjo.WARN, "batch_02 holds a rolled-back member; D6 does not hold it (design §8 Q4)")
    assert bjo.recommended_batch_outcome(rolled, "batch_01")[0] == bjo.PASS


# ---- step advancement -----------------------------------------------------------------------------
def test_batch_step_outcome():
    after = rollups([joined("batch_01", "wave_01", 1, **CONVERTED),
                     joined("batch_01", "wave_02", 2, **CONVERTED)])[0]
    fresh = {"as_of_ms": 2_000}
    got = bjo.batch_step_outcome(step=pl.STEP_BULK_CONVERT, after=after, next_row=fresh, run_start_ms=1_000)
    assert [v for v, _ in got] == [bjo.PASS, bjo.PASS] and "bulk_convert -> bulk_validate" in got[1][1]
    stale = bjo.batch_step_outcome(step=pl.STEP_BULK_CONVERT, after=after, next_row={"as_of_ms": 500},
                                   run_start_ms=1_000)
    assert len(stale) == 1 and stale[0][0] == bjo.FAIL
    owed = bjo.batch_step_outcome(step=pl.STEP_BULK_VALIDATE, after=after, next_row=fresh, run_start_ms=1_000)
    assert owed[1][0] == bjo.WARN
    back = bjo.batch_step_outcome(step=pl.STEP_BULK_OPTIMIZE, after=after, next_row=fresh, run_start_ms=1_000)
    assert back[1][0] == bjo.FAIL
    unchecked = bjo.batch_step_outcome(step=pl.STEP_BULK_CONVERT, after=after, next_row=fresh, run_start_ms=None)
    assert unchecked[0][0] == bjo.WARN
    unreadable = bjo.batch_step_outcome(step=pl.STEP_BULK_CONVERT, after=after, next_row=None, run_start_ms=1_000)
    assert unreadable[0][0] == bjo.WARN and "not readable" in unreadable[0][1]
    assert len(unreadable) == 2 and unreadable[1][0] == bjo.PASS


def test_batch_step_outcome_a_resume_is_a_warn():
    partial = rollups([joined("batch_01", "wave_01", 1, wave_state="IN_PROGRESS", converted=2,
                              dry_run_ok=1, convert_ready=False),
                       joined("batch_01", "wave_02", 2, **CONVERTED)])[0]
    got = bjo.batch_step_outcome(step=pl.STEP_BULK_VALIDATE, after=partial, next_row={"as_of_ms": 2},
                                 run_start_ms=1)
    assert got[1][0] == bjo.WARN and "resume" in got[1][1]


def test_dry_run_outcome():
    r = rollups(TWO)[0]
    assert bjo.dry_run_outcome(pl.STEP_BULK_CONVERT, r)[0] == bjo.PASS
    assert bjo.dry_run_outcome(pl.STEP_BULK_FREEZE, r)[0] == bjo.FAIL
    assert bjo.dry_run_outcome(pl.STEP_BULK_FREEZE, None)[0] == bjo.FAIL


def test_convert_outcome():
    def m(state, eligibility="ELIGIBLE", n="t"):
        return {"full_name": f"c.s.{n}", "state": state, "eligibility": eligibility}
    got = bjo.convert_outcome({
        "wave_01": [m("CONVERTED", n="a"), m("CONVERTED", n="b")],
        "wave_02": [m("CONVERTED", n="c"), m("DRY_RUN_OK", n="d")],
        "wave_03": [m("CONVERTED", n="e"), m("DRY_RUN_OK", "NEEDS_REVIEW", n="f")],
        "wave_04": [m("DRY_RUN_OK", n="g")],
        "wave_05": [m("CONVERTED", n="h"), m("CONVERTING", n="i")],
    })
    assert [v for v, _ in got] == [bjo.PASS, bjo.WARN, bjo.WARN, bjo.FAIL, bjo.FAIL]
    held_only = bjo.convert_outcome({
        "wave_06": [{"full_name": "c.s.x", "state": "DRY_RUN_OK", "eligibility": "NEEDS_REVIEW"}],
    })
    assert held_only[0][0] == bjo.WARN and "held back" in held_only[0][1]


# ---- the pre-convert checks panel -----------------------------------------------------------------
def test_precheck_targets_cap_and_resume():
    ms = [joined("batch_01", f"w{i}", i, dry_run_ok=1 if i % 2 else 0, group_key=f"c.s{i}")
          for i in range(1, 7)]
    assert bjo.precheck_targets(ms, False, 4) == (["c.s1", "c.s2", "c.s3", "c.s4"], ["c.s5", "c.s6"])
    assert bjo.precheck_targets(ms, True, 4) == (["c.s1", "c.s3", "c.s5"], [])


def test_parse_precheck_copy():
    assert bjo.parse_precheck_copy("All 2 waves pass.")["n"] == 2
    assert bjo.parse_precheck_copy("All 1 wave pass.")["status"] == "pass"
    p = bjo.parse_precheck_copy("Blocked: c.a · Unknown: c.b, c.c — c.a: no dry run yet")
    assert (p["status"], p["blocked"], p["unknown"], p["detail"]) == (
        "blocked", ["c.a"], ["c.b", "c.c"], "c.a: no dry run yet")
    assert bjo.parse_precheck_copy("No wave to check.")["status"] == "none"
    assert bjo.parse_precheck_copy("something else")["status"] == "unparsed"


def test_prechecks_outcome_all_pass():
    rows = [(label, "All 2 waves pass.") for label in bjo.PRECHECK_LABELS]
    outs, rec = bjo.prechecks_outcome(rows, ["c.a", "c.b"])
    assert [v for v, _ in outs] == [bjo.PASS]
    assert rec == {m: dict.fromkeys(bjo.PRECHECK_LABELS, "pass") for m in ("c.a", "c.b")}


def test_prechecks_outcome_blocked_and_unknown_are_recorded_per_member():
    rows = [("Dry run", "All 2 waves pass."), ("Permission", "Blocked: c.b — c.b: run_as owns 0 of 2"),
            ("Freeze", "Unknown: c.a — c.a: no preflight"), ("Managed location", "All 2 waves pass.")]
    outs, rec = bjo.prechecks_outcome(rows, ["c.a", "c.b"], unchecked=["c.e"])
    assert [v for v, _ in outs] == [bjo.WARN, bjo.WARN, bjo.WARN]
    assert rec["c.b"]["Permission"] == "blocked" and rec["c.a"]["Freeze"] == "unknown"
    assert rec["c.a"]["Permission"] == "pass"


def test_prechecks_outcome_empty_expected_accepts_no_wave_copy():
    rows = [(label, "No wave to check.") for label in bjo.PRECHECK_LABELS]
    outs, _ = bjo.prechecks_outcome(rows, [])
    assert all(v != bjo.FAIL for v, _ in outs)


def test_prechecks_outcome_fails_on_drift():
    assert bjo.prechecks_outcome([("Dry run", "All 2 waves pass.")], ["c.a", "c.b"])[0][0][0] == bjo.FAIL
    one = [(label, "All 1 wave pass.") for label in bjo.PRECHECK_LABELS]
    assert bjo.prechecks_outcome(one, ["c.a", "c.b"])[0][0][0] == bjo.FAIL
    stray = [(label, "Blocked: c.z") for label in bjo.PRECHECK_LABELS]
    assert bjo.prechecks_outcome(stray, ["c.a"])[0][0][0] == bjo.FAIL


# ---- the trigger guard ----------------------------------------------------------------------------
def test_trigger_guard_bulk_convert_is_exactly_n1():
    ok = {"plan_run_id": PLAN, "batch_id": "batch_01", "confirm_compaction_paused": True}
    assert bjo.trigger_guard("bulk_convert", ok, **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("bulk_convert", {**ok, "include_streaming": True}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("bulk_convert", {**ok, "confirm_compaction_paused": False}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("bulk_convert", {**ok, "batch_id": "batch_02"}, **KW)[0] == bjo.FAIL


def test_trigger_guard_scope_and_gate_relaxing_args():
    base = {"plan_run_id": PLAN, "batch_id": "batch_01", "confirm_execute": True}
    assert bjo.trigger_guard("bulk_freeze", base, **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("bulk_freeze", {**base, "require_compaction_frozen": False}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("enable_optimization", {**base, "enable_po_on_convert": True}, **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("reshare", {**base, "recent_vacuum_minutes": 0}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("convert", base, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("bulk_validate", {"plan_run_id": "other", "batch_id": "batch_01"}, **KW)[0] == bjo.FAIL


def test_trigger_guard_defer_is_narrowed_to_fixture_keys():
    d = {"plan_run_id": PLAN, "batch_id": "batch_01", "finish_action": "defer", "defer_reason": "r",
         "confirm_execute": True}
    assert bjo.trigger_guard("reshare", {**d, "action_keys": "RESHARE_D2D|c.a.t|SHARE"}, **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("reshare", {**d, "action_keys": ""}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("reshare", {**d, "action_keys": "RESHARE_D2D|c.a.real|SHARE"}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("reshare", {**d, "finish_action": "Defer", "action_keys": ""}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("reshare", {**d, "defer_reason": " "}, **KW)[0] == bjo.FAIL
    reopen = {**d, "finish_action": "reopen", "defer_reason": ""}
    assert bjo.trigger_guard("reshare", {**reopen, "action_keys": "RESHARE_D2D|c.a.t|SHARE"}, **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("reshare", {**reopen, "action_keys": ""}, **KW)[0] == bjo.FAIL
    base = {"plan_run_id": PLAN, "batch_id": "batch_01", "confirm_execute": True}
    defer_freeze = {**base, "finish_action": "defer", "defer_reason": "r",
                    "action_keys": "RESHARE_D2D|c.a.t|SHARE"}
    assert bjo.trigger_guard("bulk_freeze", defer_freeze, **KW)[0] == bjo.FAIL


BASE = {"plan_run_id": PLAN, "batch_id": "batch_01"}
APP_SETS = {  # exactly what the /bulk card and the shared finish CTAs send for a batch
    "bulk_freeze": {**BASE, "confirm_execute": True},
    "lease_ownership": {**BASE, "confirm_execute": True},
    "bulk_validate": dict(BASE),
    "bulk_unfreeze": {**BASE, "confirm_execute": True},
    "reshare": {**BASE, "confirm_execute": True},
    "enable_compat": {**BASE, "confirm_execute": True, "compat_location_map": "{}"},
    "enable_optimization": {**BASE, "confirm_execute": True, "enable_po_on_convert": True,
                            "auto_liquid_policy": "enable_eligible"},
}


@pytest.mark.parametrize("alias", sorted(APP_SETS))
def test_trigger_guard_passes_the_exact_app_param_sets(alias):
    assert bjo.trigger_guard(alias, APP_SETS[alias], **KW)[0] == bjo.PASS


@pytest.mark.parametrize("alias", sorted(APP_SETS))
@pytest.mark.parametrize("extra", [{"wave_ids": "wave_01"}, {"full_name": "c.a.t"},
                                   {"target_schema": "c.a"}, {"wave_id": "wave_01"}])
def test_trigger_guard_fails_an_extra_scope_selector(alias, extra):
    verdict, msg = bjo.trigger_guard(alias, {**APP_SETS[alias], **extra}, **KW)
    assert verdict == bjo.FAIL and sorted(extra)[0] in msg


def test_trigger_guard_enable_optimization_is_pinned_to_po_on():
    ok = APP_SETS["enable_optimization"]
    assert bjo.trigger_guard("enable_optimization", {**ok, "enable_po_on_convert": False}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("enable_optimization", {**ok, "enable_po_on_convert": "false"}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("enable_optimization", {**ok, "enable_po_on_convert": "true"}, **KW)[0] == bjo.PASS
    no_po = {k: v for k, v in ok.items() if k != "enable_po_on_convert"}
    assert bjo.trigger_guard("enable_optimization", no_po, **KW)[0] == bjo.FAIL  # job default is PO off
    assert bjo.trigger_guard("enable_optimization", {**ok, "auto_liquid_policy": "disable"}, **KW)[0] == bjo.FAIL


def test_trigger_guard_rollback_allows_only_the_app_keys():
    rb = {"reason": "done", "wave_id": "wave_01", "plan_run_id": PLAN, "full_name": ""}
    assert bjo.trigger_guard("rollback", rb, **KW, rollback_wave="wave_01")[0] == bjo.PASS
    assert bjo.trigger_guard("rollback", {**rb, "batch_id": "batch_01"}, **KW, rollback_wave="wave_01")[0] == bjo.FAIL


def _jsx_dialogs(text):
    """alias -> keys of every JobTriggerDialog params={{ ... }} in a TSX source."""
    out = {}
    for block in text.split("<JobTriggerDialog")[1:]:
        alias = re.search(r'alias="(\w+)"', block)
        params = re.search(r"params=\{\{(.*?)\}\}", block, re.S)
        if alias and params:
            body = re.sub(r"\.\.\.scope\.params", "plan_run_id: 1, batch_id: 1", params.group(1))
            out.setdefault(alias.group(1), set()).update(re.findall(r"(\w+)\s*:", body))
    return out


def _fn_keys(text, name):
    body = re.search(rf"export function {name}\(.*?return \{{(.*?)\}};", text, re.S).group(1)
    return set(re.findall(r"(\w+)\s*:", body)) | ({"plan_run_id", "batch_id"} if "...scope.params" in body else set())


def test_param_allowlists_mirror_the_app_sources():
    """Drift in what the app sends must fail here, not in a live proof."""
    card = _jsx_dialogs(_src("pages/bulk/BatchCard.tsx"))
    cta = _jsx_dialogs(_src("components/FinishStepCta.tsx"))
    fin = _src("lib/finishScope.pure.ts")
    derived = {
        "bulk_freeze": card["bulk_freeze"],
        "lease_ownership": card["lease_ownership"],
        "bulk_validate": card["bulk_validate"],
        "bulk_unfreeze": card["bulk_unfreeze"],
        "reshare": cta["reshare"] | _fn_keys(fin, "deferParams") | _fn_keys(fin, "reopenParams"),
        "enable_compat": _fn_keys(fin, "compatParams") | _fn_keys(fin, "deferParams")
                         | _fn_keys(fin, "reopenParams"),
        "enable_optimization": _fn_keys(fin, "optimizeParams"),
        "rollback": _jsx_dialogs(_src("pages/convert/postConvertActions.tsx"))["rollback"],
    }
    for alias, keys in derived.items():
        assert set(bjo.PARAM_ALLOWLIST[alias]) == keys, alias
    assert set(bjo.CAMPAIGN_PARAMS) <= _jsx_dialogs(_src("pages/convert/ConvertPage.tsx"))["campaign"]
    # The app's enable / review modes send OPTIMIZE_AUTO_LIQUID; only the explicit skip mode sends
    # `recommend` (liquid-clustering step M4), and the journey walk never presses skip, so the
    # trigger guard stays strict on enable_eligible.
    assert f"auto_liquid_policy: mode === 'skip' ? 'recommend' : '{bjo.OPTIMIZE_AUTO_LIQUID}'" in fin
    assert "enable_po_on_convert: mode !== 'skip'" in fin


def test_trigger_guard_campaign_and_rollback():
    assert bjo.trigger_guard("campaign", {"target_schema": "c.a", "confirm_compaction_paused": True},
                             **KW)[0] == bjo.PASS
    assert bjo.trigger_guard("campaign", {"target_schema": "c.other"}, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("campaign", {"target_schema": "c.a", "wave_ids": "w1"}, **KW)[0] == bjo.FAIL
    rb = {"reason": "done", "wave_id": "wave_01", "plan_run_id": PLAN}
    assert bjo.trigger_guard("rollback", rb, **KW)[0] == bjo.FAIL
    assert bjo.trigger_guard("rollback", rb, **KW, rollback_wave="wave_01")[0] == bjo.PASS
    assert bjo.trigger_guard("rollback", {**rb, "wave_id": "wave_02"}, **KW, rollback_wave="wave_01")[0] == bjo.FAIL
    assert bjo.trigger_guard("rollback", {**rb, "full_name": "c.a.t"}, **KW, rollback_wave="wave_01")[0] == bjo.FAIL
    assert bjo.trigger_guard("rollback", {**rb, "reason": " "}, **KW, rollback_wave="wave_01")[0] == bjo.FAIL


# ---- fixture, rollback, end state -----------------------------------------------------------------
def test_fixture_wave_is_the_first_converted_member():
    by_wave = {"wave_01": [{"state": "DRY_RUN_OK"}], "wave_02": [{"state": "VALIDATED"}]}
    assert bjo.fixture_wave(by_wave, ["wave_01", "wave_02"]) == "wave_02"
    assert bjo.fixture_wave({"wave_01": [{"state": "ROLLED_BACK"}]}, ["wave_01"]) is None


def test_rollback_targets():
    got = bjo.rollback_targets(["wave_01", "wave_02"], {"wave_01": NOW - DAY, "wave_02": None}, NOW)
    assert got[0][:2] == ("wave_01", True) and got[0][2][0] == bjo.PASS
    assert got[1][:2] == ("wave_02", False) and got[1][2][0] == bjo.WARN


def test_external_outcome():
    names = ["c.s.a", "c.s.b"]
    assert bjo.external_outcome({"c.s.a": "EXTERNAL", "c.s.b": "EXTERNAL"}, names)[0] == bjo.PASS
    assert bjo.external_outcome({"c.s.a": "EXTERNAL", "c.s.b": "MANAGED"}, names)[0] == bjo.FAIL
    assert bjo.external_outcome({"c.s.a": "EXTERNAL"}, names)[0] == bjo.WARN
    assert bjo.external_outcome(None, names)[0] == bjo.WARN


def test_cockpit_params_are_show_only():
    assert bjo.cockpit_params(PLAN, "batch_01") == {"plan_run_id": PLAN, "batch_id": "batch_01", "action": "show"}
