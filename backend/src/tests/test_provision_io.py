"""The install edge (``migration.provision_io``) against fakes — no network, no Spark."""

from types import SimpleNamespace as NS

from migration import provision as pv
from migration import provision_io as pio

SP = "11111111-2222-3333-4444-555555555555"
APP_SP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DEPLOY = "[e2e] managed-table-migration"


class FakeSql:
    """Records every statement; answers the probe reads; fails statements matching ``fail``."""

    def __init__(self, fail=(), deny_reads=False):
        self.calls, self.fail, self.deny_reads = [], tuple(fail), deny_reads

    def __call__(self, s):
        self.calls.append(s)
        if any(s.startswith(f) or (f in s and s.startswith(("GRANT", "REVOKE"))) for f in self.fail):
            raise RuntimeError("PERMISSION_DENIED")
        if self.deny_reads and s.startswith("SELECT"):
            raise RuntimeError("PERMISSION_DENIED")
        if "information_schema.catalogs" in s:
            return [{"catalog_name": "main", "catalog_owner": "ann@x.com"},
                    {"catalog_name": "sales", "catalog_owner": "sales-owners"}]
        if "information_schema.schemata" in s:
            return [{"catalog_name": "main", "schema_name": "migration", "schema_owner": SP},
                    {"catalog_name": "sales", "schema_name": "orders", "schema_owner": "ann@x.com"},
                    {"catalog_name": "sales", "schema_name": "returns", "schema_owner": "bob@x.com"}]
        if "information_schema.tables" in s:
            return [{"table_catalog": "sales", "table_schema": "orders", "tables": 212},
                    {"table_catalog": "sales", "table_schema": "returns", "tables": 48}]
        return []

    def grants(self):
        return [c for c in self.calls if c.startswith(("GRANT", "REVOKE"))]


def _job(name, run_as=SP, params=None):
    tasks = [NS(notebook_task=NS(base_parameters=params))] if params is not None else []
    return NS(job_id=hash(name) & 0xFFFF, run_as_user_name=run_as,
              settings=NS(name=name, run_as=None, tasks=tasks))


def _w(groups=("data-eng",), jobs=None, owner="ms-admins"):
    jobs = jobs if jobs is not None else [
        _job(f"{DEPLOY} :: 00 profiler"),
        _job(f"{DEPLOY} :: 99 setup", params={"installer_group": "", "app_name": "mcp-x"}),
        _job("[demo] managed-table-migration :: 00 profiler", run_as="someone@else.com"),
    ]
    by_id = {j.job_id: j for j in jobs}
    return NS(
        current_user=NS(me=lambda: NS(user_name="ann@x.com", groups=[NS(display=g) for g in groups])),
        metastores=NS(summary=lambda: NS(owner=owner)),
        jobs=NS(list=lambda: [NS(job_id=j.job_id, settings=NS(name=j.settings.name)) for j in jobs],
                get=lambda i: by_id[i]),
        apps=NS(get=lambda name: NS(service_principal_client_id=APP_SP)),
        service_principals=NS(list=lambda filter: [NS(active=True)]),
    )


def test_probe_reads_only_this_deployment_and_judges_coverage():
    probes, ctx = pio.probe(_w(), FakeSql(), catalog="main", schema="migration", deployment=DEPLOY)
    assert probes["jobs"]["agree"] and probes["jobs"]["identity"]["name"] == SP
    assert probes["jobs"]["count"] == 2
    assert ctx["app_sp"] == APP_SP and ctx["installer_group"] is None
    assert probes["control"]["exists"] is True and probes["control"]["can_grant"] is True
    rep = pv.rights_report(probes)
    assert rep["can_install"] is True
    assert rep["coverage"]["headline"].startswith("You can grant on 1 of 2 schemas")
    assert rep["suggested_scopes"] == "sales.orders"
    assert ctx["grantable_schemas"] == ["sales.orders"]


