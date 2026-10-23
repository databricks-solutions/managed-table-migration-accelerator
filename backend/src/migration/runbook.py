"""The migration runbook: one ordered, copy-pasteable list of every manual statement.

This is the **trust surface** — anything the tooling can do, an operator can also do by hand.
It UNIONs, per ``catalog.schema`` (and wave), the exact statements from three sources:

- open ``migration_actions`` — their generator-owned ``remediation_sql`` / ``remediation_api``,
- ``freeze_ledger`` PLANNED/FROZEN ops — the exact freeze statement (and, when a restore is
  warranted, the exact POST_CONVERT restore statement), from :mod:`executor`,
- dry-run / managed-location **blockers** carried on the manifest ``blockers`` CSV — with a
  precise remediation hint per known blocker code,
- ACTIVE path-based redirect **opt-ins** on the manifest — a non-blocking POST_CONVERT note to
  restart path-based streams.

Everything is ordered by phase (``PRE_CONVERT`` -> ``CONVERT`` -> ``POST_CONVERT``) so it reads
top-to-bottom as a runbook. This module is PURE (no Spark at import time); the only Spark
touchpoints are the thin view edges at the bottom.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import actions as _ac
from . import authority as _authz
from . import compat as _compat
from . import executor as _ex
from . import ownership as _own
from . import path_access as _pa

# --------------------------------------------------------------------------- #
# Phase ordering — the runbook reads PRE_CONVERT -> CONVERT -> POST_CONVERT.
# --------------------------------------------------------------------------- #

PHASE_PRE_CONVERT = _ac.PHASE_PRE_CONVERT
PHASE_CONVERT = "CONVERT"
PHASE_POST_CONVERT = _ac.PHASE_POST_CONVERT

_PHASE_ORDER = {PHASE_PRE_CONVERT: 0, PHASE_CONVERT: 1, PHASE_POST_CONVERT: 2}


def phase_order(phase: Optional[str]) -> int:
    """Sort rank for a phase (unknown phases sort last, deterministically)."""
    return _PHASE_ORDER.get((phase or "").strip().upper(), 9)


# Runbook source kinds (what produced the entry).
SOURCE_ACTION = "ACTION"
SOURCE_FREEZE = "FREEZE_LEDGER"
SOURCE_RESTORE = "FREEZE_LEDGER"
SOURCE_BLOCKER = "BLOCKER"
SOURCE_OWNERSHIP = "OWNERSHIP_LEDGER"
SOURCE_PATH_REDIRECT = "PATH_REDIRECT"

ITEM_PATH_REDIRECT_OPT_IN = "PATH_REDIRECT_OPT_IN"
# Split around {full_name} so the view's CONCAT renders the same text as the pure builder.
_REDIRECT_NOTE_HEAD = "Path-based redirect is opted in for "
_REDIRECT_NOTE_TAIL = (
    ". After conversion, restart every stream that reads or writes it by path. Redirect adds "
    "latency per access and keeps the old Delta log live, so move the path consumers to "
    "name-based access when you can."
)

# A runbook entry's normalized shape (also the column order for the view / export).
RUNBOOK_FIELDS: tuple[str, ...] = (
    "phase", "phase_rank", "blocking", "scope", "catalog", "schema", "full_name",
    "source_kind", "item_kind", "title", "statement", "status", "plan_run_id", "wave_id",
)


# --------------------------------------------------------------------------- #
# Managed-location / dry-run blocker remediation hints (precise, per code).
# --------------------------------------------------------------------------- #

# Known blocker codes -> a precise, copy-pasteable remediation. ``{cat}``/``{sch}`` are
# formatted with the row's catalog/schema. Codes mirror locations.DRY_RUN_OWNED_BLOCKERS and
# the assessment stages; unknown codes still surface (with a generic "resolve" hint).
_BLOCKER_REMEDIATION = {
    "no_managed_location": (
        "Set a managed storage location before converting: "
        "`ALTER CATALOG {cat} SET MANAGED LOCATION '<url>';` (or per-schema "
        "`ALTER SCHEMA {cat}.{sch} SET MANAGED LOCATION '<url>';`)."),
    "managed_location_unresolved": (
        "Managed-location lookup errored (could not prove one exists). Grant the runner "
        "USE on {cat}/{cat}.{sch} and re-run dry-run; if truly absent, set one with "
        "`ALTER CATALOG {cat} SET MANAGED LOCATION '<url>';`."),
    "path_based_access_must_be_fixed_first": (
        "Rewrite path-based readers/writers to name-based `catalog.schema.table` access "
        "first (see the FIX_PATH_ACCESS action for this table)."),
    "path_based_access_blocks_managed": (
        "Held for review: a path-based reader/writer means this table can't be `SET MANAGED` "
        "yet. Migrate the consumer to name-based `catalog.schema.table` access (see the "
        "FIX_PATH_ACCESS action for {full_name}), then re-run the source scan: a CLEAN verdict "
        "clears this. Non-Databricks engines can't be scanned; their hold clears only once the "
        "path access ages out of the 90-day lineage window."),
    "path_redirect_requires_name_based_or_opt_in": (
        "Held for review: a Databricks job reads or writes {full_name} by path. Recommended: "
        "switch it to name-based `catalog.schema.table` access at the lines the source scan "
        "found (see the FIX_PATH_ACCESS action), then re-run the scan: a CLEAN verdict clears "
        "this. Can't change the code? You may opt in to path-based redirect once the table is "
        "eligible: every path consumer is a Databricks job on DBR 18.1+ (reads) / 18.2+ (writes) "
        "or serverless. Redirect adds latency per access, keeps the old Delta log live, and needs "
        "path-based streams restarted."),
    "path_redirect_opt_in_required": (
        "A Databricks job reads or writes {full_name} by path and no valid redirect opt-in "
        "covers it. Switch it to name-based `catalog.schema.table` access and re-run the source "
        "scan, or, if the table is eligible (every path consumer on DBR 18.1+ reads / 18.2+ "
        "writes or serverless), opt in to path-based redirect first. An opt-in lapses when a "
        "new path consumer appears."),
    "runtime_below_17_2_uniform": (
        "Run the convert on DBR 17.3+ or Serverless — `SET MANAGED TRUNCATE UNIFORM HISTORY` "
        "(Iceberg reads) requires 17.3+."),
    "corrupted_or_inaccessible": (
        "`DESCRIBE DETAIL {full_name}` must succeed before converting — repair/restore the "
        "table (it appears corrupted or inaccessible)."),
    "convert_identity_cannot_read": (
        "Held for review: the convert identity cannot read {full_name} (`DESCRIBE DETAIL` was "
        "refused for missing SELECT), so its format, features and size are unknown. It is not "
        "corrupted. Authorize {cat}.{sch} from the Convert permission page (job_03d_lease_ownership "
        "target_schema={cat}.{sch}, confirm_execute=true) or run the grant its convert_authority "
        "request names, then rescan the estate: the next profile re-assesses it."),
    "unknown_owning_workspace": (
        "Unknown owning workspace in a federated estate — run the profiler where {full_name} "
        "lives so discovered_in_workspace_id is stamped, or pass owning_workspace=<this workspace "
        "id> to convert to assert ownership here."),
    "foreign_workspace": (
        "{full_name} was discovered by a DIFFERENT workspace — run the convert from the owning "
        "workspace, or pass owning_workspace=<this workspace id> to override (D4.2)."),
}


def blocker_remediation(code: str, *, catalog: Optional[str] = None,
                        schema: Optional[str] = None, full_name: Optional[str] = None) -> str:
    """Precise remediation text for a blocker code (never empty)."""
    tmpl = _BLOCKER_REMEDIATION.get((code or "").strip())
    if tmpl:
        return tmpl.format(cat=catalog, sch=schema, full_name=full_name)
    return f"Resolve blocker '{code}' on {full_name or f'{catalog}.{schema}'} before converting."


# --------------------------------------------------------------------------- #
# Pure entry builders
# --------------------------------------------------------------------------- #

def _entry(*, phase, blocking, scope, catalog, schema, full_name, source_kind, item_kind,
           title, statement, status=None, plan_run_id=None, wave_id=None) -> dict:
    return {
        "phase": phase,
        "phase_rank": phase_order(phase),
        "blocking": bool(blocking),
        "scope": scope,
        "catalog": catalog,
        "schema": schema,
        "full_name": full_name,
        "source_kind": source_kind,
        "item_kind": item_kind,
        "title": title,
        "statement": statement,
        "status": status,
        "plan_run_id": plan_run_id,
        "wave_id": wave_id,
    }


def _is_open_apis_decision(a: dict) -> bool:
    """A DEFERRED ENABLE_COMPAT whose resolution records "readers use the UC open APIs"."""
    return (a.get("action_kind") == _ac.KIND_ENABLE_COMPAT
            and a.get("status") == _ac.STATUS_DEFERRED
            and _compat.is_open_apis_resolution(a.get("resolution")))


def entries_from_actions(action_rows: Iterable[dict]) -> list[dict]:
    """One runbook entry per open action, carrying its exact remediation statement.

    An external-readers item marked "readers use the UC open APIs" keeps its row (it is
    DEFERRED, still listed) but reads as not needed rather than pending work.
    """
    out: list[dict] = []
    for a in action_rows or []:
        stmt = a.get("remediation_sql") or a.get("remediation_api")
        if not stmt:
            continue
        title = _compat.OPEN_APIS_NOT_NEEDED_TITLE if _is_open_apis_decision(a) else a.get("title")
        out.append(_entry(
            phase=a.get("phase"),
            blocking=a.get("blocking"),
            scope=a.get("scope"),
            catalog=a.get("catalog"),
            schema=a.get("schema"),
            full_name=a.get("full_name"),
            source_kind=SOURCE_ACTION,
            item_kind=a.get("action_kind"),
            title=title,
            statement=stmt,
            status=a.get("status"),
            plan_run_id=a.get("plan_run_id"),
            wave_id=a.get("wave_id"),
        ))
    return out


def entries_from_ledger(ledger_rows: Iterable[dict]) -> list[dict]:
    """Freeze (PRE) + restore (POST) statements from PLANNED/FROZEN ledger rows.

    A PLANNED (dry-run) or FROZEN row yields the exact freeze statement it would/did run;
    the paired POST_CONVERT restore statement is emitted only when the ledger's recorded
    prior state warrants acting (never blanket re-enable PO, etc.).
    """
    out: list[dict] = []
    for lr in ledger_rows or []:
        status = (lr.get("status") or "").strip().upper()
        if status not in (_ex.LEDGER_PLANNED, _ex.LEDGER_FROZEN):
            continue
        freeze_stmt = _ex.statement_text(_ex.op_statement(lr))
        if freeze_stmt:
            out.append(_entry(
                phase=PHASE_PRE_CONVERT,
                blocking=True,
                scope=lr.get("scope"),
                catalog=lr.get("catalog"),
                schema=lr.get("schema"),
                full_name=lr.get("full_name"),
                source_kind=SOURCE_FREEZE,
                item_kind=lr.get("op_kind"),
                title=f"Freeze: {lr.get('op_kind')} ({status})",
                statement=freeze_stmt,
                status=status,
                plan_run_id=lr.get("plan_run_id"),
                wave_id=lr.get("wave_id"),
            ))
        ro = _ex.restore_op_from_ledger(lr)
        if ro.get("should_act"):
            restore_stmt = _ex.statement_text(_ex.restore_statement(ro))
            if restore_stmt:
                out.append(_entry(
                    phase=PHASE_POST_CONVERT,
                    blocking=False,
                    scope=lr.get("scope"),
                    catalog=lr.get("catalog"),
                    schema=lr.get("schema"),
                    full_name=lr.get("full_name"),
                    source_kind=SOURCE_RESTORE,
                    item_kind=ro.get("restore_kind"),
                    title=f"Restore: {ro.get('restore_kind')}",
                    statement=restore_stmt,
                    status=status,
                    plan_run_id=lr.get("plan_run_id"),
                    wave_id=lr.get("wave_id"),
                ))
    return out


def _split_blockers(csv: Optional[str]) -> list[str]:
    if not csv:
        return []
    return [p.strip() for p in str(csv).split(",") if p.strip()]


def entries_from_blockers(blocker_rows: Iterable[dict]) -> list[dict]:
    """One PRE_CONVERT entry per blocker code on a manifest row (managed-location, etc.).

    ``blocker_rows`` are manifest-shaped dicts with ``catalog``/``schema``/``full_name`` and a
    ``blockers`` CSV. Each code becomes a blocking runbook entry with a precise remediation.
    """
    out: list[dict] = []
    for r in blocker_rows or []:
        cat, sch, fn = r.get("catalog"), r.get("schema"), r.get("full_name")
        for code in _split_blockers(r.get("blockers")):
            out.append(_entry(
                phase=PHASE_PRE_CONVERT,
                blocking=True,
                scope=_ac.SCOPE_TABLE,
                catalog=cat,
                schema=sch,
                full_name=fn,
                source_kind=SOURCE_BLOCKER,
                item_kind=code,
                title=f"Blocker: {code}",
                statement=blocker_remediation(code, catalog=cat, schema=sch, full_name=fn),
                status="BLOCKED",
                plan_run_id=r.get("plan_run_id"),
                wave_id=r.get("wave_id"),
            ))
    return out


def sort_runbook(entries: Iterable[dict]) -> list[dict]:
    """Stable runbook order: phase, then blocking-first, then scope/table, then kind."""
    return sorted(
        entries or [],
        key=lambda e: (
            e.get("phase_rank", 9),
            0 if e.get("blocking") else 1,
            str(e.get("catalog") or ""),
            str(e.get("schema") or ""),
            str(e.get("full_name") or ""),
            str(e.get("source_kind") or ""),
            str(e.get("item_kind") or ""),
        ),
    )


_UNKNOWN_PRIOR_OWNER = "<original owner>"


def entries_from_ownership_ledger(ledger_rows: Iterable[dict]) -> list[dict]:
    """Runbook entries from ``ownership_ledger`` rows.

    ``GRANT`` rows (Authorize): the GRANT (PRE) and, for a grant Authorize added, its REVOKE (POST).
    Legacy ``LEASE`` rows (taken before convert authority): ownership never changes now, so a lease
    is never offered again — a ``PLANNED`` preview yields nothing and a real ``LEASED`` row yields
    only the POST restore. A restore whose prior owner was never captured is surfaced as a by-hand
    step with a placeholder owner (never a guessed one).
    """
    out: list[dict] = []
    for lr in ledger_rows or []:
        status = (lr.get("status") or "").strip().upper()
        if (lr.get("op_kind") or _own.OP_LEASE) == _own.OP_GRANT:
            out.extend(_grant_entries(lr, status))
            continue
        fn = lr.get("full_name")
        if status != _own.LEDGER_LEASED or not fn:
            continue
        ro = _own.restore_op_from_ledger(lr)
        known = bool(ro.get("should_act"))
        owner = ro.get("prior_owner") if known else _UNKNOWN_PRIOR_OWNER
        out.append(_entry(
            phase=PHASE_POST_CONVERT, blocking=False, scope=lr.get("scope"),
            catalog=lr.get("catalog"), schema=lr.get("schema"), full_name=fn,
            source_kind=SOURCE_OWNERSHIP, item_kind="RESTORE_OWNERSHIP",
            title=(f"Restore: OWNER TO {owner}" if known
                   else "Restore the owner by hand (prior owner was not captured)"),
            statement=_own.restore_statement(fn, owner),
            status=status, plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id"),
        ))
    return out


ITEM_GRANT_PRIVILEGE = "GRANT_PRIVILEGE"
ITEM_REVOKE_PRIVILEGE = "REVOKE_PRIVILEGE"


def _grant_entries(lr: dict, status: str) -> list[dict]:
    """A PLANNED / GRANTED convert-permission row: the GRANT (PRE, blocking) and, unless the
    privilege was already held, the REVOKE cleanup will run (POST). Undo before do."""
    if status not in (_own.LEDGER_PLANNED, _own.LEDGER_GRANTED):
        return []
    req = {k: lr.get(k) for k in ("securable_type", "securable_name", "privilege", "grantee")}
    if not all(req.values()):
        return []
    what = f"{req['privilege']} on {str(req['securable_type']).lower()} {req['securable_name']}"
    common = dict(scope=lr.get("scope"), catalog=lr.get("catalog"), schema=lr.get("schema"),
                  full_name=lr.get("full_name"), source_kind=SOURCE_OWNERSHIP, status=status,
                  plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id"))
    out = [_entry(phase=PHASE_PRE_CONVERT, blocking=True, item_kind=ITEM_GRANT_PRIVILEGE,
                  title=f"Grant convert permission: {what} ({status})",
                  statement=_authz.grant_statement(req) + ";", **common)]
    if not lr.get("prior_held") and (status == _own.LEDGER_PLANNED or lr.get("added")):
        out.append(_entry(phase=PHASE_POST_CONVERT, blocking=False, item_kind=ITEM_REVOKE_PRIVILEGE,
                          title=f"Revoke after cleanup: {what}",
                          statement=_authz.revoke_statement(req) + ";", **common))
    return out


REDIRECT_ROWS_PREDICATE = f"UPPER(path_redirect_optin_status) = '{_pa.OPTIN_ACTIVE}'"


def redirect_optin_note(full_name: Optional[str]) -> str:
    return f"{_REDIRECT_NOTE_HEAD}{full_name}{_REDIRECT_NOTE_TAIL}"


def entries_from_redirect_optins(manifest_rows: Iterable[dict]) -> list[dict]:
    """One non-blocking POST_CONVERT note per table with an ACTIVE path-based redirect opt-in.

    The opt-in closes the table's FIX_PATH_ACCESS action, so without this entry the runbook
    would lose the one obligation redirect still carries: restart path-based streams.
    """
    out: list[dict] = []
    for r in manifest_rows or []:
        if (r.get("path_redirect_optin_status") or "").strip().upper() != _pa.OPTIN_ACTIVE:
            continue
        fn = r.get("full_name")
        out.append(_entry(
            phase=PHASE_POST_CONVERT, blocking=False, scope=_ac.SCOPE_TABLE,
            catalog=r.get("catalog"), schema=r.get("schema"), full_name=fn,
            source_kind=SOURCE_PATH_REDIRECT, item_kind=ITEM_PATH_REDIRECT_OPT_IN,
            title="Path-based redirect opted in: restart path-based streams",
            statement=redirect_optin_note(fn),
            status=_pa.OPTIN_ACTIVE, plan_run_id=r.get("plan_run_id"), wave_id=r.get("wave_id"),
        ))
    return out


def build_runbook(
    action_rows: Iterable[dict],
    ledger_rows: Optional[Iterable[dict]] = None,
    blocker_rows: Optional[Iterable[dict]] = None,
    ownership_ledger_rows: Optional[Iterable[dict]] = None,
    redirect_rows: Optional[Iterable[dict]] = None,
) -> list[dict]:
    """The full, phase-ordered runbook from actions + freeze ledger + ownership ledger + blockers
    + path-based redirect opt-ins (pure)."""
    entries: list[dict] = []
    entries.extend(entries_from_actions(action_rows))
    entries.extend(entries_from_ledger(ledger_rows or []))
    entries.extend(entries_from_ownership_ledger(ownership_ledger_rows or []))
    entries.extend(entries_from_blockers(blocker_rows or []))
    entries.extend(entries_from_redirect_optins(redirect_rows or []))
    return sort_runbook(entries)


# --------------------------------------------------------------------------- #
# Markdown / SQL export (pure text — the operator can run it outside the tool)
# --------------------------------------------------------------------------- #

def to_markdown(entries: Iterable[dict], *, title: str = "Migration runbook") -> str:
    """Render the ordered runbook as a commented Markdown doc (read-only artifact)."""
    entries = list(entries or [])
    lines = [f"# {title}", ""]
    if not entries:
        lines.append("_No open manual statements — nothing to do._")
        return "\n".join(lines)
    current_phase = None
    for e in entries:
        if e.get("phase") != current_phase:
            current_phase = e.get("phase")
            lines += ["", f"## {current_phase}", ""]
        flag = " **[BLOCKING]**" if e.get("blocking") else ""
        scope = e.get("full_name") or f"{e.get('catalog')}.{e.get('schema')}"
        lines.append(f"- [{e.get('source_kind')}/{e.get('item_kind')}] {scope}{flag}: "
                     f"{e.get('title')}")
        stmt = e.get("statement")
        if stmt:
            lines += ["", "  ```sql", *(f"  {ln}" for ln in str(stmt).splitlines()), "  ```"]
    return "\n".join(lines)


def to_sql_script(entries: Iterable[dict], *, header: Optional[str] = None) -> str:
    """Render the ordered runbook as a commented ``.sql`` script (read-only artifact).

    API-only remediations (no SQL) are emitted as comments so nothing is silently dropped and
    the script never contains a non-SQL line that would fail if pasted into a SQL editor.
    """
    out = [f"-- {header}" if header else "-- Migration runbook (generated; review before running)", ""]
    current_phase = None
    for e in entries or []:
        if e.get("phase") != current_phase:
            current_phase = e.get("phase")
            out += ["", f"-- ===== {current_phase} ====="]
        scope = e.get("full_name") or f"{e.get('catalog')}.{e.get('schema')}"
        out.append(f"-- [{e.get('source_kind')}/{e.get('item_kind')}] {scope}"
                   f"{' [BLOCKING]' if e.get('blocking') else ''}: {e.get('title')}")
        stmt = str(e.get("statement") or "").strip()
        if not stmt:
            continue
        # A SQL statement ends with ';'. Everything else is an API/CLI call -> comment it.
        if stmt.endswith(";"):
            out.append(stmt)
        else:
            out += [f"--   (manual/API) {ln}" for ln in stmt.splitlines()]
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Spark edges (thin) — the migration_runbook view unions actions + ledger.
# --------------------------------------------------------------------------- #

RUNBOOK_VIEW = "migration_runbook"


def runbook_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{RUNBOOK_VIEW}"


def _blocker_remediation_case_sql(code_col: str = "code") -> str:
    """Build a SQL ``CASE`` that mirrors :data:`_BLOCKER_REMEDIATION` (single source of truth).

    Each Python template is translated to a SQL ``CONCAT`` so the view's blocker statements
    match the pure :func:`blocker_remediation` verbatim — the placeholders ``{cat}`` / ``{sch}``
    / ``{full_name}`` map to the ``catalog`` / ``schema`` / ``full_name`` columns, and the ``ELSE``
    reproduces the generic "Resolve blocker '<code>' on <scope> before converting." fallback.
    Generating the CASE from the dict (rather than hand-writing it) keeps the view and the
    Python builder from drifting.
    """
    import re

    placeholders = {"{cat}": "catalog", "{sch}": "schema", "{full_name}": "full_name"}

    def _to_sql_concat(template: str) -> str:
        parts = re.split(r"(\{cat\}|\{sch\}|\{full_name\})", template)
        pieces: list[str] = []
        for part in parts:
            if part in placeholders:
                pieces.append(placeholders[part])
            elif part:
                pieces.append("'" + part.replace("'", "''") + "'")
        if not pieces:
            return "''"
        return pieces[0] if len(pieces) == 1 else "CONCAT(" + ", ".join(pieces) + ")"

    whens = "\n".join(
        f"      WHEN '{code}' THEN {_to_sql_concat(tmpl)}"
        for code, tmpl in _BLOCKER_REMEDIATION.items()
    )
    generic = (
        "CONCAT('Resolve blocker ''', " + code_col
        + ", ''' on ', COALESCE(full_name, CONCAT(catalog, '.', schema)), ' before converting.')"
    )
    return f"CASE {code_col}\n{whens}\n      ELSE {generic}\n    END"


def create_runbook_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """A phase-ordered view UNIONing open actions + PLANNED/FROZEN freeze-ledger ops + blockers.

    Statements are computed in SQL (CASE) mirroring :func:`executor.op_statement` /
    :func:`executor.restore_statement` and :data:`_BLOCKER_REMEDIATION`, so the view is
    self-contained (no UDFs) and carries the SAME three sources as the pure
    :func:`build_runbook` builder: open actions, freeze-ledger ops, and the dry-run /
    managed-location blockers exploded from the manifest ``blockers`` CSV. The blocker branch
    is what makes the dashboard Runbook page a complete blocker list (a table blocked only by
    the managed-location gate has no action/ledger row — its blocker lives on the manifest).
    The ``rdr`` branch mirrors :func:`entries_from_redirect_optins`.
    """
    view = runbook_view_fqn(manifest_catalog, manifest_schema)
    actions_tbl = _ac.actions_fqn(manifest_catalog, manifest_schema)
    ledger_tbl = _ex.freeze_ledger_fqn(manifest_catalog, manifest_schema)
    own_tbl = _own.ownership_ledger_fqn(manifest_catalog, manifest_schema)
    manifest_tbl = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    pre, post = PHASE_PRE_CONVERT, PHASE_POST_CONVERT
    blocker_case = _blocker_remediation_case_sql("code")
    act_statuses = ", ".join(f"'{s}'" for s in _ac.RUNBOOK_ACTION_STATUSES)
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH act AS (
  SELECT
    phase,
    blocking,
    scope, catalog, schema, full_name,
    '{SOURCE_ACTION}' AS source_kind,
    action_kind AS item_kind,
    CASE WHEN action_kind = '{_ac.KIND_ENABLE_COMPAT}' AND status = '{_ac.STATUS_DEFERRED}'
              AND {_compat.open_apis_resolution_sql("resolution")}
         THEN '{_compat.OPEN_APIS_NOT_NEEDED_TITLE}' ELSE title END AS title,
    COALESCE(remediation_sql, remediation_api) AS statement,
    status, plan_run_id, wave_id
  FROM {actions_tbl}
  WHERE status IN ({act_statuses})
    AND COALESCE(remediation_sql, remediation_api) IS NOT NULL
),
freeze AS (
  SELECT
    '{pre}' AS phase,
    TRUE AS blocking,
    scope, catalog, schema, full_name,
    '{SOURCE_FREEZE}' AS source_kind,
    op_kind AS item_kind,
    CONCAT('Freeze: ', op_kind, ' (', status, ')') AS title,
    CASE op_kind
      WHEN '{_ex.OP_DISABLE_PO}' THEN CONCAT('ALTER SCHEMA ', catalog, '.', schema, ' DISABLE PREDICTIVE OPTIMIZATION;')
      WHEN '{_ex.OP_PAUSE_JOB}' THEN CONCAT('databricks jobs update --job-id ', target_id, ' --json ... pause_status=PAUSED')
      WHEN '{_ex.OP_STOP_STREAM_JOB}' THEN CONCAT('databricks jobs cancel-all-runs --job-id ', target_id)
      WHEN '{_ex.OP_STOP_PIPELINE}' THEN CONCAT('databricks pipelines stop --pipeline-id ', target_id)
      WHEN '{_ex.OP_PAUSE_WRITER_JOB}' THEN CONCAT('Set pause_status=PAUSED on the writer job schedule/trigger/continuous block, then cancel active runs: databricks jobs update --job-id ', target_id, ' --json ... && databricks jobs cancel-all-runs --job-id ', target_id)
    END AS statement,
    status, plan_run_id, wave_id
  FROM {ledger_tbl}
  WHERE status IN ('{_ex.LEDGER_PLANNED}', '{_ex.LEDGER_FROZEN}')
),
own AS (
  SELECT
    '{post}' AS phase, FALSE AS blocking,
    scope, catalog, schema, full_name,
    '{SOURCE_OWNERSHIP}' AS source_kind,
    'RESTORE_OWNERSHIP' AS item_kind,
    CASE WHEN prior_owner IS NULL THEN 'Restore the owner by hand (prior owner was not captured)'
         ELSE CONCAT('Restore: OWNER TO ', prior_owner) END AS title,
    CONCAT('ALTER TABLE ', full_name, ' OWNER TO `',
           COALESCE(prior_owner, '{_UNKNOWN_PRIOR_OWNER}'), '`;') AS statement,
    status, plan_run_id, wave_id
  FROM {own_tbl}
  WHERE status = '{_own.LEDGER_LEASED}' AND full_name IS NOT NULL
    AND COALESCE(op_kind, '{_own.OP_LEASE}') = '{_own.OP_LEASE}'
  UNION ALL
  SELECT
    '{pre}' AS phase, TRUE AS blocking,
    scope, catalog, schema, full_name,
    '{SOURCE_OWNERSHIP}' AS source_kind,
    '{ITEM_GRANT_PRIVILEGE}' AS item_kind,
    CONCAT('Grant convert permission: ', privilege, ' on ', LOWER(securable_type), ' ', securable_name,
           ' (', status, ')') AS title,
    CONCAT('GRANT ', privilege, ' ON ', securable_type, ' ', securable_name, ' TO `',
           REPLACE(grantee, '`', '``'), '`;') AS statement,
    status, plan_run_id, wave_id
  FROM {own_tbl}
  WHERE op_kind = '{_own.OP_GRANT}' AND status IN ('{_own.LEDGER_PLANNED}', '{_own.LEDGER_GRANTED}')
    AND securable_type IS NOT NULL AND securable_name IS NOT NULL AND privilege IS NOT NULL
    AND grantee IS NOT NULL
  UNION ALL
  SELECT
    '{post}' AS phase, FALSE AS blocking,
    scope, catalog, schema, full_name,
    '{SOURCE_OWNERSHIP}' AS source_kind,
    '{ITEM_REVOKE_PRIVILEGE}' AS item_kind,
    CONCAT('Revoke after cleanup: ', privilege, ' on ', LOWER(securable_type), ' ', securable_name) AS title,
    CONCAT('REVOKE ', privilege, ' ON ', securable_type, ' ', securable_name, ' FROM `',
           REPLACE(grantee, '`', '``'), '`;') AS statement,
    status, plan_run_id, wave_id
  FROM {own_tbl}
  WHERE op_kind = '{_own.OP_GRANT}' AND COALESCE(prior_held, FALSE) = FALSE
    AND (status = '{_own.LEDGER_PLANNED}' OR (status = '{_own.LEDGER_GRANTED}' AND added = TRUE))
    AND securable_type IS NOT NULL AND securable_name IS NOT NULL AND privilege IS NOT NULL
    AND grantee IS NOT NULL
),
blk AS (
  SELECT
    '{pre}' AS phase,
    TRUE AS blocking,
    '{_ac.SCOPE_TABLE}' AS scope,
    catalog, schema, full_name,
    '{SOURCE_BLOCKER}' AS source_kind,
    code AS item_kind,
    CONCAT('Blocker: ', code) AS title,
    {blocker_case} AS statement,
    'BLOCKED' AS status,
    CAST(NULL AS STRING) AS plan_run_id,
    CAST(NULL AS STRING) AS wave_id
  FROM (
    SELECT catalog, schema, full_name, TRIM(code_raw) AS code
    FROM {manifest_tbl}
    LATERAL VIEW explode(split(COALESCE(blockers, ''), ',')) exploded AS code_raw
    WHERE TRIM(code_raw) <> ''
  )
),
rdr AS (
  SELECT
    '{post}' AS phase,
    FALSE AS blocking,
    '{_ac.SCOPE_TABLE}' AS scope,
    catalog, schema, full_name,
    '{SOURCE_PATH_REDIRECT}' AS source_kind,
    '{ITEM_PATH_REDIRECT_OPT_IN}' AS item_kind,
    'Path-based redirect opted in: restart path-based streams' AS title,
    CONCAT('{_REDIRECT_NOTE_HEAD}', full_name, '{_REDIRECT_NOTE_TAIL}') AS statement,
    '{_pa.OPTIN_ACTIVE}' AS status,
    CAST(NULL AS STRING) AS plan_run_id,
    CAST(NULL AS STRING) AS wave_id
  FROM {manifest_tbl}
  WHERE {REDIRECT_ROWS_PREDICATE}
)
SELECT
  phase,
  CASE phase WHEN '{pre}' THEN 0 WHEN '{PHASE_CONVERT}' THEN 1 WHEN '{post}' THEN 2 ELSE 9 END AS phase_rank,
  blocking, scope, catalog, schema, full_name, source_kind, item_kind, title, statement,
  status, plan_run_id, wave_id
FROM (SELECT * FROM act UNION ALL SELECT * FROM freeze UNION ALL SELECT * FROM own UNION ALL SELECT * FROM blk
      UNION ALL SELECT * FROM rdr)
WHERE statement IS NOT NULL
ORDER BY phase_rank, blocking DESC, catalog, schema, full_name, source_kind, item_kind"""


def ensure_runbook_view(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_runbook_view_sql(manifest_catalog, manifest_schema))
    return runbook_view_fqn(manifest_catalog, manifest_schema)
