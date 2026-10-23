from datetime import datetime

from migration import job_schedule as js


def _now(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi)  # tz-naive == UTC by our convention


# --- next_fire ---------------------------------------------------------------

def test_next_fire_daily_2am_utc():
    # Quartz 6-field: sec min hour dom month dow  -> 02:00 every day
    nf = js.next_fire("0 0 2 * * ?", "UTC", _now(2026, 1, 1, 3, 0))
    assert nf == datetime(2026, 1, 2, 2, 0)          # today's 02:00 already passed


def test_next_fire_hourly_top_of_hour():
    nf = js.next_fire("0 0 * * * ?", "UTC", _now(2026, 1, 1, 3, 15))
    assert nf == datetime(2026, 1, 1, 4, 0)


def test_next_fire_named_dow_wednesday():
    nf = js.next_fire("0 0 2 ? * WED", "UTC", _now(2026, 1, 1, 0, 0))
    assert nf is not None
    assert nf.weekday() == 2


def test_next_fire_named_dow_range_skips_weekends():
    nf = js.next_fire("0 0 9 ? * MON-FRI", "UTC", _now(2026, 1, 2, 10, 0))
    assert nf == datetime(2026, 1, 5, 9, 0)
    assert nf.weekday() < 5


def test_next_fire_restricted_year_returns_none_but_star_year_fires():
    now = _now(2026, 1, 1, 3, 0)
    six_field = js.next_fire("0 0 2 * * ?", "UTC", now)

    assert js.next_fire("0 0 2 * * ? 2027", "UTC", _now(2026, 1, 1, 0, 0)) is None
    assert js.next_fire("0 0 2 * * ? *", "UTC", now) == six_field
    assert six_field is not None


def test_next_fire_dom_and_dow_both_restricted_use_or_semantics():
    nf = js.next_fire("0 0 2 15 * MON", "UTC", _now(2026, 1, 1, 0, 0))

    assert nf == datetime(2026, 1, 5, 2, 0)
    assert nf.day == 15 or nf.weekday() == 0


def test_next_fire_respects_timezone_dst_boundary():
    # 02:30 America/New_York on 2026-03-08 does not exist (spring-forward 02:00->03:00);
    # the next valid 02:30 fire is the following day. We only assert it lands in UTC and
    # is strictly after 'now' — the point is it does not raise and does not fire in the gap.
    nf = js.next_fire("0 30 2 * * ?", "America/New_York", _now(2026, 3, 8, 0, 0))
    assert nf is not None and nf > datetime(2026, 3, 8, 0, 0)


def test_next_fire_specials_return_none_not_misfire():
    assert js.next_fire("0 0 2 L * ?", "UTC", _now(2026, 1, 1, 0, 0)) is None     # L
    assert js.next_fire("0 0 2 ? * 6#3", "UTC", _now(2026, 1, 1, 0, 0)) is None   # #
    assert js.next_fire("not a cron", "UTC", _now(2026, 1, 1, 0, 0)) is None


# --- next_run_from_schedule --------------------------------------------------

def test_next_run_from_schedule_scheduled_cron():
    sched = {"trigger_type": "SCHEDULED", "quartz_cron": "0 0 2 * * ?", "timezone_id": "UTC",
             "pause_status": "UNPAUSED"}
    assert js.next_run_from_schedule(sched, _now(2026, 1, 1, 3, 0)) == datetime(2026, 1, 2, 2, 0)


def test_next_run_from_schedule_none_for_continuous_manual_paused_or_missing():
    assert js.next_run_from_schedule(None, _now(2026, 1, 1, 3, 0)) is None
    assert js.next_run_from_schedule(
        {"trigger_type": "CONTINUOUS", "quartz_cron": None, "timezone_id": None, "pause_status": None},
        _now(2026, 1, 1, 3, 0)) is None
    assert js.next_run_from_schedule(
        {"trigger_type": "MANUAL", "quartz_cron": None, "timezone_id": None, "pause_status": None},
        _now(2026, 1, 1, 3, 0)) is None
    assert js.next_run_from_schedule(
        {"trigger_type": "SCHEDULED", "quartz_cron": "0 0 2 * * ?", "timezone_id": "UTC",
         "pause_status": "PAUSED"},
        _now(2026, 1, 1, 3, 0)) is None


# --- writer_verdict ----------------------------------------------------------