def test_deployed_app_name_comes_from_this_deployments_setup_job():
    setup = _job(f"{DEPLOY} :: 99 setup", params={"app_name": " mcp-renamed "})
    w = _w(jobs=[setup, _job(f"{DEPLOY} :: 00 profiler")])
    assert pio.deployed_app_name(w, setup.job_id) == "mcp-renamed"
    blank = _job(f"{DEPLOY} :: 99 setup", params={"app_name": ""})
    assert pio.deployed_app_name(_w(jobs=[blank]), blank.job_id) is None
    assert pio.deployed_app_name(w, None) is None
    assert pio.deployed_app_name(NS(jobs=NS(get=lambda i: (_ for _ in ()).throw(RuntimeError("denied")))), 7) is None


def test_deployed_setup_parameters_are_trimmed_and_empty_when_unknown():
    setup = _job(f"{DEPLOY} :: 99 setup", params={"genie_warehouse_id": " wh1 ", "manifest_catalog": "c"})
    assert pio.deployed_setup_parameters(_w(jobs=[setup]), setup.job_id) == {
        "genie_warehouse_id": "wh1", "manifest_catalog": "c"}
    assert pio.deployed_setup_parameters(_w(jobs=[setup]), None) == {}


def test_probe_degrades_to_unknown_when_reads_fail():
    probes, _ = pio.probe(_w(), FakeSql(deny_reads=True), catalog="main", schema="migration", deployment=DEPLOY)
    rep = pv.rights_report(probes)
    ctl = next(c for c in rep["checks"] if c["key"] == "control_catalog")
    assert ctl["status"] == pv.UNKNOWN and rep["can_install"] is False


def test_probe_picks_installer_group_from_deployed_setup():
    jobs = [_job(f"{DEPLOY} :: 99 setup", params={"installer_group": "mtm-installers"})]
    probes, ctx = pio.probe(_w(groups=("mtm-installers",), jobs=jobs), FakeSql(), catalog="main",
                            schema="migration", deployment=DEPLOY)
    assert ctx["installer_group"] == "mtm-installers"
    assert probes["records"] == {"group": "mtm-installers", "member": True}


def test_installer_group_membership_follows_the_account_group():
    """An account group not assigned to the workspace is missing from me().groups."""
    class AccountSql(FakeSql):
        def __call__(self, s):
            if "is_account_group_member('mtm-installers')" in s:
                self.calls.append(s)
                return [{"m": True}]
            return super().__call__(s)

    jobs = [_job(f"{DEPLOY} :: 99 setup", params={"installer_group": "mtm-installers"})]
    probes, _ = pio.probe(_w(groups=("data-eng",), jobs=jobs), AccountSql(), catalog="main",
                          schema="migration", deployment=DEPLOY)
    assert probes["records"]["member"] is True
    assert pio.group_member(FakeSql(deny_reads=True), "mtm-installers", ["mtm-installers"]) is True
    assert pio.group_member(FakeSql(deny_reads=True), "mtm-installers", ["data-eng"]) is False


def _lines():
    return pv.grant_plan(catalog="main", schema="migration", jobs_identity={"kind": "service_principal", "name": SP},
                         scopes=["sales.orders"], installer="ann@x.com", control_schema_exists=True)


def test_apply_writes_audit_before_each_grant_and_skips_held():
    lines = [ln for ln in _lines() if ln["stage"] == pv.STAGE_MAIN]
    held_key = pv.line_key(lines[0])
    sql = FakeSql()
    out = pio.apply_lines(sql, lines, catalog="main", schema="migration", door="cli", pid="abc123abc123",
                          operator="ann@x.com", held_before={held_key: {"BROWSE"}})
    assert out["results"][0]["outcome"] == "skipped"
    granted = [r for r in out["results"] if r["outcome"] == "granted"]
    assert len(granted) == len(lines) - 1
    for r in granted:
        i = sql.calls.index(r["statement"])
        assert sql.calls[i - 1].startswith("INSERT INTO main.migration.audit_log") and "'STARTED'" in sql.calls[i - 1]
        assert "'SUCCESS'" in sql.calls[i + 1]
    assert "INSTALL_GRANT_SKIPPED" in sql.calls[0]


