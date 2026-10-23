"""WS5 federation guardrails + staleness (pure logic, no Spark import at load).

This module realizes two of the app design-deltas:

- **D4 (convert-ownership guard):** in a multi-workspace / shared-metastore
  federation, a table is only safely converted by the workspace that discovered
  it. Converting a foreign-owned (or unknown-owned) table from the wrong
  workspace risks racing another install's control plane. Ownership is therefore
  **fail-closed**: anything that is not provably owned by *this* workspace is
  blocked unless an explicit operator override names this workspace.

- **D1 (honest staleness):** the manifest is a union across federated runs. A row
  that has not been re-seen by a covering discovery run is flagged STALE for
  operator review, but is **never deleted** — absence of a recent sighting is not
  proof the table is gone. If there is no covering run to measure against, we
  refuse to guess (flag nothing).

It also provides a small config-sanity preflight for the `manifest_catalog`
collision case (D4 config surface): warn when the control catalog is the shared
default (`main`) or is not even visible in this workspace (likely a binding/typo).

Pure and unit-testable: no pyspark, no Databricks SDK. Spark/SDK I/O lives in the
notebook edges that call these functions.
"""

from __future__ import annotations

from typing import Optional

from . import run_ledger as rl
from .audit import (
    ACTION_RECONCILE,
    AUDIT_TABLE,
    PHASE_SKIPPED,
    PHASE_SUCCESS,
    STATE_ASSESSED,
    STATE_CLEANED,
    STATE_CONVERTED,
    STATE_CONVERTED_EXTERNAL,
    STATE_CONVERTING,
    STATE_DROPPED,
    STATE_VALIDATED,
)

# --- ownership classifications --------------------------------------------- #
OWNERSHIP_OWN = "own"
OWNERSHIP_FOREIGN = "foreign"
OWNERSHIP_OVERRIDE = "override"
OWNERSHIP_UNKNOWN = "unknown"

# --- blocker labels -------------------------------------------------------- #
BLOCKER_FOREIGN_WORKSPACE = "foreign_workspace"
BLOCKER_UNKNOWN_OWNER = "unknown_owning_workspace"


def _blank(x) -> bool:
    """True when ``x`` is None or an empty / whitespace-only string."""
    return x is None or str(x).strip() == ""


def classify_ownership(
    *,
    discovered_in_workspace_id,
    current_workspace_id,
    owning_workspace=None,
    federation_seen: bool = True,
) -> str:
    """Classify a table's convert ownership relative to *this* workspace.

    Fail-closed: only tables discovered by this workspace (or explicitly
    overridden to it) are `OWN`/`OVERRIDE`. A mismatched discovery is `FOREIGN`
    (blocked) unless overridden here.

    ``federation_seen`` guards the *blank-provenance* case. In a genuinely federated
    estate (more than one workspace has ever written the control plane — the caller
    passes ``federation_seen=True``) a blank ``discovered_in_workspace_id`` is
    `UNKNOWN` (blocked) unless the override names this workspace, because we cannot
    prove this workspace owns it. But when there is NO federation evidence
    (``federation_seen=False`` — a single-workspace install, e.g. one whose provenance
    was never stamped because the runtime could not resolve the workspace id), a blank
    provenance is `OWN`: a lone estate owns its own unattributed rows, so the wall must
    not brick it. FOREIGN is always fail-closed regardless of ``federation_seen``.
    The default ``True`` preserves the historical fail-closed behavior for callers that
    do not pass the signal.
    """
    override_matches = (not _blank(owning_workspace)) and str(
        owning_workspace
    ).strip() == str(current_workspace_id).strip()

    if _blank(discovered_in_workspace_id):
        if override_matches:
            return OWNERSHIP_OVERRIDE
        return OWNERSHIP_UNKNOWN if federation_seen else OWNERSHIP_OWN

    if str(discovered_in_workspace_id).strip() == str(current_workspace_id).strip():
        return OWNERSHIP_OWN

    return OWNERSHIP_OVERRIDE if override_matches else OWNERSHIP_FOREIGN


