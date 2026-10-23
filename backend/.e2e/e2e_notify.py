#!/usr/bin/env python3
"""Gated live E2E for the L5 notify/comms surfaces — the "how does an operator learn about pending
work" layer of the e2e suite (rows N1–N3).

These three surfaces are PULL/EXPORT, not push (no webhook/email is sent by the tool):

  * N2 ``comms_feed``     — per-wave-member owner advisory, written by the wave planner (02);
  * N1 digest            — open blocking actions + stale freezes (``migration.digest.build_digest``);
  * N3 runbook export    — phase-ordered manual statements (``migration.runbook.build_runbook``)
                            plus the ``migration_runbook`` view.

Rather than trigger the off-by-default ``09_cockpit`` (``manual=digest`` / ``export_runbook``) edge —
which is a manual notebook op, NOT a deployed job — this driver reads the live control tables via the
warehouse and re-runs the SAME pure builders that edge uses, then classifies with the pure oracles in
``migration.real_estate`` (``comms_feed_outcome`` / ``digest_reflects_estate`` /
``runbook_reflects_estate``). That is an L3-style cross-surface oracle: "the pure builder over the
live estate produces a digest/runbook/comms feed that reflects the estate's real actions & warnings",
and it needs no new deployed job. Read-only: it reads control tables and (only with
``--write-volume``) writes artifacts to its own ``e2e_artifacts`` UC Volume (created if missing; never
the ``genie_knowledge`` volume, which the Genie Agent indexes as knowledge) — it NEVER runs
``SET MANAGED``, pauses a job, or mutates a user table.

Every surface degrades to WARN on a quiet/clean estate or a grants/runtime limitation — never a false
FAIL on an estate that simply has nothing pending.

Prereq (a populated control plane — profiled + planned, so comms_feed/actions/ledger have rows):
  python .e2e/seed_real_estate.py --preset populate   # or any run that reaches `plan`

Env (required, shared with the other .e2e drivers; see .env.e2e.example):
  DATABRICKS_CONFIG_PROFILE  (required)
  E2E_WAREHOUSE_ID           (required)
  E2E_CATALOG                (required)
  E2E_SCHEMA                 (control schema; default migration)

Usage:
  python .e2e/e2e_notify.py --comms            # N2: comms_feed advisory reflects the plan
  python .e2e/e2e_notify.py --digest           # N1: build_digest reflects the estate's blockers
  python .e2e/e2e_notify.py --runbook          # N3: build_runbook == migration_runbook view
  python .e2e/e2e_notify.py --all              # all three
  python .e2e/e2e_notify.py --all --scope <catalog.schema>       # narrow to one schema
  python .e2e/e2e_notify.py --all --write-volume                 # also prove the export write path
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# Import the pure builders + oracles from the backend package (no Spark needed — all pure).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import actions as ax  # noqa: E402
from migration import digest as dg  # noqa: E402
from migration import executor as ex  # noqa: E402
from migration import genie_space as gs  # noqa: E402
from migration import lineage as lin  # noqa: E402
from migration import real_estate as re_  # noqa: E402
from migration import runbook as rb  # noqa: E402

import _e2e_env  # noqa: E402

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
WAREHOUSE = _e2e_env.require("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.require("E2E_CATALOG")
CONTROL_SCHEMA = os.environ.get("E2E_SCHEMA", "migration")

w = WorkspaceClient(profile=PROFILE)
_failures: list[str] = []
_warnings: list[str] = []


# --------------------------------------------------------------------------- #
# assertion helpers (mirror the other .e2e drivers)
# --------------------------------------------------------------------------- #
def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def warn(label: str, detail: str = "") -> None:
    print(f"  [WARN] {label}{(' — ' + detail) if detail else ''}")
    _warnings.append(label)


def record(tag: str, label: str, msg: str) -> None:
    """Route a pure ``(tag, message)`` oracle outcome to the driver's PASS/WARN/FAIL tally."""
    tag = (tag or "").upper()
    if tag == "PASS":
        check(label, True, msg)
    elif tag == "FAIL":
        check(label, False, msg)
    else:
        warn(label, msg)


