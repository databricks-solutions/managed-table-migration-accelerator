"""Ownership classification for the SET MANAGED convert (read-only planning tier).

Pure, unit-testable logic only — no Spark, no network, no I/O.

Databricks requires the convert ``run_as`` identity to be an OWNER-equivalent of an EXTERNAL
table before ``ALTER TABLE ... SET MANAGED`` (neither MODIFY nor MANAGE substitutes — see
``app/docs/app-sp-grants.md``). This module classifies, per table, WHETHER and HOW ownership
must change so the App service principal can convert — it mutates nothing.

It is the foundation the ownership-lease execute bracket (Plan 4) builds on: ``LEASE`` is the
only class that will generate a lease/restore action; ``JOIN_GROUP`` and ``NONE`` generate
nothing.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from . import actions as _ac  # KIND_*, action_key, scope_ref, SCOPE_TABLE (one-way import; no cycle)
from . import concurrency as _cc

# comms_feed.ownership_change vocabulary. New plans write NONE (ownership never changes; the
# convert permission lives in convert_authority). LEASE / JOIN_GROUP survive in older plans and
# the legacy lease drain (07 restores LEASED ledger rows), so readers keep accepting them.
OWNERSHIP_NONE = "NONE"
OWNERSHIP_JOIN_GROUP = "JOIN_GROUP"   # legacy: owner was a group the SP could join
OWNERSHIP_LEASE = "LEASE"             # legacy: a temporary ownership lease was planned

ALL_OWNERSHIP_CHANGES = (OWNERSHIP_NONE, OWNERSHIP_JOIN_GROUP, OWNERSHIP_LEASE)


# --------------------------------------------------------------------------- #
# Authorize ledger vocabulary: GRANT rows plus legacy LEASE rows (modeled on executor.py)
# --------------------------------------------------------------------------- #

OWNERSHIP_LEDGER_TABLE = "ownership_ledger"

# Ops the ledger records. GRANT is the convert-permission op (one row per table per privilege;
# 07 revokes the row). LEASE is legacy: an ownership lease taken before convert authority
# existed (the restore reuses the same row, LEASED -> RESTORED); 07 still drains those.
OP_GRANT = "GRANT"
OP_LEASE = "LEASE"

# Ledger lifecycle status (ownership.py owns this end to end).
LEDGER_PLANNED = "PLANNED"      # dry-run: planned (a grant or legacy lease), nothing run
LEDGER_GRANTED = "GRANTED"      # Authorize ran (or skipped as already held) this privilege
LEDGER_REVOKED = "REVOKED"      # 07 revoked an added privilege after cleanup / rollback
LEDGER_LEASED = "LEASED"        # legacy: lease applied and verified (live owner == new_owner)
LEDGER_RESTORED = "RESTORED"    # legacy: restored after cleanup (live owner == prior_owner)
LEDGER_FAILED = "FAILED"        # the op errored (state persisted; stage fails loud)


def ownership_ledger_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{OWNERSHIP_LEDGER_TABLE}"


def ledger_key(full_name: str) -> str:
    """Deterministic, human-readable identity: ``OWNERSHIP|<full_name>`` (ownership is per-table)."""
    return f"OWNERSHIP|{full_name}"


def restore_statement(full_name: str, prior_owner: str) -> str:
    """The exact legacy restore (pure): hands a lease taken before convert authority back to its
    captured prior owner. Principal is backtick-quoted (group or SP client-id)."""
    return f"ALTER TABLE {full_name} OWNER TO `{prior_owner}`;"


def owner_verified(live_owner, expected) -> bool:
    """Did the (lease|restore) land? Live UC owner == the expected principal (pure).

    Both sides must be present — a NULL live owner or a NULL expected principal can never be
    "verified" (SR7: absence of evidence is never proof). Whitespace and case are normalized: a
    principal is a SQL identifier, so UC resolves ``OWNER TO `Ann@x.com``` to the same principal
    and reads it back as stored (``ann@x.com``); an exact compare could never verify that lease.
    """
    if live_owner is None or expected is None:
        return False
    return str(live_owner).strip().casefold() == str(expected).strip().casefold()


# --------------------------------------------------------------------------- #
# Column definition — single source of truth for DDL + StructType + row builder.
# Column order is the contract's ownership_ledger block (control-tables.md).
# --------------------------------------------------------------------------- #

LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ledger_key", "STRING"),          # deterministic identity (MERGE key)
    ("action_key", "STRING"),          # the LEASE_OWNERSHIP action this op satisfies
    ("action_id", "STRING"),
    ("op_kind", "STRING"),             # always LEASE (the restore reuses the same row)
    ("scope", "STRING"),               # TABLE — ownership is per-table
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("table_name", "STRING"),
    ("full_name", "STRING"),
    ("prior_owner", "STRING"),         # captured LIVE before transfer — the exact-restore input
    ("new_owner", "STRING"),           # the lease target (e.g. migration-owners)
    ("dry_run", "BOOLEAN"),            # True = PLANNED preview; does NOT satisfy convert coverage
    ("status", "STRING"),              # PLANNED | LEASED | RESTORED | FAILED
    ("leased_at", "TIMESTAMP"),
    ("leased_by", "STRING"),
    ("restored_at", "TIMESTAMP"),
    ("restored_by", "STRING"),
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("evidence", "STRING"),
    ("notes", "STRING"),
    ("updated_at", "TIMESTAMP"),
    # GRANT rows (op_kind=GRANT). leased_by / restored_by carry who granted / revoked.
    ("securable_type", "STRING"),      # TABLE | SCHEMA | CATALOG
    ("securable_name", "STRING"),
    ("privilege", "STRING"),           # USE CATALOG | USE SCHEMA | MANAGE | SELECT | MODIFY
    ("grantee", "STRING"),             # the convert identity
    ("prior_held", "BOOLEAN"),         # held directly before Authorize ran it (never revoked)
    ("added", "BOOLEAN"),              # Authorize ran the GRANT — the only rows 07 revokes
    ("granted_at", "TIMESTAMP"),
    ("revoked_at", "TIMESTAMP"),
)
LEDGER_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in LEDGER_COLUMNS)

# Two-way partition (must cover LEDGER_COLUMN_NAMES exactly once). IDENTITY is set on the first
# INSERT and PRESERVED on every MERGE; MUTABLE is refreshed. prior_owner is IDENTITY on purpose:
# a dry-run PLANNED row captures the TRUE original owner; a later real LEASED MERGE (same
# ledger_key) must not overwrite it with the now-transferred owner. (The row can never be
# re-captured wrongly because a leased+verified action is DONE and 03d skips it — but keeping
# prior_owner in IDENTITY makes the "never overwritten once captured" contract structural.)
LEDGER_IDENTITY_COLUMNS = frozenset({
    "ledger_key", "action_key", "action_id", "op_kind", "scope", "catalog", "schema",
    "table_name", "full_name", "prior_owner", "new_owner",
    "securable_type", "securable_name", "privilege", "grantee",
})
LEDGER_MUTABLE_COLUMNS = frozenset({
    "dry_run", "status", "leased_at", "leased_by", "restored_at", "restored_by",
    "plan_run_id", "wave_id", "evidence", "notes", "updated_at",
    "prior_held", "added", "granted_at", "revoked_at",
})


def build_ownership_ledger_row(
    op: dict,
    *,
    status: str,
    dry_run: bool,
    prior_owner: Optional[str] = None,
    leased_at=None,
    leased_by: Optional[str] = None,
    restored_at=None,
    restored_by: Optional[str] = None,
    evidence: Optional[str] = None,
    notes: Optional[str] = None,
    updated_at=None,
    prior_held: Optional[bool] = None,
    added: Optional[bool] = None,
    granted_at=None,
    revoked_at=None,
) -> dict:
    """Assemble one ownership-ledger row (every column present; Nones allowed)."""
    row = {
        "ledger_key": op["ledger_key"],
        "action_key": op.get("action_key"),
        "action_id": op.get("action_id"),
        "op_kind": op.get("op_kind") or OP_LEASE,
        "scope": op.get("scope") or _ac.SCOPE_TABLE,
        "catalog": op.get("catalog"),
        "schema": op.get("schema"),
        "table_name": op.get("table_name"),
        "full_name": op.get("full_name"),
        "prior_owner": prior_owner,
        "new_owner": op.get("new_owner"),
        "dry_run": bool(dry_run),
        "status": status,
        "leased_at": leased_at,
        "leased_by": leased_by,
        "restored_at": restored_at,
        "restored_by": restored_by,
        "plan_run_id": op.get("plan_run_id"),
        "wave_id": op.get("wave_id"),
        "evidence": evidence,
        "notes": notes,
        "updated_at": updated_at,
        "securable_type": op.get("securable_type"),
        "securable_name": op.get("securable_name"),
        "privilege": op.get("privilege"),
        "grantee": op.get("grantee"),
        "prior_held": prior_held,
        "added": added,
        "granted_at": granted_at,
        "revoked_at": revoked_at,
    }
    return {name: row.get(name) for name in LEDGER_COLUMN_NAMES}


def create_ownership_ledger_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in LEDGER_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{ownership_ledger_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def ownership_ledger_struct_type():
    from pyspark.sql.types import (
        BooleanType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {"STRING": StringType(), "BOOLEAN": BooleanType(), "TIMESTAMP": TimestampType()}
    return StructType([StructField(name, sql_to_spark[t], True) for name, t in LEDGER_COLUMNS])


# --------------------------------------------------------------------------- #
# Spark edges (thin) — ownership_ledger table I/O (mirrors executor.py).
# --------------------------------------------------------------------------- #

def _reconcile_columns(spark, fqn: str) -> list[str]:
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in LEDGER_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_ownership_ledger_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the ownership ledger if absent and reconcile any missing columns."""
    spark.sql(create_ownership_ledger_table_sql(manifest_catalog, manifest_schema))
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def ledger_merge_update_columns() -> list[str]:
    """MUTABLE columns refreshed on MERGE-MATCH (pure; IDENTITY columns are preserved)."""
    return [c for c in LEDGER_COLUMN_NAMES if c in LEDGER_MUTABLE_COLUMNS]