def is_convert_permitted(
    *,
    discovered_in_workspace_id,
    current_workspace_id,
    owning_workspace=None,
    federation_seen: bool = True,
) -> bool:
    """True only when this workspace may convert the table (OWN or OVERRIDE)."""
    return classify_ownership(
        discovered_in_workspace_id=discovered_in_workspace_id,
        current_workspace_id=current_workspace_id,
        owning_workspace=owning_workspace,
        federation_seen=federation_seen,
    ) in (OWNERSHIP_OWN, OWNERSHIP_OVERRIDE)


def has_federation_evidence(workspace_ids) -> bool:
    """True when the control plane shows GENUINE federation — more than one distinct,
    non-blank workspace id has ever written it. The caller collects the ids (e.g. from
    ``profiler_run_ledger.workspace_id`` UNION ``table_manifest.discovered_in_workspace_id``);
    this reduces them to the single boolean the ownership guard needs (``federation_seen``).
    A single-workspace estate (0 or 1 distinct id, including the all-NULL case) is NOT
    federated, so the guard will not brick its unattributed rows.
    """
    distinct = {str(w).strip() for w in (workspace_ids or []) if not _blank(w)}
    return len(distinct) > 1


def foreign_ownership_blockers(
    rows,
    *,
    current_workspace_id,
    owning_workspace=None,
    federation_seen: bool = True,
) -> list:
    """Select + label manifest rows this workspace may NOT convert.

    Permitted (OWN/OVERRIDE) rows are skipped; every other row yields a blocker
    dict labeled by why (unknown owner vs. foreign workspace). Input order is
    preserved. ``federation_seen`` is forwarded to :func:`classify_ownership` — when
    ``False`` (no cross-workspace evidence), blank-provenance rows are OWN and drop out
    of the blocker list; FOREIGN rows are still blocked.
    """
    out: list = []
    for row in rows:
        discovered = row.get("discovered_in_workspace_id")
        classification = classify_ownership(
            discovered_in_workspace_id=discovered,
            current_workspace_id=current_workspace_id,
            owning_workspace=owning_workspace,
            federation_seen=federation_seen,
        )
        if classification in (OWNERSHIP_OWN, OWNERSHIP_OVERRIDE):
            continue
        blocker = (
            BLOCKER_UNKNOWN_OWNER
            if classification == OWNERSHIP_UNKNOWN
            else BLOCKER_FOREIGN_WORKSPACE
        )
        out.append(
            {
                "full_name": row.get("full_name"),
                "discovered_in_workspace_id": discovered,
                "blocker": blocker,
            }
        )
    return out


def stale_full_names(
    manifest_rows,
    *,
    covering_cutoff,
    seen_column: str = "last_seen_at",
) -> list:
    """Full names not re-seen at/after ``covering_cutoff`` (honest staleness).

    Without a covering run (``covering_cutoff`` falsy) we cannot judge staleness,
    so we flag nothing. Otherwise a row is stale when its last-seen timestamp is
    NULL or strictly predates the cutoff. Never deletes — selection only.
    """
    if not covering_cutoff:
        return []
    out: list = []
    for row in manifest_rows:
        seen = row.get(seen_column)
        if seen is None or seen < covering_cutoff:
            out.append(row.get("full_name"))
    return out


def summarize_stale(stale_names, *, cutoff) -> str:
    """Human summary that states the count and that stale rows are KEPT."""
    return (
        f"{len(stale_names)} table(s) not seen since {cutoff} — flagged STALE "
        "(kept in the manifest, never deleted)."
    )


# --------------------------------------------------------------------------- #
# Out-of-band lifecycle reconcile (pure classifier)
#
# The job_00_profiler reconcile pass reads each in-scope manifest row's LIVE table_type from
# information_schema and diffs it against the row's state. This function is the pure verdict:
# it flips state ONLY on proven positive evidence (found live MANAGED, or a positive NOT_FOUND
# probe) — never on mere absence, which stays STALE (D1). FOREIGN/UNKNOWN ownership is filtered
# by the caller (classify_ownership) BEFORE this is reached; this function is ownership-agnostic.
# --------------------------------------------------------------------------- #
RECONCILE_CONVERTED_EXTERNAL = "converted_external"     # -> STATE_CONVERTED_EXTERNAL
RECONCILE_ROLLED_BACK_EXTERNAL = "rolled_back_external"  # -> STATE_ASSESSED (out-of-band UNSET)
RECONCILE_DROPPED = "dropped"                            # -> STATE_DROPPED (proven NOT_FOUND only)
RECONCILE_NONE = "none"                                  # no change (normal / unproven / terminal)

