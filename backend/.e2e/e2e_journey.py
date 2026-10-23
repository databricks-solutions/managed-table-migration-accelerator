#!/usr/bin/env python3
"""M5 one-wave journey helper: the shared control-table edge for the UI and MCP journey drivers,
plus the operator commands around them.

  survey           which silver waves the journey uses, the UI wave's convert scope, which steps
                   apply, recency, rollback windows; with --batch: the parallel batches, the
                   batch choice, per-member dry-run need, recency, manual actions, D6
  check            re-run the step/finish-first oracle after any job (read-only); --batch: the
                   bulk step oracle
  fixture          --inject WAVE | --inject-batch BATCH (MERGE labelled RESHARE_D2D/ENABLE_COMPAT
                   rows) | --retire (-> OBSOLETE)
  resolve-manual   operator-resolve a wave's (or every --batch member's) MANUAL compaction
                   freezes (HUMAN GATE)
  reset-preflight  refuse a --reset while any estate table holds MANAGED data
  end-state        no journey-wave (or --batch member) table left MANAGED, fixtures retired,
                   estate reset-safe
  cockpit          run job_11e_bulk_cockpit (action=show, read-only) and cross-check its summary
  rollback-plan    the --batch members' rollback windows and the per-wave UI rollback commands

Writes happen only under --confirm (fixture / resolve-manual). The only job it triggers is the
read-only job_11e (action=show) in `cockpit`. Convert, bracket and rollback jobs run through the
app (e2e_app_ui.py). --profile is required and never defaulted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import actions as ax  # noqa: E402
from migration import authority as authz  # noqa: E402
from migration import bulk_journey_oracle as bjo  # noqa: E402
from migration import journey_oracle as jo  # noqa: E402
from migration import parallelism as pl  # noqa: E402

import _e2e_env  # noqa: E402

WAREHOUSE = _e2e_env.optional("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.optional("E2E_CATALOG")
CONTROL_SCHEMA = os.environ.get("E2E_SCHEMA", "migration")
JOURNEY_SCHEMA = os.environ.get("E2E_JOURNEY_SCHEMA", "e2e_real_silver")
ESTATE_SCHEMA_LIKE = "'e2e\\_real\\_%'"
CONVERT_JOB_SUFFIX = "managed-table-migration :: 04 convert"
BULK_CONVERT_JOB_SUFFIX = "managed-table-migration :: 11 bulk convert"
COCKPIT_JOB_SUFFIX = "managed-table-migration :: 11e bulk cockpit"
CONDUCTOR_JOB_SUFFIX = "managed-table-migration :: 08 conductor"
GATE_LOG = "/tmp/m6_4_journey.log"
SQL_TIMEOUT_S = 600
_CATALOG_NAME = re.compile(r"[A-Za-z0-9_-]+")
_failures: list[str] = []
_warnings: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def warn(label: str, detail: str = "") -> None:
    print(f"  [WARN] {label}{(' — ' + detail) if detail else ''}")
    _warnings.append(label)


def report(label: str, outcome: jo.Outcome, *, check=check, warn=warn) -> None:
    verdict, msg = outcome
    if verdict == jo.WARN:
        warn(label, msg)
    else:
        check(label, verdict == jo.PASS, msg)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _catalog_ident(name: object) -> str:
    """Backticked catalog identifier; raises on anything that is not a plain catalog name."""
    s = str(name or "")
    if not _CATALOG_NAME.fullmatch(s):
        raise ValueError(f"not a catalog name: {name!r}")
    return f"`{s}`"


class Journey:
    """Thin SQL edge over the control tables for the journey drivers."""

    def __init__(self, w: WorkspaceClient, *, warehouse: str = WAREHOUSE, catalog: str = CATALOG,
                 control_schema: str = CONTROL_SCHEMA):
        self.w, self.warehouse, self.catalog, self.ctl = w, warehouse, catalog, control_schema
        # Set by wait_run for the run it just waited on; None until a run's tasks were read.
        self.last_failed_tasks: list[str] | None = None

    def t(self, name: str) -> str:
        return f"{self.catalog}.{self.ctl}.{name}"

    def sql(self, statement: str) -> list[dict]:
        r = self.w.statement_execution.execute_statement(
            warehouse_id=self.warehouse, statement=statement, wait_timeout="50s")
        deadline = time.time() + SQL_TIMEOUT_S
        while r.status and r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.time() > deadline:
                raise TimeoutError(f"SQL statement {r.statement_id} still {r.status.state} after "
                                   f"{SQL_TIMEOUT_S}s :: {statement[:200]}")
            time.sleep(3)
            r = self.w.statement_execution.get_statement(r.statement_id)
        state = r.status.state if r.status else None
        if state != StatementState.SUCCEEDED:
            msg = r.status.error.message if r.status and r.status.error else ""
            raise RuntimeError(f"SQL {state}: {msg} :: {statement[:200]}")
        m = r.manifest
        if m and ((m.total_chunk_count or 0) > 1 or m.truncated):
            raise RuntimeError(f"SQL result of statement {r.statement_id} spans {m.total_chunk_count} chunk(s) "
                               f"(truncated={m.truncated}); only the first is read :: {statement[:200]}")
        cols = [c.name for c in (r.manifest.schema.columns or [])] if r.manifest and r.manifest.schema else []
        return [dict(zip(cols, row)) for row in ((r.result.data_array if r.result else None) or [])]

    # ---- reads -----------------------------------------------------------------------------
    def next_row(self) -> dict | None:
        rows = self.sql(
            "SELECT unix_millis(as_of) AS as_of_ms, plan_run_id, next_step_kind, next_wave_id, next_bucket, "
            f"next_selector, next_job_alias FROM {self.t('migration_next_actions')} ORDER BY as_of DESC LIMIT 1")
        return rows[0] if rows else None

    def plan_run_id(self) -> str:
        rows = self.sql(f"SELECT DISTINCT plan_run_id FROM {self.t('wave_plan')}")
        if len(rows) != 1:
            raise RuntimeError(f"expected exactly one plan in wave_plan, found {len(rows)}")
        return str(rows[0]["plan_run_id"])

    def wave_rows(self, prid: str) -> list[dict]:
        return self.sql(f"SELECT * FROM {self.t('wave_status')} WHERE plan_run_id = {jo.sql_string(prid)}")

    def wave_row(self, prid: str, wave_id: str) -> dict | None:
        return next((r for r in self.wave_rows(prid) if r.get("wave_id") == wave_id), None)

    def members(self, prid: str, wave_id: str) -> list[dict]:
        return self.sql(
            "SELECT m.full_name, m.state, m.eligibility, m.optimize_run_at, m.optimize_po_declined, "
            "unix_millis(m.last_compaction_at) AS last_compaction_ms, unix_millis(m.last_vacuum_at) AS last_vacuum_ms "
            f"FROM {self.t('wave_plan')} p JOIN {self.t('table_manifest')} m ON m.full_name = p.full_name "
            f"WHERE p.plan_run_id = {jo.sql_string(prid)} AND p.wave_id = {jo.sql_string(wave_id)}")

    def converted_members(self, prid: str, wave_id: str) -> list[dict]:
        """The manifest rows of the tables a successful CONVERT audit row names for this pin — the
        set 06_rollback rolls back. Read from audit_log, so it survives a re-plan that rewrote
        wave_plan (``members`` would then read nothing)."""
        return self.sql(
            "SELECT m.full_name, m.state, m.eligibility, m.optimize_run_at, m.optimize_po_declined "
            f"FROM (SELECT DISTINCT full_name FROM {self.t('audit_log')} WHERE action = 'CONVERT' "
            f"AND to_state = 'CONVERTED' AND plan_run_id = {jo.sql_string(prid)} "
            f"AND wave_id = {jo.sql_string(wave_id)}) a "
            f"JOIN {self.t('table_manifest')} m ON m.full_name = a.full_name")

    def schemas_by_wave(self, prid: str) -> dict[str, set[str]]:
        rows = self.sql(
            "SELECT p.wave_id, concat(m.`catalog`, '.', m.`schema`) AS scope "
            f"FROM {self.t('wave_plan')} p JOIN {self.t('table_manifest')} m ON m.full_name = p.full_name "
            f"WHERE p.plan_run_id = {jo.sql_string(prid)} GROUP BY 1, 2")
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(str(r["wave_id"]), set()).add(str(r["scope"]))
        return out

    def action(self, key: str) -> dict | None:
        rows = self.sql(f"SELECT * FROM {self.t('migration_actions')} WHERE action_key = {jo.sql_string(key)}")
        return rows[0] if rows else None

    def fixture_rows(self, full_names: Iterable[str] | None = None) -> list[dict]:
        """Fixture rows, estate-wide by default or only those on ``full_names`` (empty -> [])."""
        where = f"source = {jo.sql_string(jo.FIXTURE_SOURCE)}"
        if full_names is not None:
            names = sorted({str(n) for n in full_names})
            if not names:
                return []
            where += f" AND full_name IN ({', '.join(jo.sql_string(n) for n in names)})"
        return self.sql(
            f"SELECT action_key, action_kind, full_name, status, resolution FROM {self.t('migration_actions')} "
            f"WHERE {where} ORDER BY action_key")

    def open_manual_pre_convert(self, prid: str, wave_id: str) -> list[dict]:
        return self.sql(
            "SELECT a.action_id, a.action_key, a.action_kind, a.full_name, a.target_kind "
            f"FROM {self.t('migration_actions')} a JOIN {self.t('wave_plan')} p ON p.full_name = a.full_name "
            f"WHERE p.plan_run_id = {jo.sql_string(prid)} AND p.wave_id = {jo.sql_string(wave_id)} "
            "AND a.phase = 'PRE_CONVERT' AND a.blocking = true AND a.status IN ('OPEN', 'IN_PROGRESS') "
            "AND a.action_kind <> 'LEASE_OWNERSHIP' AND coalesce(a.target_kind, 'NONE') NOT IN ('JOB', 'PIPELINE')")

    def open_lease_pre_convert(self, prid: str, wave_id: str) -> list[dict]:
        """Open blocking LEASE_OWNERSHIP rows on the wave: closed by 03d, never by the freeze."""
        return self.sql(
            "SELECT a.action_key, a.full_name, "
            f"{jo.sql_string(wave_id)} AS wave_id "
            f"FROM {self.t('migration_actions')} a JOIN {self.t('wave_plan')} p ON p.full_name = a.full_name "
            f"WHERE p.plan_run_id = {jo.sql_string(prid)} AND p.wave_id = {jo.sql_string(wave_id)} "
            "AND a.phase = 'PRE_CONVERT' AND a.blocking = true AND a.status IN ('OPEN', 'IN_PROGRESS') "
            "AND a.action_kind = 'LEASE_OWNERSHIP'")

    def needs_a_person(self, prid: str, wave_ids: Iterable[str], run_as: str) -> list[dict]:
        """Stored verdicts on these waves' tables still to convert that Authorize cannot close
        (``NEEDS_GRANT`` / ``UNKNOWN``): someone else must run a grant, so a human decides."""
        members, verdicts = self.authority_inputs(prid, run_as)
        waves = {str(w) for w in wave_ids}
        names = {m["full_name"] for m in members if str(m.get("wave_id")) in waves}
        return [v for v in verdicts if v.get("full_name") in names
                and v.get("status") in (authz.STATUS_NEEDS_GRANT, authz.STATUS_UNKNOWN)]

    def audit_since(self, full_names: list[str], since_ms: int) -> list[dict]:
        if not full_names:
            return []
        names = ", ".join(jo.sql_string(n) for n in full_names)
        return self.sql(
            f"SELECT action, full_name, detail, unix_millis(created_at) AS created_ms FROM {self.t('audit_log')} "
            f"WHERE full_name IN ({names}) AND unix_millis(created_at) >= {int(since_ms)}")

    def estate_state_counts(self) -> dict[str, int]:
        rows = self.sql(f"SELECT state, count(*) AS n FROM {self.t('table_manifest')} "
                        f"WHERE `schema` LIKE {ESTATE_SCHEMA_LIKE} GROUP BY state")
        return {str(r["state"]): int(r["n"]) for r in rows}

    def estate_managed_tables(self) -> list[str] | None:
        """Estate tables information_schema reports as MANAGED, sorted; None when any read fails.

        Reads every estate catalog in the manifest plus the configured catalog, so a wiped
        manifest cannot hide a MANAGED estate table."""
        try:
            rows = self.sql(f"SELECT DISTINCT `catalog` FROM {self.t('table_manifest')} "
                            f"WHERE `schema` LIKE {ESTATE_SCHEMA_LIKE}")
            catalogs = sorted({str(r["catalog"]) for r in rows} | {self.catalog})
            out: list[str] = []
            for cat in catalogs:
                out += [f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}" for r in self.sql(
                    "SELECT table_catalog, table_schema, table_name "
                    f"FROM {_catalog_ident(cat)}.information_schema.tables "
                    f"WHERE table_schema LIKE {ESTATE_SCHEMA_LIKE} AND table_type = 'MANAGED'")]
            return sorted(set(out))
        except Exception as e:  # noqa: BLE001 — any read failure is "not proven", never "none"
            print(f"    information_schema read failed: {type(e).__name__}: {e}")
            return None

    def authority_inputs(self, prid: str, run_as: str) -> tuple[list[dict], list[dict]]:
        """The plan's members (with state) and the stored convert_authority verdicts FOR run_as.

        The harness runs as the developer, not run_as, so it never re-reads facts (information_schema
        is filtered to the caller); it reads what the jobs persisted for the convert identity."""
        members = self.sql(authz.wave_members_sql(self.t("wave_plan"), self.t("table_manifest"), prid))
        names = authz.to_convert_names(members)
        if not names:
            return members, []
        return members, self.sql(authz.stored_authority_sql(self.t(authz.CONVERT_AUTHORITY_TABLE), names, run_as))

    def _job_id(self, suffix: str) -> int | None:
        """The one deployed job whose name ends with ``suffix``; None when absent or ambiguous."""
        ids = [j.job_id for j in self.w.jobs.list()
               if j.settings and (j.settings.name or "").endswith(suffix)]
        if len(ids) != 1:
            print(f"    {len(ids)} deployed job(s) end with {suffix!r}")
            return None
        return ids[0]

    def _run_as(self, suffix: str) -> str | None:
        job_id = self._job_id(suffix)
        if job_id is None:
            return None
        return (self.w.jobs.get(job_id).run_as_user_name or "").strip() or None

    def convert_run_as(self) -> str | None:
        """run_as of the one deployed 04 convert job; None when absent or ambiguous."""
        return self._run_as(CONVERT_JOB_SUFFIX)

    def bulk_convert_run_as(self) -> str | None:
        """run_as of the one deployed 11 bulk convert job (the identity the cockpit counts leases against)."""
        return self._run_as(BULK_CONVERT_JOB_SUFFIX)

    def batch_rollups(self, prid: str) -> tuple[list[dict], list[dict], str]:
        """(rollups, joined rows, lease note): the cockpit's read, open-freeze count and the
        authority-based lease count for the bulk run_as. A failed read keeps the view's open_lease."""
        joined = bjo.coerce_joined_rows(self.sql(pl.batch_joined_sql(
            self.t(pl.WAVE_PARALLEL_GROUPS_TABLE), self.t("wave_status"), prid)))
        frozen = self.sql(pl.frozen_ledger_sql(self.t("freeze_ledger")))
        note = "lease count as the view computed it (bulk convert run_as unresolved)"
        try:
            run_as = self.bulk_convert_run_as()
            if run_as:
                members, verdicts = self.authority_inputs(prid, run_as)
                joined = jo.authority_aware_waves(joined, members, verdicts)
                note = f"convert-permission count from stored verdicts (run_as={run_as})"
        except Exception as e:  # noqa: BLE001 — degrade to the view's count, never a false 0
            note = f"lease count as the view computed it (authority read failed: {type(e).__name__})"
        return pl.batch_readiness(joined, joined, pl.open_freeze_by_batch(joined, frozen)), joined, note

    def batch_wave_ids(self, prid: str, batch_id: str) -> list[str]:
        return [str(m["wave_id"]) for m in bjo.batch_members(self.batch_rollups(prid)[1], batch_id)]

    def table_types(self, full_names: Iterable[str]) -> dict[str, str] | None:
        """Live table_type per table from information_schema; None when any read fails."""
        by_catalog: dict[str, list[str]] = {}
        for fn in sorted({str(n) for n in full_names}):
            parts = fn.split(".", 2)
            if len(parts) == 3:
                by_catalog.setdefault(parts[0], []).append(f"{parts[1]}.{parts[2]}")
        out: dict[str, str] = {}
        try:
            for cat, names in by_catalog.items():
                for r in self.sql(
                        "SELECT table_catalog, table_schema, table_name, table_type "
                        f"FROM {_catalog_ident(cat)}.information_schema.tables "
                        f"WHERE concat(table_schema, '.', table_name) IN ({', '.join(jo.sql_string(n) for n in names)})"):
                    out[f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}"] = str(r.get("table_type"))
        except Exception as e:  # noqa: BLE001 — any read failure is "not proven"
            print(f"    information_schema read failed: {type(e).__name__}: {e}")
            return None
        return out

    def run_cockpit(self, prid: str, batch_id: str, *, timeout_s: int = 1800) -> dict | None:
        """Run job_11e_bulk_cockpit with action=show (read-only) and return its exit summary."""
        job_id = self._job_id(COCKPIT_JOB_SUFFIX)
        if job_id is None:
            return None
        run_id = self.w.jobs.run_now(job_id=job_id, job_parameters=bjo.cockpit_params(prid, batch_id)).run_id
        try:
            result, _ = self.wait_run(run_id, timeout_s=timeout_s, poll_s=15)
        except TimeoutError as e:
            print(f"    job_11e did not finish: {e}")
            return None
        if result != "SUCCESS":
            return None
        tasks = self.w.jobs.get_run(run_id).tasks or []
        if not tasks:
            return None
        out = self.w.jobs.get_run_output(tasks[0].run_id)
        raw = out.notebook_output.result if out and out.notebook_output else None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            print(f"    job_11e exit value is not JSON: {str(raw)[:160]}")
            return None

    # ---- focus mode + team pins ------------------------------------------------------------------
    def scoped_rows(self) -> list[dict]:
        """The latest per-scope batch (migration_next_actions_by_scope); [] before the first run."""
        fqn = self.t("migration_next_actions_by_scope")
        try:
            return self.sql(
                # Same shape the estate next_row() returns (as_of_ms, plan_run_id, the step and its job),
                # so the one-wave oracles read a scoped row exactly like the estate row.
                "SELECT unix_millis(as_of) AS as_of_ms, plan_run_id, scope_key, scope_status, next_step_kind, "
                "next_wave_id, next_bucket, next_selector, next_job_alias, wave_index, wave_count, "
                "held_streaming_waves, rolled_back_waves, unplanned_tables, plan_stale_waves, pin_rank "
                f"FROM {fqn} WHERE as_of = (SELECT MAX(as_of) FROM {fqn}) ORDER BY scope_key")
        except RuntimeError as e:
            print(f"    per-scope rows unreadable: {str(e)[:160]}")
            return []

    def pins_row(self) -> dict | None:
        rows = self.sql(
            "SELECT next_step_kind, next_wave_id, next_group_key, focus_pins, pin_applied_scope "
            f"FROM {self.t('migration_next_actions')} ORDER BY as_of DESC LIMIT 1")
        return rows[0] if rows else None

    def run_conductor(self, params: dict | None = None, *, timeout_s: int = 1800) -> str | None:
        """Run job_08_conductor (a pin change when ``params`` has pin_action) and wait for it."""
        job_id = self._job_id(CONDUCTOR_JOB_SUFFIX)
        if job_id is None:
            return None
        run_id = self.w.jobs.run_now(job_id=job_id, job_parameters=params or None).run_id
        result, _ = self.wait_run(run_id, timeout_s=timeout_s, poll_s=10)
        return result

    def base_used(self, base: str) -> int:
        rows = self.sql(f"SELECT count(*) AS n FROM {self.t('table_manifest')} "
                        f"WHERE storage_location LIKE {jo.sql_string(base.rstrip('/') + '/%')}")
        return int(rows[0]["n"]) if rows else 0

    def converted_at_ms(self, prid: str, wave_id: str) -> int | None:
        rows = self.sql(
            f"SELECT unix_millis(min(created_at)) AS ms FROM {self.t('audit_log')} WHERE action = 'CONVERT' "
            f"AND to_state = 'CONVERTED' AND plan_run_id = {jo.sql_string(prid)} AND wave_id = {jo.sql_string(wave_id)}")
        return int(rows[0]["ms"]) if rows and rows[0].get("ms") else None

    def wait_run(self, run_id: int | str, *, timeout_s: int = 3600, poll_s: int = 20) -> tuple[str, int]:
        """Poll a job run (its recompute_next_step tail is a task of the same run) -> (result, start_ms).
        Also sets ``last_failed_tasks`` for ``jo.run_outcome``."""
        self.last_failed_tasks = None
        deadline = time.time() + timeout_s
        while True:
            run = self.w.jobs.get_run(int(run_id))
            life = run.state.life_cycle_state.value if run.state and run.state.life_cycle_state else "?"
            if life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
                result = run.state.result_state.value if run.state and run.state.result_state else life
                self.last_failed_tasks = jo.failed_task_keys(
                    (t.task_key, t.state.result_state.value if t.state and t.state.result_state else None)
                    for t in run.tasks or [])
                failed = f" failed: {', '.join(self.last_failed_tasks)}" if self.last_failed_tasks else ""
                print(f"    run {run_id}: {result}{failed} {run.run_page_url or ''}")
                return result, int(run.start_time or 0)
            if time.time() > deadline:
                raise TimeoutError(f"run {run_id} still {life} after {timeout_s}s: {run.run_page_url}")
            time.sleep(poll_s)

    # ---- writes (fixture + operator resolve only) -----------------------------------------------
    def inject_fixture(self, prid: str, wave_id: str) -> list[str]:
        converted = sorted(str(m["full_name"]) for m in self.members(prid, wave_id)
                           if str(m.get("state") or "").upper() in ax.MIGRATED_STATES)
        if not converted:
            raise RuntimeError(f"{wave_id} has no converted member to hang the fixture on")
        rows = jo.fixture_action_rows(converted[0], plan_run_id=prid, wave_id=wave_id)
        self.sql(jo.fixture_merge_sql(self.t("migration_actions"), rows))
        print(f"    fixture: {[r['action_key'] for r in rows]}")
        return [r["action_key"] for r in rows]

    def retire_fixture(self) -> None:
        self.sql(jo.fixture_retire_sql(self.t("migration_actions")))

    def resolve_manual(self, prid: str, wave_id: str) -> list[str]:
        ids = [str(a["action_id"]) for a in self.open_manual_pre_convert(prid, wave_id)
               if a.get("action_kind") == ax.KIND_FREEZE_COMPACTION]
        if ids:
            self.sql(jo.resolve_manual_sql(self.t("migration_actions"), ids))
        return ids


