"""Pure builders + verifiers for the admin App service-principal control-plane grants.

The admin App queries the control plane AS its service principal, so that SP needs Unity
Catalog access: read across the whole control schema and write ONLY the one operator
table. This is deliberately least-privilege — NOT the tutorial's ``GRANT ALL PRIVILEGES
ON SCHEMA``, which would let the App rewrite ``audit_log`` / ``wave_plan`` /
``freeze_ledger``. No Spark/SDK import here; the profiler edge runs the statements and
verifies the read-back (``SHOW GRANTS``), never trusting a response code.

Required set (see docs/architecture.md → "runtime→deploy handoff surface"):

  ``USE CATALOG`` on the catalog
  ``USE SCHEMA``  on the control schema
  ``SELECT``      on the control schema  (inherits to all current + future tables AND views)
  ``MODIFY``      on ``<schema>.migration_actions``  (the ONLY writable table — notes/assignee)

``USE CATALOG`` is the one grant that can exceed the profiler run identity's authority
(the catalog is a pre-existing shared securable). When the profiler cannot issue it, the
read-back catches the miss and the notebook prints the exact statement for an admin — see
the README "App service principal grants" fallback.
"""

from typing import List, Tuple

ACTIONS_TABLE = "migration_actions"

# Each spec is (privilege, securable_type, securable_fqn). ``securable_type`` is the SQL
# keyword used in both GRANT ... ON <TYPE> and SHOW GRANTS ... ON <TYPE>.
GrantSpec = Tuple[str, str, str]


def app_sp_grant_specs(
    catalog: str, schema: str, actions_table: str = ACTIONS_TABLE
) -> List[GrantSpec]:
    """The minimal (privilege, securable_type, securable_fqn) set the App SP needs."""
    cat = catalog
    sch = f"{catalog}.{schema}"
    tbl = f"{catalog}.{schema}.{actions_table}"
    return [
        ("USE CATALOG", "CATALOG", cat),
        ("USE SCHEMA", "SCHEMA", sch),
        ("SELECT", "SCHEMA", sch),
        ("MODIFY", "TABLE", tbl),
    ]


def grant_statement(privilege: str, securable_type: str, securable_fqn: str, sp_uuid: str) -> str:
    """A single additive GRANT. The SP is keyed by its client-id UUID, backtick-escaped
    (the dashes require it); a display name would be accepted and grant nothing."""
    return f"GRANT {privilege} ON {securable_type} {securable_fqn} TO `{sp_uuid}`"


def show_grants_statement(securable_type: str, securable_fqn: str, sp_uuid: str) -> str:
    """SHOW GRANTS scoped to the SP on one securable — the read-back for verification."""
    return f"SHOW GRANTS `{sp_uuid}` ON {securable_type} {securable_fqn}"


def app_sp_grant_plan(
    catalog: str, schema: str, sp_uuid: str, actions_table: str = ACTIONS_TABLE
) -> List[Tuple[str, str, str, str, str]]:
    """Return ``[(privilege, securable_type, securable_fqn, grant_sql, show_sql), ...]`` so
    the profiler edge can loop: run ``grant_sql``, run ``show_sql``, verify, collect misses."""
    plan: List[Tuple[str, str, str, str, str]] = []
    for privilege, securable_type, securable_fqn in app_sp_grant_specs(
        catalog, schema, actions_table
    ):
        plan.append(
            (
                privilege,
                securable_type,
                securable_fqn,
                grant_statement(privilege, securable_type, securable_fqn, sp_uuid),
                show_grants_statement(securable_type, securable_fqn, sp_uuid),
            )
        )
    return plan


def app_sp_grant_statements(
    catalog: str, schema: str, sp_uuid: str, actions_table: str = ACTIONS_TABLE
) -> List[str]:
    """Just the GRANT statements — for the README fallback / manual-run path."""
    return [row[3] for row in app_sp_grant_plan(catalog, schema, sp_uuid, actions_table)]


def _norm_priv(privilege) -> str:
    """Uppercase, collapse whitespace, treat ``_`` as a space so ``USE_CATALOG`` and
    ``USE CATALOG`` compare equal across runtime SHOW GRANTS spellings."""
    return " ".join(str(privilege or "").upper().replace("_", " ").split())


def _row_get(row, *keys):
    """Read the first present key from a SHOW GRANTS row (dict or SDK Row-like)."""
    for key in keys:
        if isinstance(row, dict):
            if key in row:
                return row[key]
        elif hasattr(row, key):
            return getattr(row, key)
    return None


def sp_has_grant(show_grants_rows, sp_uuid: str, privilege: str) -> bool:
    """True iff the read-back shows the SP holding ``privilege`` (or ``ALL PRIVILEGES``).

    ``SHOW GRANTS `<sp>` ON <securable>`` is already scoped to the principal, but we still
    match ``Principal`` when the runtime returns it (tolerating dict/Row and the
    ``Principal``/``ActionType`` vs ``principal``/``action_type`` column spellings). A row
    that carries only a display name — not the UUID — is not treated as a match.
    """
    if not sp_uuid:
        return False
    want = _norm_priv(privilege)
    for row in show_grants_rows or []:
        principal = _row_get(row, "Principal", "principal")
        if principal is not None and str(principal).lower() != str(sp_uuid).lower():
            continue
        action = _norm_priv(_row_get(row, "ActionType", "action_type", "actionType"))
        if action == want or action == "ALL PRIVILEGES":
            return True
    return False