# --------------------------------------------------------------------------- #
# ground-truth SQL edge — read-only reads (verbatim polling pattern from the other drivers)
# --------------------------------------------------------------------------- #
def _execute(statement: str):
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, statement=statement, wait_timeout="50s")
    sid = resp.statement_id
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        resp = w.statement_execution.get_statement(sid)
    st = resp.status.state if resp.status else None
    if st != StatementState.SUCCEEDED:
        err = resp.status.error.message if (resp.status and resp.status.error) else st
        raise RuntimeError(f"SQL failed [{st}]: {err}\n  {statement}")
    return resp


def sql(statement: str) -> list[list]:
    resp = _execute(statement)
    return (resp.result.data_array if resp.result else None) or []


def sql_dicts(statement: str) -> list[dict]:
    """``SELECT *``-style read that returns rows as dicts keyed by the result column names, so the
    pure builders (which consume ``asDict()``-shaped rows in the notebooks) get the same shape here.
    """
    resp = _execute(statement)
    cols = []
    if resp.manifest and resp.manifest.schema and resp.manifest.schema.columns:
        cols = [c.name for c in resp.manifest.schema.columns]
    data = (resp.result.data_array if resp.result else None) or []
    return [dict(zip(cols, row)) for row in data]


def _to_dt(v):
    """Coerce a warehouse TIMESTAMP (returned as a STRING by the statement-execution API) to a
    ``datetime`` so the pure builders — which in the notebooks receive real ``asDict()`` timestamps
    — get the same shape. ``None``/blank/unparseable → ``None`` (treated as not-stale downstream).
    """
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:19].replace("T", " "))
        except ValueError:
            return None


def _scope_pred(scope: str | None, *, cat_col: str = "catalog", sch_col: str = "schema") -> str:
    """A ``catalog.schema`` filter as a leading WHERE clause (blank = all in-scope)."""
    if scope and "." in scope:
        c, s = scope.split(".", 1)
        return f" WHERE `{cat_col}` = '{c}' AND `{sch_col}` = '{s}'"
    return ""


def _read_builder_inputs(scope: str | None, *, runbook: bool = False
                         ) -> tuple[list[dict], list[dict], list[dict]]:
    """The SAME three inputs the ``09_cockpit`` digest/export_runbook edge reads: actions,
    freeze ledger, and manifest blocker rows. Each read degrades to [] (WARN downstream).
    ``runbook=True`` reads actions in ``RUNBOOK_ACTION_STATUSES`` (incl. DEFERRED, like the
    export and the view); the digest keeps the OPEN/IN_PROGRESS ``action_queue_open`` read."""
    pred = _scope_pred(scope)

    def _rows(fqn: str, extra: str = "") -> list[dict]:
        try:
            return sql_dicts(f"SELECT * FROM {fqn}{extra}")
        except Exception as e:  # noqa: BLE001
            warn(f"read {fqn} failed (grants/runtime) — treated as empty", str(e)[:120])
            return []

    if runbook:
        act_statuses = ", ".join(f"'{s}'" for s in ax.RUNBOOK_ACTION_STATUSES)
        act = _rows(ax.actions_fqn(CATALOG, CONTROL_SCHEMA),
                    (pred + " AND" if pred else " WHERE") + f" status IN ({act_statuses})")
    else:
        act = _rows(ax.action_queue_open_view_fqn(CATALOG, CONTROL_SCHEMA), pred)
    led = _rows(ex.freeze_ledger_fqn(CATALOG, CONTROL_SCHEMA), pred)
    # The stale-freeze age math (digest.freeze_staleness) needs a real datetime, but the
    # statement-execution API returns TIMESTAMP columns as strings — coerce the freeze timestamps.
    for lr in led:
        for col in ("frozen_at", "unfrozen_at", "rolled_back_at"):
            if col in lr:
                lr[col] = _to_dt(lr.get(col))
    blk_pred = (pred + " AND" if pred else " WHERE") + " blockers IS NOT NULL"
    blk = _rows(f"{CATALOG}.{CONTROL_SCHEMA}.table_manifest", blk_pred)
    return act, led, blk