# ---- commands -----------------------------------------------------------------------------------
def survey(j: Journey, *, schema: str) -> None:
    prid = j.plan_run_id()
    row = j.next_row() or {}
    print(f"plan_run_id: {prid}\nconductor:   {row.get('next_step_kind')} {row.get('next_wave_id') or '-'}")
    waves = j.wave_rows(prid)
    scopes = j.schemas_by_wave(prid)
    ui, mcp, why = jo.pick_journey_waves(waves, scopes, schema=f"{j.catalog}.{schema}",
                                         next_wave_id=row.get("next_wave_id"))
    print(f"journey waves: {why}")
    try:
        run_as = j.convert_run_as()
        if run_as:
            members, verdicts = j.authority_inputs(prid, run_as)
            waves = jo.authority_aware_waves(waves, members, verdicts)
    except Exception as e:  # noqa: BLE001 — degrade to the view's count, never a false 0
        run_as = None
        print(f"    convert-permission read failed: {type(e).__name__}: {str(e)[:160]}")
    print(f"convert run_as: {run_as or '(not resolved — permission counts are the view latest verdicts)'}")
    if ui:
        selector = str(row.get("next_selector") or "")
        report("S1b UI wave convert scope", jo.ui_convert_scope_outcome(
            ui, next_wave_id=row.get("next_wave_id"), next_bucket=row.get("next_bucket"), next_selector=selector,
            waves_in_selector=sum(1 for s in scopes.values() if s == {selector})))
    now = int(time.time() * 1000)
    for wid, label in ((ui, "UI pass"), (mcp, "MCP pass")):
        if not wid:
            warn(f"S1 {label} wave", why)
            continue
        wave = next(r for r in waves if r.get("wave_id") == wid)
        print(f"\n{label}: {wid} ({wave.get('wave_state')}, {wave.get('total_tables')} tables)")
        for step, applies, note in jo.journey_steps_for_wave(wave):
            print(f"  [{'x' if applies else ' '}] {step:<19} {note}")
        clear = jo.recency_clear_at_ms(j.members(prid, wid))
        if clear and clear > now:
            warn(f"S2 {wid} convert recency", f"the 120-min OPTIMIZE/VACUUM gate clears at {_iso(clear)}")
        else:
            check(f"S2 {wid} convert recency", True, "clear by the manifest (the job re-reads DESCRIBE HISTORY)")
        manual = j.open_manual_pre_convert(prid, wid)
        if manual:
            warn(f"S3 {wid} manual pre-convert actions",
                 "; ".join(f"{m['action_kind']} {m['full_name']} ({m['target_kind']})" for m in manual)
                 + " (HUMAN GATE: UI conscious skip or `resolve-manual`)")
    for wave in waves:
        conv = j.converted_at_ms(prid, str(wave["wave_id"])) if jo.wave_started(wave) else None
        if conv:
            report(f"S4 {wave['wave_id']} rollback window", jo.rollback_window_outcome(conv, now))
    fixtures = j.fixture_rows()
    print(f"\nfixture rows: {len(fixtures)} ({', '.join(sorted({str(r.get('status')) for r in fixtures})) or 'none'})")