# Manifest states the reconcile pass never touches: CLEANED is terminal; CONVERTING is owned by
# job_04_convert (its ALREADY_MANAGED path), so reconciling it here would race + mislabel a
# mid-flight app conversion as external; CONVERTED_EXTERNAL/DROPPED are idempotent (self-heal).
_RECONCILE_INERT_STATES = frozenset(
    {STATE_CLEANED, STATE_CONVERTING, STATE_CONVERTED_EXTERNAL, STATE_DROPPED}
)
# States whose data is already ours-and-managed — a live MANAGED reading is HEALTHY, not external.
_ALREADY_MANAGED_STATES = frozenset({STATE_CONVERTED, STATE_VALIDATED})

# Disposition -> the manifest state to write (RECONCILE_NONE has no target).
RECONCILE_TARGET_STATE = {
    RECONCILE_CONVERTED_EXTERNAL: STATE_CONVERTED_EXTERNAL,
    RECONCILE_ROLLED_BACK_EXTERNAL: STATE_ASSESSED,
    RECONCILE_DROPPED: STATE_DROPPED,
    RECONCILE_NONE: None,
}


def _norm_type(live_type) -> str:
    return "" if live_type is None else str(live_type).strip().upper()


def classify_live_type(
    *,
    manifest_state,
    live_type,
    found,
    not_found_proven: bool = False,
) -> str:
    """Pure verdict: (manifest_state, live_type, found, proven-not-found) -> disposition.

    Rules (see the plan's Part-D matrix):
      * CLEANED / CONVERTING / CONVERTED_EXTERNAL / DROPPED -> RECONCILE_NONE (inert).
      * found live MANAGED:
          - CONVERTED/VALIDATED -> RECONCILE_NONE (healthy; never re-flip a legit conversion).
          - any other EXTERNAL-side state -> RECONCILE_CONVERTED_EXTERNAL.
      * found live EXTERNAL:
          - CONVERTED/VALIDATED -> RECONCILE_ROLLED_BACK_EXTERNAL (out-of-band UNSET).
          - any other state -> RECONCILE_NONE (normal EXTERNAL row).
      * not found:
          - proven NOT_FOUND -> RECONCILE_DROPPED.
          - absence only (or PERMISSION_DENIED) -> RECONCILE_NONE (stays STALE; D1).
      * found but live_type is neither MANAGED nor EXTERNAL (e.g. VIEW) -> RECONCILE_NONE.
    """
    state = "" if manifest_state is None else str(manifest_state).strip().upper()
    if state in _RECONCILE_INERT_STATES:
        return RECONCILE_NONE

    if found:
        lt = _norm_type(live_type)
        if lt == "MANAGED":
            if state in _ALREADY_MANAGED_STATES:
                return RECONCILE_NONE
            return RECONCILE_CONVERTED_EXTERNAL
        if lt == "EXTERNAL":
            if state in _ALREADY_MANAGED_STATES:
                return RECONCILE_ROLLED_BACK_EXTERNAL
            return RECONCILE_NONE
        # Unknown/other live type (VIEW, etc.) — don't guess.
        return RECONCILE_NONE

    # Not found: only a POSITIVE not-found probe is proof; absence alone stays stale.
    if not_found_proven:
        return RECONCILE_DROPPED
    return RECONCILE_NONE


def reconcile_target_state(disposition: str):
    """The manifest state a disposition maps to (None for RECONCILE_NONE / unknown)."""
    return RECONCILE_TARGET_STATE.get(disposition)


