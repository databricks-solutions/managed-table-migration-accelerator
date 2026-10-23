"""Unit tests for WS5 federation guardrails + staleness. No Spark."""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import reconcile as rc  # noqa: E402

WS = "111"        # this workspace
OTHER = "222"     # a foreign workspace


def _mrow(full_name, discovered, last_seen):
    return {"full_name": full_name, "discovered_in_workspace_id": discovered,
            "last_seen_at": last_seen}


# --- ownership ------------------------------------------------------------- #
def test_ownership_own_permitted():
    assert rc.classify_ownership(discovered_in_workspace_id=WS, current_workspace_id=WS) == rc.OWNERSHIP_OWN
    assert rc.is_convert_permitted(discovered_in_workspace_id=WS, current_workspace_id=WS) is True


def test_ownership_foreign_blocked():
    assert rc.classify_ownership(discovered_in_workspace_id=OTHER, current_workspace_id=WS) == rc.OWNERSHIP_FOREIGN
    assert rc.is_convert_permitted(discovered_in_workspace_id=OTHER, current_workspace_id=WS) is False


def test_ownership_override_permits_only_for_this_workspace():
    assert rc.classify_ownership(
        discovered_in_workspace_id=OTHER, current_workspace_id=WS, owning_workspace=WS) == rc.OWNERSHIP_OVERRIDE
    assert rc.is_convert_permitted(
        discovered_in_workspace_id=OTHER, current_workspace_id=WS, owning_workspace=WS) is True
    # override naming a DIFFERENT workspace never permits
    assert rc.is_convert_permitted(
        discovered_in_workspace_id=OTHER, current_workspace_id=WS, owning_workspace=OTHER) is False


def test_ownership_unknown_blocks_unless_overridden():
    assert rc.classify_ownership(discovered_in_workspace_id=None, current_workspace_id=WS) == rc.OWNERSHIP_UNKNOWN
    assert rc.is_convert_permitted(discovered_in_workspace_id="", current_workspace_id=WS) is False
    assert rc.is_convert_permitted(
        discovered_in_workspace_id=None, current_workspace_id=WS, owning_workspace=WS) is True


def test_foreign_ownership_blockers_selects_and_labels():
    rows = [
        _mrow("c.s.own", WS, None),
        _mrow("c.s.foreign", OTHER, None),
        _mrow("c.s.unknown", None, None),
    ]
    out = rc.foreign_ownership_blockers(rows, current_workspace_id=WS)
    names = {b["full_name"]: b for b in out}
    assert "c.s.own" not in names                       # permitted -> not a blocker
    assert names["c.s.foreign"]["blocker"] == rc.BLOCKER_FOREIGN_WORKSPACE
    assert names["c.s.unknown"]["blocker"] == rc.BLOCKER_UNKNOWN_OWNER
    # an override for this workspace clears them all
    assert rc.foreign_ownership_blockers(rows, current_workspace_id=WS, owning_workspace=WS) == []


# --- evidence-based ownership (single-workspace estates never brick) ------- #
def test_has_federation_evidence():
    assert rc.has_federation_evidence([WS, WS, None, ""]) is False   # one distinct id
    assert rc.has_federation_evidence([None, None]) is False          # all NULL (the bug case)
    assert rc.has_federation_evidence([]) is False
    assert rc.has_federation_evidence([WS, OTHER]) is True            # genuine federation


def test_unknown_provenance_is_own_when_no_federation_evidence():
    # The exact regression: profiler could not stamp workspace id -> blank provenance.
    # A lone estate (federation_seen=False) OWNS its unattributed rows -> convert permitted.
    assert rc.classify_ownership(
        discovered_in_workspace_id=None, current_workspace_id=WS, federation_seen=False
    ) == rc.OWNERSHIP_OWN
    assert rc.is_convert_permitted(
        discovered_in_workspace_id=None, current_workspace_id=WS, federation_seen=False
    ) is True
    # But with real federation evidence it stays fail-closed (UNKNOWN, blocked).
    assert rc.classify_ownership(
        discovered_in_workspace_id=None, current_workspace_id=WS, federation_seen=True
    ) == rc.OWNERSHIP_UNKNOWN


