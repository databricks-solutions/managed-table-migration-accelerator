"""Install and permissions — the thin I/O edge shared by the notebook and the CLI.

Every judgment lives in ``migration.provision`` (pure). This module only reads and writes:

* ``run_sql(statement) -> list[dict]`` is injected — ``spark.sql`` in the install notebook,
  Statement Execution on the deploy's warehouse in ``backend/scripts/install.py``
  (``statement_runner``). Both run as the person installing, so every probe and grant is theirs.
* ``w`` is a ``WorkspaceClient`` for identity, jobs, apps and effective-permission read-backs.

Only the install notebook and the install CLI may import this module (guarded by
``test_grant_execution_sites.py``). Every probe degrades to ``None`` (read as UNKNOWN by
``provision.rights_report``, never GO); nothing here runs ``SET MANAGED`` or touches data.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import ownership as _own
from . import provision as pv

RunSql = Callable[[str], List[dict]]

SETUP_LABEL = "99 setup"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _err(ex: BaseException) -> str:
    return f"{type(ex).__name__}: {str(ex)[:200]}"


# --------------------------------------------------------------------------- #
# SQL runners
# --------------------------------------------------------------------------- #

def spark_runner(spark) -> RunSql:
    return lambda s: [r.asDict() for r in spark.sql(s).collect()]


def statement_runner(w, warehouse_id: str, *, timeout_s: int = 600) -> RunSql:
    """Statement Execution as the caller, on ``warehouse_id``; raises on a failed statement."""
    from databricks.sdk.service.sql import StatementState  # noqa: PLC0415 — SDK only at the edge

    def run(statement: str) -> List[dict]:
        resp = w.statement_execution.execute_statement(warehouse_id=warehouse_id, statement=statement,
                                                       wait_timeout="50s")
        deadline = time.monotonic() + timeout_s
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.monotonic() > deadline:
                raise TimeoutError(f"statement did not finish in {timeout_s}s")
            time.sleep(2)
            resp = w.statement_execution.get_statement(resp.statement_id)
        state = resp.status.state if resp.status else None
        if state != StatementState.SUCCEEDED:
            err = resp.status.error if resp.status else None
            raise RuntimeError(f"[{getattr(state, 'value', state)}] {getattr(err, 'message', '') or ''}".strip())
        cols = [c.name for c in (resp.manifest.schema.columns or [])] if resp.manifest and resp.manifest.schema else []
        rows: List[dict] = [dict(zip(cols, r)) for r in ((resp.result.data_array if resp.result else None) or [])]
        chunk = resp.result.next_chunk_index if resp.result else None
        while chunk is not None:
            part = w.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk)
            rows += [dict(zip(cols, r)) for r in (part.data_array or [])]
            chunk = part.next_chunk_index
        return rows

    return run


def _try(run_sql: RunSql, statement: str) -> Tuple[Optional[List[dict]], Optional[str]]:
    try:
        return run_sql(statement), None
    except Exception as ex:  # noqa: BLE001 — every read degrades; the caller stamps UNKNOWN
        return None, _err(ex)


# --------------------------------------------------------------------------- #
# Who is installing
# --------------------------------------------------------------------------- #

def check_warehouse(run_sql: RunSql) -> Optional[str]:
    """None when the runner can execute a trivial query, else the error (no CAN_USE, stopped, …)."""
    return _try(run_sql, "SELECT 1 AS ok")[1]


def whoami(w) -> Tuple[Optional[str], List[str]]:
    try:
        me = w.current_user.me()
    except Exception:  # noqa: BLE001
        return None, []
    groups = [g.display for g in (me.groups or []) if getattr(g, "display", None)]
    return me.user_name, groups


def group_member(run_sql: RunSql, group: str, groups: Sequence[str]) -> bool:
    """Is the caller in ``group``? ``is_account_group_member`` first: Unity Catalog grants follow
    account groups, and ``me().groups`` lists only groups assigned to this workspace. The
    workspace list is the fallback when the query cannot run."""
    rows, _ = _try(run_sql, pv.group_member_sql(group))
    if rows:
        return str(rows[0].get("m")).lower() == "true"
    return any(pv.same_principal(group, g) for g in groups)


def metastore_admin(w, principals: Sequence[str]) -> Optional[bool]:
    try:
        owner = w.metastores.summary().owner
    except Exception:  # noqa: BLE001
        return None
    return any(pv.same_principal(owner, p) for p in principals)


# --------------------------------------------------------------------------- #
# Deployed jobs: identity, installer_group, app name
# --------------------------------------------------------------------------- #

E2E_HELPER_STAGE_PREFIX = "e2e "


def deployed_jobs(w, deployment: Optional[str]) -> List[Any]:
    """Full job objects of this deployment (``[<target>] <bundle> :: …``, dev prefix allowed).

    The dev-only e2e helper jobs (``:: e2e …``) are left out: they write the test estate as the
    deployer and do not follow ``jobs_run_as``."""
    marker = f"{deployment} :: " if deployment else None
    out = []
    for j in w.jobs.list():
        name = (j.settings.name if j.settings else "") or ""
        if marker and marker not in name:
            continue
        if not marker and " :: " not in name:
            continue
        if name.split(" :: ", 1)[-1].startswith(E2E_HELPER_STAGE_PREFIX):
            continue
        try:
            out.append(w.jobs.get(j.job_id))
        except Exception:  # noqa: BLE001 — a job we cannot read is skipped, not guessed
            continue
    return out


def job_identity(job) -> Optional[dict]:
    rs = getattr(getattr(job, "settings", None), "run_as", None)
    if rs is not None:
        ident = pv.identity_from_run_as({
            "service_principal_name": getattr(rs, "service_principal_name", None),
            "group_name": getattr(rs, "group_name", None),
            "user_name": getattr(rs, "user_name", None)})
        if ident:
            return ident
    return pv.identity_from_principal(getattr(job, "run_as_user_name", None))


def setup_parameters(jobs: Iterable[Any]) -> Dict[str, str]:
    """The deployed 99 setup task's base parameters (``installer_group``, ``app_name``, …)."""
    for j in jobs:
        name = (j.settings.name if j.settings else "") or ""
        if not name.endswith(":: " + SETUP_LABEL):
            continue
        for t in (j.settings.tasks or []):
            nt = getattr(t, "notebook_task", None)
            if nt and nt.base_parameters is not None:
                return dict(nt.base_parameters)
    return {}