def test_apply_never_grants_when_the_audit_row_cannot_be_written():
    lines = [ln for ln in _lines() if ln["stage"] == pv.STAGE_MAIN]
    sql = FakeSql(fail=("INSERT INTO",))
    out = pio.apply_lines(sql, lines, catalog="main", schema="migration", door="cli", pid="p",
                          operator="ann@x.com", held_before={})
    assert sql.grants() == []
    assert {r["outcome"] for r in out["results"]} == {"not_run"}


def test_bootstrap_and_records_defer_their_audit_rows():
    lines = [ln for ln in _lines() if ln["stage"] == pv.STAGE_RECORDS]
    sql = FakeSql()
    out = pio.apply_lines(sql, lines, catalog="main", schema="migration", door="notebook", pid="p",
                          operator="ann@x.com", held_before={}, audit_now=False)
    assert not any(c.startswith("INSERT") for c in sql.calls)
    assert len(out["deferred"]) == 2 * len(lines)
    pio.flush_audit(sql, "main", "migration", out["deferred"])
    assert sql.calls[-1].startswith("INSERT INTO main.migration.audit_log")


def test_one_failed_grant_does_not_stop_the_rest():
    lines = [ln for ln in _lines() if ln["stage"] == pv.STAGE_MAIN]
    sql = FakeSql(fail=("ON CATALOG `system`",))
    out = pio.apply_lines(sql, lines, catalog="main", schema="migration", door="cli", pid="p",
                          operator="ann@x.com", held_before={})
    outcomes = [r["outcome"] for r in out["results"]]
    assert "failed" in outcomes and outcomes.count("granted") == len(lines) - 1


def test_scope_and_ledger_records():
    lines = [ln for ln in _lines() if ln["stage"] == pv.STAGE_MAIN]
    sql = FakeSql()
    res = pio.apply_lines(sql, lines, catalog="main", schema="migration", door="cli", pid="p",
                          operator="ann@x.com", held_before={})["results"]
    assert pio.record_scopes(sql, catalog="main", schema="migration", scopes=["sales.orders"], operator="ann@x.com",
                             door="cli", results=res, convert_rights="all_at_once") is None
    assert sql.calls[-1].startswith("MERGE INTO main.migration.migration_scope")
    assert "'Change,Read'" in sql.calls[-1]
    assert pio.record_ledger(sql, catalog="main", schema="migration", results=res, operator="ann@x.com",
                             door="cli", pid="p") is None
    assert sql.calls[-1].startswith("MERGE INTO main.migration.ownership_ledger")
    assert "INSTALL|SCHEMA sales.orders|MANAGE|" in sql.calls[-1]


def test_records_read_through_statement_execution_coerce_ledger_booleans():
    key = "INSTALL|SCHEMA sales.orders|MANAGE|" + SP

    class StringSql(FakeSql):
        def __call__(self, s):
            if "ownership_ledger" in s and s.startswith("SELECT"):
                return [{"ledger_key": key, "op_kind": "GRANT", "securable_type": "SCHEMA",
                         "securable_name": "sales.orders", "privilege": "MANAGE", "grantee": SP,
                         "status": pv.LEDGER_STATUS_GRANTED, "dry_run": "false", "added": "true",
                         "prior_held": "false"}]
            return super().__call__(s)

    rec = pio.read_records(StringSql(), "main", "migration")
    assert rec["ledger"][0]["dry_run"] is False and rec["ledger"][0]["added"] is True
    plan = pio.handback_prepare({"records": rec}, {"scope_list": ["sales.orders"]})
    assert [r["ledger_key"] for r in plan["revokes"]] == [key]