def _v(**kw):
    base = dict(trigger_type=None, pause_status=None, quartz_cron=None, timezone_id="UTC",
                last_run_at=None, now=_now(2026, 1, 1, 3, 0), est_downtime_min=20.0,
                safety_margin_min=15.0)
    base.update(kw)
    return js.writer_verdict(**base)


def test_verdict_paused_is_safe_now():
    assert _v(pause_status="PAUSED", trigger_type="SCHEDULED")["verdict"] == js.VERDICT_SAFE_NOW


def test_verdict_manual_is_safe_now():
    assert _v(trigger_type="MANUAL")["verdict"] == js.VERDICT_SAFE_NOW


def test_verdict_event_triggers_are_must_stop():
    for t in ("CONTINUOUS", "FILE_ARRIVAL", "TABLE_UPDATE", "MODEL_UPDATE"):
        assert _v(trigger_type=t)["verdict"] == js.VERDICT_MUST_STOP


def test_verdict_scheduled_far_fire_is_safe_now():
    r = _v(trigger_type="SCHEDULED", quartz_cron="0 0 2 * * ?", now=_now(2026, 1, 1, 3, 0))
    assert r["verdict"] == js.VERDICT_SAFE_NOW and r["next_scheduled_write_at"] is not None
    assert "next fire" in r["reason"]


def test_verdict_scheduled_imminent_fire_is_warn_window():
    # Fires at 03:05; now 03:00; downtime+margin = 35m > 5m slack -> WARN_WINDOW.
    r = _v(trigger_type="SCHEDULED", quartz_cron="0 5 3 * * ?", now=_now(2026, 1, 1, 3, 0))
    assert r["verdict"] == js.VERDICT_WARN_WINDOW


def test_verdict_scheduled_but_unreadable_cron_is_unknown():
    assert _v(trigger_type="SCHEDULED", quartz_cron=None)["verdict"] == js.VERDICT_UNKNOWN
    assert _v(trigger_type="SCHEDULED", quartz_cron="0 0 2 L * ?")["verdict"] == js.VERDICT_UNKNOWN


def test_verdict_unresolvable_trigger_is_unknown():
    assert _v(trigger_type=None, quartz_cron=None)["verdict"] == js.VERDICT_UNKNOWN


# --- combine_writer_verdicts -------------------------------------------------

def test_combine_takes_most_restrictive():
    verdicts = [
        {"verdict": js.VERDICT_SAFE_NOW, "next_scheduled_write_at": None, "reason": "a"},
        {"verdict": js.VERDICT_WARN_WINDOW, "next_scheduled_write_at": None, "reason": "b"},
        {"verdict": js.VERDICT_MUST_STOP, "next_scheduled_write_at": None, "reason": "c"},
    ]
    assert js.combine_writer_verdicts(verdicts)["verdict"] == js.VERDICT_MUST_STOP
    assert js.combine_writer_verdicts(verdicts)["reason"] == "c"


def test_combine_empty_is_unknown_never_safe():
    assert js.combine_writer_verdicts([])["verdict"] == js.VERDICT_UNKNOWN


# --- combine_writer_verdicts_with_availability -------------------------------
# A writer whose schedule can't be READ (deleted job / stale lineage edge) must not mask a live
# sibling's verdict. See 2026-09-19-writer-schedule-stale-lineage-masking.md.

def _vd(verdict, nf=None, reason=""):
    return {"verdict": verdict, "next_scheduled_write_at": nf, "reason": reason}


def test_availability_live_safe_now_not_masked_by_unresolvable():
    # The bug: a live far-out SAFE_NOW alongside N unresolvable (deleted) SCHEDULED_JOB edges
    # previously combined to UNKNOWN. It must now stay SAFE_NOW (the unresolvable ones are advisory).
    nf = datetime(2026, 9, 20, 0, 27)
    combined = js.combine_writer_verdicts_with_availability(
        [_vd(js.VERDICT_SAFE_NOW, nf, "next fire far out")], unresolved_count=5
    )
    assert combined["verdict"] == js.VERDICT_SAFE_NOW
    assert combined["next_scheduled_write_at"] == nf


def test_availability_live_warn_window_survives_unresolvable():
    # Regression guard: WARN_WINDOW (rank 1) already outranked UNKNOWN and must keep governing.
    combined = js.combine_writer_verdicts_with_availability(
        [_vd(js.VERDICT_WARN_WINDOW, None, "imminent")], unresolved_count=3
    )
    assert combined["verdict"] == js.VERDICT_WARN_WINDOW