def deployed_setup_parameters(w, setup_job_id) -> Dict[str, str]:
    """This deployment's 99 setup base parameters, as the last deploy set them; {} when unknown."""
    if not setup_job_id:
        return {}
    try:
        job = w.jobs.get(setup_job_id)
    except Exception:  # noqa: BLE001 — unreadable means unknown, never a guess
        return {}
    return {k: (v or "").strip() for k, v in setup_parameters([job]).items()}


def deployed_app_name(w, setup_job_id) -> Optional[str]:
    """The app name the bundle gave this deployment's 99 setup (``var.app_name``). A target that
    renames the app is only knowable here: the bundle name would resolve another target's app."""
    return deployed_setup_parameters(w, setup_job_id).get("app_name") or None


def deploy_folder_open(w, jobs: Iterable[Any]) -> Optional[bool]:
    """Whether every workspace user can edit the deployment folder; None when it cannot be read."""
    root = deploy_folder(jobs)
    if not root:
        return None
    try:
        oid = w.workspace.get_status(root).object_id
        acl = w.permissions.get("directories", str(oid)).access_control_list or []
    except Exception:  # noqa: BLE001
        return None
    return pv.folder_open_to_users(
        {"group_name": a.group_name,
         "levels": [str(getattr(p.permission_level, "value", p.permission_level)) for p in a.all_permissions or []]}
        for a in acl)


def sp_active(w, application_id: str) -> Optional[bool]:
    try:
        sps = list(w.service_principals.list(filter=f'applicationId eq "{application_id}"'))
    except Exception:  # noqa: BLE001
        return None
    if not sps:
        return None
    return bool(sps[0].active) if sps[0].active is not None else None


ALL_ACCOUNT_USERS = "account users"


def principal_groups(w, principal: str, kind: Optional[str], *, me: Optional[str] = None,
                     my_groups: Sequence[str] = ()) -> Optional[List[str]]:
    """The groups a grant to ``principal`` can come through (direct membership, plus
    ``account users`` for a person or service principal), or None when they cannot be read."""
    if kind == pv.KIND_GROUP:
        return []
    if me and pv.same_principal(principal, me):
        return list(my_groups) + [ALL_ACCOUNT_USERS]
    try:
        if kind == pv.KIND_SERVICE_PRINCIPAL:
            found = list(w.service_principals.list(filter=f'applicationId eq "{principal}"'))
        else:
            found = list(w.users.list(filter=f'userName eq "{principal}"', attributes="groups"))
    except Exception:  # noqa: BLE001
        return None
    groups = [g.display for f in found[:1] for g in (getattr(f, "groups", None) or []) if getattr(g, "display", None)]
    return groups + [ALL_ACCOUNT_USERS]


def groups_resolver(w, context: Mapping[str, Any]) -> Callable[[str, Optional[str]], Optional[List[str]]]:
    cache: Dict[str, Optional[List[str]]] = {}

    def resolve(principal: str, kind: Optional[str]) -> Optional[List[str]]:
        if principal not in cache:
            cache[principal] = principal_groups(w, principal, kind, me=context.get("user"),
                                                my_groups=context.get("groups") or ())
        return cache[principal]
    return resolve


def jobs_probe(w, jobs: Sequence[Any]) -> dict:
    by_job = {(j.settings.name if j.settings else str(j.job_id)): job_identity(j) for j in jobs}
    summary = pv.jobs_identity_summary(by_job)
    ident = summary.get("identity")
    if ident and ident["kind"] == pv.KIND_SERVICE_PRINCIPAL:
        summary["active"] = sp_active(w, ident["name"])
    if not jobs:
        summary.update({"identity": None, "agree": False, "distinct": []})
    return summary


def app_service_principal(w, app_name: Optional[str]) -> Optional[str]:
    if not app_name:
        return None
    try:
        return w.apps.get(name=app_name).service_principal_client_id
    except Exception:  # noqa: BLE001 — app not deployed yet / no apps access
        return None


# --------------------------------------------------------------------------- #
# Ownership, MANAGE holders, estate
# --------------------------------------------------------------------------- #