def test_revoke_audits_first_and_flips_the_ledger():
    item = {"ledger_key": "INSTALL|SCHEMA sales.orders|MANAGE|" + SP, "securable_type": "SCHEMA",
            "securable": "sales.orders", "privilege": "MANAGE", "grantee": SP,
            "statement": pv.revoke_sql("MANAGE", "SCHEMA", "sales.orders", SP)}
    sql = FakeSql()
    out = pio.revoke_items(sql, [item], catalog="main", schema="migration", door="cli", pid="p", operator="ann@x.com")
    assert out[0]["outcome"] == "revoked"
    i = sql.calls.index(item["statement"])
    assert "INSTALL_REVOKE" in sql.calls[i - 1] and "'STARTED'" in sql.calls[i - 1]
    assert sql.calls[-2].startswith("UPDATE main.migration.ownership_ledger SET status = 'REVOKED'")
    assert sql.calls[-1].startswith("UPDATE main.migration.migration_actions SET status = 'DONE'")
    assert f"'RESTORE_OWNERSHIP|sales.orders|MANAGE:{SP}'" in sql.calls[-1]


def test_read_held_marks_owner_and_unknown():
    lines = [ln for ln in _lines() if ln["set"] == pv.SET_READ]

    def eff(securable_type, full_name, principal):
        if full_name == "sales":
            raise RuntimeError("denied")
        return NS(privilege_assignments=[NS(principal=principal, privileges=[NS(privilege=NS(value="SELECT"))])])

    w = NS(grants=NS(get_effective=eff),
           catalogs=NS(get=lambda n: NS(owner="x")), schemas=NS(get=lambda n: NS(owner=SP)),
           tables=NS(get=lambda n: NS(owner="x")))
    held = pio.read_held(w, lines)
    by = {pv.line_key(ln)[3]: held[pv.line_key(ln)] for ln in lines}
    assert by["sales"] is None
    assert "OWNER" in by["sales.orders"] and "SELECT" in by["sales.orders"]


def _full_w(eff_privs=("BROWSE",)):
    w = _w()
    w.grants = NS(get_effective=lambda securable_type, full_name, principal: NS(privilege_assignments=[
        NS(principal=principal, privileges=[NS(privilege=NS(value=p)) for p in eff_privs])]))
    w.catalogs = NS(get=lambda n: NS(owner="ms-admins"))
    w.schemas = NS(get=lambda n: NS(owner="ann@x.com" if n == "sales.orders" else "bob@x.com"))
    w.tables = NS(get=lambda n: NS(owner="x"))
    return w


def _opts(**kw):
    opts, errors = pv.parse_options({"catalog": "main", "schema": "migration", **kw}, door=pv.DOOR_CLI)
    assert errors == []
    return opts


def test_prepare_builds_the_plan_with_status_and_grantors():
    prep = pio.prepare(_full_w(), FakeSql(), opts=_opts(scopes="sales.orders,sales.returns"),
                       catalog="main", schema="migration", deployment=DEPLOY, door=pv.DOOR_CLI)
    assert prep["scopes"] == ["sales.orders", "sales.returns"] and prep["scopes_from"] == "option"
    assert prep["plan_id"] == pv.plan_id(prep["lines"])
    assert set(prep["statuses"].values()) <= {pv.STATUS_PRESENT, pv.STATUS_MISSING, pv.STATUS_UNKNOWN}
    blocked = [k for k in prep["grantors"] if k[3].startswith("sales.returns")]
    assert blocked, "sales.returns is owned by someone else, so its lines name who to ask"


def test_blank_scopes_with_none_stored_plan_all():
    prep = pio.prepare(_full_w(), FakeSql(), opts=_opts(), catalog="main", schema="migration",
                       deployment=DEPLOY, door=pv.DOOR_CLI)
    assert prep["scopes"] == ["sales.orders"] and prep["scopes_from"] == pv.SCOPES_FROM_ALL
    assert prep["scope_note"] is None