def merge_ownership_ledger_rows(
    spark, manifest_catalog: str, manifest_schema: str, rows: list[dict],
    *, max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Idempotently MERGE ledger rows on ``ledger_key`` (refresh only MUTABLE columns).

    Retries transient row-level write conflicts so parallel bulk waves writing DISJOINT rows
    never fail spuriously; metadata/protocol conflicts are not retried. Mirrors
    ``executor.merge_ledger_rows``.
    """
    if not rows:
        return 0
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame(
        [{name: r.get(name) for name in LEDGER_COLUMN_NAMES} for r in rows],
        schema=ownership_ledger_struct_type(),
    )
    src_view = "_ownership_ledger_src"
    df.createOrReplaceTempView(src_view)

    update_cols = ledger_merge_update_columns()
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in update_cols)
    insert_cols = ", ".join(LEDGER_COLUMN_NAMES)
    insert_vals = ", ".join(f"s.{c}" for c in LEDGER_COLUMN_NAMES)
    merge_sql = (
        f"""
        MERGE INTO {fqn} AS t
        USING {src_view} AS s
        ON t.ledger_key = s.ledger_key
        WHEN MATCHED THEN UPDATE SET
            {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols})
            VALUES ({insert_vals})
        """
    )

    def _do():
        spark.sql(merge_sql)
        return len(rows)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def read_leased_ledger_rows(
    spark, manifest_catalog: str, manifest_schema: str, *, where_predicate: Optional[str] = None
) -> list[dict]:
    """Real (``dry_run=false``) LEASED ledger rows awaiting restore (optionally scoped)."""
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT * FROM {fqn} WHERE status = '{LEDGER_LEASED}' AND dry_run = false{pred}"
    ).collect()
    return [r.asDict() for r in rows]


def mark_restored_sql(
    manifest_catalog: str, manifest_schema: str, ledger_keys: list[str],
    *, restored_by: Optional[str], restored_at_lit: str, evidence: Optional[str],
) -> str:
    """The exact UPDATE that flips LEASED -> RESTORED (pure; tested)."""
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in ledger_keys)
    by_lit = "NULL" if not restored_by else "'" + restored_by.replace("'", "''") + "'"
    ev_set = ""
    if evidence is not None:
        ev_set = f", evidence = '{evidence.replace(chr(39), chr(39) * 2)}'"
    return (
        f"UPDATE {fqn} "
        f"SET status = '{LEDGER_RESTORED}', restored_at = TIMESTAMP '{restored_at_lit}', "
        f"restored_by = {by_lit}, updated_at = TIMESTAMP '{restored_at_lit}'{ev_set} "
        f"WHERE ledger_key IN ({quoted}) AND status = '{LEDGER_LEASED}'"
    )


def mark_restored(
    spark, manifest_catalog: str, manifest_schema: str, ledger_keys: list[str],
    *, restored_by: Optional[str] = None, restored_at=None, evidence: Optional[str] = None,
    max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Flip LEASED ledger rows to RESTORED (never delete). Retries transient row conflicts."""
    keys = [k for k in (ledger_keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    ts = restored_at or datetime.now(timezone.utc)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    update_sql = mark_restored_sql(
        manifest_catalog, manifest_schema, keys,
        restored_by=restored_by, restored_at_lit=ts_lit, evidence=evidence,
    )

    def _do():
        spark.sql(update_sql)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


# --------------------------------------------------------------------------- #
# Legacy lease restore (07 drains leases taken before convert authority existed)
# --------------------------------------------------------------------------- #

def paired_action_key(ledger_row: dict) -> Optional[str]:
    """The POST_CONVERT RESTORE_OWNERSHIP action key to close when this row is restored."""
    fn = ledger_row.get("full_name")
    if not fn:
        return None
    ref = _ac.scope_ref(_ac.SCOPE_TABLE, full_name=fn)
    return _ac.action_key(_ac.KIND_RESTORE_OWNERSHIP, ref, None)


def restore_op_from_ledger(ledger_row: dict) -> dict:
    """Derive the restore op from a LEASED ledger row.

    ``should_act`` is the exact-restore guard: restore ONLY to a captured ``prior_owner`` —
    never to an unknown owner (SR7). Mirrors ``executor.restore_op_from_ledger``.
    """
    prior_owner = ledger_row.get("prior_owner")
    should_act = bool(prior_owner)
    reason = ("restore ownership to the captured original owner"
              if should_act else
              "prior owner unknown — cannot restore (never guess an owner); leave leased + surface")
    return {
        "ledger_key": ledger_row.get("ledger_key"),
        "op_kind": OP_LEASE,
        "scope": ledger_row.get("scope") or _ac.SCOPE_TABLE,
        "catalog": ledger_row.get("catalog"),
        "schema": ledger_row.get("schema"),
        "full_name": ledger_row.get("full_name"),
        "prior_owner": prior_owner,
        "new_owner": ledger_row.get("new_owner"),
        "should_act": should_act,
        "reason": reason,
        "paired_action_key": paired_action_key(ledger_row),
    }


def set_owner_sql(full_name: str, owner: str) -> str:
    """The executable (no trailing ';') ALTER OWNER statement for spark.sql (pure)."""
    return f"ALTER TABLE {full_name} OWNER TO `{owner}`"


def read_table_owner(spark, full_name: str):
    """Current UC owner of a table via information_schema, or None (best-effort; degrade honestly).

    ``information_schema.tables`` is permission-filtered; an unreadable/absent row returns None
    (never a fabricated owner). ``full_name`` is ``catalog.schema.table``.
    """
    parts = (full_name or "").split(".", 2)
    if len(parts) != 3:
        return None
    cat, sch, tbl = parts
    try:
        rows = spark.sql(
            "SELECT table_owner FROM system.information_schema.tables "
            f"WHERE table_catalog = '{cat}' AND table_schema = '{sch}' AND table_name = '{tbl}'"
        ).collect()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return rows[0]["table_owner"]


def set_table_owner(spark, full_name: str, owner: str) -> None:
    """Transfer table ownership (reversible metadata; idempotent). Never touches data."""
    spark.sql(set_owner_sql(full_name, owner))


# --------------------------------------------------------------------------- #
# Convert permission: Authorize (03d) self-grants, cleanup (07) revokes what it added.
# The verdict and the grant plan come from migration.authority; this is the ledger side.
# --------------------------------------------------------------------------- #

def grant_ledger_key(full_name: str, req: dict) -> str:
    """``GRANT|<full_name>|<securable_type> <securable_name>|<privilege>|<grantee>`` (pure)."""
    return (f"GRANT|{full_name}|{req.get('securable_type')} {req.get('securable_name')}|"
            f"{req.get('privilege')}|{req.get('grantee')}")


def grant_ops_for_table(authority_row: dict, *, action_row: Optional[dict] = None) -> list[dict]:
    """The GRANT ops Authorize runs for ONE table, or ``[]`` (pure).

    All or nothing: only a ``SELF_AUTHORIZABLE`` table (every request self-grantable) gets ops.
    A partial grant would leave the table still blocked and add privileges with no conversion to
    justify them."""
    from . import authority as _authz  # noqa: PLC0415 — authority imports nothing from here

    a = authority_row or {}
    if a.get("status") != _authz.STATUS_SELF_AUTHORIZABLE:
        return []
    reqs = [r for r in (a.get("grant_requests") or []) if r]
    if not reqs or not all(r.get("self_grantable") for r in reqs):
        return []
    fn = a["full_name"]
    parts = fn.split(".", 2)
    act = action_row or {}
    # A stored request carries no grantee: the verdict's actor is who every request is for.
    reqs = [{**r, "grantee": r.get("grantee") or a.get("actor")} for r in reqs]
    return [{
        "op_kind": OP_GRANT,
        "ledger_key": grant_ledger_key(fn, r),
        "action_key": act.get("action_key"),
        "action_id": act.get("action_id"),
        "scope": _ac.SCOPE_TABLE,
        "catalog": parts[0] if len(parts) > 0 else None,
        "schema": parts[1] if len(parts) > 1 else None,
        "table_name": parts[2] if len(parts) > 2 else None,
        "full_name": fn,
        "securable_type": r.get("securable_type"),
        "securable_name": r.get("securable_name"),
        "privilege": r.get("privilege"),
        "grantee": r.get("grantee"),
        "statement": r.get("statement") or _authz.grant_statement(r),
        "plan_run_id": act.get("plan_run_id") or a.get("plan_run_id"),
        "wave_id": act.get("wave_id") or a.get("wave_id"),
    } for r in reqs]


def plan_authorize(authority_rows: Iterable[dict], action_by_fn: Optional[dict] = None) -> dict:
    """Split verdicts into what Authorize does (pure).

    ``ops`` — ``{full_name: [grant ops]}`` for self-authorizable tables; ``manual`` — the rows
    someone else must grant (``NEEDS_GRANT``) or that could not be read (``UNKNOWN``);
    ``authorized`` — the full_names already ``AUTHORIZED`` (their open action can close)."""
    from . import authority as _authz  # noqa: PLC0415

    out: dict = {"ops": {}, "manual": [], "authorized": []}
    for r in authority_rows or []:
        fn = r.get("full_name")
        if not fn:
            continue
        status = r.get("status")
        if status == _authz.STATUS_AUTHORIZED:
            out["authorized"].append(fn)
            continue
        ops = grant_ops_for_table(r, action_row=(action_by_fn or {}).get(fn))
        if ops:
            out["ops"][fn] = ops
        else:
            out["manual"].append(r)
    out["authorized"].sort()
    return out


def grant_identity(req: dict) -> tuple:
    """What makes two recorded grants the same privilege in Unity Catalog (pure)."""
    return (str(req.get("securable_type") or "").upper(), req.get("securable_name"),
            _norm_priv(req.get("privilege")), str(req.get("grantee") or "").strip().casefold())


def grant_outcome(held: Optional[bool], ours: bool) -> dict:
    """Decide one grant from the live read and the ledger (pure).

    ``held`` — SHOW GRANTS shows it on the securable (``None`` = the read failed); ``ours`` — a
    real ledger row already records Authorize adding it (a schema/catalog grant shared with
    another table). Returns ``run`` (issue the GRANT), ``prior_held`` and ``added``. Revocation
    fails closed: a privilege is ``added`` only when Authorize provably added it, so an
    unreadable prior state is never revoked later."""
    if ours:
        return {"run": held is not True, "prior_held": False, "added": True}
    if held is None:
        return {"run": True, "prior_held": None, "added": False}
    if held:
        return {"run": False, "prior_held": True, "added": False}
    return {"run": True, "prior_held": False, "added": True}


def distinct_grants(ops_by_table: dict) -> list[dict]:
    """The distinct grants behind every table's ops, in grant order (catalog, schema, table
    MANAGE, data), each with the ``ops`` that need it (pure). A shared parent grant runs once."""
    from . import authority as _authz  # noqa: PLC0415

    by_id: dict = {}
    for fn in sorted(ops_by_table or {}):
        for op in ops_by_table[fn]:
            g = by_id.setdefault(grant_identity(op), {**{k: op.get(k) for k in (
                "securable_type", "securable_name", "privilege", "grantee", "statement")}, "ops": []})
            g["ops"].append(op)
    return list(reversed(_authz.revoke_order(list(by_id.values()))))


def _norm_priv(p) -> str:
    return " ".join(str(p or "").replace("_", " ").upper().split())


def held_directly(grant_rows: Iterable[dict], securable_type: str, privilege: str, grantee: str) -> bool:
    """Does ``SHOW GRANTS <grantee> ON <securable>`` show ``privilege`` granted on that securable
    itself (pure)? Inherited rows (another ``ObjectType``) and other principals do not count."""
    want_priv = _norm_priv(privilege)
    want_type = str(securable_type or "").upper()
    who = str(grantee or "").strip().casefold()
    for g in grant_rows or []:
        principal = str(g.get("Principal") or g.get("principal") or "").strip().casefold()
        action = _norm_priv(g.get("ActionType") or g.get("action_type") or g.get("Privilege"))
        otype = str(g.get("ObjectType") or g.get("object_type") or "").upper()
        if principal == who and action == want_priv and (not otype or otype == want_type):
            return True
    return False


def _revocable(r: dict) -> bool:
    return (r.get("op_kind") == OP_GRANT and r.get("status") == LEDGER_GRANTED and not r.get("dry_run")
            and bool(r.get("added")) and not r.get("prior_held"))


def revoke_plan(granted_rows: Iterable[dict], terminal_full_names: Iterable[str]) -> dict:
    """What cleanup may revoke (pure).

    Only real (``dry_run=false``), ``GRANTED`` rows with ``added=true`` and ``prior_held`` not
    true are candidates. A privilege is shared when several tables recorded the same grant (USE
    CATALOG / USE SCHEMA on a common parent): it is revoked once, only when EVERY table that
    recorded it is terminal, so a sibling still converting keeps its access. Returns ``revokes``
    (one per grant, in :func:`authority.revoke_order`, each with its ``ledger_keys`` and
    ``full_names``) and ``held_back`` (grants a terminal table recorded that a non-terminal
    sibling still needs)."""
    from . import authority as _authz  # noqa: PLC0415

    terminal = set(terminal_full_names or ())
    groups: dict = {}
    for r in granted_rows or []:
        if not _revocable(r):
            continue
        k = (str(r.get("securable_type") or "").upper(), r.get("securable_name"),
             _norm_priv(r.get("privilege")), r.get("grantee"))
        g = groups.setdefault(k, {"securable_type": k[0], "securable_name": k[1],
                                  "privilege": r.get("privilege"), "grantee": k[3],
                                  "ledger_keys": [], "full_names": []})
        g["ledger_keys"].append(r["ledger_key"])
        g["full_names"].append(r.get("full_name"))
    revokes, held_back = [], []
    for g in groups.values():
        g["full_names"] = sorted(set(g["full_names"]))
        g["ledger_keys"] = sorted(g["ledger_keys"])
        g["statement"] = _authz.revoke_statement(g)
        if all(fn in terminal for fn in g["full_names"]):
            revokes.append(g)
        elif any(fn in terminal for fn in g["full_names"]):
            held_back.append(g)
    return {"revokes": _authz.revoke_order(revokes), "held_back": held_back}


def restore_actions_to_close(granted_rows: Iterable[dict], revoked_keys: Iterable[str]) -> list[str]:
    """RESTORE_OWNERSHIP action keys for tables whose every added grant was revoked this run (pure)."""
    revoked = set(revoked_keys or ())
    keys_by_fn: dict = {}
    for r in granted_rows or []:
        if _revocable(r) and r.get("full_name"):
            keys_by_fn.setdefault(r["full_name"], set()).add(r["ledger_key"])
    return sorted(paired_action_key({"full_name": fn}) for fn, keys in keys_by_fn.items()
                  if keys and keys <= revoked)


def authorize_preview(ops_by_table: dict) -> list[str]:
    """One line per planned GRANT, with the exact statement — no side effects (pure)."""
    return [f"[DRY-RUN] AUTHORIZE {fn}  ->  {op['statement']};"
            for fn in sorted(ops_by_table or {}) for op in ops_by_table[fn]]


def show_grants_sql(securable_type: str, securable_name: str, grantee: str) -> str:
    return f"SHOW GRANTS `{str(grantee).replace('`', '``')}` ON {securable_type} {securable_name}"


def read_direct_grants(spark, securable_type: str, securable_name: str, grantee: str) -> list[dict]:
    """``SHOW GRANTS`` rows for ``grantee`` on one securable (thin edge; raises on a failed read)."""
    return [r.asDict() for r in spark.sql(show_grants_sql(securable_type, securable_name, grantee)).collect()]


def read_granted_ledger_rows(spark, manifest_catalog: str, manifest_schema: str) -> list[dict]:
    """Real (``dry_run=false``) ``GRANTED`` GRANT rows — the revoke candidates (thin edge)."""
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    return [r.asDict() for r in spark.sql(
        f"SELECT * FROM {fqn} WHERE op_kind = '{OP_GRANT}' AND status = '{LEDGER_GRANTED}' "
        "AND dry_run = false").collect()]


def mark_revoked_sql(manifest_catalog: str, manifest_schema: str, ledger_keys: list[str], *,
                     revoked_by: Optional[str], revoked_at_lit: str) -> str:
    """The exact UPDATE that flips GRANTED -> REVOKED (pure; never deletes)."""
    fqn = ownership_ledger_fqn(manifest_catalog, manifest_schema)
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in ledger_keys)
    by_lit = "NULL" if not revoked_by else "'" + revoked_by.replace("'", "''") + "'"
    return (f"UPDATE {fqn} SET status = '{LEDGER_REVOKED}', revoked_at = TIMESTAMP '{revoked_at_lit}', "
            f"restored_by = {by_lit}, updated_at = TIMESTAMP '{revoked_at_lit}' "
            f"WHERE ledger_key IN ({quoted}) AND status = '{LEDGER_GRANTED}'")


def mark_revoked(spark, manifest_catalog: str, manifest_schema: str, ledger_keys: list[str], *,
                 revoked_by: Optional[str] = None, revoked_at=None, max_attempts: int = 5,
                 sleep=time.sleep) -> int:
    keys = [k for k in (ledger_keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    ts = (revoked_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    stmt = mark_revoked_sql(manifest_catalog, manifest_schema, keys, revoked_by=revoked_by, revoked_at_lit=ts)

    def _do():
        spark.sql(stmt)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)
