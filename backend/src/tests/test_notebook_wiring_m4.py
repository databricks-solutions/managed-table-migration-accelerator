"""Text-level guards for the M4 wiring: 03d Authorize self-grants, 07 revokes only what it added
(notebooks are not importable without Spark)."""

import os

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def _code(src: str) -> str:
    """The notebook without its markdown/comment lines."""
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def test_03d_never_changes_ownership():
    code = _code(_read("03d_lease_ownership.py"))
    for forbidden in ("OWNER TO", "set_table_owner", "plan_lease", "owner_group=OWNER", "SET MANAGED"):
        assert forbidden not in code, forbidden


def test_03d_evaluates_as_its_own_identity_then_seeds_actions_from_the_verdicts():
    src = _read("03d_lease_ownership.py")
    i_eval = src.index("authority_rows = authz.refresh_authority(")
    i_seed = src.index("ax.generate_ownership_actions(authority_rows, wave_by_table=wave_by_table,")
    i_plan = src.index("plan = own.plan_authorize(authority_rows, action_by_fn=open_lease)")
    assert i_eval < i_seed < i_plan
    assert "source=authz.SOURCE_AUTHORIZE" in src[i_eval:i_seed]
    assert "expected_actor" not in src[i_eval:i_seed]  # the job identity IS the convert identity


def test_03d_requires_a_selector_and_keeps_batch_first():
    src = _read("03d_lease_ownership.py")
    i_batch, i_wave, i_schema = (src.index("if BATCH_ID:"), src.index("elif PLAN_RUN_ID and WAVE_ID:"),
                                 src.index("elif TARGET_SCHEMA:"))
    assert i_batch < i_wave < i_schema
    assert "no implicit authorize-all" in src
    assert "authz.to_convert_names(members)" in src


def test_03d_grants_only_under_confirm_execute_and_audits_first():
    src = _read("03d_lease_ownership.py")
    i_started = src.index("audit.ACTION_GRANT_PRIVILEGE, phase=audit.PHASE_STARTED")
    i_grant = src.index('spark.sql(g["statement"])')
    assert i_started < i_grant
    assert "if not CONFIRM_EXECUTE or not res[\"outcome\"][\"run\"]:\n        return res" in src
    assert "if CONFIRM_EXECUTE and ops_by_table:" in src[:i_started]


def test_03d_records_prior_state_and_never_downgrades_a_real_grant_on_dry_run():
    src = _read("03d_lease_ownership.py")
    assert "own.held_directly(" in src and "own.read_direct_grants(" in src
    assert "own.grant_outcome(res[\"held\"], own.grant_identity(g) in ours)" in src
    assert "if not CONFIRM_EXECUTE and key in granted_keys:\n        continue" in src
    assert "prior_held=out[\"prior_held\"]" in src


def test_03d_verifies_by_re_evaluating_before_closing_actions_and_fails_loud_last():
    src = _read("03d_lease_ownership.py")
    i_verify = src.index("after = authz.refresh_authority(")
    i_ledger = src.index("own.merge_ownership_ledger_rows(")
    i_done = src.index("status=ax.STATUS_DONE, resolution=\"authorized (verified)\"")
    i_fail = src.index("if failed_tables:\n    raise RuntimeError(")
    assert i_verify < i_ledger < i_done < i_fail
    assert "authz.unverified_grant_text(len(inprogress_keys))" in src
    assert "status=ax.STATUS_IN_PROGRESS" in src


def test_03d_marks_metastore_admin_self_grants_in_the_audit():
    assert '"self-grant as metastore admin"' in _read("03d_lease_ownership.py")


def test_03d_table_owner_group_is_a_deprecated_no_op():
    src = _read("03d_lease_ownership.py")
    assert 'dbutils.widgets.text("table_owner_group", "", "Deprecated (ignored)' in src
    code = _code(src)
    assert code.count("LEGACY_OWNER_GROUP") == 3  # read, test, print — never used to operate


def test_07_revokes_added_grants_audit_first_then_verifies_then_marks():
    src = _read("07_post_convert_cleanup.py")
    i_sec = src.index("# MAGIC ## Convert permission revoke")
    i_legacy = src.index("# MAGIC ## Legacy ownership restore")
    cell = src[i_sec:i_legacy]
    assert "cl.revoke_eligible(" in cell
    assert "own.revoke_plan(_granted, _terminal)" in cell
    i_started = cell.index("audit.ACTION_REVOKE_PRIVILEGE, phase=audit.PHASE_STARTED")
    i_revoke = cell.index('spark.sql(g["statement"])')
    i_check = cell.index("gone = not own.held_directly(")
    i_mark = cell.index("own.mark_revoked(")
    i_close = cell.index("own.restore_actions_to_close(_granted, revoked_keys)")
    assert i_started < i_revoke < i_check < i_mark < i_close
    assert "if _rplan[\"revokes\"] and not CONFIRM_EXECUTE:" in cell
    for forbidden in ("OWNER TO", "VACUUM", "UNSET MANAGED"):
        assert forbidden not in _code(cell)


def test_07_marks_installer_manage_ready_and_opens_the_hand_back_in_every_mode():
    src = _read("07_post_convert_cleanup.py")
    i_cell = src.index("# MAGIC ## Installer convert rights")
    cell = src[i_cell:src.index("# MAGIC ## Legacy ownership restore")]
    i_ready = cell.index("pv.handback_ready_keys(_installer_rows, _schema_states)")
    i_mark = cell.index("status=pv.LEDGER_STATUS_HANDBACK_READY")
    i_open = cell.index("pv.handback_action_rows(_installer_rows, _ready")
    assert i_ready < i_mark < i_open
    code = _code(cell)
    assert "CONFIRM_EXECUTE" not in code
    for forbidden in ("REVOKE ", "OWNER TO", "VACUUM", "UNSET MANAGED"):
        assert forbidden not in code.replace("[HAND BACK]", "")


def test_07_keeps_the_legacy_lease_drain_and_fails_loud_after_both():
    src = _read("07_post_convert_cleanup.py")
    i_legacy = src.index("# MAGIC ## Legacy ownership restore")
    assert "own.read_leased_ledger_rows(" in src[i_legacy:]
    i_fail = src.index("_problems = []")
    assert i_legacy < i_fail
    assert "revoke(s) failed or did NOT verify" in src[i_fail:]


def test_06_rollback_never_revokes():
    code = _code(_read("06_rollback.py"))
    assert "REVOKE" not in code and "revoke_plan" not in code and "mark_revoked" not in code
