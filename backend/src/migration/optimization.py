"""Enable-Predictive-Optimization executor logic — pure planner + verifier.

The POST_CONVERT partner for ``ENABLE_PREDICTIVE_OPTIMIZATION`` actions. A converted (MANAGED)
table is maintained automatically by Predictive Optimization (PO) — Databricks recommends
retiring scheduled OPTIMIZE jobs under PO (see M1's ``RETIRE_MAINTENANCE_JOB``). Where the
customer had PO turned OFF and the operator opts in (``enable_po_on_convert=true``), this stage
turns it back on with the exact ``ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION`` statement.

Pure decisions live here:
  - PO OFF  + opted-in -> executable op (exact statement);
  - PO ON              -> skip (already maintained — no-op);
  - PO unknown (None)  -> MANUAL (degrade honestly: never assume ON, never auto-enable);
  - not opted-in       -> nothing executable (PO enable stays opt-in).

No Spark/SDK at import time — the thin edge lives in ``notebooks/08c_enable_optimization.py``,
which reads live PO via ``executor.read_po_enabled_for_table`` (added in M1). Verification reuses
``executor.po_enabled_from_describe`` through :func:`po_verified`.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from . import eligibility as _elig

PO_TABLE = "TABLE"
PO_SCHEMA = "SCHEMA"


def build_enable_po_sql(name: str, scope_kind: str = PO_TABLE) -> str:
    """The exact, executable ``ALTER {TABLE|SCHEMA} … ENABLE PREDICTIVE OPTIMIZATION`` statement.

    M2 emits table-grain only; ``scope_kind`` accepts ``PO_SCHEMA`` for the documented schema-grain
    follow-up (spec §10 Q2).
    """
    kw = PO_SCHEMA if str(scope_kind).strip().upper() == PO_SCHEMA else PO_TABLE
    return f"ALTER {kw} {name} ENABLE PREDICTIVE OPTIMIZATION;"


def po_op(scope_row: dict) -> dict:
    """One executable enable-PO op: table + the exact statement + passthrough identity."""
    full_name = scope_row.get("full_name")
    return {
        "full_name": full_name,
        "catalog": scope_row.get("catalog"),
        "schema": scope_row.get("schema"),
        "action_key": scope_row.get("action_key"),
        "action_id": scope_row.get("action_id"),
        "plan_run_id": scope_row.get("plan_run_id"),
        "wave_id": scope_row.get("wave_id"),
        "statement": build_enable_po_sql(full_name, PO_TABLE),
    }


def plan_enable_po(scope_rows: Iterable[dict], *, enable_po: bool):
    """Split converted-table scope rows into ``(ops, manual, skip)``.

    ``enable_po`` is the opt-in gate (``enable_po_on_convert``). When ``False``, PO enable stays
    opt-in: nothing is executable and nothing is surfaced as MANUAL — every row is ``skip``.
    When ``True``:
      - ``po_enabled is False`` -> executable op;
      - ``po_enabled is None``  -> MANUAL (unknown/unreadable PO — never auto-enable);
      - ``po_enabled is True``  -> skip (already PO-managed).
    """
    ops: list[dict] = []
    manual: list[dict] = []
    skip: list[dict] = []
    for r in scope_rows or []:
        po = r.get("po_enabled")
        if not enable_po:
            skip.append(r)
            continue
        if po is False:
            ops.append(po_op(r))
        elif po is None:
            manual.append(r)
        else:  # po is True
            skip.append(r)
    return ops, manual, skip


PO_DECLINED_DETAIL = (
    "operator declined PO (enable_po_on_convert=false): optimize step closed without enabling "
    "Predictive Optimization"
)


def optimize_stamp_rows(converted: Iterable[dict], *, enable_po: bool,
                        failed_full_names: Iterable[Optional[str]],
                        po_on_full_names: Iterable[Optional[str]]) -> list[dict]:
    """Manifest rows an EXECUTED 08c stamps: ``{"full_name", "optimize_po_declined"}``.

    ``converted`` rows carry ``full_name`` and the current ``optimize_run_at``. The stamp clears
    ``wave_status.optimize_pending``.

    - PO pass ran (``enable_po``): every table with no FAILED op (PO or auto-liquid), declined
      ``False`` — PO-unreadable and manual auto-liquid tables included (their ENABLE_* actions stay
      OPEN as runbook work). Re-stamps an already-stamped table, so a PO pass clears a decline.
    - PO declined (``enable_po=False``): every table with no FAILED op that is NOT yet stamped,
      declined unless it was read as already PO-on (``po_on_full_names``). An existing stamp is
      kept, so a declined run never undoes a PO pass.

    A FAILED table stays unstamped so optimize is offered again. Order-preserving, deduplicated,
    blanks dropped.
    """
    failed = {f for f in (failed_full_names or []) if f}
    po_on = {f for f in (po_on_full_names or []) if f}
    out: list[dict] = []
    seen: set[str] = set()
    for row in converted or []:
        fn = row.get("full_name")
        if not fn or fn in failed or fn in seen:
            continue
        seen.add(fn)
        if enable_po:
            out.append({"full_name": fn, "optimize_po_declined": False})
        elif row.get("optimize_run_at") is None:
            out.append({"full_name": fn, "optimize_po_declined": fn not in po_on})
    return out


def dry_run_preview(ops: Iterable[dict]) -> list[str]:
    """Human-readable dry-run plan lines (the exact statements) — no side effects."""
    return [f"[DRY-RUN] {op.get('full_name')} :: {op.get('statement')}" for op in ops or []]


def po_verified(po_enabled) -> bool:
    """True when live PO status confirms ENABLE (platform truth). ``None``/``False`` -> not verified.

    The notebook supplies the value from ``executor.read_po_enabled_for_table`` (which delegates to
    the pure ``executor.po_enabled_from_describe``).
    """
    return po_enabled is True


# --------------------------------------------------------------------------- #
# Auto-liquid clustering (M3) — CLUSTER BY AUTO, default-on for the eligible clean set (opt-out).
# --------------------------------------------------------------------------- #

AUTO_LIQUID_ENABLE = "enable_eligible"   # default: executable op for the clean set
AUTO_LIQUID_RECOMMEND = "recommend"      # advisory: persist statement, run nothing
AUTO_LIQUID_OFF = "off"                  # don't touch clustering at all

AL_ELIGIBLE = "ELIGIBLE"
AL_MANUAL = "MANUAL"
AL_SKIP = "SKIP"


def build_enable_auto_liquid_sql(name: str) -> str:
    """The exact, executable ``ALTER TABLE … CLUSTER BY AUTO`` statement (metadata-only)."""
    return f"ALTER TABLE {name} CLUSTER BY AUTO;"


def auto_liquid_eligibility(row: dict) -> tuple:
    """Pure gate: ``(verdict, reason)`` for auto-liquid on one converted table.

    verdict ∈ ``ELIGIBLE`` (clean set -> executable under ``enable_eligible``) | ``MANUAL``
    (surfaced/advisory, never auto-run) | ``SKIP`` (no-op / not applicable). Order matters — the
    first matching rule wins. Layout fields (``partition_columns`` / ``clustering_columns`` /
    ``cluster_by_auto`` / ``uniform_iceberg_enabled``) are the effective layout (live first,
    manifest per field, see ``effective_layout``); ``po_effective`` = PO is ON now or being enabled
    in this same 08c run; ``zorder_hints`` = validated ZORDER columns from ``DESCRIBE HISTORY``
    (``clustering_hints``) and ``zorder_found`` = a ZORDER was found at all. Layout degrades
    honestly: if EITHER ``partition_columns`` OR ``clustering_columns`` is ``None`` (unread), the
    table is ``layout_unknown`` (MANUAL) — an unread column is never assumed empty/clean.

    Automatic clustering needs PO, so ``po_off`` is checked before any ELIGIBLE path. Manual keys
    switch to AUTO (``manual_keys_to_auto``), which keeps them as the starting keys; a ZORDERed
    table gets its ZORDER columns as hints (``zorder_hints``), or plain AUTO when none of them is
    valid (``zorder_no_valid_hints``).
    """
    if row.get("cluster_by_auto") is True:
        return AL_SKIP, "already_auto"

    fmt = (row.get("data_source_format") or "").strip().upper()
    if not _elig.is_delta_format(fmt):
        if fmt == "ICEBERG":
            # Managed Iceberg v3 needs DBR 18.0+ -> surface, don't auto-apply (spec §6.2).
            return AL_MANUAL, "iceberg_needs_confirm"
        return AL_SKIP, "not_delta"

    if row.get("uniform_iceberg_enabled") is True:
        # UniForm (Iceberg reads) tables report format=DELTA; hold for confirmation (spec §6.2).
        return AL_MANUAL, "uniform_needs_confirm"

    pcs = row.get("partition_columns")
    ccs = row.get("clustering_columns")
    if pcs is None or ccs is None:
        # Partially/fully unread layout -> degrade honestly. ``if pcs:``/``if ccs:`` below
        # treat None as "no keys" (silently assumes clean); an unread partition/clustering
        # column must never be assumed empty.
        return AL_MANUAL, "layout_unknown"

    if pcs:  # non-empty partition list -> needs a REORG, excluded from the default (spec §4 N4).
        return AL_MANUAL, "partitioned_needs_reorg"
    if row.get("po_effective") is not True:  # auto-liquid depends on PO (spec §3.6).
        return AL_MANUAL, "po_off"
    if ccs:  # manual keys: CLUSTER BY AUTO keeps them as the starting keys (spike Q4).
        # (already_auto returned at the top-of-function guard, so cba is never True here.)
        if row.get("cluster_by_auto") is not False:
            # AUTO flag unread: the table may already be on AUTO, and a rollback would then turn
            # off an AUTO that predates 08c. Surface it instead of switching.
            return AL_MANUAL, "manual_keys_auto_unknown"
        return AL_ELIGIBLE, "manual_keys_to_auto"
    if row.get("zorder_hints"):  # PO doesn't ZORDER: carry the ZORDER columns over as hints.
        return AL_ELIGIBLE, "zorder_hints"
    if row.get("zorder_found") is True:
        return AL_ELIGIBLE, "zorder_no_valid_hints"
    return AL_ELIGIBLE, "clean"


def _quote_ident(name: str) -> str:
    """One identifier part in back-quotes, embedded back-quotes doubled."""
    return "`" + str(name).replace("`", "``") + "`"


def build_cluster_by_columns_sql(name: str, columns) -> str:
    """The exact ``ALTER TABLE … CLUSTER BY (cols)`` statement (metadata-only), columns quoted."""
    return f"ALTER TABLE {name} CLUSTER BY ({', '.join(_quote_ident(c) for c in columns)});"


def auto_liquid_statements(name: str, reason: str, hints=None) -> list:
    """The statements to run, in order. ``zorder_hints`` is the pair ``CLUSTER BY (hints)`` then
    ``CLUSTER BY AUTO`` (no single SQL form sets both: the grammar is exclusive); every other
    reason is its one remediation statement, or none."""
    if reason == "zorder_hints" and hints:
        return [build_cluster_by_columns_sql(name, hints), build_enable_auto_liquid_sql(name)]
    one = auto_liquid_remediation(name, reason)
    return [one] if one else []


def auto_liquid_remediation(name: str, reason: str) -> "Optional[str]":
    """The exact statement/advisory for one table+reason (goes on the action ``evidence``)."""
    if reason in ("clean", "manual_keys_to_auto", "manual_keys_auto_unknown",
                  "zorder_no_valid_hints", "po_off"):
        return build_enable_auto_liquid_sql(name)
    if reason == "partitioned_needs_reorg":
        return (f"ALTER TABLE {name} REPLACE PARTITIONED BY WITH CLUSTER BY AUTO; "
                "-- REORG/rewrite, DBR 18.1+; excluded from the default (advisory)")
    if reason == "iceberg_needs_confirm":
        return (f"ALTER TABLE {name} CLUSTER BY AUTO; "
                "-- managed Iceberg v3 requires DBR 18.0+ (advisory)")
    if reason == "uniform_needs_confirm":
        return (f"ALTER TABLE {name} CLUSTER BY AUTO; "
                "-- UniForm (Iceberg reads) table — confirm before applying (advisory)")
    return None  # layout_unknown -> no auto statement (verify manually)


def auto_liquid_op(scope_row: dict, reason: str) -> dict:
    """One auto-liquid op: table + the reason-specific statement + passthrough identity."""
    full_name = scope_row.get("full_name")
    hints = list(scope_row.get("zorder_hints") or []) if reason == "zorder_hints" else []
    statements = auto_liquid_statements(full_name, reason, hints)
    statement = "\n".join(statements) if statements else None
    return {
        "full_name": full_name,
        "catalog": scope_row.get("catalog"),
        "schema": scope_row.get("schema"),
        "action_key": scope_row.get("action_key"),
        "action_id": scope_row.get("action_id"),
        "plan_run_id": scope_row.get("plan_run_id"),
        "wave_id": scope_row.get("wave_id"),
        "reason": reason,
        "statement": statement,
        # Run in order, one spark.sql each (``statement`` is their text, for evidence + runbook).
        "statements": statements,
        "hints": hints,
        # The layout 08c saw before acting (``layout_state_json``), recorded as the audit
        # ``from_state`` so a rollback restores what was there, not the profile-time manifest.
        "pre_layout": scope_row.get("pre_layout"),
    }


def plan_enable_auto_liquid(scope_rows: Iterable[dict], *, policy: str):
    """Split converted-table scope rows into ``(ops, manual, skip)`` per ``auto_liquid_policy``.

    - ``off``            -> everything is ``skip`` (clustering untouched).
    - ``enable_eligible`` (default) -> ELIGIBLE rows become executable ``ops``.
    - anything else (``recommend`` OR an unknown/typo string) -> ELIGIBLE rows join MANUAL
      (statement surfaced, never auto-run). **Fail-closed**: only the explicit
      ``enable_eligible`` value can produce a mutating op, so a typo never silently runs
      ``ALTER TABLE … CLUSTER BY AUTO`` (project golden rule for a mutating gate).
    MANUAL verdicts (partitioned / iceberg / uniform / po_off / layout_unknown) are always
    ``manual``; SKIP verdicts (already_auto / not_delta) are always
    ``skip``.
    """
    ops: list = []
    manual: list = []
    skip: list = []
    pol = (policy or AUTO_LIQUID_ENABLE).strip()
    for r in scope_rows or []:
        if pol == AUTO_LIQUID_OFF:
            skip.append(r)
            continue
        verdict, reason = auto_liquid_eligibility(r)
        if verdict == AL_SKIP:
            skip.append(r)
        elif verdict == AL_MANUAL:
            manual.append(auto_liquid_op(r, reason))
        elif pol == AUTO_LIQUID_ENABLE:  # AL_ELIGIBLE + explicit enable_eligible -> executable
            ops.append(auto_liquid_op(r, reason))
        else:  # AL_ELIGIBLE + recommend / unknown policy -> fail-closed to MANUAL (never ops)
            manual.append(auto_liquid_op(r, reason))
    return ops, manual, skip


MAX_CLUSTERING_KEYS = 4
DEFAULT_STATS_COLUMNS = 32
# Column types a clustering key may have (Databricks docs), as `DESCRIBE TABLE EXTENDED … AS JSON`
# names them. Complex types (struct / map / array) and binary / boolean are not allowed.
CLUSTERING_KEY_TYPES = frozenset({
    "date", "timestamp", "timestamp_ltz", "timestamp_ntz", "string", "int", "integer", "bigint", "long",
    "smallint", "short", "tinyint", "byte", "float", "double", "decimal",
})


def zorder_columns_from_history(history_rows) -> "Optional[list]":
    """The newest non-empty ``OPTIMIZE … ZORDER BY`` columns in ``DESCRIBE HISTORY`` (newest first).

    ``operationParameters.zOrderBy`` is a JSON string (``'["c1","c2"]'``) inside the parameters
    (a JSON string or a dict). PO's own OPTIMIZE rows record ``"[]"``, so only a non-empty list
    counts. Returns ``[]`` when the history was read and holds no ZORDER, and ``None`` when it was
    not read (unknown, never "no ZORDER"). History retention bounds what can be found.
    """
    if history_rows is None:
        return None
    for r in history_rows:
        if str((r or {}).get("operation") or "").upper() != "OPTIMIZE":
            continue
        params = r.get("operationParameters")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(params, dict):
            continue
        z = params.get("zOrderBy")
        if isinstance(z, str):
            try:
                z = json.loads(z)
            except Exception:  # noqa: BLE001
                continue
        if isinstance(z, list) and z:
            return [str(c) for c in z if str(c).strip()]
    return []


def _leaf_names(columns, prefix=""):
    """Top-down leaf column names in schema order (struct fields flattened), for stats counting."""
    out = []
    for c in columns or []:
        name = f"{prefix}{c.get('name')}"
        t = c.get("type") or {}
        if (t.get("name") == "struct") and isinstance(t.get("fields"), list):
            out.extend(_leaf_names(t["fields"], prefix=name + "."))
        else:
            out.append(name)
    return out


def _stats_columns(columns, props) -> set:
    """Lower-cased top-level names of the columns Delta collects statistics for."""
    props = props or {}
    explicit = props.get("delta.dataSkippingStatsColumns")
    if explicit:
        return {p.strip().split(".")[0].lower() for p in str(explicit).split(",") if p.strip()}
    try:
        n = int(props.get("delta.dataSkippingNumIndexedCols", DEFAULT_STATS_COLUMNS))
    except (TypeError, ValueError):
        n = DEFAULT_STATS_COLUMNS
    leaves = _leaf_names(columns)
    covered = leaves if n < 0 else leaves[:n]
    return {name.lower() for name in covered if "." not in name}


def clustering_hints(zorder_columns, columns, table_properties):
    """``(hints, dropped)``: which ZORDER columns can be clustering keys, in order.

    A hint is kept only when it is a top-level column of a clustering-key type that has
    statistics, at most ``MAX_CLUSTERING_KEYS``; it takes the table's own spelling. Every other
    column is ``dropped`` with a reason (``nested`` / ``missing`` / ``unsupported_type`` /
    ``no_stats`` / ``duplicate`` / ``over_limit`` / ``schema_unread``). A miscount is safe: a key
    without statistics is rejected at ALTER time and the table is unchanged (spike Q6), and the
    executor then falls back to plain AUTO.
    """
    if not zorder_columns:
        return [], []
    if not columns:
        return [], [(c, "schema_unread") for c in zorder_columns]
    by_lower = {str(c.get("name")).lower(): c for c in columns}
    stats = _stats_columns(columns, table_properties)
    hints, dropped, seen = [], [], set()
    for z in zorder_columns:
        low = str(z).lower()
        if "." in low:
            dropped.append((z, "nested"))
            continue
        col = by_lower.get(low)
        if col is None:
            dropped.append((z, "missing"))
            continue
        if low in seen:
            dropped.append((z, "duplicate"))
            continue
        if ((col.get("type") or {}).get("name") or "").lower() not in CLUSTERING_KEY_TYPES:
            dropped.append((z, "unsupported_type"))
            continue
        if low not in stats:
            dropped.append((z, "no_stats"))
            continue
        if len(hints) >= MAX_CLUSTERING_KEYS:
            dropped.append((z, "over_limit"))
            continue
        seen.add(low)
        hints.append(col.get("name"))
    return hints, dropped


def compensation_statement(name: str, pre_layout: Optional[dict]) -> "Optional[str]":
    """The statement that puts a table back to ``pre_layout`` after a half-applied hint pair.

    Prior manual keys -> ``CLUSTER BY (those keys)``; no keys and not auto -> ``CLUSTER BY NONE``;
    already auto or an unknown pre-state -> ``None`` (nothing safe to run).
    """
    if not isinstance(pre_layout, dict) or pre_layout.get("cluster_by_auto") is True:
        return None
    keys = pre_layout.get("clustering_columns")
    if keys:
        return build_cluster_by_columns_sql(name, keys)
    if keys == [] and pre_layout.get("cluster_by_auto") in (False, None):
        return f"ALTER TABLE {name} CLUSTER BY NONE;"
    return None


def _error_class(err) -> str:
    """The ``[ERROR_CLASS]`` a Databricks error message starts with, else the exception type."""
    import re
    m = re.search(r"\[([A-Z][A-Z0-9_.]+)\]", str(err))
    return m.group(1) if m else type(err).__name__


def execute_auto_liquid_op(op: dict, *, run_sql, read_layout) -> dict:
    """Run one auto-liquid op's statements and verify (orchestration only; I/O is injected).

    ``run_sql(statement)`` raises on failure; ``read_layout()`` returns the live layout dict. For
    the ZORDER pair: statement 1 rejected (e.g. a key without statistics, which leaves the table
    unchanged) -> fall back to plain ``CLUSTER BY AUTO``, recorded as ``hints_rejected``; statement
    2 failing after statement 1 -> undo with ``compensation_statement`` and report FAILED, with
    ``compensated`` = whether the re-read layout matches the pre-state. Verified = live
    ``clusterByAuto`` is true after the run.
    """
    res = {"op": op, "status": "CLUSTERED", "verified": False, "error": None, "evidence": None,
           "post_layout": None, "statements_run": [], "fallback": False, "compensated": None}
    statements = list(op.get("statements") or [])
    notes = []
    try:
        pre = json.loads(op["pre_layout"]) if op.get("pre_layout") else None
    except Exception:  # noqa: BLE001
        pre = None
    pair = op.get("reason") == "zorder_hints" and len(statements) == 2
    try:
        if pair:
            try:
                run_sql(statements[0])
                res["statements_run"].append(statements[0])
            except Exception as e1:  # noqa: BLE001 — table unchanged; plain AUTO instead
                res["fallback"] = True
                notes.append(f"hints_rejected={_error_class(e1)}")
            try:
                run_sql(statements[1])
                res["statements_run"].append(statements[1])
            except Exception as e2:  # noqa: BLE001
                if res["statements_run"]:  # the keys landed but AUTO did not: undo them
                    undo = compensation_statement(op.get("full_name"), pre)
                    res["compensated"] = False
                    if undo:
                        try:
                            run_sql(undo)
                            res["statements_run"].append(undo)
                        except Exception as e3:  # noqa: BLE001
                            notes.append(f"undo_failed={_error_class(e3)}")
                raise e2
        else:
            for stmt in statements:
                run_sql(stmt)
                res["statements_run"].append(stmt)
    except Exception as e:  # noqa: BLE001
        res["status"] = "FAILED"
        res["error"] = (str(e)[:380] + (" :: " + "; ".join(notes) if notes else ""))[:400]
    try:
        post = read_layout()
    except Exception:  # noqa: BLE001
        post = None
    res["post_layout"] = post
    cba = (post or {}).get("cluster_by_auto")
    if res["status"] == "FAILED":
        if res["compensated"] is False and post is not None and pre is not None:
            res["compensated"] = (post.get("cluster_by_auto") is not True
                                  and (post.get("clustering_columns") or []) == (pre.get("clustering_columns") or []))
        return res
    res["verified"] = auto_liquid_verified(cba)
    ev = f"cluster_by_auto={cba!r}"
    if pair:
        ev += f" clustering_columns={(post or {}).get('clustering_columns')!r}"
    res["evidence"] = " ".join([ev, *notes])
    return res


def auto_liquid_counts(results) -> dict:
    """Telemetry counts for executed auto-liquid ops (never raises): ``hinted`` = clustered with
    its ZORDER hints, ``switched`` = manual keys switched to AUTO, ``hint_fallback`` = hints
    rejected, plain AUTO used."""
    out = {"hinted": 0, "switched": 0, "hint_fallback": 0}
    for r in results or []:
        try:
            if not isinstance(r, dict) or r.get("status") != "CLUSTERED":
                continue
            reason = (r.get("op") or {}).get("reason")
            if r.get("fallback"):
                out["hint_fallback"] += 1
            elif reason == "zorder_hints":
                out["hinted"] += 1
            elif reason == "manual_keys_to_auto":
                out["switched"] += 1
        except Exception:  # noqa: BLE001
            continue
    return out


LAYOUT_FIELDS = ("partition_columns", "clustering_columns", "cluster_by_auto")


def effective_layout(manifest_row: dict, live: Optional[dict]):
    """``(layout, source)``: the layout to plan on, live value first, manifest per field.

    The manifest layout is read at profile time, while the table was still EXTERNAL; the live
    read (``executor.read_layout_for_table``) is what the table is now. A field the live read
    could not determine (``None``) falls back to the manifest value, so an unread layout stays
    unknown rather than assumed clean. ``source`` is ``live`` / ``manifest`` / ``mixed``.
    """
    live = live or {}
    out, used_live = {}, []
    for f in LAYOUT_FIELDS:
        lv = live.get(f)
        if lv is not None:
            out[f] = lv
            used_live.append(True)
        else:
            out[f] = (manifest_row or {}).get(f)
            used_live.append(False)
    source = "live" if all(used_live) else ("manifest" if not any(used_live) else "mixed")
    return out, source


def layout_state_json(layout: dict, source: str) -> str:
    """The layout as stable JSON (sorted keys) for the audit ``from_state`` / ``to_state``."""
    body = {f: (layout or {}).get(f) for f in LAYOUT_FIELDS}
    body["source"] = source
    return json.dumps(body, sort_keys=True)


def already_auto_close_keys(scope_rows: Iterable[dict], *, open_keys) -> list:
    """Open ``ENABLE_AUTO_LIQUID`` action keys to close because the table is on AUTO right now.

    Only a LIVE ``clusterByAuto`` of ``True`` counts (``cluster_by_auto_live``); a profile-time
    manifest value or an unreadable flag never closes an action. ``open_keys`` = the scope's
    OPEN / IN_PROGRESS action keys of that kind.
    """
    open_keys = set(open_keys or ())
    return [r["action_key"] for r in scope_rows or []
            if r.get("cluster_by_auto_live") is True and r.get("action_key") in open_keys]


def auto_liquid_verified(cluster_by_auto) -> bool:
    """True when live ``clusterByAuto`` confirms the enable. ``None``/``False`` -> not verified."""
    return cluster_by_auto is True


# --------------------------------------------------------------------------- #
# Finalize reversal (RB-M2) — reverse 08c's one-way deltas on rollback, fail-closed.
# A converted table returned to EXTERNAL is no longer PO-maintained, so 08c's
# proactively-enabled PO / auto-liquid should be reversed to the captured pre-migration
# state — but ONLY where the audit log proves 08c applied it, the live state still shows
# it on, and (auto-liquid) the manifest pre-migration layout was clean. Metadata-only
# statements (docs: DISABLE PREDICTIVE OPTIMIZATION / CLUSTER BY NONE never rewrite data).
# --------------------------------------------------------------------------- #

REVERT = "REVERT"     # executable reversal op
RV_MANUAL = "MANUAL"  # surfaced statement, never auto-run
RV_SKIP = "SKIP"      # no-op / not applicable

REVERSE_PO = "REVERSE_PO"
REVERSE_AUTO_LIQUID = "REVERSE_AUTO_LIQUID"


def build_disable_po_sql(name: str) -> str:
    """The exact, executable ``ALTER TABLE … DISABLE PREDICTIVE OPTIMIZATION`` statement."""
    return f"ALTER TABLE {name} DISABLE PREDICTIVE OPTIMIZATION;"


def reverse_po_decision(row: dict) -> tuple:
    """Pure ``(verdict, reason)`` for reversing a PO enable on one rolled-back table.

    ``finalized_po`` = a SUCCESS ``ENABLE_PREDICTIVE_OPTIMIZATION`` audit row exists (08c turned
    PO on; by 08c construction the pre-migration state was PO-OFF). ``po_live`` = current PO status
    (``executor.read_po_enabled_for_table``; ``None`` = unreadable). Fail-closed: revert ONLY when
    finalized AND still live-on; unknown live status stays MANUAL.
    """
    if not row.get("finalized_po"):
        return RV_SKIP, "not_finalized"
    live = row.get("po_live")
    if live is None:
        return RV_MANUAL, "po_unknown"
    if live is False:
        return RV_SKIP, "already_off"
    return REVERT, "revert_to_prior_off"


def po_reversal_verified(po_live) -> bool:
    """True when live PO status confirms the DISABLE landed (``False``). ``None``/``True`` -> not verified."""
    return po_live is False


def build_disable_auto_liquid_sql(name: str) -> str:
    """The exact, executable ``ALTER TABLE … CLUSTER BY NONE`` statement (metadata-only; docs:
    does not rewrite clustered data, only stops future OPTIMIZE from clustering)."""
    return f"ALTER TABLE {name} CLUSTER BY NONE;"


def _parse_layout_state(text) -> Optional[dict]:
    """A ``layout_state_json`` string back to a dict (``None`` when absent or unreadable)."""
    if not text:
        return None
    try:
        obj = json.loads(text) if isinstance(text, str) else text
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def auto_liquid_rollback_facts(audit_rows, *, since: Optional[dict] = None) -> dict:
    """Per ``full_name``: what 08c did to the layout, from its ``ENABLE_AUTO_LIQUID`` audit rows.

    Only ``SUCCESS`` / ``FAILED`` rows count (``SKIPPED`` = 08c changed nothing). ``finalized`` = a
    SUCCESS row exists; ``recorded_pre`` = the ``from_state`` of the earliest row that has one (the
    layout before 08c first acted; rows from before M1 have none); ``residue`` = a FAILED row whose
    re-read ``to_state`` shows keys or AUTO that its ``from_state`` did not have (a pair that
    failed and could not be undone). ``since`` (``full_name`` -> the table's latest convert time)
    drops rows from an earlier convert cycle, so a rollback never acts on a stale pre-state.
    """
    out: dict = {}
    since = since or {}
    rows = [r for r in (audit_rows or [])
            if (r or {}).get("action") == "ENABLE_AUTO_LIQUID" and r.get("phase") in ("SUCCESS", "FAILED")
            and not (since.get(r.get("full_name")) is not None
                     and str(r.get("created_at") or "") < str(since[r.get("full_name")]))]
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    for r in rows:
        fn = r.get("full_name")
        f = out.setdefault(fn, {"finalized": False, "recorded_pre": None, "residue": False})
        pre = _parse_layout_state(r.get("from_state"))
        if f["recorded_pre"] is None and pre is not None:
            f["recorded_pre"] = pre
        if r.get("phase") == "SUCCESS":
            f["finalized"] = True
        else:
            post = _parse_layout_state(r.get("to_state"))
            if post is not None and pre is not None and (
                    post.get("cluster_by_auto") is True
                    or (post.get("clustering_columns") or []) != (pre.get("clustering_columns") or [])):
                f["residue"] = True
    return out


def reverse_auto_liquid_decision(row: dict) -> tuple:
    """Pure ``(verdict, reason)`` for reversing 08c's clustering change on one rolled-back table.

    ``finalized_auto_liquid`` = a SUCCESS ``ENABLE_AUTO_LIQUID`` audit row exists;
    ``residue_auto_liquid`` = a FAILED pair left keys or AUTO behind
    (``auto_liquid_rollback_facts``). ``cluster_by_auto_live`` / ``clustering_columns_live`` =
    the current layout (``None`` = unreadable). The prior layout is ``recorded_pre_layout`` (what
    08c saw before acting, from the audit ``from_state``) when present, else the manifest
    PROFILE-time ``partition_columns`` / ``clustering_columns`` / ``cluster_by_auto`` (rows from
    before M1). Fail-closed, first match wins:

    - not finalized and no residue -> SKIP ``not_finalized``; live unreadable -> MANUAL ``cba_unknown``;
    - prior unread -> MANUAL ``layout_unknown``; prior AUTO -> MANUAL ``prior_auto_conflict``;
      prior partitioned -> MANUAL ``prior_explicit_layout``;
    - prior manual keys: recorded -> REVERT ``restore_prior_keys`` (``CLUSTER BY (keys)``), or SKIP
      ``already_restored`` when live already matches; manifest-only -> MANUAL ``prior_explicit_layout``;
    - prior none: live AUTO or live keys -> REVERT ``revert_to_prior_none`` (``CLUSTER BY NONE``),
      else SKIP ``already_off``.
    """
    if not row.get("finalized_auto_liquid") and not row.get("residue_auto_liquid"):
        return RV_SKIP, "not_finalized"
    live = row.get("cluster_by_auto_live")
    live_ccs = row.get("clustering_columns_live")
    if live is None:
        return RV_MANUAL, "cba_unknown"
    recorded = row.get("recorded_pre_layout")
    prior = recorded if isinstance(recorded, dict) else row
    pcs = prior.get("partition_columns")
    ccs = prior.get("clustering_columns")
    if pcs is None or ccs is None:
        return RV_MANUAL, "layout_unknown"
    if prior.get("cluster_by_auto") is True:
        return RV_MANUAL, "prior_auto_conflict"
    if pcs:
        return RV_MANUAL, "prior_explicit_layout"
    if ccs:
        if not isinstance(recorded, dict):
            return RV_MANUAL, "prior_explicit_layout"
        if live is True:
            return REVERT, "restore_prior_keys"
        if live_ccs is None:  # AUTO off but the keys could not be read: never assume them
            return RV_MANUAL, "layout_unknown"
        if list(live_ccs) == list(ccs):
            return RV_SKIP, "already_restored"
        return REVERT, "restore_prior_keys"
    if live is True:
        return REVERT, "revert_to_prior_none"
    if not isinstance(recorded, dict):
        return RV_SKIP, "already_off"  # rows from before M1: today's rule, keys 08c never set stay
    if live_ccs is None:
        return RV_MANUAL, "layout_unknown"
    if live_ccs:  # a failed pair left keys behind
        return REVERT, "revert_to_prior_none"
    return RV_SKIP, "already_off"


def auto_liquid_reversal_verified(cluster_by_auto_live) -> bool:
    """True when live ``clusterByAuto`` confirms the CLUSTER BY NONE landed (``False``).
    ``None``/``True`` -> not verified."""
    return cluster_by_auto_live is False


def auto_liquid_reversal_verified_layout(op: dict, live_layout: Optional[dict]) -> bool:
    """A reversal is verified when the re-read shows AUTO off AND exactly the expected keys
    (``op["expected_keys"]``: ``[]`` after ``CLUSTER BY NONE``, the prior keys after a restore)."""
    if not isinstance(live_layout, dict):
        return False
    return (live_layout.get("cluster_by_auto") is False
            and list(live_layout.get("clustering_columns") or []) == list(op.get("expected_keys") or []))


def _reverse_op(scope_row: dict, statement: str, reason: str, kind: str) -> dict:
    """One reversal op: table + the exact statement + passthrough identity + kind/reason."""
    return {
        "full_name": scope_row.get("full_name"),
        "catalog": scope_row.get("catalog"),
        "schema": scope_row.get("schema"),
        "plan_run_id": scope_row.get("plan_run_id"),
        "wave_id": scope_row.get("wave_id"),
        "kind": kind,
        "reason": reason,
        "statement": statement,
    }


def plan_reverse_po(scope_rows: Iterable[dict], *, reconcile: bool):
    """Split rolled-back scope rows into ``(ops, manual, skip)`` for PO reversal.

    **Fail-closed gate**: when ``reconcile`` is False, every row is ``skip`` (reconcile stays
    opt-out-able; nothing mutating and nothing surfaced). When True: ``REVERT`` -> executable
    ``ops``; ``RV_MANUAL`` -> ``manual`` (statement surfaced, never auto-run); ``RV_SKIP`` -> ``skip``.
    """
    ops: list = []
    manual: list = []
    skip: list = []
    for r in scope_rows or []:
        if not reconcile:
            skip.append(r)
            continue
        verdict, reason = reverse_po_decision(r)
        op = _reverse_op(r, build_disable_po_sql(r.get("full_name")), reason, REVERSE_PO)
        if verdict == REVERT:
            ops.append(op)
        elif verdict == RV_MANUAL:
            manual.append(op)
        else:
            skip.append(r)
    return ops, manual, skip


def plan_reverse_auto_liquid(scope_rows: Iterable[dict], *, reconcile: bool):
    """Split rolled-back scope rows into ``(ops, manual, skip)`` for auto-liquid reversal.

    Same fail-closed gate as :func:`plan_reverse_po`: ``reconcile=False`` -> everything ``skip``;
    ``reconcile=True`` -> ``REVERT`` executable, ``RV_MANUAL`` surfaced, ``RV_SKIP`` skipped.
    """
    ops: list = []
    manual: list = []
    skip: list = []
    for r in scope_rows or []:
        if not reconcile:
            skip.append(r)
            continue
        verdict, reason = reverse_auto_liquid_decision(r)
        recorded = r.get("recorded_pre_layout") if isinstance(r.get("recorded_pre_layout"), dict) else {}
        keys = list(recorded.get("clustering_columns") or []) if reason in (
            "restore_prior_keys", "already_restored") else []
        stmt = (build_cluster_by_columns_sql(r.get("full_name"), keys) if reason == "restore_prior_keys"
                else build_disable_auto_liquid_sql(r.get("full_name")))
        op = _reverse_op(r, stmt, reason, REVERSE_AUTO_LIQUID)
        op["expected_keys"] = keys
        if verdict == REVERT:
            ops.append(op)
        elif verdict == RV_MANUAL:
            manual.append(op)
        else:
            skip.append(r)
    return ops, manual, skip


def finalized_full_names(audit_rows, *, action: str, success_phase: str = "SUCCESS") -> set:
    """The set of ``full_name``s with a SUCCESS finalize audit row for ``action`` — durable
    evidence that 08c actually applied+verified this delta (the fail-closed source of truth for
    reversal). Pure: the notebook supplies ``row.asDict()`` dicts from ``audit_log``.
    """
    out: set = set()
    for r in audit_rows or []:
        d = r if isinstance(r, dict) else {}
        if d.get("action") == action and d.get("phase") == success_phase and d.get("full_name"):
            out.add(d["full_name"])
    return out