def survey_multi_wave(j: Journey) -> None:
    """Scopes with two or more happy-path waves — the focus-mode CUJ candidates."""
    rows = j.scoped_rows()
    multi = [r for r in rows if int(r.get("wave_count") or 0) >= 2]
    print(f"{len(rows)} scope(s) in the latest per-scope batch; {len(multi)} with >= 2 waves:")
    for r in multi:
        print(f"  {r['scope_key']:<40} {r['scope_status']:<15} {r['next_step_kind'] or '-':<9} "
              f"{r['next_wave_id'] or '-':<10} wave {r.get('wave_index') or '-'} of {r.get('wave_count')}")
    if not multi:
        warn("S multi-wave scope", "none — re-plan with a smaller wave_max_tables to split a schema")


def focus_check(j: Journey, *, schema: str) -> None:
    """Read-only: the scope's row exists, its status is honest, and the estate pins are consistent."""
    row = next((r for r in j.scoped_rows() if r.get("scope_key") == schema), None)
    check(f"P {schema} has a per-scope row", row is not None)
    if row:
        print(f"    {schema}: {row['scope_status']} · {row['next_step_kind']} on {row['next_wave_id'] or '-'} "
              f"(wave {row.get('wave_index') or '-'} of {row.get('wave_count')})")
        report("P scope status", jo.scope_status_outcome(row))
    report("P pins", jo.pins_outcome(j.pins_row(), j.wave_rows(j.plan_run_id())))