def read_owners(run_sql: RunSql) -> dict:
    """What the installer can see of catalog / schema owners and MANAGE grants (visible only)."""
    cats, e1 = _try(run_sql, "SELECT catalog_name, catalog_owner FROM system.information_schema.catalogs")
    schs, e2 = _try(run_sql, "SELECT catalog_name, schema_name, schema_owner FROM system.information_schema.schemata")
    cman, _ = _try(run_sql, "SELECT catalog_name, grantee FROM system.information_schema.catalog_privileges "
                            "WHERE privilege_type = 'MANAGE'")
    sman, _ = _try(run_sql, "SELECT catalog_name, schema_name, grantee FROM system.information_schema.schema_privileges "
                            "WHERE privilege_type = 'MANAGE'")
    out = {"catalog_owner": {}, "schema_owner": {}, "catalog_manage": {}, "schema_manage": {},
           "errors": [e for e in (e1, e2) if e]}
    for r in cats or []:
        out["catalog_owner"][str(r["catalog_name"]).lower()] = r.get("catalog_owner")
    for r in schs or []:
        out["schema_owner"][f"{r['catalog_name']}.{r['schema_name']}".lower()] = r.get("schema_owner")
    for r in cman or []:
        out["catalog_manage"].setdefault(str(r["catalog_name"]).lower(), []).append(r.get("grantee"))
    for r in sman or []:
        out["schema_manage"].setdefault(f"{r['catalog_name']}.{r['schema_name']}".lower(), []).append(r.get("grantee"))
    out["readable"] = cats is not None and schs is not None
    return out


def can_grant_schema(owners: Mapping[str, Any], principals: Sequence[str], schema_fqn: str,
                     ma: Optional[bool]) -> Optional[bool]:
    cat = schema_fqn.split(".")[0]
    if not owners.get("readable"):
        return True if ma else None
    return pv.can_grant_on(
        principals, schema_owner=owners["schema_owner"].get(schema_fqn),
        catalog_owner=owners["catalog_owner"].get(cat),
        manage_holders=(owners["schema_manage"].get(schema_fqn, []) + owners["catalog_manage"].get(cat, [])),
        metastore_admin=ma)


def can_grant_catalog(owners: Mapping[str, Any], principals: Sequence[str], cat: str,
                      ma: Optional[bool]) -> Optional[bool]:
    if not owners.get("readable"):
        return True if ma else None
    return pv.can_grant_on(principals, catalog_owner=owners["catalog_owner"].get(cat),
                           schema_owner=None, manage_holders=owners["catalog_manage"].get(cat, []),
                           metastore_admin=ma) or False


ESTATE_SQL = ("SELECT table_catalog, table_schema, count(*) AS tables FROM system.information_schema.tables "
              "WHERE table_type = 'EXTERNAL' AND upper(coalesce(data_source_format, '')) = 'DELTA' "
              "AND table_catalog <> 'system' AND table_schema <> 'information_schema' GROUP BY 1, 2")


def catalog_types(w) -> Tuple[Dict[str, Optional[str]], Optional[str]]:
    """``{catalog: catalog_type}`` for the catalogs the installer can see, or ``({}, error)``."""
    try:
        out = {}
        for c in w.catalogs.list():
            t = getattr(c, "catalog_type", None)
            out[str(c.name).lower()] = getattr(t, "value", t)
        return out, None
    except Exception as ex:  # noqa: BLE001
        return {}, _err(ex)


def estate_rows(run_sql: RunSql, owners: Mapping[str, Any], principals: Sequence[str],
                ma: Optional[bool], excluded: Iterable[str] = ()) -> Tuple[Optional[List[dict]], Optional[str]]:
    rows, err = _try(run_sql, ESTATE_SQL)
    if rows is None:
        return None, err
    skip = {str(c).lower() for c in excluded} | set(pv.NON_MIGRATABLE_CATALOGS)
    out = []
    for r in rows:
        cat = str(r["table_catalog"]).lower()
        if cat in skip:
            continue
        fqn = f"{r['table_catalog']}.{r['table_schema']}".lower()
        can = can_grant_schema(owners, principals, fqn, ma)
        out.append({"catalog": cat, "schema": str(r["table_schema"]).lower(),
                    "tables": int(r.get("tables") or 0), "can_grant": bool(can),
                    "catalog_can_grant": bool(can_grant_catalog(owners, principals, cat, ma)),
                    "grantor": None if can else pv.schema_grantor(owners["schema_owner"].get(fqn),
                                                                  owners["catalog_owner"].get(cat))})
    return sorted(out, key=lambda x: (x["catalog"], x["schema"])), None


# --------------------------------------------------------------------------- #
# The whole probe
# --------------------------------------------------------------------------- #

def probe(w, run_sql: RunSql, *, catalog: str, schema: str, deployment: Optional[str],
          app_name: Optional[str] = None, door: str = pv.DOOR_NOTEBOOK,
          warehouse_usable: Optional[bool] = None) -> Tuple[dict, dict]:
    """``(probes, context)``: ``probes`` feeds ``provision.rights_report``; ``context`` carries what
    the plan and the apply step need (who, groups, owners, the jobs' identity, app SP, …)."""
    user, groups = whoami(w)
    principals = pv.principals_of(user, groups)
    ma = metastore_admin(w, principals)
    owners = read_owners(run_sql)
    jobs = deployed_jobs(w, deployment)
    setup = setup_parameters(jobs)
    jobs_summary = jobs_probe(w, jobs)
    installer_group = (setup.get("installer_group") or "").strip() or None
    app = (app_name or setup.get("app_name") or "").strip() or None
    app_sp = app_service_principal(w, app)

    cat = catalog.lower()
    ctl_fqn = f"{cat}.{schema.lower()}"
    exists = (ctl_fqn in owners["schema_owner"]) if owners.get("readable") else None
    ctl_can = can_grant_catalog(owners, principals, cat, ma)
    control = {"catalog": catalog, "schema": schema, "exists": bool(exists),
               "can_grant": ctl_can, "grantor": pv.schema_grantor(None, owners["catalog_owner"].get(cat))}
    if exists is None and ctl_can is None:
        control = {}
    records = {"group": installer_group}
    if installer_group:
        records["member"] = group_member(run_sql, installer_group, groups)
    else:
        records["can_self_grant"] = (can_grant_schema(owners, principals, ctl_fqn, ma)
                                     if exists else ctl_can)
    types, types_err = catalog_types(w)
    estate, estate_err = estate_rows(run_sql, owners, principals, ma, pv.excluded_catalogs(types))

    probes = {"installer": user, "workspace_admin": "admins" in groups, "metastore_admin": ma,
              "control": control, "jobs": jobs_summary, "records": records,
              "estate": estate or []}
    if warehouse_usable is not None:
        probes["warehouse_can_use"] = warehouse_usable
    if deploy_folder_open(w, jobs):
        probes["deploy_folder_open"] = True
    grantable_schemas = [f"{r['catalog']}.{r['schema']}" for r in (estate or []) if r["can_grant"]]
    grantable_catalogs = sorted({c for c in owners["catalog_owner"]
                                 if can_grant_catalog(owners, principals, c, ma)})
    context = {"user": user, "groups": groups, "principals": principals, "metastore_admin": ma,
               "owners": owners, "jobs": jobs, "jobs_identity": jobs_summary.get("identity"),
               "installer_group": installer_group, "app_name": app, "app_sp": app_sp,
               "control_exists": bool(exists), "grantable_schemas": grantable_schemas,
               "grantable_catalogs": grantable_catalogs, "estate_error": estate_err,
               "catalog_types_error": types_err,
               "door": door, "deployment": deployment}
    return probes, context