def test_availability_all_unresolvable_is_unknown_never_safe():
    # SR7 preserved: if EVERY attributed writer was unresolvable, fall back to UNKNOWN (not SAFE_NOW).
    combined = js.combine_writer_verdicts_with_availability([], unresolved_count=4)
    assert combined["verdict"] == js.VERDICT_UNKNOWN
    assert "unresolvable" in combined["reason"]


def test_availability_no_writers_is_unknown():
    combined = js.combine_writer_verdicts_with_availability([], unresolved_count=0)
    assert combined["verdict"] == js.VERDICT_UNKNOWN
    assert combined["reason"] == "no attributed schedulable writer job"


def test_availability_live_unparseable_cron_still_masks():
    # A LIVE-but-unreadable cron (Quartz L/W/# special) is an HONEST unknown, not an artifact — it
    # is a resolved verdict and MUST still mask a live SAFE_NOW (only UNRESOLVABLE edges are demoted).
    live_unknown = js.writer_verdict(
        trigger_type="SCHEDULED", pause_status=None, quartz_cron="0 0 0 L * ?",
        timezone_id="UTC", last_run_at=None, now=datetime(2026, 9, 19, 12, 0),
        est_downtime_min=30, safety_margin_min=30,
    )
    assert live_unknown["verdict"] == js.VERDICT_UNKNOWN  # sanity: L-special is unparseable
    combined = js.combine_writer_verdicts_with_availability(
        [_vd(js.VERDICT_SAFE_NOW, datetime(2026, 9, 20, 0, 27)), live_unknown], unresolved_count=0
    )
    assert combined["verdict"] == js.VERDICT_UNKNOWN


def test_build_manifest_schedule_update_shape():
    combined = {"verdict": js.VERDICT_WARN_WINDOW,
                "next_scheduled_write_at": datetime(2026, 1, 1, 2, 0), "reason": "why"}
    upd = js.build_manifest_schedule_update("c.s.t", combined, notes=None)
    assert upd["full_name"] == "c.s.t"
    assert upd["writer_schedule_verdict"] == js.VERDICT_WARN_WINDOW
    assert upd["next_scheduled_write_at"] == datetime(2026, 1, 1, 2, 0)
    assert upd["writer_schedule_reason"] == "why" and upd["writer_schedule_notes"] is None
    assert set(upd) == {"full_name", *js.SCHEDULE_COLUMN_NAMES}


# --- wave_schedule_safety / schedule_safety_sort_key -------------------------

def test_wave_schedule_safety_worst_verdict_and_earliest_fire():
    rows = [
        {"writer_schedule_verdict": "SAFE_NOW", "next_scheduled_write_at": datetime(2026, 9, 19, 6, 0)},
        {"writer_schedule_verdict": "WARN_WINDOW", "next_scheduled_write_at": datetime(2026, 9, 18, 22, 0)},
        {"writer_schedule_verdict": "SAFE_NOW", "next_scheduled_write_at": None},
    ]
    out = js.wave_schedule_safety(rows)
    assert out["wave_verdict"] == "WARN_WINDOW"                       # worst (min-rank) wins
    assert out["wave_next_scheduled_write_at"] == datetime(2026, 9, 18, 22, 0)  # earliest fire


def test_wave_schedule_safety_empty_is_unknown():
    assert js.wave_schedule_safety([]) == {
        "wave_verdict": "UNKNOWN", "wave_next_scheduled_write_at": None}
    assert js.wave_schedule_safety(
        [{"writer_schedule_verdict": None, "next_scheduled_write_at": None}]
    )["wave_verdict"] == "UNKNOWN"


def test_schedule_safety_sort_key_orders_safest_first():
    # SAFE_NOW before UNKNOWN before WARN_WINDOW before MUST_STOP.
    keys = [
        js.schedule_safety_sort_key("MUST_STOP", None),
        js.schedule_safety_sort_key("WARN_WINDOW", None),
        js.schedule_safety_sort_key("UNKNOWN", None),
        js.schedule_safety_sort_key("SAFE_NOW", None),
    ]
    assert keys == sorted(keys, reverse=True)  # listed worst..safest; ascending sort reverses it
    # Within one verdict, a LATER fire (or None) is safer -> sorts first.
    later = js.schedule_safety_sort_key("WARN_WINDOW", datetime(2026, 9, 20, 0, 0))
    sooner = js.schedule_safety_sort_key("WARN_WINDOW", datetime(2026, 9, 18, 22, 0))
    none_fire = js.schedule_safety_sort_key("WARN_WINDOW", None)
    assert none_fire < later < sooner