def test_scopes_all_takes_the_whole_catalog_when_you_can_grant_on_it():
    probes, ctx = pio.probe(_w(owner="ann@x.com"), FakeSql(), catalog="main", schema="migration",
                            deployment=DEPLOY)
    assert all(r["catalog_can_grant"] for r in probes["estate"])
    assert pv.rights_report(probes)["coverage"]["candidates"] == ["sales.*"]
    assert ctx["catalog_types_error"], "the fake client has no catalogs.list, so the read degrades"


def test_scopes_all_leaves_out_shared_and_foreign_catalogs():
    w = _w(owner="ann@x.com")
    w.catalogs = NS(list=lambda: [NS(name="Sales", catalog_type=NS(value="FOREIGN_CATALOG"))])
    probes, ctx = pio.probe(w, FakeSql(), catalog="main", schema="migration", deployment=DEPLOY)
    assert probes["estate"] == [] and ctx["catalog_types_error"] is None


class AuditSql(FakeSql):
    def __init__(self, audit):
        super().__init__()
        self.audit = audit

    def __call__(self, s):
        if s == pv.install_audit_sql("main", "migration"):
            self.calls.append(s)
            return self.audit
        return super().__call__(s)


def test_prepare_stops_when_the_jobs_moved_off_the_identity_that_holds_the_grants():
    line = {"set": pv.SET_READ, "grantee": "old@x.com", "grantee_kind": pv.KIND_USER, "privilege": "SELECT",
            "securable_type": "SCHEMA", "securable": "sales.orders"}
    audit = [{"action": pv.AUDIT_INSTALL_GRANT, "phase": "SUCCESS", "created_at": "2026-10-01 10:00:00",
              "detail": pv.audit_detail(line, door="cli", pid="p", added=True)}]
    prep = pio.prepare(_full_w(), AuditSql(audit), opts=_opts(), catalog="main", schema="migration",
                       deployment=DEPLOY, door=pv.DOOR_CLI)
    check = next(c for c in prep["rights"]["checks"] if c["key"] == "jobs_identity")
    assert check["status"] == pv.NO_GO and not prep["rights"]["can_install"]
    assert "jobs_identity=user:old@x.com, then redeploy" in check["detail"]
    prep = pio.prepare(_full_w(), AuditSql(audit), opts=_opts(jobs_identity=f"sp:{SP}"), catalog="main",
                       schema="migration", deployment=DEPLOY, door=pv.DOOR_CLI)
    assert prep["rights"]["can_install"]


def test_apply_stage_never_tries_a_line_the_person_cannot_grant():
    prep = pio.prepare(_full_w(eff_privs=()), FakeSql(), opts=_opts(scopes="sales.orders,sales.returns"),
                       catalog="main", schema="migration", deployment=DEPLOY, door=pv.DOOR_CLI)
    sql = FakeSql()
    out = pio.apply_stage(sql, prep, pv.STAGE_MAIN, catalog="main", schema="migration", door=pv.DOOR_CLI)
    cannot = [r for r in out["results"] if r["outcome"] == "cannot_grant"]
    assert cannot and all(r["grantor"] for r in cannot)
    assert not any(r["statement"] in sql.calls for r in cannot)
    assert any(r["outcome"] == "granted" for r in out["results"])


def test_handback_and_uninstall_prepare_fingerprint_their_plans():
    rows = [{"ledger_key": "INSTALL|SCHEMA sales.orders|MANAGE|" + SP, "securable_type": "SCHEMA",
             "securable_name": "sales.orders", "privilege": "MANAGE", "grantee": SP, "added": True,
             "prior_held": False, "dry_run": False, "status": pv.LEDGER_STATUS_HANDBACK_READY}]
    prep = {"records": {"ledger": rows, "audit": [], "states": {"sales.orders": {"CLEANED": 3}},
                        "window_ends": {}}, "context": {"user": "ann@x.com"}}
    hb = pio.handback_prepare(prep, _opts())
    assert len(hb["revokes"]) == 1 and hb["plan_id"] == pio.handback_pid(hb["revokes"])
    un = pio.uninstall_prepare(prep)
    assert un["refused"] is None and un["revokes"][0]["statement"] == hb["revokes"][0]["statement"]
    text = pv.revoke_text(hb, hb["plan_id"], title="Hand-back plan")
    assert text[0].startswith("Hand-back plan ") and "REVOKE MANAGE" in text[1]