def plan_for(context: Mapping[str, Any], opts: Mapping[str, Any], *, catalog: str, schema: str,
             wave_schemas: Sequence[str] = ()) -> List[dict]:
    ident = opts.get("identity") or context.get("jobs_identity")
    return pv.grant_plan(
        catalog=catalog, schema=schema, jobs_identity=ident, app_sp=context.get("app_sp"),
        scopes=opts.get("scope_list") or [], convert_rights=opts.get("convert_rights") or pv.CONVERT_ALL_AT_ONCE,
        action=opts.get("action") or pv.ACTION_INSTALL, wave_schemas=wave_schemas,
        wave_id=opts.get("wave_id") or None, grantee_group=opts.get("grantee_group") or None,
        installer=context.get("user"), installer_group=context.get("installer_group"),
        control_schema_exists=bool(context.get("control_exists")),
        deployment=context.get("deployment"), job_ids=[j.job_id for j in context.get("jobs") or []],
        deploy_folder=deploy_folder(context.get("jobs") or []))


def grantors_for(lines: Iterable[Mapping[str, Any]], context: Mapping[str, Any]) -> Dict[Tuple, str]:
    """For each installer line the person cannot issue, who can."""
    out = {}
    for ln in lines:
        can, who = pv.line_grantable(ln, grantable_schemas=context.get("grantable_schemas") or [],
                                     grantable_catalogs=context.get("grantable_catalogs") or [],
                                     metastore_admin=context.get("metastore_admin"))
        if can is False:
            out[pv.line_key(ln)] = who
    return out


# --------------------------------------------------------------------------- #
# Read-backs
# --------------------------------------------------------------------------- #

def _effective(w, securable_type: str, full_name: str, principal: str) -> Optional[set]:
    st = securable_type.lower()
    try:
        try:
            resp = w.grants.get_effective(securable_type=st, full_name=full_name, principal=principal)
        except AttributeError:  # older SDKs take the SecurableType enum
            from databricks.sdk.service.catalog import SecurableType  # noqa: PLC0415
            resp = w.grants.get_effective(securable_type=SecurableType(st.upper()), full_name=full_name,
                                          principal=principal)
    except Exception:  # noqa: BLE001 — unreadable is UNKNOWN, never present
        return None
    held = set()
    for a in resp.privilege_assignments or []:
        if a.principal and not pv.same_principal(a.principal, principal):
            continue
        for p in a.privileges or []:
            v = getattr(p.privilege, "value", p.privilege)
            if v:
                held.add(str(v))
    return held


def _owner(w, cache: dict, securable_type: str, full_name: str) -> Optional[str]:
    key = (securable_type, full_name)
    if key not in cache:
        try:
            api = {"CATALOG": w.catalogs, "SCHEMA": w.schemas, "TABLE": w.tables}[securable_type]
            cache[key] = api.get(full_name).owner
        except Exception:  # noqa: BLE001
            cache[key] = None
    return cache[key]


def _job_levels(w, job_id: str, principals: Sequence[str]) -> Optional[set]:
    """Permission levels ``principals`` hold on one job (direct or inherited), or None if unreadable."""
    try:
        acl = w.permissions.get("jobs", str(job_id)).access_control_list or []
    except Exception:  # noqa: BLE001
        return None
    out: set = set()
    for a in acl:
        who = a.user_name or a.group_name or a.service_principal_name
        if any(pv.same_principal(who, p) for p in principals):
            for p in a.all_permissions or []:
                out.add(str(getattr(p.permission_level, "value", p.permission_level)))
    return out


_FOLDER_READ_LEVELS = frozenset({"CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"})


def _folder_id(w, folder: str) -> str:
    return str(w.workspace.get_status(folder).object_id)


def _folder_read_held(w, folder: str, principals: Sequence[str]) -> Optional[set]:
    """``{CAN_READ}`` when ``principals`` (or the workspace ``users`` group every identity is in)
    can read the folder, ``set()`` when not, None when its permissions cannot be read."""
    try:
        acl = w.permissions.get("directories", _folder_id(w, folder)).access_control_list or []
    except Exception:  # noqa: BLE001
        return None
    for a in acl:
        who = a.user_name or a.group_name or a.service_principal_name
        if str(a.group_name or "").lower() == pv.WORKSPACE_USERS_GROUP or any(pv.same_principal(who, p) for p in principals):
            levels = {str(getattr(p.permission_level, "value", p.permission_level)) for p in a.all_permissions or []}
            if levels & _FOLDER_READ_LEVELS:
                return {pv.FOLDER_READ_PRIVILEGE}
    return set()