# --- recheck_verdict_freshness -----------------------------------------------

def test_recheck_escalates_stale_safe_now_to_warn():
    # slack 25m: downtime (20m) < 25 <= downtime+margin (35m) -> WARN_WINDOW band exactly.
    now = datetime(2026, 9, 18, 21, 0)
    out = js.recheck_verdict_freshness(
        stored_verdict="SAFE_NOW", next_scheduled_write_at=datetime(2026, 9, 18, 21, 25),
        now=now, est_downtime_min=20, safety_margin_min=15)
    assert out["verdict"] == "WARN_WINDOW"
    assert out["escalated"] is True


def test_recheck_escalates_to_must_stop_when_fire_inside_downtime():
    now = datetime(2026, 9, 18, 22, 0)
    out = js.recheck_verdict_freshness(
        stored_verdict="SAFE_NOW", next_scheduled_write_at=datetime(2026, 9, 18, 22, 5),
        now=now, est_downtime_min=20, safety_margin_min=15)
    assert out["verdict"] == "MUST_STOP"
    assert out["escalated"] is True


def test_recheck_never_upgrades_or_touches_non_safe():
    now = datetime(2026, 9, 18, 21, 0)
    # A stored WARN_WINDOW is never upgraded to SAFE_NOW even if the fire is far out now.
    out = js.recheck_verdict_freshness(
        stored_verdict="WARN_WINDOW", next_scheduled_write_at=datetime(2026, 9, 25, 0, 0),
        now=now, est_downtime_min=20, safety_margin_min=15)
    assert out["verdict"] == "WARN_WINDOW"
    assert out["escalated"] is False


def test_recheck_safe_now_stays_safe_when_fire_far_out():
    now = datetime(2026, 9, 18, 21, 0)
    out = js.recheck_verdict_freshness(
        stored_verdict="SAFE_NOW", next_scheduled_write_at=datetime(2026, 9, 20, 0, 0),
        now=now, est_downtime_min=20, safety_margin_min=15)
    assert out["verdict"] == "SAFE_NOW"
    assert out["escalated"] is False


def test_recheck_null_fire_is_noop():
    now = datetime(2026, 9, 18, 21, 0)
    out = js.recheck_verdict_freshness(
        stored_verdict="SAFE_NOW", next_scheduled_write_at=None,
        now=now, est_downtime_min=20, safety_margin_min=15)
    assert out["verdict"] == "SAFE_NOW"
    assert out["escalated"] is False


# --- format_pause_warning ----------------------------------------------------

def test_format_pause_warning_execute_lists_named_jobs():
    rows = [
        {"entity_id": "111", "writer_class": "SCHEDULED_JOB", "full_name": "c.s.t1",
         "next_scheduled_write_at": datetime(2026, 9, 18, 22, 0)},
        {"entity_id": "222", "writer_class": "EVENT_JOB", "full_name": "c.s.t2",
         "next_scheduled_write_at": None},
        {"entity_id": None, "writer_class": "NOTEBOOK", "full_name": "c.s.t3"},
    ]
    lines = js.format_pause_warning(rows, dry_run=False)
    text = "\n".join(lines)
    assert "WILL BE PAUSED" in text
    assert "111" in text and "222" in text            # both schedulable jobs named
    assert "SCHEDULED_JOB" in text and "EVENT_JOB" in text
    assert "2026-09-18 22:00" in text                 # next fire shown
    assert "schedule unknown" in text                 # event job with no fire
    assert "cannot be paused" in text.lower()         # ad-hoc advisory
    assert "05b" in text or "unfreeze" in text.lower()  # won't auto-resume caveat


def test_format_pause_warning_dry_run_header():
    lines = js.format_pause_warning(
        [{"entity_id": "111", "writer_class": "SCHEDULED_JOB", "full_name": "c.s.t1"}], dry_run=True)
    assert any("WOULD BE PAUSED" in ln for ln in lines)


def test_format_pause_warning_empty_no_schedulable_is_quiet():
    lines = js.format_pause_warning([{"entity_id": None, "writer_class": "USER",
                                      "full_name": "c.s.t"}], dry_run=False)
    # No schedulable job -> no "PAUSED" banner, but the ad-hoc advisory still appears.
    assert not any("PAUSED" in ln for ln in lines)
    assert any("cannot be paused" in ln.lower() for ln in lines)