def plan_reconcile(
    diff_rows,
    *,
    current_workspace_id,
    federation_seen: bool,
    now,
    owner_override=None,
) -> dict:
    """Pure planner: information_schema-diff rows -> the writes the reconcile edge performs.

    No Spark, no SDK, no uuid — the thin notebook edge turns these intents into actual
    audit rows / MERGEs. Each ``diff_row`` is a dict:
      ``full_name, manifest_state, manifest_type, live_type, found (bool),
        not_found_proven (bool), discovered_in_workspace_id, has_streaming_writer``.

    Ownership is fail-closed (D4): a row this workspace may not convert (FOREIGN/UNKNOWN) is
    NEVER flipped — it is collected in ``skipped_foreign`` for logging. Returns:
      * ``manifest_updates`` — UPDATE-only rows {full_name, state, table_type, last_seen_at}
        for a merge_manifest_columns(update_cols=['state','table_type','last_seen_at']) call,
      * ``audit_intents`` — kwargs for build_audit_row (written BEFORE the state flip),
        1:1 and index-aligned with ``manifest_updates``,
      * ``restart_full_names`` — CONVERTED_EXTERNAL streaming tables owed an advisory restart,
      * ``skipped_foreign`` — full_names skipped by the ownership gate.
    """
    manifest_updates: list = []
    audit_intents: list = []
    restart_full_names: list = []
    skipped_foreign: list = []

    for r in diff_rows:
        fn = r.get("full_name")
        # Fail-closed ownership: only OWN/OVERRIDE rows may be reconciled by this workspace.
        if not is_convert_permitted(
            discovered_in_workspace_id=r.get("discovered_in_workspace_id"),
            current_workspace_id=current_workspace_id,
            owning_workspace=owner_override,
            federation_seen=federation_seen,
        ):
            skipped_foreign.append(fn)
            continue

        disposition = classify_live_type(
            manifest_state=r.get("manifest_state"),
            live_type=r.get("live_type"),
            found=r.get("found"),
            not_found_proven=bool(r.get("not_found_proven")),
        )
        if disposition == RECONCILE_NONE:
            continue

        target = reconcile_target_state(disposition)
        if disposition == RECONCILE_CONVERTED_EXTERNAL:
            new_type, phase = "MANAGED", PHASE_SUCCESS
        elif disposition == RECONCILE_ROLLED_BACK_EXTERNAL:
            new_type, phase = "EXTERNAL", PHASE_SUCCESS
        else:  # RECONCILE_DROPPED — table is gone; keep last-known table_type.
            new_type, phase = r.get("manifest_type"), PHASE_SKIPPED

        manifest_updates.append(
            {"full_name": fn, "state": target, "table_type": new_type, "last_seen_at": now}
        )
        audit_intents.append(
            {
                "full_name": fn,
                "action": ACTION_RECONCILE,
                "phase": phase,
                "from_state": r.get("manifest_state"),
                "to_state": target,
                "detail": (
                    f"reconcile disposition={disposition} live_type={r.get('live_type')} "
                    f"found={r.get('found')} evidence=information_schema"
                ),
            }
        )
        if disposition == RECONCILE_CONVERTED_EXTERNAL and r.get("has_streaming_writer"):
            restart_full_names.append(fn)

    return {
        "manifest_updates": manifest_updates,
        "audit_intents": audit_intents,
        "restart_full_names": restart_full_names,
        "skipped_foreign": skipped_foreign,
    }


# --------------------------------------------------------------------------- #
# stale_manifest view — manifest rows not re-seen by their workspace's latest run
# (read-only; surfaces reconcile.stale_full_names semantics for the App/dashboard).
# --------------------------------------------------------------------------- #
STALE_MANIFEST_VIEW = "stale_manifest"


def stale_manifest_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{STALE_MANIFEST_VIEW}"


def create_stale_manifest_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Rows NOT re-seen by their workspace's LATEST covering run (pure SQL builder).

    The covering cutoff is the newest ``profiler_run_ledger.finished_at`` for the row's
    ``discovered_in_workspace_id`` (WS1 provenance) — the same "latest run per workspace"
    the coverage_report view uses. A row is stale when that cutoff exists AND its
    ``last_seen_at`` is NULL or strictly predates the cutoff — mirroring the pure
    ``stale_full_names``. Rows whose workspace has no covering run are omitted (we refuse
    to guess). Read-only; never deletes. The App/dashboard read it.
    """
    view = stale_manifest_view_fqn(manifest_catalog, manifest_schema)
    ledger = rl.run_ledger_fqn(manifest_catalog, manifest_schema)
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH latest AS (
  SELECT workspace_id, finished_at, run_id, ROW_NUMBER() OVER (
      PARTITION BY workspace_id ORDER BY finished_at DESC, run_id DESC
  ) AS rn
  FROM {ledger}
),
covering AS (
  SELECT workspace_id, finished_at AS covering_cutoff, run_id AS covering_run_id
  FROM latest WHERE rn = 1
)
SELECT
  m.full_name, m.catalog, m.schema, m.table_name, m.state,
  m.discovered_in_workspace_id AS workspace_id,
  m.last_seen_at, c.covering_cutoff, c.covering_run_id,
  DATEDIFF(c.covering_cutoff, m.last_seen_at) AS staleness_days,
  TRUE AS is_stale
FROM {manifest} m
JOIN covering c ON c.workspace_id = m.discovered_in_workspace_id
WHERE c.covering_cutoff IS NOT NULL
  AND (m.last_seen_at IS NULL OR m.last_seen_at < c.covering_cutoff)"""