def grant_folder_read(w, line: Mapping[str, Any]) -> Optional[str]:
    """Add ``CAN_READ`` on the deployment folder for the line's grantee (additive). The error, or None."""
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel  # noqa: PLC0415

    key = {pv.KIND_SERVICE_PRINCIPAL: "service_principal_name", pv.KIND_GROUP: "group_name"}.get(
        line.get("grantee_kind"), "user_name")
    try:
        w.permissions.update("directories", _folder_id(w, line["securable"]), access_control_list=[
            AccessControlRequest(**{key: line["grantee"]}, permission_level=PermissionLevel.CAN_READ)])
    except Exception as ex:  # noqa: BLE001
        return f"folder {line['securable']}: {_err(ex)}"
    return None


def grant_api(w, line: Mapping[str, Any]) -> Optional[str]:
    """Run a line that is a workspace permission, not SQL."""
    if str(line.get("securable_type")).upper() == pv.FOLDER_SECURABLE:
        return grant_folder_read(w, line)
    return grant_job_run(w, line)


def deploy_folder(jobs: Iterable[Any]) -> Optional[str]:
    """The deployment's root folder, from the first deployed notebook task path."""
    for j in jobs:
        for t in getattr(getattr(j, "settings", None), "tasks", None) or []:
            folder = pv.deploy_root(getattr(getattr(t, "notebook_task", None), "notebook_path", None))
            if folder:
                return folder
    return None


def _jobs_run_held(w, job_ids: Sequence[str], principals: Sequence[str]) -> Optional[set]:
    """``{CAN_MANAGE_RUN}`` when every job grants it (or more), ``set()`` when one does not, None
    when one could not be read and none is missing."""
    unreadable = False
    for jid in job_ids:
        levels = _job_levels(w, jid, principals)
        if levels is None:
            unreadable = True
        elif not pv.held_covers(levels, pv.JOB_RUN_PRIVILEGE):
            return set()
    return None if unreadable else {pv.JOB_RUN_PRIVILEGE}


def grant_job_run(w, line: Mapping[str, Any]) -> Optional[str]:
    """Add ``CAN_MANAGE_RUN`` for the line's grantee on each of its jobs (an additive update, so
    the app's own run permission is kept). The first error, or None."""
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel  # noqa: PLC0415

    key = {pv.KIND_SERVICE_PRINCIPAL: "service_principal_name", pv.KIND_GROUP: "group_name"}.get(
        line.get("grantee_kind"), "user_name")
    errors = []
    for jid in line.get("job_ids") or []:
        try:
            w.permissions.update("jobs", str(jid), access_control_list=[AccessControlRequest(
                **{key: line["grantee"]}, permission_level=PermissionLevel.CAN_MANAGE_RUN)])
        except Exception as ex:  # noqa: BLE001
            errors.append(f"job {jid}: {_err(ex)}")
    return "; ".join(errors) or None


def read_held(w, lines: Iterable[Mapping[str, Any]],
              groups_of: Optional[Callable[[str, Optional[str]], Optional[List[str]]]] = None
              ) -> Dict[Tuple, Optional[set]]:
    """``line_key -> effective privileges`` of each line's grantee (``OWNER`` when it, or a group
    it is in, owns the securable or a parent), or None when unreadable.

    ``get_effective`` filtered to one principal leaves out what it holds through a group, so when
    the grantee's own privileges do not cover the line, its groups (``groups_of``) are read too.
    An unreadable own read stays None unless a group proves the privilege.
    """
    owners: dict = {}
    eff: Dict[Tuple[str, str, str], Optional[set]] = {}
    out: Dict[Tuple, Optional[set]] = {}

    def effective(stype: str, fqn: str, principal: str) -> Optional[set]:
        k = (stype, fqn, principal)
        if k not in eff:
            eff[k] = _effective(w, stype, fqn, principal)
        return eff[k]

    for ln in lines:
        stype, fqn, grantee = str(ln["securable_type"]).upper(), str(ln["securable"]), str(ln["grantee"])
        groups = (groups_of(grantee, ln.get("grantee_kind")) if groups_of else None) or []
        if stype == pv.JOBS_SECURABLE:
            out[pv.line_key(ln)] = _jobs_run_held(w, ln.get("job_ids") or [], [grantee] + list(groups))
            continue
        if stype == pv.FOLDER_SECURABLE:
            out[pv.line_key(ln)] = _folder_read_held(w, fqn, [grantee] + list(groups))
            continue
        held = effective(stype, fqn, grantee)
        parts = fqn.split(".")
        chain = [("CATALOG", parts[0])] + ([("SCHEMA", ".".join(parts[:2]))] if len(parts) >= 2 else []) + \
                ([("TABLE", fqn)] if len(parts) == 3 else [])
        whos = [grantee] + list(groups)
        if any(any(pv.same_principal(_owner(w, owners, t, n), p) for p in whos) for t, n in chain):
            held = (held or set()) | {"OWNER"}
        if not pv.held_covers(held, ln["privilege"]):
            via: set = set()
            for g in groups:
                via |= effective(stype, fqn, g) or set()
                if pv.held_covers(via, ln["privilege"]):
                    break
            if pv.held_covers(via, ln["privilege"]):
                held = (held or set()) | via
            elif held is not None:
                held = held | via
        out[pv.line_key(ln)] = held
    return out