# --------------------------------------------------------------------------- #
# N2 — comms_feed (owner advisory written by the wave planner)
# --------------------------------------------------------------------------- #
def check_comms(scope: str | None) -> None:
    print(f"N2 comms_feed — {lin.comms_feed_fqn(CATALOG, CONTROL_SCHEMA)}")
    fqn = lin.comms_feed_fqn(CATALOG, CONTROL_SCHEMA)
    # comms_feed has no catalog/schema columns — scope by full_name prefix when narrowing.
    where = ""
    label = "estate (all waves)"
    if scope and "." in scope:
        where = f" WHERE full_name LIKE '{scope}.%'"
        label = scope
    try:
        rows = sql(
            "SELECT COUNT(*), "
            "COUNT(CASE WHEN owner_notice IS NOT NULL AND trim(owner_notice) <> '' THEN 1 END) "
            f"FROM {fqn}{where}")
    except Exception as e:  # noqa: BLE001
        warn("N2 comms_feed read failed (grants/runtime) — inconclusive", str(e)[:120])
        return
    total = int(rows[0][0]) if rows and rows[0][0] is not None else 0
    with_notice = int(rows[0][1]) if rows and rows[0][1] is not None else 0
    tag, msg = re_.comms_feed_outcome(total_rows=total, with_notice=with_notice, scope_label=label)
    record(tag, "N2 comms_feed reflects the plan's owner advisory", msg)


# --------------------------------------------------------------------------- #
# N1 — digest (open blocking actions + stale freezes)
# --------------------------------------------------------------------------- #
def check_digest(scope: str | None) -> None:
    print("N1 digest — build_digest over live action-queue / freeze-ledger / manifest-blockers")
    act, led, blk = _read_builder_inputs(scope)
    digest = dg.build_digest(act, led, blk, now=datetime.now(timezone.utc))
    text = dg.to_text(digest, title=f"Migration digest — scope={scope or 'ALL'}")
    # Independent ground truth: count open BLOCKING rows straight from the action queue (the digest
    # must not under-report these). This is the load-bearing directional cross-check.
    open_blocking = sum(1 for a in act if a.get("blocking"))
    tag, msg = re_.digest_reflects_estate(
        blocking_count=int(digest.get("blocking_count", 0)),
        stale_freeze_count=int(digest.get("stale_freeze_count", 0)),
        open_blocking_actions=open_blocking,
        text_rendered=bool(text and text.strip()),
    )
    record(tag, "N1 digest reflects the estate's open blockers + stale freezes", msg)
    print(f"  [INFO] digest: {digest.get('blocking_count', 0)} blocking / "
          f"{digest.get('stale_freeze_count', 0)} stale freeze / "
          f"{digest.get('total_open_statements', 0)} total open; "
          f"{open_blocking} raw open-blocking action(s)")


# --------------------------------------------------------------------------- #
# N3 — runbook export (build_runbook == migration_runbook view, phase-ordered)
# --------------------------------------------------------------------------- #
def check_runbook(scope: str | None) -> None:
    print("N3 runbook export — build_runbook over live reads vs. migration_runbook view")
    act, led, blk = _read_builder_inputs(scope, runbook=True)
    try:
        rdr_pred = (_scope_pred(scope) + " AND" if _scope_pred(scope) else " WHERE")
        rdr = sql_dicts(f"SELECT * FROM {CATALOG}.{CONTROL_SCHEMA}.table_manifest"
                        f"{rdr_pred} {rb.REDIRECT_ROWS_PREDICATE}")
    except Exception as e:  # noqa: BLE001
        warn("N3 redirect opt-in read failed — treated as empty", str(e)[:120])
        rdr = []
    entries = rb.build_runbook(act, led, blk, redirect_rows=rdr)
    phase_ordered = re_.phase_ranks_nondecreasing(e.get("phase_rank") for e in entries)

    # migration_runbook view parity (None when unavailable — grants/runtime → WARN, not FAIL).
    view_rows: int | None
    try:
        vpred = _scope_pred(scope)
        vrows = sql(f"SELECT COUNT(*) FROM {rb.runbook_view_fqn(CATALOG, CONTROL_SCHEMA)}{vpred}")
        view_rows = int(vrows[0][0]) if vrows and vrows[0][0] is not None else 0
    except Exception as e:  # noqa: BLE001
        warn("N3 migration_runbook view read failed — parity skipped", str(e)[:120])
        view_rows = None

    tag, msg = re_.runbook_reflects_estate(
        builder_entries=len(entries), view_rows=view_rows, phase_ordered=phase_ordered)
    record(tag, "N3 runbook export reflects the estate (builder == view, phase-ordered)", msg)

    # to_sql_script / to_markdown must render a non-empty artifact when there are statements.
    if entries:
        sql_text = rb.to_sql_script(entries, header="e2e_notify runbook export")
        md_text = rb.to_markdown(entries, title="e2e_notify runbook export")
        check("N3 runbook renders a non-empty .sql + .md export",
              bool(sql_text.strip()) and bool(md_text.strip()),
              f"{len(sql_text)} sql chars / {len(md_text)} md chars")
        _maybe_write_volume(scope, sql_text, md_text, dg.to_text(
            dg.build_digest(act, led, blk, now=datetime.now(timezone.utc))))