def pins_cmd(j: Journey, *, action: str, confirm: bool, auto_cleared: str | None) -> int:
    nr = j.pins_row()
    pins = jo.team_pins(nr)
    print("team pins: " + (", ".join(f"{p.get('rank')}. {p.get('scope_key')} [{p.get('status')}]" for p in pins) or "none"))
    if action == "list":
        report("P pins", jo.pins_outcome(nr, j.wave_rows(j.plan_run_id())))
        if auto_cleared:
            report(f"P {auto_cleared} auto-cleared", jo.auto_clear_outcome(nr, auto_cleared))
        return 0
    if not confirm:
        print("HUMAN GATE: rerun with --confirm to clear every team pin (job_08_conductor pin_action=clear).")
        return 2
    result = j.run_conductor({"pin_action": "clear"})
    check("P clear pins run", result == "SUCCESS", str(result))
    check("P no pins left", not jo.team_pins(j.pins_row()))
    return 0


def check_step(j: Journey, *, wave_id: str, step: str, run_start_ms: int | None) -> None:
    prid = j.plan_run_id()
    for o in jo.step_advanced_outcome(step=step, wave_id=wave_id, wave_after=j.wave_row(prid, wave_id),
                                      next_row=j.next_row(), run_start_ms=run_start_ms):
        report(f"C {step}", o)
    report("C finish-first", jo.finish_first_outcome(j.next_row(), j.wave_rows(prid)))