def read_back(w, prep: Mapping[str, Any]) -> List[dict]:
    """Every installer line of the plan with its live ``status`` (after an apply)."""
    inst = pv.installer_lines(prep["lines"])
    return pv.verify(inst, read_held(w, inst, groups_resolver(w, prep["context"])))


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def _write_audit(run_sql: RunSql, catalog: str, schema: str, rows: Sequence[dict]) -> Optional[str]:
    if not rows:
        return None
    _, err = _try(run_sql, pv.audit_insert_sql(catalog, schema, rows))
    return err


def apply_lines(run_sql: RunSql, lines: Sequence[Mapping[str, Any]], *, catalog: str, schema: str,
                door: str, pid: str, operator: Optional[str], held_before: Mapping[Tuple, Optional[set]],
                audit_now: bool = True, api: Optional[Callable[[Mapping[str, Any]], Optional[str]]] = None) -> dict:
    """Run each installer line not already held, in order.

    With ``audit_now`` each grant writes its ``STARTED`` audit row first, then ``SUCCESS`` /
    ``FAILED``; a held line writes ``INSTALL_GRANT_SKIPPED``. Without it (the bootstrap and records
    stages, before the installer can write ``audit_log``) the rows are returned in ``deferred`` for
    ``flush_audit``. One failed grant never stops the rest.
    """
    results, deferred, audit_errors = [], [], []
    for ln in pv.installer_lines(lines):
        k = pv.line_key(ln)
        prior = pv.held_covers(held_before.get(k), ln["privilege"])
        now = _now()
        if prior:
            row = pv.install_audit_row(ln, action=pv.AUDIT_INSTALL_GRANT_SKIPPED, phase="SUCCESS", door=door,
                                       pid=pid, operator=operator, now=now, added=False, prior_held=True)
            (deferred.append(row) if not audit_now else audit_errors.append(_write_audit(run_sql, catalog, schema, [row])))
            results.append({**ln, "outcome": "skipped", "added": False, "prior_held": True})
            continue
        started = pv.install_audit_row(ln, action=pv.AUDIT_INSTALL_GRANT, phase="STARTED", door=door,
                                       pid=pid, operator=operator, now=now, prior_held=prior)
        if audit_now:
            err = _write_audit(run_sql, catalog, schema, [started])
            if err:
                results.append({**ln, "outcome": "not_run", "error": f"audit row could not be written first ({err})"})
                continue
        else:
            deferred.append(started)
        if str(ln["securable_type"]).upper() in pv.API_SECURABLES:
            gerr = api(ln) if api else "no workspace client to update workspace permissions"
        else:
            _, gerr = _try(run_sql, ln["statement"])
        done = pv.install_audit_row(ln, action=pv.AUDIT_INSTALL_GRANT, phase="FAILED" if gerr else "SUCCESS",
                                    door=door, pid=pid, operator=operator, now=_now(), added=not gerr,
                                    prior_held=prior, error=gerr)
        (deferred.append(done) if not audit_now else audit_errors.append(_write_audit(run_sql, catalog, schema, [done])))
        results.append({**ln, "outcome": "failed" if gerr else "granted", "added": not gerr,
                        "prior_held": prior, "error": gerr})
    return {"results": results, "deferred": deferred, "audit_errors": [e for e in audit_errors if e]}


def flush_audit(run_sql: RunSql, catalog: str, schema: str, rows: Sequence[dict]) -> Optional[str]:
    return _write_audit(run_sql, catalog, schema, list(rows))


def record_scopes(run_sql: RunSql, *, catalog: str, schema: str, scopes: Sequence[str], operator: Optional[str],
                  door: str, results: Sequence[Mapping[str, Any]], convert_rights: str) -> Optional[str]:
    """MERGE the scopes that got Read (and Change) into ``migration_scope``."""
    applied: Dict[str, set] = {}
    for r in results:
        if r.get("outcome") in ("granted", "skipped") and r.get("scope") and r["set"] in (pv.SET_READ, pv.SET_CHANGE):
            applied.setdefault(r["scope"], set()).add(r["set"])
    keep = [s for s in scopes if pv.SET_READ in applied.get(s, set())]
    if not keep:
        return None
    now = _now()
    rows = []
    for s in keep:
        rows += pv.scope_rows([s], added_by=operator, door=door, sets=sorted(applied[s]),
                              convert_rights=convert_rights, now=now)
    _, err = _try(run_sql, pv.scope_merge_sql(catalog, schema, rows))
    return err


def remove_scopes(run_sql: RunSql, *, catalog: str, schema: str, scopes: Sequence[str],
                  operator: Optional[str]) -> Optional[str]:
    """Mark scopes removed in ``migration_scope`` (uninstall). Rows are kept, never deleted."""
    if not scopes:
        return None
    _, err = _try(run_sql, pv.scope_remove_sql(catalog, schema, scopes, removed_by=operator, now=_now()))
    return err


def record_ledger(run_sql: RunSql, *, catalog: str, schema: str, results: Sequence[Mapping[str, Any]],
                  operator: Optional[str], door: str, pid: str) -> Optional[str]:
    """One ownership_ledger row per Change line granted (``added``) or found held (``prior_held``)."""
    now = _now()
    rows = [pv.ledger_row(r, installer=operator, door=door, pid=pid, now=now, added=bool(r.get("added")),
                          prior_held=bool(r.get("prior_held")))
            for r in results if r["set"] == pv.SET_CHANGE and r.get("outcome") in ("granted", "skipped")]
    if not rows:
        return None
    _, err = _try(run_sql, pv.ledger_merge_sql(catalog, schema, rows, _own.LEDGER_COLUMNS,
                                               _own.LEDGER_MUTABLE_COLUMNS))
    return err