# --------------------------------------------------------------------------- #
# reconciled_manifest view — the out-of-band reconcile surface for the App/dashboard.
# Splits "reconciled (cause known)" from "stale (cause unknown)": one row per manifest table
# that is CONVERTED_EXTERNAL / DROPPED (a proven reconcile verdict) OR still in stale_manifest
# (unexplained). Carries a `disposition` + the latest RECONCILE audit timestamp/detail, so the
# app never re-derives the split client-side. Read-only; never deletes.
# --------------------------------------------------------------------------- #
RECONCILED_MANIFEST_VIEW = "reconciled_manifest"


def reconciled_manifest_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{RECONCILED_MANIFEST_VIEW}"


def create_reconciled_manifest_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Reconciled + still-unexplained manifest rows, with disposition + last RECONCILE audit."""
    view = reconciled_manifest_view_fqn(manifest_catalog, manifest_schema)
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    stale = stale_manifest_view_fqn(manifest_catalog, manifest_schema)
    audit = f"{manifest_catalog}.{manifest_schema}.{AUDIT_TABLE}"
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH last_reconcile AS (
  SELECT full_name, to_state, detail AS reconcile_detail, created_at AS reconciled_at,
         ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY created_at DESC) AS rn
  FROM {audit}
  WHERE action = '{ACTION_RECONCILE}'
)
SELECT
  m.full_name, m.catalog, m.schema, m.table_name, m.state,
  CASE
    WHEN UPPER(m.state) = '{STATE_CONVERTED_EXTERNAL}' THEN 'converted_external'
    WHEN UPPER(m.state) = '{STATE_DROPPED}' THEN 'dropped'
    ELSE 'stale_unexplained'
  END AS disposition,
  m.discovered_in_workspace_id AS workspace_id,
  m.last_seen_at,
  lr.reconciled_at,
  lr.reconcile_detail
FROM {manifest} m
LEFT JOIN {stale} s ON s.full_name = m.full_name
LEFT JOIN last_reconcile lr ON lr.full_name = m.full_name AND lr.rn = 1
WHERE UPPER(m.state) IN ('{STATE_CONVERTED_EXTERNAL}', '{STATE_DROPPED}')
   OR s.full_name IS NOT NULL"""


def manifest_catalog_collision_warning(
    *,
    manifest_catalog,
    visible_catalogs,
    default_catalog: str = "main",
    metastore_id=None,
) -> Optional[str]:
    """Config-sanity preflight for the control ``manifest_catalog``.

    On a shared metastore the control plane must live in a deliberate,
    workspace-local (bound) catalog — not the shared default and not a catalog
    that isn't even visible here. Returns a WARN string when either smell is
    present, else None.
    """
    if manifest_catalog == default_catalog:
        metastore_hint = f" (metastore {metastore_id})" if metastore_id else ""
        return (
            f"manifest_catalog is the shared default '{manifest_catalog}'"
            f"{metastore_hint}: on a shared metastore this risks colliding with "
            "another workspace's control plane. Choose a deliberate per-install "
            "workspace-local (bound) catalog for the manifest."
        )
    if manifest_catalog not in (visible_catalogs or []):
        return (
            f"manifest_catalog '{manifest_catalog}' is not visible in this "
            "workspace: it may not be bound here (or is a typo). Verify the "
            "catalog binding before writing the control plane."
        )
    return None