def test_foreign_always_blocked_regardless_of_federation_seen():
    # A genuinely foreign row is never waved through just because evidence is absent.
    assert rc.classify_ownership(
        discovered_in_workspace_id=OTHER, current_workspace_id=WS, federation_seen=False
    ) == rc.OWNERSHIP_FOREIGN
    rows = [_mrow("c.s.foreign", OTHER, None), _mrow("c.s.unknown", None, None)]
    out = rc.foreign_ownership_blockers(rows, current_workspace_id=WS, federation_seen=False)
    names = {b["full_name"] for b in out}
    assert names == {"c.s.foreign"}   # unknown dropped (OWN), foreign kept


# --- staleness ------------------------------------------------------------- #
def test_stale_full_names_flags_unseen_and_null():
    cutoff = dt.datetime(2026, 8, 30, 12, 0, 0)
    rows = [
        _mrow("c.s.fresh", WS, dt.datetime(2026, 8, 30, 12, 0, 1)),   # seen at/after cutoff
        _mrow("c.s.old", WS, dt.datetime(2026, 8, 29, 0, 0, 0)),      # predates -> stale
        _mrow("c.s.never", WS, None),                                  # null -> stale
    ]
    stale = rc.stale_full_names(rows, covering_cutoff=cutoff)
    assert set(stale) == {"c.s.old", "c.s.never"}


def test_stale_empty_and_no_cutoff():
    assert rc.stale_full_names([], covering_cutoff=dt.datetime.now()) == []
    # no covering run yet -> cannot judge staleness -> flag nothing (honest)
    assert rc.stale_full_names([_mrow("c.s.x", WS, None)], covering_cutoff=None) == []


def test_summarize_stale_mentions_count_and_never_deletes():
    s = rc.summarize_stale(["c.s.a", "c.s.b"], cutoff=dt.datetime(2026, 8, 30))
    assert "2" in s and "never" in s.lower()


# --- collision preflight --------------------------------------------------- #
def test_collision_warns_on_default_catalog():
    w = rc.manifest_catalog_collision_warning(manifest_catalog="main", visible_catalogs=["main", "x"])
    assert w is not None and "main" in w


def test_collision_warns_when_control_catalog_not_visible():
    w = rc.manifest_catalog_collision_warning(manifest_catalog="ctrl", visible_catalogs=["main", "x"])
    assert w is not None and "ctrl" in w


def test_collision_ok_for_deliberate_visible_catalog():
    assert rc.manifest_catalog_collision_warning(
        manifest_catalog="ws_local", visible_catalogs=["ws_local", "main"]) is None


# --- stale_manifest view --------------------------------------------------- #
def test_stale_manifest_view_fqn():
    from migration import reconcile as rec

    assert (
        rec.stale_manifest_view_fqn("main", "migration")
        == "main.migration.stale_manifest"
    )


def test_create_stale_manifest_view_sql_shape():
    from migration import reconcile as rec

    sql = rec.create_stale_manifest_view_sql("c", "s")
    # Read-only view over the two existing tables, keyed to the latest run per workspace.
    assert "CREATE OR REPLACE VIEW c.s.stale_manifest" in sql
    assert "c.s.profiler_run_ledger" in sql
    assert "c.s.table_manifest" in sql
    # Honest staleness: NULL last_seen OR older than the covering cutoff, and only when a
    # covering run exists (cutoff not null) — mirrors reconcile.stale_full_names.
    assert "last_seen_at IS NULL OR" in sql
    assert "covering_cutoff" in sql
    assert "is_stale" in sql
    # Ownership attribution comes from WS1 provenance, joined per workspace.
    assert "discovered_in_workspace_id" in sql


# --- out-of-band lifecycle reconcile (classify_live_type) ------------------ #
def _cls(state, live_type, found, proven=False):
    return rc.classify_live_type(
        manifest_state=state, live_type=live_type, found=found, not_found_proven=proven
    )