# --------------------------------------------------------------------------- #
# Records: stored scopes, ledger, audit, manifest states
# --------------------------------------------------------------------------- #

LEDGER_BOOLEAN_COLUMNS = tuple(c for c, t in _own.LEDGER_COLUMNS if t == "BOOLEAN")


def coerce_ledger(rows: Iterable[Mapping[str, Any]]) -> List[dict]:
    """Ledger rows with real booleans: Statement Execution returns every value as a string, and
    ``'false'`` is truthy."""
    out = []
    for r in rows or []:
        row = dict(r)
        for col in LEDGER_BOOLEAN_COLUMNS:
            if row.get(col) is not None:
                row[col] = bool(pv._bool(row[col]))
        out.append(row)
    return out


def read_records(run_sql: RunSql, catalog: str, schema: str) -> dict:
    scopes, e1 = _try(run_sql, pv.stored_scopes_sql(catalog, schema))
    ledger, e2 = _try(run_sql, pv.installer_ledger_sql(catalog, schema))
    ledger = coerce_ledger(ledger) if ledger is not None else None
    audit, e3 = _try(run_sql, pv.install_audit_sql(catalog, schema))
    states, e4 = _try(run_sql, pv.manifest_states_sql(catalog, schema))
    ends, _ = _try(run_sql, pv.window_ends_sql(catalog, schema))
    return {"scopes": scopes or [], "ledger": ledger or [], "audit": audit or [],
            "states": pv.schema_states(states or []),
            "window_ends": {r["scope"]: r["ends"] for r in (ends or [])},
            "errors": [e for e in (e1, e2, e3, e4) if e]}


def wave_schemas(run_sql: RunSql, catalog: str, schema: str, wave_id: str,
                 plan_run_id: Optional[str] = None) -> Tuple[List[str], Optional[str], Optional[str]]:
    """The ``catalog.schema`` set of a wave in the current (or pinned) plan: ``(schemas, plan_run_id, error)``."""
    pin = f"AND plan_run_id = {pv.sql_lit(plan_run_id)}" if plan_run_id else (
        f"AND plan_run_id = (SELECT max_by(plan_run_id, planned_at) FROM {catalog}.{schema}.wave_plan)")
    rows, err = _try(run_sql, f"SELECT DISTINCT lower(catalog) AS c, lower(schema) AS s, plan_run_id "
                              f"FROM {catalog}.{schema}.wave_plan WHERE wave_id = {pv.sql_lit(wave_id)} {pin}")
    if rows is None:
        return [], None, err
    prids = sorted({r["plan_run_id"] for r in rows})
    return sorted({f"{r['c']}.{r['s']}" for r in rows}), (prids[0] if len(prids) == 1 else None), None


# --------------------------------------------------------------------------- #
# Revoke (hand-back and uninstall)
# --------------------------------------------------------------------------- #

def revoke_items(run_sql: RunSql, items: Sequence[Mapping[str, Any]], *, catalog: str, schema: str, door: str,
                 pid: str, operator: Optional[str]) -> List[dict]:
    """Audit ``STARTED`` first, ``REVOKE``, then ``SUCCESS`` / ``FAILED``; installer ledger rows flip
    to ``REVOKED`` on success. One failure never stops the rest."""
    out = []
    for it in items:
        line = {"set": it.get("set") or pv.SET_CHANGE, "grantee": it["grantee"], "privilege": it["privilege"],
                "securable_type": it["securable_type"], "securable": it["securable"], "statement": it["statement"],
                "scope": None, "wave_id": None, "grantee_kind": None}
        now = _now()
        started = pv.install_audit_row(line, action=pv.AUDIT_INSTALL_REVOKE, phase="STARTED", door=door, pid=pid,
                                       operator=operator, now=now, statement=it["statement"])
        err = _write_audit(run_sql, catalog, schema, [started])
        if err:
            out.append({**it, "outcome": "not_run", "error": f"audit row could not be written first ({err})"})
            continue
        _, rerr = _try(run_sql, it["statement"])
        done = pv.install_audit_row(line, action=pv.AUDIT_INSTALL_REVOKE, phase="FAILED" if rerr else "SUCCESS",
                                    door=door, pid=pid, operator=operator, now=_now(), error=rerr,
                                    statement=it["statement"])
        _write_audit(run_sql, catalog, schema, [done])
        if not rerr and it.get("ledger_key"):
            _try(run_sql, pv.ledger_status_sql(catalog, schema, [it["ledger_key"]], status=pv.LEDGER_STATUS_REVOKED,
                                               now=_now(), by=operator,
                                               from_status=(pv.LEDGER_STATUS_GRANTED, pv.LEDGER_STATUS_HANDBACK_READY)))
            _try(run_sql, pv.handback_done_sql(catalog, schema, [pv.handback_action_key(it)], now=_now(),
                                               by=operator))
        out.append({**it, "outcome": "failed" if rerr else "revoked", "error": rerr})
    return out


def handback_pid(items: Sequence[Mapping[str, Any]]) -> str:
    """A fingerprint for a revoke plan, so ``plan_id`` pins hand-back and uninstall too."""
    return pv.plan_id([{"set": i.get("set") or pv.SET_CHANGE, "executor": pv.EXECUTOR_INSTALLER, **i} for i in items],
                      revoke=True)


# --------------------------------------------------------------------------- #
# The flow both text doors share
# --------------------------------------------------------------------------- #