def reset_preflight(j: Journey, *, external_base: str, profile: str) -> None:
    manifest = jo.reset_safe_outcome(j.estate_state_counts())
    report("P1 no estate table holds MANAGED data", manifest)
    live = jo.live_managed_outcome(j.estate_managed_tables())
    report("P1b no estate table is MANAGED in information_schema", live)
    report("P2 fresh external base", jo.external_base_outcome(external_base, j.base_used(external_base)))
    fixtures = j.fixture_rows()
    if fixtures:
        report("P3 fixture rows retired", jo.fixture_retired_outcome(fixtures))
    if _failures:
        return
    if manifest[0] != jo.PASS or live[0] != jo.PASS:
        print("\nReset-safety NOT PROVEN (P1b could not confirm it live). HUMAN GATE — verify every "
              "e2e_real_* table is EXTERNAL in information_schema.tables before resetting; "
              "the --reset command is withheld.")
        return
    print("\nSafe to reset. HUMAN GATE — this drops the e2e_real_* schemas (EXTERNAL registrations), "
          "clears the estate files and the estate's control-row slice:")
    print(f"  {{ echo \"== reset $(date -u +%FT%TZ)\"; python backend/.e2e/seed_real_estate.py --profile {profile} "
          f"--reset --preset populate --external-base {external_base}; }} 2>&1 | tee -a /tmp/m5_reset.log")