def test_remove_scopes_marks_rows_and_skips_when_empty():
    sql = FakeSql()
    assert pio.remove_scopes(sql, catalog="main", schema="migration", scopes=[], operator="a") is None
    assert sql.calls == []
    pio.remove_scopes(sql, catalog="main", schema="migration", scopes=["sales.orders"], operator="a")
    assert "migration_scope" in sql.calls[-1]


def test_read_held_counts_privileges_held_through_a_group():
    lines = [ln for ln in _lines() if ln["set"] == pv.SET_READ]

    def eff(securable_type, full_name, principal):
        privs = ["SELECT", "USE SCHEMA", "USE CATALOG", "BROWSE"] if principal == "account users" else []
        return NS(privilege_assignments=[NS(principal=principal, privileges=[NS(privilege=NS(value=p)) for p in privs])])

    def deny(securable_type, full_name, principal):
        if principal == SP:
            raise RuntimeError("denied")
        return NS(privilege_assignments=[])

    owner = NS(get=lambda n: NS(owner="x"))
    w = NS(grants=NS(get_effective=eff), catalogs=owner, schemas=owner, tables=owner,
           service_principals=NS(list=lambda filter: [NS(groups=[NS(display="etl")])]))
    groups = pio.groups_resolver(w, {"user": "ann@x.com", "groups": []})
    assert groups(SP, pv.KIND_SERVICE_PRINCIPAL) == ["etl", "account users"]
    held = pio.read_held(w, lines, groups)
    assert all(s["status"] == pv.STATUS_PRESENT for s in pv.verify(lines, held))
    assert all(s["status"] == pv.STATUS_MISSING for s in pv.verify(lines, pio.read_held(w, lines)))
    w.grants = NS(get_effective=deny)
    assert all(s["status"] == pv.STATUS_UNKNOWN for s in pv.verify(lines, pio.read_held(w, lines, groups)))


def test_jobs_run_read_back_and_additive_grant():
    from databricks.sdk.service.iam import PermissionLevel

    def acl(jid):
        levels = {"1": ["CAN_MANAGE_RUN"], "2": ["IS_OWNER"], "3": ["CAN_VIEW"]}[jid]
        return NS(access_control_list=[NS(user_name=None, group_name="etl", service_principal_name=None,
                                          all_permissions=[NS(permission_level=NS(value=v)) for v in levels])])
    updates = []
    w = NS(permissions=NS(get=lambda t, jid: acl(jid),
                          update=lambda t, jid, access_control_list: updates.append((t, jid, access_control_list))))
    assert pio._jobs_run_held(w, ["1", "2"], [SP, "etl"]) == {pv.JOB_RUN_PRIVILEGE}
    assert pio._jobs_run_held(w, ["1", "3"], [SP, "etl"]) == set()
    line = {"grantee": SP, "grantee_kind": pv.KIND_SERVICE_PRINCIPAL, "job_ids": ["1", "3"]}
    assert pio.grant_job_run(w, line) is None
    assert [u[1] for u in updates] == ["1", "3"]
    req = updates[0][2][0]
    assert req.service_principal_name == SP and req.permission_level == PermissionLevel.CAN_MANAGE_RUN


def test_deploy_folder_open_reads_the_directory_acl():
    task = NS(notebook_task=NS(notebook_path="/Shared/.bundle/mtm/prod/files/backend/notebooks/99_setup"))
    job = NS(settings=NS(tasks=[task]))
    seen = []
    acl = [NS(group_name="users", all_permissions=[NS(permission_level=NS(value="CAN_MANAGE"))])]
    w = NS(workspace=NS(get_status=lambda p: (seen.append(p), NS(object_id=7))[1]),
           permissions=NS(get=lambda t, oid: NS(access_control_list=acl)))
    assert pio.deploy_folder_open(w, [job]) is True and seen == ["/Shared/.bundle/mtm/prod"]
    assert pio.deploy_folder_open(w, []) is None