def prepare(w, run_sql: RunSql, *, opts: Mapping[str, Any], catalog: str, schema: str,
            deployment: Optional[str], door: str, app_name: Optional[str] = None,
            warehouse_usable: Optional[bool] = None) -> dict:
    """Everything a door prints before it changes anything: rights, records, scopes, the plan
    with its ``plan_id``, per-line read-back status and who can grant what the person cannot."""
    probes, ctx = probe(w, run_sql, catalog=catalog, schema=schema, deployment=deployment,
                        app_name=app_name, door=door, warehouse_usable=warehouse_usable)
    records = read_records(run_sql, catalog, schema)
    drift = pv.identity_drift(ctx.get("jobs_identity"), pv.recorded_identities(records["audit"]),
                              opts.get("identity"))
    rights = pv.apply_identity_drift(pv.rights_report(probes, door=door), drift)
    stored = [r["scope"] for r in records["scopes"]]
    wave = None
    if opts.get("action") == pv.ACTION_GRANT_CONVERT:
        pinned = opts.get("plan_run_id") or None
        wave_list, prid, err = wave_schemas(run_sql, catalog, schema, opts["wave_id"])
        if pinned and not err and prid != pinned:
            err = (f"the wave plan changed: plan_run_id {pinned} is no longer the current plan"
                   f"{f' ({prid})' if prid else ''}. Review the new plan, then run with its plan_run_id.")
            wave_list = []
        wave = {"wave_id": opts["wave_id"], "schemas": wave_list, "plan_run_id": prid, "error": err}
    prep = {"probes": probes, "context": ctx, "rights": rights, "records": records,
            "stored_scopes": stored, "wave": wave}
    return replan(w, prep, opts, catalog=catalog, schema=schema)


def replan(w, prep: Mapping[str, Any], opts: Mapping[str, Any], *, catalog: str, schema: str) -> dict:
    """The plan half of ``prepare`` again, for new options (the CLI's scope picker), reusing the probe."""
    stored = prep["stored_scopes"]
    if opts.get("action") in (pv.ACTION_INSTALL, pv.ACTION_CHECK):
        scopes, scopes_from = pv.resolve_scopes(opts.get("scope_list") or [], bool(opts.get("scope_all")), stored,
                                                prep["rights"]["coverage"]["candidates"])
    else:
        scopes = list(opts.get("scope_list") or []) or list(stored)
        scopes_from = (pv.SCOPES_FROM_OPTION if opts.get("scope_list") else
                       pv.SCOPES_FROM_STORED if stored else pv.SCOPES_FROM_NONE)
    wave_list = (prep.get("wave") or {}).get("schemas") or []
    ctx = prep["context"]
    lines = plan_for(ctx, {**opts, "scope_list": scopes}, catalog=catalog, schema=schema, wave_schemas=wave_list)
    inst = pv.installer_lines(lines)
    held = read_held(w, inst, groups_resolver(w, ctx))
    checked = pv.verify(inst, held)
    return {**prep, "scopes": scopes, "scopes_from": scopes_from, "scope_note": pv.narrowing_note(scopes),
            "lines": lines, "plan_id": pv.plan_id(lines), "held": held, "checked": checked,
            "statuses": {pv.line_key(r): r["status"] for r in checked},
            "grantors": grantors_for([r for r in checked if r["status"] != pv.STATUS_PRESENT], ctx)}


def missing_in_stage(prep: Mapping[str, Any], stage: str) -> List[dict]:
    return [r for r in prep["checked"] if r["stage"] == stage and r["status"] != pv.STATUS_PRESENT]


def apply_stage(run_sql: RunSql, prep: Mapping[str, Any], stage: str, *, catalog: str, schema: str, door: str,
                held: Optional[Mapping[Tuple, Optional[set]]] = None, w=None) -> dict:
    """Run one stage's installer lines. Lines the person provably cannot grant are not tried; they
    come back as ``cannot_grant`` with who to ask. Bootstrap and records audit rows are deferred."""
    held = held if held is not None else prep["held"]
    lines = [ln for ln in pv.installer_lines(prep["lines"]) if ln["stage"] == stage]
    blocked = [ln for ln in lines if pv.line_key(ln) in prep["grantors"]
               and not pv.held_covers(held.get(pv.line_key(ln)), ln["privilege"])]
    runnable = [ln for ln in lines if ln not in blocked]
    out = apply_lines(run_sql, runnable, catalog=catalog, schema=schema, door=door, pid=prep["plan_id"],
                      operator=prep["context"].get("user"), held_before=held, audit_now=(stage == pv.STAGE_MAIN),
                      api=(lambda ln: grant_api(w, ln)) if w is not None else None)
    out["results"] += [{**ln, "outcome": "cannot_grant", "grantor": prep["grantors"][pv.line_key(ln)],
                        **({"commands": pv.api_commands(ln)} if pv.api_commands(ln) else {})}
                       for ln in blocked]
    return out


def outcome_counts(results: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in results:
        out[r["outcome"]] = out.get(r["outcome"], 0) + 1
    return out


def handback_prepare(prep: Mapping[str, Any], opts: Mapping[str, Any]) -> dict:
    rec = prep["records"]
    plan = pv.handback_plan(rec["ledger"], rec["states"], scopes=opts.get("scope_list") or [],
                            window_ends=rec["window_ends"])
    return {**plan, "plan_id": handback_pid(plan["revokes"]), "kept": []}


def uninstall_prepare(prep: Mapping[str, Any]) -> dict:
    rec = prep["records"]
    plan = pv.uninstall_plan(rec["audit"], rec["ledger"], rec["states"], installer=prep["context"].get("user"))
    return {**plan, "plan_id": handback_pid(plan["revokes"])}
