"""Delta Sharing (D2D) re-share executor logic — pure planner + verifier.

After ``SET MANAGED`` a Databricks-to-Databricks recipient is **not** auto-updated: the shared
table keeps pointing at the old (external) location until it is removed and re-added so the
share tracks the new managed location. This module:

- discovers which shares contain a converted table (parsing ``SHOW ALL IN SHARE`` output),
- plans the exact ``ALTER SHARE ... REMOVE/ADD TABLE ... WITH HISTORY`` statements (reusing
  :func:`eligibility.build_reshare_commands`), and
- verifies the table is present in the share afterwards, so the paired ``RESHARE_D2D`` action
  is closed only against platform truth.

Pure (no Spark/SDK at import time); the thin Spark/SDK edges live in ``notebooks/08_reshare.py``.
Nothing here executes SQL — it only builds the statements the operator (or the gated stage) runs.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import actions as _ac
from . import eligibility as _elig

# When no share can be attributed to a table, the statement keeps this placeholder so the
# operator fills it in — such an action stays MANUAL (surfaced, never auto-operated).
SHARE_NAME_PLACEHOLDER = "<SHARE_NAME>"


# --------------------------------------------------------------------------- #
# SHOW ALL IN SHARE parsing (pure)
# --------------------------------------------------------------------------- #

def _lower_keys(d: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in (d or {}).items()}


def parse_share_objects(rows: Iterable[dict]) -> list[dict]:
    """Normalize ``SHOW ALL IN SHARE`` output rows to ``[{name, type, shared_as}]`` (TABLE only).

    ``SHOW ALL IN SHARE`` columns vary by runtime; we read case-insensitively and keep only
    TABLE securables. ``name`` is the fully-qualified source object (``catalog.schema.table``);
    ``shared_as`` is the recipient-facing alias (may be NULL -> defaults to ``name``).
    """
    out: list[dict] = []
    for r in rows or []:
        d = _lower_keys(r.asDict() if hasattr(r, "asDict") else r)
        obj_type = str(d.get("type") or "").strip().upper()
        if obj_type and obj_type != "TABLE":
            continue
        name = d.get("name") or d.get("object") or d.get("securable")
        if not name:
            continue
        out.append({
            "name": str(name),
            "type": obj_type or "TABLE",
            "shared_as": (str(d["shared_as"]) if d.get("shared_as") else None),
        })
    return out


def shares_for_table(objects_by_share: dict, full_name: str) -> list[dict]:
    """Every ``{share_name, alias}`` whose objects include ``full_name`` (deterministic order).

    ``objects_by_share`` is ``{share_name: [parsed objects]}`` (from :func:`parse_share_objects`).
    Matching is exact on the fully-qualified name. ``alias`` falls back to the table's last part.
    """
    fn = (full_name or "").strip()
    hits: list[dict] = []
    for share in sorted(objects_by_share or {}):
        for obj in objects_by_share[share] or []:
            if str(obj.get("name") or "").strip() == fn:
                alias = obj.get("shared_as") or fn.split(".")[-1]
                hits.append({"share_name": share, "alias": alias})
    return hits


# --------------------------------------------------------------------------- #
# Pure planning: RESHARE_D2D action + discovered shares -> reshare ops
# --------------------------------------------------------------------------- #

def reshare_statements(full_name: str, share_name: str, alias: Optional[str] = None) -> list[str]:
    """The exact ``ALTER SHARE`` REMOVE+ADD statements (reuses eligibility.build_reshare_commands)."""
    return _elig.build_reshare_commands(full_name, share_name, alias)


def reshare_op(action_row: dict, share_name: str, alias: Optional[str] = None) -> dict:
    """One executable re-share op: the share + alias + exact statements for a table."""
    full_name = action_row.get("full_name")
    alias = alias or (full_name or "").split(".")[-1]
    return {
        "full_name": full_name,
        "catalog": action_row.get("catalog"),
        "schema": action_row.get("schema"),
        "share_name": share_name,
        "alias": alias,
        "action_key": action_row.get("action_key"),
        "action_id": action_row.get("action_id"),
        "plan_run_id": action_row.get("plan_run_id"),
        "wave_id": action_row.get("wave_id"),
        "statements": reshare_statements(full_name, share_name, alias),
    }


def plan_reshare(action_rows: Iterable[dict], shares_by_table: dict) -> tuple[list[dict], list[dict]]:
    """Split open RESHARE_D2D actions into ``(executable_ops, manual_actions)``.

    A table found in one or more shares yields one op per share (each with exact statements).
    A table with no attributed share stays MANUAL: surfaced with the ``<SHARE_NAME>`` placeholder
    so the operator fills it in — never auto-operated.
    """
    ops: list[dict] = []
    manual: list[dict] = []
    for a in action_rows or []:
        hits = shares_for_table(shares_by_table or {}, a.get("full_name"))
        if not hits:
            manual.append(a)
            continue
        for h in hits:
            ops.append(reshare_op(a, h["share_name"], h["alias"]))
    return ops, manual


def dry_run_preview(ops: Iterable[dict]) -> list[str]:
    """Human-readable dry-run plan lines (the exact statements) — no side effects."""
    lines: list[str] = []
    for op in ops or []:
        for stmt in op.get("statements") or []:
            lines.append(f"[DRY-RUN] {op.get('share_name')} :: {stmt}")
    return lines


# --------------------------------------------------------------------------- #
# Verification (post-convert): is the table present in the share after re-share?
# --------------------------------------------------------------------------- #

def reshare_verified(full_name: str, share_objects_after: Iterable[dict]) -> bool:
    """True when ``full_name`` is present among the share's objects after the re-add."""
    fn = (full_name or "").strip()
    for obj in share_objects_after or []:
        if str(obj.get("name") or "").strip() == fn:
            return True
    return False