def test_reconcile_external_side_found_managed_is_converted_external():
    for state in ("DISCOVERED", "ASSESSED", "ELIGIBLE", "DRY_RUN_OK", "BLOCKED", "ROLLED_BACK"):
        assert _cls(state, "MANAGED", True) == rc.RECONCILE_CONVERTED_EXTERNAL, state


def test_reconcile_healthy_converted_found_managed_is_noop():
    # A legitimately app-converted table must NEVER be re-flipped to CONVERTED_EXTERNAL.
    assert _cls("CONVERTED", "MANAGED", True) == rc.RECONCILE_NONE
    assert _cls("VALIDATED", "MANAGED", True) == rc.RECONCILE_NONE
    assert _cls("CLEANED", "MANAGED", True) == rc.RECONCILE_NONE


def test_reconcile_converting_is_never_touched():
    # Owned by job_04_convert's ALREADY_MANAGED path; the profiler must not race it.
    assert _cls("CONVERTING", "MANAGED", True) == rc.RECONCILE_NONE
    assert _cls("CONVERTING", "EXTERNAL", True) == rc.RECONCILE_NONE


def test_reconcile_converted_validated_found_external_is_rolled_back_external():
    assert _cls("CONVERTED", "EXTERNAL", True) == rc.RECONCILE_ROLLED_BACK_EXTERNAL
    assert _cls("VALIDATED", "EXTERNAL", True) == rc.RECONCILE_ROLLED_BACK_EXTERNAL


def test_reconcile_external_side_found_external_is_noop():
    assert _cls("DISCOVERED", "EXTERNAL", True) == rc.RECONCILE_NONE
    assert _cls("DRY_RUN_OK", "EXTERNAL", True) == rc.RECONCILE_NONE


def test_reconcile_dropped_requires_proven_not_found():
    # Absence alone (or PERMISSION_DENIED, modeled as found=False, proven=False) stays STALE.
    assert _cls("DISCOVERED", None, False, proven=False) == rc.RECONCILE_NONE
    assert _cls("CONVERTED", None, False, proven=False) == rc.RECONCILE_NONE
    # A positive NOT_FOUND probe is proof.
    assert _cls("DISCOVERED", None, False, proven=True) == rc.RECONCILE_DROPPED
    assert _cls("CONVERTED", None, False, proven=True) == rc.RECONCILE_DROPPED


def test_reconcile_idempotent_on_reconciled_states():
    for state in ("CONVERTED_EXTERNAL", "DROPPED"):
        assert _cls(state, "MANAGED", True) == rc.RECONCILE_NONE
        assert _cls(state, None, False, proven=True) == rc.RECONCILE_NONE


def test_reconcile_unknown_live_type_is_noop():
    assert _cls("DISCOVERED", "VIEW", True) == rc.RECONCILE_NONE


def test_reconcile_target_state_mapping():
    assert rc.reconcile_target_state(rc.RECONCILE_CONVERTED_EXTERNAL) == "CONVERTED_EXTERNAL"
    assert rc.reconcile_target_state(rc.RECONCILE_ROLLED_BACK_EXTERNAL) == "ASSESSED"
    assert rc.reconcile_target_state(rc.RECONCILE_DROPPED) == "DROPPED"
    assert rc.reconcile_target_state(rc.RECONCILE_NONE) is None


# --- reconciled_manifest view (C2) ---------------------------------------- #
def test_reconciled_manifest_view_sql_shape():
    sql = rc.create_reconciled_manifest_view_sql("c", "s")
    assert "CREATE OR REPLACE VIEW c.s.reconciled_manifest" in sql
    assert rc.reconciled_manifest_view_fqn("c", "s") == "c.s.reconciled_manifest"
    # Reads the manifest, the stale view, and the audit log's latest RECONCILE row.
    assert "c.s.table_manifest" in sql
    assert "c.s.stale_manifest" in sql
    assert "c.s.audit_log" in sql
    assert "action = 'RECONCILE'" in sql
    # The three-way disposition the app splits on.
    assert "'converted_external'" in sql
    assert "'dropped'" in sql
    assert "'stale_unexplained'" in sql
    assert "disposition" in sql
    # Includes reconciled rows OR still-unexplained stale rows.
    assert "IN ('CONVERTED_EXTERNAL', 'DROPPED')" in sql
    assert "s.full_name IS NOT NULL" in sql