def test_folder_read_back_and_additive_grant():
    from databricks.sdk.service.iam import PermissionLevel

    def entry(levels, group=None, sp=None):
        return NS(user_name=None, group_name=group, service_principal_name=sp,
                  all_permissions=[NS(permission_level=NS(value=v)) for v in levels])
    acls = {"/Users/ann/.bundle/mtm/e2e": [entry(["CAN_MANAGE"], sp="ann-sp")],
            "/Shared/.bundle/mtm/prod": [entry(["CAN_RUN"], group="users")]}
    updates = []
    w = NS(workspace=NS(get_status=lambda p: NS(object_id=p)),
           permissions=NS(get=lambda t, oid: NS(access_control_list=acls[oid]),
                          update=lambda t, oid, access_control_list: updates.append((t, oid, access_control_list))))
    assert pio._folder_read_held(w, "/Users/ann/.bundle/mtm/e2e", [SP]) == set()
    assert pio._folder_read_held(w, "/Shared/.bundle/mtm/prod", [SP]) == {pv.FOLDER_READ_PRIVILEGE}
    assert pio._folder_read_held(w, "/missing", [SP]) is None
    line = {"grantee": SP, "grantee_kind": pv.KIND_SERVICE_PRINCIPAL, "securable_type": pv.FOLDER_SECURABLE,
            "securable": "/Users/ann/.bundle/mtm/e2e"}
    assert pio.grant_api(w, line) is None
    (t, oid, (req,)), = updates
    assert t == "directories" and oid == "/Users/ann/.bundle/mtm/e2e"
    assert req.service_principal_name == SP and req.permission_level == PermissionLevel.CAN_READ
    job = NS(settings=NS(tasks=[NS(notebook_task=None),
                                NS(notebook_task=NS(notebook_path="/Users/ann/.bundle/mtm/e2e/files/x/99_setup"))]))
    assert pio.deploy_folder([job]) == "/Users/ann/.bundle/mtm/e2e"


class WaveSql(FakeSql):
    def __call__(self, s):
        if "FROM main.migration.wave_plan" in s:
            self.calls.append(s)
            return [{"c": "sales", "s": "orders", "plan_run_id": "plan-new"}]
        return super().__call__(s)


def test_grant_convert_pinned_to_an_old_plan_stops_before_any_grant():
    opts = _opts(action=pv.ACTION_GRANT_CONVERT, wave_id="w1", plan_run_id="plan-old")
    prep = pio.prepare(_full_w(), WaveSql(), opts=opts, catalog="main", schema="migration",
                       deployment=DEPLOY, door=pv.DOOR_CLI)
    assert prep["wave"]["schemas"] == [] and "plan-old is no longer the current plan (plan-new)" in prep["wave"]["error"]
    prep = pio.prepare(_full_w(), WaveSql(), opts={**opts, "plan_run_id": "plan-new"}, catalog="main",
                       schema="migration", deployment=DEPLOY, door=pv.DOOR_CLI)
    assert prep["wave"]["error"] is None and prep["wave"]["schemas"] == ["sales.orders"]
    assert pv.parse_options({"plan_run_id": "x'; DROP"})[1]


def test_e2e_helper_jobs_are_not_part_of_the_jobs_identity():
    jobs = [_job(f"{DEPLOY} :: 00 profiler"), _job(f"{DEPLOY} :: e2e seed stream (integration)", run_as="ann@x.com")]
    probes, _ = pio.probe(_w(jobs=jobs), FakeSql(), catalog="main", schema="migration", deployment=DEPLOY)
    assert probes["jobs"]["agree"] and probes["jobs"]["count"] == 1
