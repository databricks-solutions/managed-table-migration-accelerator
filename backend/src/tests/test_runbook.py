"""Unit tests for the migration runbook pure builder + view SQL. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import executor as ex  # noqa: E402
from migration import runbook as rb  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _action(kind, *, phase, blocking=False, sql=None, api=None, full_name="c.s.t",
            catalog="c", schema="s", status=ac.STATUS_OPEN):
    return {
        "action_kind": kind, "scope": ac.SCOPE_TABLE, "phase": phase, "blocking": blocking,
        "catalog": catalog, "schema": schema, "full_name": full_name,
        "title": f"{kind} on {full_name}", "remediation_sql": sql, "remediation_api": api,
        "status": status, "plan_run_id": "p1", "wave_id": "w1",
    }


def _ledger(op_kind, *, status=ex.LEDGER_FROZEN, catalog="c", schema="s", full_name="c.s.t",
            target_id="job-9", scope=ac.SCOPE_TABLE, prior_pause_status=ex.PAUSE_UNPAUSED,
            prior_po_enabled=None):
    return {
        "op_kind": op_kind, "ledger_key": ex.ledger_key(op_kind, full_name, target_id),
        "status": status, "scope": scope, "catalog": catalog, "schema": schema,
        "full_name": full_name, "target_kind": ac.TARGET_JOB, "target_id": target_id,
        "prior_pause_status": prior_pause_status, "prior_po_enabled": prior_po_enabled,
        "plan_run_id": "p1", "wave_id": "w1",
    }


# --------------------------------------------------------------------------- #
# phase ordering
# --------------------------------------------------------------------------- #

def test_phase_order_pre_convert_post_then_unknown():
    assert rb.phase_order(rb.PHASE_PRE_CONVERT) == 0
    assert rb.phase_order(rb.PHASE_CONVERT) == 1
    assert rb.phase_order(rb.PHASE_POST_CONVERT) == 2
    assert rb.phase_order("WHATEVER") == 9
    assert rb.phase_order(None) == 9


# --------------------------------------------------------------------------- #
# entries_from_actions
# --------------------------------------------------------------------------- #

def test_actions_entry_carries_statement_and_skips_empty():
    rows = [
        _action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT, sql="ALTER SHARE s ADD TABLE c.s.t;"),
        _action(ac.KIND_STOP_STREAM, phase=ac.PHASE_PRE_CONVERT, api="stop job j1"),
        _action("MYSTERY", phase=ac.PHASE_PRE_CONVERT),  # no remediation -> skipped
    ]
    entries = rb.entries_from_actions(rows)
    assert len(entries) == 2
    stmts = {e["statement"] for e in entries}
    assert "ALTER SHARE s ADD TABLE c.s.t;" in stmts
    assert "stop job j1" in stmts


def test_actions_entry_prefers_sql_over_api():
    e = rb.entries_from_actions([_action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT,
                                         sql="SQL;", api="API")])[0]
    assert e["statement"] == "SQL;"


# --------------------------------------------------------------------------- #
# entries_from_ledger
# --------------------------------------------------------------------------- #

def test_ledger_emits_freeze_and_restore():
    entries = rb.entries_from_ledger([_ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_UNPAUSED)])
    phases = {e["phase"] for e in entries}
    assert rb.PHASE_PRE_CONVERT in phases and rb.PHASE_POST_CONVERT in phases
    freeze = next(e for e in entries if e["phase"] == rb.PHASE_PRE_CONVERT)
    assert "job-9" in freeze["statement"]


def test_ledger_restore_suppressed_when_prior_state_says_no_act():
    # Job was already PAUSED before freeze -> no resume restore entry.
    entries = rb.entries_from_ledger([_ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_PAUSED)])
    assert all(e["phase"] != rb.PHASE_POST_CONVERT for e in entries)


def test_ledger_disable_po_prior_off_no_reenable():
    lr = _ledger(ex.OP_DISABLE_PO, scope=ac.SCOPE_SCHEMA, full_name=None, target_id=None,
                 prior_po_enabled=False)
    entries = rb.entries_from_ledger([lr])
    assert all(e["phase"] != rb.PHASE_POST_CONVERT for e in entries)
    freeze = next(e for e in entries if e["phase"] == rb.PHASE_PRE_CONVERT)
    assert "DISABLE PREDICTIVE OPTIMIZATION" in freeze["statement"]


def test_ledger_ignores_unfrozen_and_failed():
    assert rb.entries_from_ledger([_ledger(ex.OP_PAUSE_JOB, status=ex.LEDGER_UNFROZEN)]) == []


# --------------------------------------------------------------------------- #
# entries_from_blockers
# --------------------------------------------------------------------------- #

def test_blocker_managed_location_has_precise_ddl():
    rows = [{"catalog": "c", "schema": "s", "full_name": "c.s.t",
             "blockers": "no_managed_location"}]
    e = rb.entries_from_blockers(rows)[0]
    assert e["phase"] == rb.PHASE_PRE_CONVERT and e["blocking"] is True
    assert "SET MANAGED LOCATION" in e["statement"]
    assert "c" in e["statement"]


def test_blocker_unknown_code_still_surfaces_nonempty():
    e = rb.entries_from_blockers([{"catalog": "c", "schema": "s", "full_name": "c.s.t",
                                   "blockers": "some_new_code"}])[0]
    assert e["statement"] and "some_new_code" in e["statement"]


def test_blocker_multiple_codes_split():
    rows = [{"catalog": "c", "schema": "s", "full_name": "c.s.t",
             "blockers": "no_managed_location, runtime_below_17_2_uniform"}]
    assert len(rb.entries_from_blockers(rows)) == 2


# --------------------------------------------------------------------------- #
# build_runbook — full union + ordering + contract
# --------------------------------------------------------------------------- #

def test_build_runbook_orders_pre_then_post_and_blocking_first():
    actions = [
        _action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT, sql="ALTER SHARE ...;"),
        _action(ac.KIND_FIX_PATH_ACCESS, phase=ac.PHASE_PRE_CONVERT, blocking=True, api="fix path"),
    ]
    ledger = [_ledger(ex.OP_PAUSE_JOB)]
    blockers = [{"catalog": "c", "schema": "s", "full_name": "c.s.t", "blockers": "no_managed_location"}]
    book = rb.build_runbook(actions, ledger, blockers)
    ranks = [e["phase_rank"] for e in book]
    assert ranks == sorted(ranks)  # phase-ordered
    # first entry is a PRE_CONVERT blocking item
    assert book[0]["phase"] == rb.PHASE_PRE_CONVERT and book[0]["blocking"] is True


def test_build_runbook_every_entry_has_nonempty_statement():
    actions = [_action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT, sql="ALTER SHARE ...;")]
    ledger = [_ledger(ex.OP_DISABLE_PO, scope=ac.SCOPE_SCHEMA, full_name=None, target_id=None,
                      prior_po_enabled=True)]
    blockers = [{"catalog": "c", "schema": "s", "full_name": "c.s.t", "blockers": "no_managed_location"}]
    for e in rb.build_runbook(actions, ledger, blockers):
        assert (e.get("statement") or "").strip(), f"empty statement in {e}"


def test_build_runbook_empty_inputs():
    assert rb.build_runbook([], [], []) == []


# --------------------------------------------------------------------------- #
# exports
# --------------------------------------------------------------------------- #

def test_to_sql_script_keeps_sql_and_comments_api():
    book = rb.build_runbook(
        [_action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT, sql="ALTER SHARE x ADD TABLE c.s.t;"),
         _action(ac.KIND_STOP_STREAM, phase=ac.PHASE_PRE_CONVERT, api="databricks jobs cancel-all-runs --job-id 5")],
        [], [])
    script = rb.to_sql_script(book)
    assert "ALTER SHARE x ADD TABLE c.s.t;" in script
    # API line is commented, never a bare non-SQL line
    assert "--   (manual/API) databricks jobs cancel-all-runs --job-id 5" in script


def test_to_markdown_has_phase_headers_and_handles_empty():
    assert "No open manual statements" in rb.to_markdown([])
    md = rb.to_markdown(rb.build_runbook(
        [_action(ac.KIND_FIX_PATH_ACCESS, phase=ac.PHASE_PRE_CONVERT, blocking=True, api="fix")], [], []))
    assert "## PRE_CONVERT" in md and "BLOCKING" in md


# --------------------------------------------------------------------------- #
# view SQL
# --------------------------------------------------------------------------- #

def test_runbook_view_fqn():
    assert rb.runbook_view_fqn("main", "migration") == "main.migration.migration_runbook"


def test_open_apis_decision_reads_as_not_needed_in_builder_and_view():
    from migration import compat as cm
    base = {"action_kind": ac.KIND_ENABLE_COMPAT, "scope": ac.SCOPE_TABLE,
            "phase": ac.PHASE_POST_CONVERT, "blocking": False, "catalog": "c", "schema": "s",
            "full_name": "c.s.t", "title": "Decide how external readers connect to c.s.t",
            "remediation_sql": "ALTER TABLE c.s.t SET TBLPROPERTIES (...)"}
    marked = dict(base, status=ac.STATUS_DEFERRED, resolution=f"deferred: {cm.OPEN_APIS_REASON}")
    other = dict(base, status=ac.STATUS_DEFERRED, resolution="deferred: storage team busy")
    still_open = dict(base, status=ac.STATUS_OPEN, resolution=None)
    titles = [e["title"] for e in rb.entries_from_actions([marked, other, still_open])]
    assert titles == [cm.OPEN_APIS_NOT_NEEDED_TITLE, base["title"], base["title"]]

    sql = rb.create_runbook_view_sql("main", "migration")
    assert cm.open_apis_resolution_sql("resolution") in sql
    assert f"THEN '{cm.OPEN_APIS_NOT_NEEDED_TITLE}' ELSE title END AS title" in sql


def test_create_runbook_view_sql_references_all_three_sources_and_statuses():
    sql = rb.create_runbook_view_sql("main", "migration")
    assert "CREATE OR REPLACE VIEW main.migration.migration_runbook" in sql
    assert "main.migration.migration_actions" in sql
    assert "main.migration.freeze_ledger" in sql
    # The manifest is the third source — blockers exploded from its CSV.
    assert "main.migration.table_manifest" in sql
    assert ex.LEDGER_FROZEN in sql and ex.LEDGER_PLANNED in sql
    assert ac.STATUS_OPEN in sql
    # phase-ordered
    assert "phase_rank" in sql and "ORDER BY phase_rank" in sql
    # freeze statement CASE mirrors executor op kinds
    assert ex.OP_DISABLE_PO in sql and ex.OP_PAUSE_JOB in sql


def test_runbook_view_sql_covers_writer_pause_op():
    sql = rb.create_runbook_view_sql("main", "migration")
    assert ex.OP_PAUSE_WRITER_JOB in sql
    assert "schedule/trigger/continuous" in sql


def test_writer_action_remediation_mentions_schedule_trigger_continuous():
    rows = ac.actions_from_manifest_row(
        {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t"},
        writer_rows=[{"writer_class": "EVENT_JOB", "entity_id": "812"}],
    )
    by_kind = {r["action_kind"]: r for r in rows}
    assert "schedule/trigger/continuous" in by_kind[ac.KIND_STOP_WRITER_JOB]["remediation_api"]
    assert "schedule/trigger/continuous" in by_kind[ac.KIND_RESTART_WRITER_JOB]["remediation_api"]


def test_create_runbook_view_sql_includes_blocker_branch():
    sql = rb.create_runbook_view_sql("main", "migration")
    # Blockers are exploded from the manifest CSV and tagged with the BLOCKER source.
    assert rb.SOURCE_BLOCKER in sql
    assert "explode(split(" in sql
    # The CASE is generated from _BLOCKER_REMEDIATION, so every known code + a recognisable
    # snippet of its remediation must appear verbatim (guards against view/builder drift).
    for code in rb._BLOCKER_REMEDIATION:
        assert f"WHEN '{code}'" in sql
    assert "SET MANAGED LOCATION" in sql          # no_managed_location remediation
    assert "TRUNCATE UNIFORM HISTORY" in sql       # runtime_below_17_2_uniform remediation
    # Blocker rows are PRE_CONVERT + blocking and carry no plan/wave id (not on the manifest).
    assert "'BLOCKED' AS status" in sql
    assert "CAST(NULL AS STRING) AS plan_run_id" in sql


def test_blocker_remediation_covers_ownership_codes():
    # Dry-run Check 13 stamps these; the runbook must render precise, non-generic guidance.
    unknown = rb.blocker_remediation("unknown_owning_workspace", catalog="c", schema="s",
                                     full_name="c.s.t")
    foreign = rb.blocker_remediation("foreign_workspace", catalog="c", schema="s",
                                     full_name="c.s.t")
    assert "owning_workspace" in unknown and "c.s.t" in unknown
    assert "DIFFERENT workspace" in foreign and "c.s.t" in foreign
    # Not the generic fallback.
    assert not unknown.startswith("Resolve blocker")


def test_blocker_remediation_path_access_blocks_managed():
    # The assess-time hold-back token (profiler-owned) must render a precise,
    # non-generic hint that points the operator to name-based access + the action.
    hint = rb.blocker_remediation("path_based_access_blocks_managed", catalog="c",
                                  schema="s", full_name="c.s.t")
    assert "name-based" in hint
    assert "FIX_PATH_ACCESS" in hint
    # Not the generic fallback.
    assert not hint.startswith("Resolve blocker")
    # It renders into the generated view SQL (drift guard for the new code).
    sql = rb.create_runbook_view_sql("main", "migration")
    assert "WHEN 'path_based_access_blocks_managed'" in sql


def test_blocker_remediation_path_redirect_tokens_lead_with_name_based_then_the_opt_in():
    from migration import external_writes as ew
    from migration import path_access as pa

    sql = rb.create_runbook_view_sql("main", "migration")
    for code in (ew.BLOCKER_PATH_REDIRECT_HOLD, pa.BLOCKER_REDIRECT_CHECK):
        hint = rb.blocker_remediation(code, catalog="c", schema="s", full_name="c.s.t")
        assert not hint.startswith("Resolve blocker")
        assert "18.1" in hint and "18.2" in hint
        assert hint.index("name-based") < hint.index("opt in")
        assert "eligible" in hint
        assert f"WHEN '{code}'" in sql


def test_an_active_redirect_opt_in_carries_the_restart_streams_note():
    from migration import path_access as pa

    rows = [{"catalog": "c", "schema": "s", "full_name": "c.s.t",
             "path_redirect_optin_status": pa.OPTIN_ACTIVE},
            {"catalog": "c", "schema": "s", "full_name": "c.s.u",
             "path_redirect_optin_status": pa.OPTIN_LAPSED},
            {"catalog": "c", "schema": "s", "full_name": "c.s.v"}]
    book = rb.build_runbook([], redirect_rows=rows)
    assert [e["full_name"] for e in book] == ["c.s.t"]
    note = book[0]
    assert note["phase"] == rb.PHASE_POST_CONVERT and note["blocking"] is False
    assert note["source_kind"] == rb.SOURCE_PATH_REDIRECT
    assert "restart every stream" in note["statement"] and "c.s.t" in note["statement"]


def test_the_runbook_view_mirrors_the_redirect_opt_in_note():
    from migration import path_access as pa

    sql = rb.create_runbook_view_sql("main", "migration")
    assert f"UPPER(path_redirect_optin_status) = '{pa.OPTIN_ACTIVE}'" in sql
    assert f"'{rb.ITEM_PATH_REDIRECT_OPT_IN}' AS item_kind" in sql
    head, tail = rb.redirect_optin_note("X").split("X")
    assert "'" not in head + tail
    assert f"CONCAT('{head}', full_name, '{tail}')" in sql


def test_path_hold_hint_points_at_the_scan_verify_exit():
    hint = rb.blocker_remediation("path_based_access_blocks_managed", full_name="c.s.t")
    assert "source scan" in hint
    assert "next profile clears" not in hint


def test_view_blocker_case_literals_match_templates():
    """Every static (non-placeholder) span of each template appears verbatim as a SQL literal.

    This is the drift guard: the SQL CASE is generated from ``_BLOCKER_REMEDIATION`` so the
    view's blocker text must match the pure builder's. Placeholders (``{cat}``/``{sch}``/
    ``{full_name}``) become column refs; the literal spans between them become quoted string
    literals (single quotes doubled), so each such span must be findable in the view SQL.
    """
    import re

    sql = rb.create_runbook_view_sql("main", "migration")
    for tmpl in rb._BLOCKER_REMEDIATION.values():
        for span in re.split(r"\{cat\}|\{sch\}|\{full_name\}", tmpl):
            span = span.strip()
            if len(span) < 4:  # skip trivial connectors like "." between {cat}.{sch}
                continue
            assert span.replace("'", "''") in sql, f"missing SQL literal for span: {span!r}"


def test_legacy_leased_row_emits_only_the_restore():
    # Ownership never changes now: a lease taken before convert authority is only restored.
    led = [{"status": "LEASED", "scope": "TABLE", "catalog": "c", "schema": "s",
            "full_name": "c.s.t", "prior_owner": "alice@corp.com", "new_owner": "migration-owners",
            "plan_run_id": "p1", "wave_id": "w1"}]
    entries = rb.entries_from_ownership_ledger(led)
    assert not any(e["phase"] == rb.PHASE_PRE_CONVERT for e in entries)
    assert [(e["item_kind"], e["blocking"], e["statement"]) for e in entries] == [
        ("RESTORE_OWNERSHIP", False, "ALTER TABLE c.s.t OWNER TO `alice@corp.com`;")]


def test_legacy_planned_lease_preview_emits_nothing():
    led = [{"status": "PLANNED", "scope": "TABLE", "catalog": "c", "schema": "s",
            "full_name": "c.s.t", "prior_owner": "alice@corp.com", "new_owner": "migration-owners"}]
    assert rb.entries_from_ownership_ledger(led) == []


def test_legacy_leased_row_with_unknown_prior_owner_is_a_by_hand_restore():
    led = [{"status": "LEASED", "scope": "TABLE", "catalog": "c", "schema": "s",
            "full_name": "c.s.t", "prior_owner": None, "new_owner": "migration-owners"}]
    (e,) = rb.entries_from_ownership_ledger(led)
    assert e["phase"] == rb.PHASE_POST_CONVERT and not e["blocking"]
    assert e["statement"] == "ALTER TABLE c.s.t OWNER TO `<original owner>`;"
    assert "not captured" in e["title"]


def test_build_runbook_includes_ownership_ledger_rows():
    led = [{"status": "LEASED", "scope": "TABLE", "catalog": "c", "schema": "s",
            "full_name": "c.s.t", "prior_owner": "alice@corp.com", "new_owner": "migration-owners"}]
    out = rb.build_runbook([], ownership_ledger_rows=led)
    assert any(e["source_kind"] == rb.SOURCE_OWNERSHIP for e in out)


def _grant_row(status="GRANTED", **kw):
    row = {"op_kind": "GRANT", "status": status, "scope": "TABLE", "catalog": "c", "schema": "s",
           "full_name": "c.s.t", "securable_type": "SCHEMA", "securable_name": "c.s",
           "privilege": "USE SCHEMA", "grantee": "sp-app", "added": True, "prior_held": False}
    row.update(kw)
    return row


def test_grant_rows_emit_the_grant_pre_and_the_revoke_post():
    entries = rb.entries_from_ownership_ledger([_grant_row()])
    pre = [e for e in entries if e["phase"] == rb.PHASE_PRE_CONVERT]
    post = [e for e in entries if e["phase"] == rb.PHASE_POST_CONVERT]
    assert [e["item_kind"] for e in pre] == [rb.ITEM_GRANT_PRIVILEGE]
    assert pre[0]["statement"] == "GRANT USE SCHEMA ON SCHEMA c.s TO `sp-app`;" and pre[0]["blocking"]
    assert [e["statement"] for e in post] == ["REVOKE USE SCHEMA ON SCHEMA c.s FROM `sp-app`;"]
    assert not any("OWNER TO" in e["statement"] for e in entries)


def test_grant_rows_never_promise_a_revoke_of_a_privilege_held_before():
    for row in (_grant_row(prior_held=True, added=False), _grant_row(added=False, prior_held=None)):
        entries = rb.entries_from_ownership_ledger([row])
        assert not any(e["phase"] == rb.PHASE_POST_CONVERT for e in entries)
    # a PLANNED row (dry-run) shows the revoke Authorize would owe; REVOKED/FAILED rows show nothing
    assert len(rb.entries_from_ownership_ledger([_grant_row("PLANNED", added=None)])) == 2
    assert rb.entries_from_ownership_ledger([_grant_row("REVOKED")]) == []
    assert rb.entries_from_ownership_ledger([_grant_row("FAILED")]) == []


def test_runbook_view_unions_grant_rows_and_keeps_legacy_lease_rows_apart():
    sql = rb.create_runbook_view_sql("main", "migration")
    assert f"'{rb.ITEM_GRANT_PRIVILEGE}' AS item_kind" in sql
    assert f"'{rb.ITEM_REVOKE_PRIVILEGE}' AS item_kind" in sql
    assert "op_kind = 'GRANT' AND status IN ('PLANNED', 'GRANTED')" in sql
    assert "COALESCE(prior_held, FALSE) = FALSE" in sql
    assert sql.count("COALESCE(op_kind, 'LEASE') = 'LEASE'") == 1
    assert "'Lease: OWNER TO '" not in sql
    assert "WHERE status = 'LEASED'" in sql


def test_every_freeze_kind_pair_kind_renders_a_runbook_entry():
    # Build one open PRE_CONVERT action per FREEZE_KIND_PAIRS kind, each with the remediation the
    # generator would attach, and assert the runbook surfaces every one (none silently skipped).
    rows = [
        _action(kind, phase=ac.PHASE_PRE_CONVERT, blocking=True,
                api=f"do {kind}") for kind in ac.FREEZE_KIND_PAIRS
    ]
    entries = rb.entries_from_actions(rows)
    assert len(entries) == len(ac.FREEZE_KIND_PAIRS)
    assert all(e["statement"] for e in entries)
    assert ac.KIND_STOP_WRITER_JOB in {e["item_kind"] for e in entries}


def test_runbook_view_lists_deferred_actions():
    sql = rb.create_runbook_view_sql("c", "s")
    assert (f"WHERE status IN ('{ac.STATUS_OPEN}', '{ac.STATUS_IN_PROGRESS}', "
            f"'{ac.STATUS_DEFERRED}')") in sql