# --- plan_reconcile (B3 pure planner) ------------------------------------- #
NOW = dt.datetime(2026, 9, 12, 9, 0, 0)


def _diff(full_name, state, live_type, found, *, proven=False, discovered=WS,
          manifest_type="EXTERNAL", streaming=False):
    return {
        "full_name": full_name, "manifest_state": state, "manifest_type": manifest_type,
        "live_type": live_type, "found": found, "not_found_proven": proven,
        "discovered_in_workspace_id": discovered, "has_streaming_writer": streaming,
    }


def _plan(rows, federation_seen=False):
    return rc.plan_reconcile(
        rows, current_workspace_id=WS, federation_seen=federation_seen, now=NOW
    )


def test_plan_reconcile_converted_external_flip_and_audit_pairing():
    out = _plan([_diff("c.s.t", "DISCOVERED", "MANAGED", True)])
    assert out["manifest_updates"] == [
        {"full_name": "c.s.t", "state": "CONVERTED_EXTERNAL", "table_type": "MANAGED", "last_seen_at": NOW}
    ]
    # audit intents are 1:1 and index-aligned with the manifest updates.
    assert len(out["audit_intents"]) == len(out["manifest_updates"]) == 1
    intent = out["audit_intents"][0]
    assert intent["action"] == "RECONCILE" and intent["phase"] == "SUCCESS"
    assert intent["from_state"] == "DISCOVERED" and intent["to_state"] == "CONVERTED_EXTERNAL"


def test_plan_reconcile_streaming_converted_external_owes_restart():
    out = _plan([_diff("c.s.t", "DRY_RUN_OK", "MANAGED", True, streaming=True)])
    assert out["restart_full_names"] == ["c.s.t"]


def test_plan_reconcile_foreign_row_is_never_flipped():
    # A row discovered by another workspace, in a federated estate, is fail-closed skipped.
    out = _plan([_diff("c.s.t", "DISCOVERED", "MANAGED", True, discovered=OTHER)],
                federation_seen=True)
    assert out["manifest_updates"] == []
    assert out["audit_intents"] == []
    assert out["skipped_foreign"] == ["c.s.t"]


def test_plan_reconcile_rolled_back_external_routes_to_assessed():
    out = _plan([_diff("c.s.t", "VALIDATED", "EXTERNAL", True, manifest_type="MANAGED")])
    upd = out["manifest_updates"][0]
    assert upd["state"] == "ASSESSED" and upd["table_type"] == "EXTERNAL"


def test_plan_reconcile_dropped_only_on_proven_not_found():
    # Absence alone -> no flip.
    assert _plan([_diff("c.s.t", "DISCOVERED", None, False, proven=False)])["manifest_updates"] == []
    # Proven NOT_FOUND -> DROPPED, phase SKIPPED, table_type kept as last-known.
    out = _plan([_diff("c.s.t", "DISCOVERED", None, False, proven=True, manifest_type="EXTERNAL")])
    assert out["manifest_updates"][0]["state"] == "DROPPED"
    assert out["manifest_updates"][0]["table_type"] == "EXTERNAL"
    assert out["audit_intents"][0]["phase"] == "SKIPPED"


def test_plan_reconcile_clean_estate_is_noop():
    rows = [
        _diff("c.s.a", "DISCOVERED", "EXTERNAL", True),   # normal external
        _diff("c.s.b", "CONVERTED", "MANAGED", True),     # healthy converted
        _diff("c.s.c", "CONVERTING", "MANAGED", True),    # owned by job_04
    ]
    out = _plan(rows)
    assert out["manifest_updates"] == []
    assert out["audit_intents"] == []
    assert out["restart_full_names"] == []