def end_state(j: Journey, *, waves: list[str], plan_run_id: str | None) -> None:
    prid = plan_run_id or j.plan_run_id()
    for wid in waves:
        report(f"E1 {wid} rolled back", jo.rollback_outcome(j.converted_members(prid, wid)))
    report("E2 fixture rows retired", jo.fixture_retired_outcome(j.fixture_rows()))
    counts = j.estate_state_counts()
    print(f"estate states: {counts}")
    report("E3 reset-safe", jo.reset_safe_outcome(counts))
    report("E3b live MANAGED check", jo.live_managed_outcome(j.estate_managed_tables()))


def survey_batch(j: Journey, *, batch_id: str | None, profile: str = "<profile>") -> None:
    prid = j.plan_run_id()
    row = j.next_row() or {}
    print(f"plan_run_id: {prid}\nconductor:   {row.get('next_step_kind')} {row.get('next_wave_id') or '-'}")
    if str(row.get("next_step_kind") or "") == "replan":
        warn("S0 plan is current", "the conductor recommends REPLAN; re-plan before a bulk proof (a human decides)")
    rollups, joined, note = j.batch_rollups(prid)
    waves = j.wave_rows(prid)
    print(f"lease: {note}\nbulk convert run_as: {j.bulk_convert_run_as() or '(not resolved)'}\n")
    reco = pl.recommended_batch(rollups)
    for r in rollups:
        labels = [bjo.member_label(m) for m in bjo.batch_members(joined, str(r["batch_id"]))]
        star = " (recommended)" if reco and reco["batch_id"] == r["batch_id"] else ""
        print(f"  {r['batch_id']:<10} {r['rollup']:<16} {r['total_count']} wave(s) {r['table_count']:>4} tables "
              f"next={pl.next_bulk_step(r):<14} started={r['started']}{star}: {', '.join(labels)}")
    pick, cands, why = bjo.pick_batch(rollups, waves)
    if batch_id:
        report("S1 batch choice", (jo.PASS, f"{batch_id} named by --batch; survey: {why}") if batch_id in cands
               else (jo.WARN, f"{batch_id} is not a clean candidate ({why}); a human decides"))
    elif pick is None:
        warn("S1 batch choice", why)
        return
    else:
        report("S1 batch choice", (jo.PASS if len(cands) == 1 else jo.WARN, why))
    target = batch_id or pick
    members = bjo.batch_members(joined, target)
    if not members:
        check(f"S1 {target} is a parallel batch of plan {prid}", False)
        return
    print(f"\n{target}: {len(members)} member(s), {sum(int(m.get('table_count') or 0) for m in members)} tables")
    for wid, schema, need, n in bjo.prep_targets(members, j.schemas_by_wave(prid)):
        print(f"  {wid:<10} {schema:<55} {'DRY RUN NEEDED' if need else 'ok':<15} {n}")
    now = int(time.time() * 1000)
    for m in members:
        wid = str(m["wave_id"])
        clear = jo.recency_clear_at_ms(j.members(prid, wid))
        if clear and clear > now:
            warn(f"S2 {wid} convert recency", f"the 120-min OPTIMIZE/VACUUM gate clears at {_iso(clear)}")
        else:
            check(f"S2 {wid} convert recency", True, "clear by the manifest (the job re-reads DESCRIBE HISTORY)")
        manual = j.open_manual_pre_convert(prid, wid)
        if manual:
            warn(f"S3 {wid} manual pre-convert actions",
                 "; ".join(f"{a['action_kind']} {a['full_name']} ({a['target_kind']})" for a in manual)
                 + f" (HUMAN GATE: `resolve-manual --batch {target}`)")
        leases = j.open_lease_pre_convert(prid, wid)
        if leases:
            print(f"  S3 {wid}: {len(leases)} open LEASE_OWNERSHIP pre-convert action(s) "
                  "(closed by bulk Authorize, never by the freeze)")
    checked, over = bjo.precheck_targets(members, False, pl.DEFAULT_BULK_MAX_PARALLEL)
    print(f"\npre-convert checks will cover: {', '.join(checked)}" + (f"; over the cap: {', '.join(over)}" if over else ""))
    report("S5 D6 recommendation", bjo.recommended_batch_outcome(rollups, target))
    for wave in waves:
        conv = j.converted_at_ms(prid, str(wave["wave_id"])) if jo.wave_started(wave) else None
        if conv:
            report(f"S4 {wave['wave_id']} rollback window", jo.rollback_window_outcome(conv, now))
    fixtures = j.fixture_rows()
    print(f"\nfixture rows: {len(fixtures)} ({', '.join(sorted({str(r.get('status')) for r in fixtures})) or 'none'})")
    app = f"python backend/.e2e/e2e_app_ui.py --profile {profile}"
    print(f"\nNext (plan {prid} is pinned; --plan-run-id is required with --confirm-execute):\n"
          f"  {app} --prep-batch --batch {target} --plan-run-id {prid}\n"
          f"  {app} --bulk-journey --batch {target} --plan-run-id {prid}")