# --------------------------------------------------------------------------- #
# optional: prove the export write path (write to the e2e_artifacts Volume + read back).
# Never the genie_knowledge volume: the Genie Agent indexes every file there as knowledge.
# --------------------------------------------------------------------------- #
_WRITE_VOLUME = False
ARTIFACT_VOLUME = "e2e_artifacts"


def _maybe_write_volume(scope: str | None, sql_text: str, md_text: str, digest_text: str) -> None:
    if not _WRITE_VOLUME:
        return
    assert ARTIFACT_VOLUME != gs.KNOWLEDGE_VOLUME
    try:
        sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{CONTROL_SCHEMA}.{ARTIFACT_VOLUME}")
    except Exception as e:  # noqa: BLE001
        warn(f"N3 could not create the {ARTIFACT_VOLUME} Volume (grants)", str(e)[:140])
        return
    base = f"/Volumes/{CATALOG}/{CONTROL_SCHEMA}/{ARTIFACT_VOLUME}/e2e_notify"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = (scope or "all").replace(".", "_")
    artifacts = {
        f"{base}/runbook_{tag}_{stamp}.sql": sql_text,
        f"{base}/runbook_{tag}_{stamp}.md": md_text,
        f"{base}/digest_{tag}_{stamp}.txt": digest_text,
    }
    for path, body in artifacts.items():
        try:
            w.files.upload(path, io.BytesIO(body.encode("utf-8")), overwrite=True)
            meta = w.files.get_metadata(path)
            size = getattr(meta, "content_length", None)
            check(f"N3 wrote + read back {os.path.basename(path)} on the {ARTIFACT_VOLUME} Volume",
                  size is None or size > 0, f"{path} ({size} bytes)")
        except Exception as e:  # noqa: BLE001
            warn(f"N3 Volume write path {os.path.basename(path)} unavailable (grants/runtime)",
                 str(e)[:140])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live L5 notify/comms e2e — comms_feed / digest / runbook export (read-only).")
    ap.add_argument("--comms", action="store_true", help="N2: comms_feed owner advisory.")
    ap.add_argument("--digest", action="store_true", help="N1: build_digest reflects the estate.")
    ap.add_argument("--runbook", action="store_true", help="N3: build_runbook == migration_runbook.")
    ap.add_argument("--all", action="store_true", help="Run N1 + N2 + N3.")
    ap.add_argument("--scope", default=None, help="Narrow to one catalog.schema (blank = all).")
    ap.add_argument("--write-volume", action="store_true",
                    help="Also write the digest/runbook artifacts to the e2e_artifacts Volume.")
    args = ap.parse_args()

    run_comms = args.comms or args.all
    run_digest = args.digest or args.all
    run_runbook = args.runbook or args.all
    if not (run_comms or run_digest or run_runbook):
        print("Pass --comms / --digest / --runbook (or --all). See the module docstring.")
        return 2

    global _WRITE_VOLUME
    _WRITE_VOLUME = args.write_volume

    print(f"Control plane: {CATALOG}.{CONTROL_SCHEMA} via warehouse {WAREHOUSE}"
          f"{('  scope=' + args.scope) if args.scope else ''}")
    first = True
    for enabled, fn in ((run_comms, check_comms), (run_digest, check_digest),
                        (run_runbook, check_runbook)):
        if not enabled:
            continue
        if not first:
            print()
        first = False
        fn(args.scope)

    print()
    if _warnings:
        print(f"WARNINGS ({len(_warnings)}): {', '.join(_warnings)}")
    if _failures:
        print(f"FAILURES ({len(_failures)}): {', '.join(_failures)}")
        return 1
    ran = " + ".join(m for m, on in (("N2 comms", run_comms), ("N1 digest", run_digest),
                                     ("N3 runbook", run_runbook)) if on)
    print(f"{ran}: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