def check_batch(j: Journey, *, batch_id: str, step: str, run_start_ms: int | None) -> None:
    prid = j.plan_run_id()
    rollups, _, _ = j.batch_rollups(prid)
    for o in bjo.batch_step_outcome(step=step, after=bjo.rollup_for(rollups, batch_id),
                                    next_row=j.next_row(), run_start_ms=run_start_ms):
        report(f"C {step}", o)
    report("C D6 recommendation", bjo.recommended_batch_outcome(rollups, batch_id))


def cockpit_check(j: Journey, *, batch_id: str) -> None:
    prid = j.plan_run_id()
    rollups, _, _ = j.batch_rollups(prid)
    summary = j.run_cockpit(prid, batch_id)
    if summary:
        print(f"job_11e: recommended {summary.get('recommended_batch')}; {batch_id or '-'} -> "
              f"{summary.get('next_step')}; {summary.get('lease_note')}")
    for o in bjo.cockpit_outcome(summary, rollups, batch_id, plan_run_id=prid):
        report("K job_11e cross-check", o)


def rollback_plan(j: Journey, *, batch_id: str, plan_run_id: str | None, profile: str) -> None:
    prid = plan_run_id or j.plan_run_id()
    wave_ids = j.batch_wave_ids(prid, batch_id)
    if not wave_ids:
        check(f"R {batch_id} is a parallel batch of plan {prid}", False)
        return
    now = int(time.time() * 1000)
    print(f"Roll back {batch_id} one wave at a time (§8 Q5: no bulk rollback). HUMAN GATE per wave:")
    for wid, needed, outcome in bjo.rollback_targets(wave_ids, {w: j.converted_at_ms(prid, w) for w in wave_ids}, now):
        report(f"R0 {wid} rollback window", outcome)
        if needed and outcome[0] != jo.FAIL:
            print(f"  {{ echo \"== rollback {wid} $(date -u +%FT%TZ)\"; python backend/.e2e/e2e_app_ui.py "
                  f"--profile {profile} --rollback-wave {wid} --plan-run-id {prid} "
                  f"--reason \"{bjo.ROLLBACK_REASON}\" --confirm-execute; }} 2>&1 | tee -a {GATE_LOG}")


def end_state_batch(j: Journey, *, batch_id: str, plan_run_id: str | None) -> None:
    prid = plan_run_id or j.plan_run_id()
    wave_ids = j.batch_wave_ids(prid, batch_id)
    if not wave_ids:
        check(f"E0 {batch_id} is a parallel batch of plan {prid}", False)
        return
    names: list[str] = []
    for wid in wave_ids:
        rows = j.members(prid, wid)
        names += [str(r["full_name"]) for r in rows]
        report(f"E1 {wid} rolled back", jo.rollback_outcome(rows))
    report("E1b proof tables read EXTERNAL", bjo.external_outcome(j.table_types(names), names))
    report("E2 fixture rows retired", jo.fixture_retired_outcome(j.fixture_rows()))
    counts = j.estate_state_counts()
    print(f"estate states: {counts}")
    report("E3 reset-safe", jo.reset_safe_outcome(counts))
    report("E3b live MANAGED check", jo.live_managed_outcome(j.estate_managed_tables()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, help="Databricks CLI profile (never defaulted).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey")
    s.add_argument("--schema", default=JOURNEY_SCHEMA)
    s.add_argument("--multi-wave", action="store_true", help="list scopes with >= 2 waves (focus CUJ)")
    s.add_argument("--batch", nargs="?", const="", default=None,
                   help="bulk survey; with a value, check that batch as the choice")
    c = sub.add_parser("check")
    cg = c.add_mutually_exclusive_group(required=True)
    cg.add_argument("--wave")
    cg.add_argument("--batch")
    c.add_argument("--step", required=True, choices=sorted(set(jo.JOURNEY_RANK) | set(bjo.BULK_RANK)))
    c.add_argument("--run-start-ms", type=int,
                   help="the job run's start (epoch ms); omitted, the freshness check WARNs 'not checked'")
    f = sub.add_parser("fixture")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--inject", metavar="WAVE_ID")
    g.add_argument("--inject-batch", metavar="BATCH_ID")
    g.add_argument("--retire", action="store_true")
    f.add_argument("--confirm", action="store_true")
    r = sub.add_parser("resolve-manual")
    rg = r.add_mutually_exclusive_group(required=True)
    rg.add_argument("--wave")
    rg.add_argument("--batch")
    r.add_argument("--confirm", action="store_true")
    p = sub.add_parser("reset-preflight")
    p.add_argument("--external-base", required=True)
    e = sub.add_parser("end-state")
    eg = e.add_mutually_exclusive_group(required=True)
    eg.add_argument("--waves", help="comma-separated wave ids")
    eg.add_argument("--batch")
    e.add_argument("--plan-run-id")
    fo = sub.add_parser("focus", help="read-only focus checks for one scope")
    fo.add_argument("--schema", required=True, help="the scope (catalog.schema)")
    pn = sub.add_parser("pins", help="list (read-only) or clear (gated) the team pins")
    pn.add_argument("action", choices=["list", "clear"])
    pn.add_argument("--confirm", action="store_true")
    pn.add_argument("--auto-cleared", metavar="SCOPE", help="also check this scope's pin auto-cleared")
    k = sub.add_parser("cockpit")
    k.add_argument("--batch", default="", help="the batch to compare (blank: the recommendation only)")
    b = sub.add_parser("rollback-plan")
    b.add_argument("--batch", required=True)
    b.add_argument("--plan-run-id")
    args = ap.parse_args()

    j = Journey(WorkspaceClient(profile=args.profile))
    if args.cmd == "survey" and args.multi_wave:
        survey_multi_wave(j)
    elif args.cmd == "focus":
        focus_check(j, schema=args.schema)
    elif args.cmd == "pins":
        rc = pins_cmd(j, action=args.action, confirm=args.confirm, auto_cleared=args.auto_cleared)
        if rc:
            return rc
    elif args.cmd == "survey":
        if args.batch is None:
            survey(j, schema=args.schema)
        else:
            survey_batch(j, batch_id=args.batch or None, profile=args.profile)
    elif args.cmd == "check":
        if args.batch is not None:
            if not args.batch:
                print("--batch requires a batch id (empty --batch is not allowed)")
                return 2
            if args.step not in bjo.BULK_RANK:
                print(f"--batch needs a bulk step: {', '.join(bjo.BULK_STEPS)}")
                return 2
            check_batch(j, batch_id=args.batch, step=args.step, run_start_ms=args.run_start_ms)
        else:
            if args.step not in jo.JOURNEY_RANK:
                print(f"--wave needs a one-wave step: {', '.join(sorted(jo.JOURNEY_RANK))}")
                return 2
            check_step(j, wave_id=args.wave, step=args.step, run_start_ms=args.run_start_ms)
    elif args.cmd == "fixture":
        if not args.confirm:
            print("HUMAN GATE: rerun with --confirm to write fixture rows (MERGE / retire to OBSOLETE).")
            return 2
        if args.retire:
            j.retire_fixture()
            report("F retire", jo.fixture_retired_outcome(j.fixture_rows()))
        elif args.inject_batch:
            prid = j.plan_run_id()
            wave_ids = j.batch_wave_ids(prid, args.inject_batch)
            if not wave_ids:
                check(f"F {args.inject_batch} is a parallel batch of plan {prid}", False)
            else:
                wid = bjo.fixture_wave({w: j.members(prid, w) for w in wave_ids}, wave_ids)
                if wid is None:
                    check(f"F {args.inject_batch} has a converted member to hang the fixture on", False)
                else:
                    j.inject_fixture(prid, wid)
        else:
            j.inject_fixture(j.plan_run_id(), args.inject)
    elif args.cmd == "resolve-manual":
        prid = j.plan_run_id()
        if args.batch is not None:
            wave_ids = j.batch_wave_ids(prid, args.batch)
            if not wave_ids:
                check(f"M {args.batch} is a parallel batch of plan {prid}", False)
        else:
            wave_ids = [args.wave]
        if wave_ids:
            for wid in wave_ids:
                manual = j.open_manual_pre_convert(prid, wid)
                print(f"manual pre-convert on {wid}: {[(m['action_kind'], m['full_name']) for m in manual]}")
            if not args.confirm:
                print("HUMAN GATE: rerun with --confirm to mark the FREEZE_COMPACTION ones DONE (operator-resolve).")
                return 2
            for wid in wave_ids:
                print(f"resolved on {wid}: {j.resolve_manual(prid, wid)}")
    elif args.cmd == "reset-preflight":
        reset_preflight(j, external_base=args.external_base, profile=args.profile)
    elif args.cmd == "end-state":
        if args.batch:
            end_state_batch(j, batch_id=args.batch, plan_run_id=args.plan_run_id)
        else:
            end_state(j, waves=[w.strip() for w in args.waves.split(",") if w.strip()],
                      plan_run_id=args.plan_run_id)
    elif args.cmd == "cockpit":
        cockpit_check(j, batch_id=args.batch)
    elif args.cmd == "rollback-plan":
        rollback_plan(j, batch_id=args.batch, plan_run_id=args.plan_run_id, profile=args.profile)

    print()
    if _warnings:
        print(f"WARNINGS ({len(_warnings)}): {', '.join(_warnings)}")
    if _failures:
        print(f"FAILURES ({len(_failures)}): {', '.join(_failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
