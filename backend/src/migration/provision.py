"""Install and permissions engine — pure, shared by every front door.

The installer has three front doors (the ``install`` notebook, the ``databricks bundle run
install`` CLI script, and the app's Settings → Permissions page). They all run the same engine:

* **One option set** (``OPTIONS`` / ``parse_options``). The notebook builds its widgets from it, the
  CLI its flags, the app its form (through the golden fixture); a guard test keeps them identical.
* **The rights and coverage report** (``rights_report``): can this installer install, and how much
  of the estate can they grant on? Every probe runs as the installer; this module only judges.
* **The grant plan** (``grant_plan``) — four closed sets (Control / Look / Read / Change) built from
  the existing ``grants.py`` specs, each line with its grantee, statement and reason, plus the
  ``plan_id`` fingerprint over the *intended* grants (a grant made by hand after review does not
  change it; it is simply skipped as present).
* **Verify** (``verify``): read-backs to ``present`` / ``missing`` / ``unknown`` (an unreadable grant
  is never ``present``).
* **Hand-back and uninstall** (``handback_plan`` / ``uninstall_plan``): revoke only what the tool
  recorded as added, never inside a table's 14-day rollback window.
* **SQL builders** for the install records (``audit_log`` / ``migration_scope`` /
  ``ownership_ledger``) so the notebook (``spark.sql``) and the CLI (Statement Execution) write the
  exact same rows.

Design: backend/docs/install-and-permissions.md. Rules: SR15–SR17 in app/docs/control_plane.md.
No Spark/SDK import here; every function is a pure transform over plain data.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import audit as _au
from . import grants as _gr

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

DOOR_NOTEBOOK = "notebook"
DOOR_CLI = "cli"
DOOR_APP = "app"
FRONT_DOORS = (DOOR_NOTEBOOK, DOOR_CLI, DOOR_APP)

ACTION_INSTALL = "install"
ACTION_CHECK = "check"
ACTION_GRANT_CONVERT = "grant_convert"
ACTION_REVOKE_CONVERT = "revoke_convert"
ACTION_UNINSTALL = "uninstall"
ACTIONS = (ACTION_INSTALL, ACTION_CHECK, ACTION_GRANT_CONVERT, ACTION_REVOKE_CONVERT, ACTION_UNINSTALL)

CONVERT_ALL_AT_ONCE = "all_at_once"
CONVERT_PER_WAVE = "per_wave"
CONVERT_RIGHTS = (CONVERT_ALL_AT_ONCE, CONVERT_PER_WAVE)

SET_CONTROL = "Control"
SET_LOOK = "Look"
SET_READ = "Read"
SET_CHANGE = "Change"
GRANT_SETS = (SET_CONTROL, SET_LOOK, SET_READ, SET_CHANGE)

# Who runs a plan line. ``setup`` lines are shown for completeness (job_99_setup grants them as
# owner of the control tables); the installer never issues them.
EXECUTOR_INSTALLER = "installer"
EXECUTOR_SETUP = "setup"

# When a line runs: ``bootstrap`` before setup creates the control tables, ``records`` right after
# (the installer's own access to the install records), ``main`` after that.
STAGE_BOOTSTRAP = "bootstrap"
STAGE_RECORDS = "records"
STAGE_MAIN = "main"
_STAGE_ORDER = {STAGE_BOOTSTRAP: 0, STAGE_RECORDS: 1, STAGE_MAIN: 2}

KIND_SERVICE_PRINCIPAL = "service_principal"
KIND_GROUP = "group"
KIND_USER = "user"
KIND_LABEL = {KIND_SERVICE_PRINCIPAL: "service principal", KIND_GROUP: "group", KIND_USER: "person"}

STATUS_PRESENT = "present"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

GO = "GO"
NO_GO = "NO-GO"
WARN = "WARN"
UNKNOWN = "UNKNOWN"

# Bumped whenever the plan rules change, so a release that changes the plan invalidates a plan_id
# approved under the old rules (CUJ-5). 2: blank scopes with none stored plan ``all``.
PLAN_RULES_VERSION = "2"

# ``scopes=all``: every catalog with external tables the installer can grant on, at catalog level.
SCOPES_ALL = "all"
SCOPES_FROM_OPTION = "option"
SCOPES_FROM_STORED = "stored"
SCOPES_FROM_ALL = "all"
SCOPES_FROM_NONE = "none"
# Never migrated, whatever their tables report.
NON_MIGRATABLE_CATALOGS = frozenset({"system", "hive_metastore", "samples", "__databricks_internal"})
# Unity Catalog catalog types that hold no tables this tool converts (Delta Sharing, foreign, …).
EXCLUDED_CATALOG_TYPES = frozenset({"DELTASHARING_CATALOG", "FOREIGN_CATALOG", "INTERNAL_CATALOG",
                                    "SYSTEM_CATALOG", "MANAGED_ONLINE_CATALOG"})

# Exit codes shared by the CLI and the notebook's exit message (design section 21).
EXIT_OK = 0
EXIT_NO_GO = 1
EXIT_INVALID = 2
EXIT_GAPS = 3
EXIT_PLAN_MISMATCH = 4
EXIT_REDEPLOY = 5

# The install records the installer writes (design section 19.4).
SCOPE_TABLE = "migration_scope"
RECORD_TABLES = (_au.AUDIT_TABLE, SCOPE_TABLE, "ownership_ledger")

# Audit actions (the installer's records in audit_log).
AUDIT_INSTALL_GRANT = _au.ACTION_INSTALL_GRANT
AUDIT_INSTALL_GRANT_SKIPPED = _au.ACTION_INSTALL_GRANT_SKIPPED
AUDIT_INSTALL_REVOKE = _au.ACTION_INSTALL_REVOKE

# Manifest states inside the convert → 14-day window: the convert rights must stay (rollback
# needs them), so hand-back and uninstall refuse a schema holding one.
IN_WINDOW_STATES = frozenset({
    _au.STATE_CONVERTING, _au.STATE_CONVERTED, _au.STATE_VALIDATED, _au.STATE_ROLLING_BACK,
    _au.STATE_CONVERTED_EXTERNAL,
})
TERMINAL_STATES = frozenset({_au.STATE_CLEANED, _au.STATE_ROLLED_BACK})

# The ownership_ledger rows the installer writes for Change. Keyed apart from Authorize's
# ``GRANT|<full_name>|…`` rows and scoped SCHEMA/CATALOG, so 07 never tries to revoke them (the
# jobs' identity cannot give up its own schema MANAGE; a person hands it back).
LEDGER_PREFIX = "INSTALL"
LEDGER_STATUS_GRANTED = "GRANTED"
LEDGER_STATUS_HANDBACK_READY = "HANDBACK_READY"
LEDGER_STATUS_REVOKED = "REVOKED"
# target_id of the schema-scoped RESTORE_OWNERSHIP hand-back action 07 opens.
HANDBACK_TARGET_PREFIX = "MANAGE:"

SYSTEM_SCHEMAS = ("access", "compute", "lakeflow")

READ_METADATA_NOTE = ("BROWSE lets the jobs' identity list tables, owners and grants in the catalog. "
                      "It does not read any data.")
VISIBILITY_CAVEAT = ("You see only what you have access to. A metastore admin sees the whole estate; "
                     "run this check as one for the full picture.")
METASTORE_ADMIN_GRANTOR = "a metastore admin"


# --------------------------------------------------------------------------- #
# Options — one set, three front doors
# --------------------------------------------------------------------------- #

# How the CLI receives an option: a flag, the bundle-script environment only, or not at all.
CLI_FLAG = "flag"
CLI_ENV = "env"


@dataclass(frozen=True)
class Option:
    name: str
    default: str
    help: str
    values: Optional[Tuple[str, ...]] = None   # None = free text
    widget: Optional[str] = None               # "dropdown" | "text" | None (no notebook widget)
    cli: Optional[str] = CLI_FLAG              # CLI_FLAG | CLI_ENV | None
    app: bool = False                          # shown on the app's Settings → Permissions form
    app_values: Optional[Tuple[str, ...]] = None  # the subset the app offers, when narrower

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")

    @property
    def env(self) -> str:
        return "MTM_" + self.name.upper()


OPTIONS: Tuple[Option, ...] = (
    Option("action", ACTION_INSTALL, "What to do: install, check (rights only), grant_convert (one "
           "wave), revoke_convert (hand convert rights back), uninstall.", ACTIONS, "dropdown",
           CLI_FLAG, True, tuple(a for a in ACTIONS if a != ACTION_UNINSTALL)),
    Option("confirm_grants", "false", "false prints the plan and runs nothing; true applies it.",
           ("false", "true"), "dropdown", CLI_FLAG, True),
    Option("jobs_identity", "keep", "Who the jobs run as: keep, sp:<application-id>, "
           "group:<name>, user:<email>. A change needs a redeploy.", None, "text", CLI_FLAG, True),
    Option("scopes", "", "What to migrate: catalog.schema, catalog.* or all, comma-separated. "
           "Blank uses the stored scopes, or all when none are stored.", None, "text", CLI_FLAG, True),
    Option("convert_rights", CONVERT_ALL_AT_ONCE, "all_at_once grants convert rights on the scopes "
           "now (handed back at the end); per_wave leaves them to grant_convert.", CONVERT_RIGHTS,
           "dropdown", CLI_FLAG, True),
    Option("wave_id", "", "The wave for grant_convert.", None, "text", CLI_FLAG, True),
    Option("plan_run_id", "", "grant_convert: the wave plan you reviewed. A newer plan stops it before "
           "any grant. Blank = the current plan.", None, "text", CLI_FLAG, True),
    Option("plan_id", "", "Apply only if the plan still matches this fingerprint from a dry run.",
           None, "text", CLI_FLAG, True),
    Option("grantee_group", "", "Grant to this group (it must contain the jobs' identity) instead "
           "of the identity itself.", None, "text", CLI_FLAG, True),
    Option("data_mode", "real", "After the grants: real profiles the estate, fixture seeds the "
           "dev-only synthetic estate, none skips.", ("real", "fixture", "none"), "dropdown",
           CLI_FLAG, False),
    Option("fixture_control_plane", "", "Extra fixture allowlist entry (catalog.schema), dev only.",
           None, "text", CLI_ENV, False),
    Option("app_name", "", "The deployed app name (blank = the bundle's).", None, "text", CLI_ENV,
           False),
    Option("catalog", "main", "Control-plane catalog (match your bundle deploy).", None, "text",
           CLI_ENV, False),
    Option("schema", "migration", "Control-plane schema (match your bundle deploy).", None, "text",
           CLI_ENV, False),
    Option("target", "dev", "Bundle target you deployed.", None, "text", CLI_ENV, False),
    Option("yes", "false", "CLI only: answer yes to the prompts (non-interactive).",
           ("false", "true"), None, CLI_FLAG, False),
    Option("output", "text", "CLI only: json prints one document for scripts and change tickets.",
           ("text", "json"), None, CLI_FLAG, False),
)
OPTION_NAMES: Tuple[str, ...] = tuple(o.name for o in OPTIONS)
OPTIONS_BY_NAME: Dict[str, Option] = {o.name: o for o in OPTIONS}
CHECK_ONLY_FLAG = "--check-only"  # CLI alias for --action check

_IDENT = r"[^`.\s,;'\"()]+"
_SCOPE_RE = re.compile(rf"^({_IDENT})\.({_IDENT}|\*)$")
_NAME_RE = re.compile(rf"^{_IDENT}$")
_PLAN_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_GROUP_RE = re.compile(r"^[^`;'\"]+$")


def notebook_widget_names() -> List[str]:
    return [o.name for o in OPTIONS if o.widget]


def cli_flags() -> List[str]:
    return [o.flag for o in OPTIONS if o.cli == CLI_FLAG]


def app_option_names() -> List[str]:
    return [o.name for o in OPTIONS if o.app]


def options_fixture() -> List[dict]:
    """The option set as plain data (the golden fixture the app's form reads)."""
    return [{
        "name": o.name, "default": o.default, "help": o.help,
        "values": list(o.values) if o.values else None, "widget": o.widget, "cli": o.cli,
        "flag": o.flag if o.cli == CLI_FLAG else None, "env": o.env, "app": o.app,
        "app_values": list(o.app_values) if o.app_values else (list(o.values) if o.app and o.values else None),
    } for o in OPTIONS]


def parse_scopes(raw: Optional[str]) -> Tuple[List[str], List[str]]:
    """``"a.b, c.*"`` → (``["a.b", "c.*"]``, errors). Lower-cased, de-duplicated, order kept.
    The token ``all`` is kept as is (``parse_options`` turns it into ``scope_all``)."""
    out: List[str] = []
    errors: List[str] = []
    for part in (raw or "").split(","):
        s = part.strip().replace("`", "").lower()
        if not s:
            continue
        if s != SCOPES_ALL and not _SCOPE_RE.match(s):
            errors.append(f"scopes: '{part.strip()}' is not catalog.schema, catalog.* or all.")
            continue
        if s not in out:
            out.append(s)
    return out, errors


def parse_jobs_identity(raw: Optional[str]) -> Tuple[Optional[dict], Optional[str]]:
    """``keep`` → (None, None); ``sp:<id>`` / ``group:<n>`` / ``user:<email>`` → ({kind, name}, None)."""
    s = (raw or "").strip()
    if not s or s.lower() == "keep":
        return None, None
    kind, _, name = s.partition(":")
    kind, name = kind.strip().lower(), name.strip()
    if kind == "sp" and _UUID_RE.match(name):
        return {"kind": KIND_SERVICE_PRINCIPAL, "name": name.lower()}, None
    if kind == "group" and name and _GROUP_RE.match(name):
        return {"kind": KIND_GROUP, "name": name}, None
    if kind == "user" and "@" in name and _GROUP_RE.match(name):
        return {"kind": KIND_USER, "name": name}, None
    if kind == "create":
        return None, ("jobs_identity: create is not available yet; create a service principal "
                      "and pass sp:<application-id>.")
    return None, (f"jobs_identity: '{s}' must be keep, sp:<application-id>, group:<name> or "
                  "user:<email>.")


def _bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n", ""):
        return False
    return None


def parse_options(raw: Mapping[str, Any], door: str = DOOR_NOTEBOOK) -> Tuple[dict, List[str]]:
    """Validate raw option values from any door. Returns ``(options, errors)``.

    ``errors`` holds one plain sentence per problem; an empty list means ``options`` is usable.
    ``options`` carries every option (defaults filled) plus the parsed ``scope_list`` and
    ``identity`` (None for ``keep``).
    """
    errors: List[str] = []
    opts: dict = {}
    for o in OPTIONS:
        v = raw.get(o.name) if raw is not None else None
        v = o.default if v is None else v
        if isinstance(v, bool):
            v = "true" if v else "false"
        v = str(v).strip()
        if o.values and v.lower() not in o.values:
            errors.append(f"{o.name}: '{v}' is not one of {', '.join(o.values)}.")
            v = o.default
        opts[o.name] = v.lower() if o.values else v
    for name in ("confirm_grants", "yes"):
        opts[name] = bool(_bool(opts[name]))
    if door == DOOR_APP and opts["action"] == ACTION_UNINSTALL:
        errors.append("action: uninstall runs from the notebook or the CLI only.")
    scope_list, scope_errors = parse_scopes(opts["scopes"])
    errors.extend(scope_errors)
    opts["scope_all"] = SCOPES_ALL in scope_list
    opts["scope_list"] = [s for s in scope_list if s != SCOPES_ALL]
    if opts["scope_all"] and opts["action"] not in (ACTION_INSTALL, ACTION_CHECK):
        errors.append(f"scopes: all applies to install and check only, not {opts['action']}.")
    identity, identity_error = parse_jobs_identity(opts["jobs_identity"])
    if identity_error:
        errors.append(identity_error)
    opts["identity"] = identity
    if opts["action"] == ACTION_GRANT_CONVERT and not opts["wave_id"]:
        errors.append("wave_id: grant_convert needs the wave to grant convert rights for.")
    if opts["wave_id"] and not _NAME_RE.match(opts["wave_id"]):
        errors.append(f"wave_id: '{opts['wave_id']}' is not a wave id.")
    if opts["plan_run_id"] and not _PLAN_RUN_ID_RE.match(opts["plan_run_id"]):
        errors.append(f"plan_run_id: '{opts['plan_run_id']}' is not a plan id.")
    if opts["plan_id"]:
        opts["plan_id"] = opts["plan_id"].lower()
        if not _PLAN_ID_RE.match(opts["plan_id"]):
            errors.append(f"plan_id: '{opts['plan_id']}' is not a 12-character plan fingerprint.")
    if opts["grantee_group"] and not _GROUP_RE.match(opts["grantee_group"]):
        errors.append(f"grantee_group: '{opts['grantee_group']}' is not a group name.")
    for name in ("catalog", "schema"):
        if not _NAME_RE.match(opts[name] or ""):
            errors.append(f"{name}: '{opts[name]}' is not a valid name.")
    return opts, errors


# --------------------------------------------------------------------------- #
# Identities
# --------------------------------------------------------------------------- #

def identity_from_run_as(run_as: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """A job's ``run_as`` (bundle or Jobs API form) → ``{kind, name}``, or None."""
    r = run_as or {}
    for key, kind in (("service_principal_name", KIND_SERVICE_PRINCIPAL), ("group_name", KIND_GROUP),
                      ("user_name", KIND_USER)):
        if r.get(key):
            return {"kind": kind, "name": str(r[key])}
    return None


def identity_from_principal(name: Optional[str]) -> Optional[dict]:
    """A bare ``run_as_user_name`` (UUID = service principal, else a person)."""
    n = (name or "").strip()
    if not n:
        return None
    return {"kind": KIND_SERVICE_PRINCIPAL if _UUID_RE.match(n) else KIND_USER, "name": n}


def run_as_value(identity: Mapping[str, str]) -> dict:
    """``{kind, name}`` → the bundle ``run_as`` mapping for ``jobs_run_as``."""
    key = {KIND_SERVICE_PRINCIPAL: "service_principal_name", KIND_GROUP: "group_name",
           KIND_USER: "user_name"}[identity["kind"]]
    return {key: identity["name"]}


def jobs_identity_summary(identities_by_job: Mapping[str, Optional[dict]]) -> dict:
    """Do all deployed jobs run as one identity? ``{identity, agree, count, distinct}``."""
    seen: Dict[Tuple[str, str], dict] = {}
    for ident in identities_by_job.values():
        if ident:
            seen.setdefault((ident["kind"], ident["name"].casefold()), ident)
    distinct = list(seen.values())
    return {"identity": distinct[0] if len(distinct) == 1 else None, "agree": len(distinct) == 1,
            "count": len(identities_by_job), "distinct": distinct}


SP_USER_ROLE_MARKER = "servicePrincipal.user"


def identity_move_note(identity: Optional[Mapping[str, str]]) -> Optional[str]:
    """What a deployer needs before the jobs can run as ``identity`` (None when nothing extra).

    A job's ``run_as`` service principal needs the deployer to hold its Service principal: User
    role, workspace admins included. A workspace token cannot read that role, so it is stated, not
    checked."""
    if not identity or identity.get("kind") != KIND_SERVICE_PRINCIPAL:
        return None
    return (f"Deploying with {identity['name']} as the jobs' identity needs the Service principal: User "
            "role on it, for workspace admins too. Grant it to the deployer under Settings → Identity "
            f"and access → Service principals → {identity['name']} → Permissions.")


def redeploy_hint(deploy_output: str, identity: Optional[Mapping[str, str]]) -> Optional[str]:
    """The fix for a redeploy that failed on the run_as service principal's role, else None."""
    if SP_USER_ROLE_MARKER not in (deploy_output or ""):
        return None
    return identity_move_note(identity) or (
        "The deployer needs the Service principal: User role on the jobs' service principal.")


def same_principal(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and str(a).strip().casefold() == str(b).strip().casefold()


def variable_overrides(existing: Optional[Mapping[str, Any]], identity: Mapping[str, str]) -> dict:
    """The target's ``variable-overrides.json`` content with ``jobs_run_as`` set to ``identity``."""
    out = dict(existing or {})
    out["jobs_run_as"] = run_as_value(identity)
    return out


# --------------------------------------------------------------------------- #
# Securables and statements
# --------------------------------------------------------------------------- #

def quote_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def quote_securable(securable_type: str, securable: str) -> str:
    if securable_type == "METASTORE":
        return ""
    return ".".join(quote_ident(p) for p in str(securable).split("."))


def grant_sql(privilege: str, securable_type: str, securable: str, grantee: str) -> str:
    on = f"{securable_type} {quote_securable(securable_type, securable)}".strip()
    return f"GRANT {privilege} ON {on} TO {quote_ident(grantee)}"


def revoke_sql(privilege: str, securable_type: str, securable: str, grantee: str) -> str:
    on = f"{securable_type} {quote_securable(securable_type, securable)}".strip()
    return f"REVOKE {privilege} ON {on} FROM {quote_ident(grantee)}"


def scope_parts(scope: str) -> Tuple[str, Optional[str]]:
    """``"c.s"`` → (c, s); ``"c.*"`` → (c, None)."""
    cat, _, sch = scope.partition(".")
    return cat, (None if sch in ("", "*") else sch)


def scope_kind(scope: str) -> str:
    return "CATALOG" if scope_parts(scope)[1] is None else "SCHEMA"


def scope_covers(scope: str, catalog: str, schema: str) -> bool:
    cat, sch = scope_parts(scope)
    return cat == str(catalog).lower() and (sch is None or sch == str(schema).lower())


def _line(set_name: str, grantee: str, grantee_kind: str, privilege: str, securable_type: str,
          securable: str, reason: str, *, executor: str = EXECUTOR_INSTALLER, stage: str = STAGE_MAIN,
          scope: Optional[str] = None, wave_id: Optional[str] = None) -> dict:
    return {
        "set": set_name, "grantee": grantee, "grantee_kind": grantee_kind, "privilege": privilege,
        "securable_type": securable_type, "securable": securable,
        "statement": grant_sql(privilege, securable_type, securable, grantee),
        "reason": reason, "executor": executor, "stage": stage, "scope": scope, "wave_id": wave_id,
    }


JOBS_SECURABLE = "JOBS"
JOB_RUN_PRIVILEGE = "CAN_MANAGE_RUN"
_JOB_RUN_KEY = {KIND_SERVICE_PRINCIPAL: "service_principal_name", KIND_GROUP: "group_name", KIND_USER: "user_name"}


def job_run_commands(line: Mapping[str, Any]) -> List[str]:
    """The exact CLI command per job for a ``JOBS`` line (run permission is not SQL)."""
    acl = json.dumps({"access_control_list": [{_JOB_RUN_KEY.get(line.get("grantee_kind"), "user_name"):
                                               line["grantee"], "permission_level": JOB_RUN_PRIVILEGE}]})
    return [f"databricks permissions update jobs {jid} --json '{acl}'" for jid in line.get("job_ids") or []]


def _jobs_line(identity: Mapping[str, str], deployment: str, job_ids: Sequence[Any]) -> dict:
    ln = _line(SET_CONTROL, identity["name"], identity["kind"], JOB_RUN_PRIVILEGE, JOBS_SECURABLE, deployment,
               "the jobs' identity runs its own jobs (cockpit launches and conductor tails)")
    ln["job_ids"] = [str(j) for j in job_ids]
    ln["statement"] = (f"-- Jobs permission (not SQL): {JOB_RUN_PRIVILEGE} on the {len(job_ids)} jobs of "
                       f"{deployment} for {identity['name']}")
    return ln


FOLDER_SECURABLE = "FOLDER"
FOLDER_READ_PRIVILEGE = "CAN_READ"
FOLDER_GRANTOR = "the deployment folder's owner or a workspace admin"
API_SECURABLES = (JOBS_SECURABLE, FOLDER_SECURABLE)


def folder_commands(line: Mapping[str, Any]) -> List[str]:
    """The exact CLI commands for a ``FOLDER`` line (a workspace permission is not SQL)."""
    acl = json.dumps({"access_control_list": [{_JOB_RUN_KEY.get(line.get("grantee_kind"), "user_name"):
                                               line["grantee"], "permission_level": FOLDER_READ_PRIVILEGE}]})
    return [f"databricks workspace get-status '{line['securable']}'",
            f"databricks permissions update directories <object_id from get-status> --json '{acl}'"]


def api_commands(line: Mapping[str, Any]) -> List[str]:
    """The CLI commands for a line that is a workspace permission, not SQL (else empty)."""
    stype = str(line.get("securable_type")).upper()
    if stype == JOBS_SECURABLE:
        return job_run_commands(line)
    return folder_commands(line) if stype == FOLDER_SECURABLE else []


def group_member_sql(group: str) -> str:
    """Is the caller in the account group ``group``? (Unity Catalog grants follow account groups.)"""
    return "SELECT is_account_group_member('" + str(group).replace("'", "''") + "') AS m"


def in_own_home(identity: Mapping[str, str], folder: str) -> bool:
    """True when ``folder`` sits in the identity's own workspace home, which it can always read."""
    f = str(folder).casefold()
    if f.startswith("/workspace/"):
        f = f[len("/workspace"):]
    return f.startswith(f"/users/{str(identity.get('name') or '').casefold()}/")


def _folder_line(identity: Mapping[str, str], folder: str) -> dict:
    ln = _line(SET_CONTROL, identity["name"], identity["kind"], FOLDER_READ_PRIVILEGE, FOLDER_SECURABLE, folder,
               "the jobs' identity reads the deployed notebooks", stage=STAGE_BOOTSTRAP)
    ln["statement"] = (f"-- Workspace permission (not SQL): {FOLDER_READ_PRIVILEGE} on the deployment folder "
                       f"{folder} for {identity['name']}")
    return ln


def line_key(line: Mapping[str, Any]) -> Tuple[str, str, str, str]:
    """What makes two plan lines the same grant in Unity Catalog."""
    return (str(line["grantee"]).casefold(), " ".join(str(line["privilege"]).upper().split()),
            str(line["securable_type"]).upper(), str(line["securable"]).casefold())


# --------------------------------------------------------------------------- #
# The grant plan
# --------------------------------------------------------------------------- #

def grant_plan(
    *,
    catalog: str,
    schema: str,
    jobs_identity: Optional[Mapping[str, str]],
    app_sp: Optional[str] = None,
    scopes: Sequence[str] = (),
    convert_rights: str = CONVERT_ALL_AT_ONCE,
    action: str = ACTION_INSTALL,
    wave_schemas: Sequence[str] = (),
    wave_id: Optional[str] = None,
    grantee_group: Optional[str] = None,
    installer: Optional[str] = None,
    installer_group: Optional[str] = None,
    control_schema_exists: bool = False,
    deployment: Optional[str] = None,
    job_ids: Sequence[Any] = (),
    deploy_folder: Optional[str] = None,
) -> List[dict]:
    """Every grant the install needs, in the order it runs (pure).

    Control goes to the app's service principal (setup grants it, as owner of the control tables)
    plus what the jobs' identity needs on the control catalog, read access to the deployment
    folder (``deploy_folder``: on dev it sits in the deployer's home) and run permission on the
    deployment's jobs (``job_ids``). Look, Read and Change go to the jobs' identity as deployed
    (or ``grantee_group`` when set). ``grant_convert`` plans only the Change lines for
    ``wave_schemas``.
    """
    lines: List[dict] = []
    ji = dict(jobs_identity) if jobs_identity else None
    target = (grantee_group or (ji or {}).get("name") or "").strip()
    target_kind = KIND_GROUP if grantee_group else (ji or {}).get("kind", KIND_SERVICE_PRINCIPAL)
    cat, sch = str(catalog), f"{catalog}.{schema}"

    if action == ACTION_GRANT_CONVERT:
        for s in wave_schemas:
            c, n = scope_parts(s)
            if not target or not n:
                continue
            lines.append(_line(SET_CHANGE, target, target_kind, "MANAGE", "SCHEMA", f"{c}.{n}",
                               "convert rights for this wave, handed back at the end",
                               scope=f"{c}.{n}", wave_id=wave_id))
        return _dedupe(lines)

    # Control: the jobs' identity creates and owns the control schema and tables.
    if ji and target:
        lines.append(_line(SET_CONTROL, ji["name"], ji["kind"], "USE CATALOG", "CATALOG", cat,
                           "the jobs' identity works in the control catalog", stage=STAGE_BOOTSTRAP))
        if not control_schema_exists:
            lines.append(_line(SET_CONTROL, ji["name"], ji["kind"], "CREATE SCHEMA", "CATALOG", cat,
                               "setup creates the control schema as the jobs' identity",
                               stage=STAGE_BOOTSTRAP))
        if deploy_folder and not in_own_home(ji, deploy_folder):
            lines.append(_folder_line(ji, deploy_folder))
        if deployment and job_ids:
            lines.append(_jobs_line(ji, deployment, job_ids))
    # Control: the app's service principal (setup grants these as owner of the control tables).
    if app_sp:
        for priv, stype, fqn in _gr.app_sp_grant_specs(catalog, schema):
            lines.append(_line(SET_CONTROL, app_sp, KIND_SERVICE_PRINCIPAL, priv, stype, fqn,
                               "the app reads the control plane (granted by setup)",
                               executor=EXECUTOR_SETUP))
    # Control: who may write the install records (design 19.4).
    if installer_group:
        for priv, stype, fqn in record_access_specs(catalog, schema):
            lines.append(_line(SET_CONTROL, installer_group, KIND_GROUP, priv, stype, fqn,
                               "installers write the install records (granted by setup)",
                               executor=EXECUTOR_SETUP))
    elif installer:
        for priv, stype, fqn in record_access_specs(catalog, schema):
            lines.append(_line(SET_CONTROL, installer, KIND_USER, priv, stype, fqn,
                               "you write the install records (no installer_group is set)",
                               stage=STAGE_RECORDS))

    if not target:
        return _dedupe(lines)

    # Look: list tables, owners and grants per scope catalog; read lineage and job history.
    catalogs = []
    for s in scopes:
        c, _ = scope_parts(s)
        if c not in catalogs:
            catalogs.append(c)
    for c in catalogs:
        lines.append(_line(SET_LOOK, target, target_kind, "BROWSE", "CATALOG", c,
                           "find tables, owners and grants", scope=c + ".*"))
    lines.append(_line(SET_LOOK, target, target_kind, "USE CATALOG", "CATALOG", "system",
                       "read lineage, compute and job history"))
    for s in SYSTEM_SCHEMAS:
        lines.append(_line(SET_LOOK, target, target_kind, "USE SCHEMA", "SCHEMA", f"system.{s}",
                           "read lineage, compute and job history"))
        lines.append(_line(SET_LOOK, target, target_kind, "SELECT", "SCHEMA", f"system.{s}",
                           "read lineage, compute and job history"))

    # Read: profile each scope (DESCRIBE DETAIL / HISTORY / TABLE need SELECT).
    for s in scopes:
        c, n = scope_parts(s)
        lines.append(_line(SET_READ, target, target_kind, "USE CATALOG", "CATALOG", c,
                           "profile and convert need SELECT", scope=s))
        if n:
            lines.append(_line(SET_READ, target, target_kind, "USE SCHEMA", "SCHEMA", f"{c}.{n}",
                               "profile and convert need SELECT", scope=s))
            lines.append(_line(SET_READ, target, target_kind, "SELECT", "SCHEMA", f"{c}.{n}",
                               "profile and convert need SELECT", scope=s))
        else:
            lines.append(_line(SET_READ, target, target_kind, "USE SCHEMA", "CATALOG", c,
                               "profile and convert need SELECT", scope=s))
            lines.append(_line(SET_READ, target, target_kind, "SELECT", "CATALOG", c,
                               "profile and convert need SELECT", scope=s))

    # Change: MANAGE on each scope, granted once (the default), handed back at the end.
    if convert_rights == CONVERT_ALL_AT_ONCE:
        for s in scopes:
            c, n = scope_parts(s)
            stype, fqn = ("SCHEMA", f"{c}.{n}") if n else ("CATALOG", c)
            lines.append(_line(SET_CHANGE, target, target_kind, "MANAGE", stype, fqn,
                               "convert rights, handed back at the end", scope=s))
    return _dedupe(lines)


def record_access_specs(catalog: str, schema: str) -> List[Tuple[str, str, str]]:
    """What a person needs to write the install records."""
    sch = f"{catalog}.{schema}"
    return [("USE CATALOG", "CATALOG", catalog), ("USE SCHEMA", "SCHEMA", sch),
            ("SELECT", "SCHEMA", sch)] + [("MODIFY", "TABLE", f"{sch}.{t}") for t in RECORD_TABLES]


def record_access_plan(catalog: str, schema: str, principal: str) -> List[Tuple[str, str, str, str, str]]:
    """``record_access_specs`` in ``grants.app_sp_grant_plan``'s shape, for job_99_setup's
    grant-then-verify loop over ``installer_group``."""
    return [(p, t, f, _gr.grant_statement(p, t, f, principal), _gr.show_grants_statement(t, f, principal))
            for p, t, f in record_access_specs(catalog, schema)]


def _dedupe(lines: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for ln in sorted(lines, key=lambda x: _STAGE_ORDER[x["stage"]]):
        k = line_key(ln)
        if k in seen:
            continue
        seen.add(k)
        out.append(ln)
    return out


def installer_lines(lines: Iterable[Mapping[str, Any]]) -> List[dict]:
    return [dict(ln) for ln in lines if ln.get("executor") == EXECUTOR_INSTALLER]


def plan_id(lines: Iterable[Mapping[str, Any]], *, revoke: bool = False) -> str:
    """The fingerprint over the INTENDED installer grants (before already-present ones are
    subtracted), so a grant made by hand after review does not change it (CUJ-5).

    ``revoke`` fingerprints a revoke plan under its own key, so a reviewed hand-back never pins a
    grant of the same lines (or the other way round)."""
    keys = sorted({(ln["set"],) + line_key(ln) for ln in installer_lines(lines)})
    payload = json.dumps({"rules": PLAN_RULES_VERSION, ("revokes" if revoke else "grants"): keys},
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def plan_counts(lines: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    out = {s: 0 for s in GRANT_SETS}
    for ln in installer_lines(lines):
        out[ln["set"]] += 1
    return out


def plan_matches(approved: Optional[str], current: str) -> bool:
    """No approved plan, or the same fingerprint."""
    return not approved or approved == current


def plan_difference(lines: Iterable[Mapping[str, Any]], approved: str) -> List[str]:
    """What to print when ``plan_id`` no longer matches (the approved lines are not stored, so the
    current plan is printed in full with both fingerprints)."""
    current = plan_id(lines)
    out = [f"The plan changed since it was reviewed: approved {approved}, now {current}.",
           "Nothing ran. Review the current plan below and re-run with the new plan_id:"]
    out += [f"  {ln['set']:8s} {ln['statement']}" for ln in installer_lines(lines)]
    return out


# --------------------------------------------------------------------------- #
# Rights and coverage
# --------------------------------------------------------------------------- #

def principals_of(user_name: Optional[str], groups: Iterable[str] = ()) -> List[str]:
    return [p for p in [user_name, *groups] if p]


def _in(principal: Optional[str], principals: Iterable[str]) -> bool:
    return any(same_principal(principal, p) for p in principals)


def can_grant_on(principals: Iterable[str], *, schema_owner: Optional[str] = None,
                 catalog_owner: Optional[str] = None, manage_holders: Iterable[str] = (),
                 metastore_admin: Optional[bool] = False) -> Optional[bool]:
    """Can someone holding ``principals`` grant on a schema? Owner of it or its catalog, MANAGE on
    either (``manage_holders``), or metastore admin. None only when nothing was readable."""
    ps = list(principals)
    if metastore_admin:
        return True
    if _in(schema_owner, ps) or _in(catalog_owner, ps):
        return True
    if any(_in(h, ps) for h in manage_holders or ()):
        return True
    if schema_owner is None and catalog_owner is None:
        return None
    return False


def schema_grantor(schema_owner: Optional[str], catalog_owner: Optional[str]) -> str:
    """Who to ask for a schema the installer cannot grant on."""
    who = []
    for o in (schema_owner, catalog_owner):
        if o and not any(same_principal(o, w) for w in who):
            who.append(o)
    if not who:
        return METASTORE_ADMIN_GRANTOR
    return ", ".join(f"`{w}`" for w in who) + f", or {METASTORE_ADMIN_GRANTOR}"


def coverage(estate: Iterable[Mapping[str, Any]]) -> dict:
    """``estate`` rows ``{catalog, schema, tables, can_grant, grantor}`` → the coverage headline."""
    rows = [dict(r) for r in estate or []]
    granted = [r for r in rows if r.get("can_grant")]
    uncovered = [r for r in rows if not r.get("can_grant")]
    by_cat: Dict[str, dict] = {}
    for r in uncovered:
        c = by_cat.setdefault(r["catalog"], {"catalog": r["catalog"], "schemas": 0, "tables": 0,
                                             "grantors": []})
        c["schemas"] += 1
        c["tables"] += int(r.get("tables") or 0)
        g = r.get("grantor") or METASTORE_ADMIN_GRANTOR
        if g not in c["grantors"]:
            c["grantors"].append(g)
    total_s, total_t = len(rows), sum(int(r.get("tables") or 0) for r in rows)
    got_s, got_t = len(granted), sum(int(r.get("tables") or 0) for r in granted)
    headline = (f"You can grant on {got_s} of {total_s} schemas with external tables "
                f"({got_t:,} of {total_t:,} tables)." if total_s else
                "No schemas with external tables are visible to you.")
    return {"schemas": total_s, "tables": total_t, "grantable_schemas": got_s,
            "grantable_tables": got_t, "uncovered": sorted(by_cat.values(), key=lambda c: c["catalog"]),
            "candidates": expand_all(rows),
            "headline": headline, "band": coverage_band(got_s, total_s)}


def excluded_catalogs(catalog_types: Optional[Mapping[str, Optional[str]]] = None) -> set:
    """Catalog names never migrated: the fixed names, plus every catalog whose Unity Catalog type
    holds nothing to convert (``catalog_types``: name → ``catalog_type``; unknown types stay in)."""
    out = set(NON_MIGRATABLE_CATALOGS)
    for name, ctype in (catalog_types or {}).items():
        if str(ctype or "").upper() in EXCLUDED_CATALOG_TYPES:
            out.add(str(name).lower())
    return out


def expand_all(estate: Iterable[Mapping[str, Any]]) -> List[str]:
    """``scopes=all`` (pure): per catalog with external tables, ``catalog.*`` when the installer can
    grant on the whole catalog (``catalog_can_grant``), else each schema they can grant on.
    Catalog order, then schema order; a catalog nobody here can grant on yields nothing."""
    by_cat: Dict[str, List[Mapping[str, Any]]] = {}
    for r in estate or []:
        c = str(r.get("catalog") or "").lower()
        if c and c not in NON_MIGRATABLE_CATALOGS:
            by_cat.setdefault(c, []).append(r)
    out: List[str] = []
    for c in sorted(by_cat):
        rows = by_cat[c]
        if any(r.get("catalog_can_grant") for r in rows):
            out.append(f"{c}.*")
        else:
            out += sorted(f"{c}.{str(r['schema']).lower()}" for r in rows if r.get("can_grant"))
    return out


def resolve_scopes(scope_list: Sequence[str], scope_all: bool, stored: Sequence[str],
                   expansion: Sequence[str]) -> Tuple[List[str], str]:
    """``(scopes, scopes_from)`` for a run (pure).

    Named scopes win; blank keeps the stored scopes (a deliberate narrowing is never widened);
    blank with nothing stored is ``all``. ``all`` named adds the expansion to the stored scopes."""
    if scope_all:
        out: List[str] = []
        for s in [*stored, *expansion, *scope_list]:
            if s not in out:
                out.append(s)
        return out, SCOPES_FROM_ALL
    if scope_list:
        return list(scope_list), SCOPES_FROM_OPTION
    if stored:
        return list(stored), SCOPES_FROM_STORED
    if expansion:
        return list(expansion), SCOPES_FROM_ALL
    return [], SCOPES_FROM_NONE


SCOPES_FROM_LABEL = {SCOPES_FROM_OPTION: "from the scopes option",
                     SCOPES_FROM_STORED: "stored by an earlier install",
                     SCOPES_FROM_ALL: "all you can grant on"}

NARROWING_NOTE = ("Catalog-level grants also cover schemas created later, and are handed back when every "
                  "table in the catalog is CLEANED or ROLLED_BACK. To narrow: scopes=<catalog.schema,...> "
                  "for named schemas, or convert_rights=per_wave to grant MANAGE one wave at a time.")


def narrowing_note(scopes: Iterable[str]) -> Optional[str]:
    """How to lower the grain, shown only when a ``catalog.*`` scope is in the plan."""
    return NARROWING_NOTE if any(str(s).endswith(".*") for s in scopes or ()) else None


def coverage_band(got: int, total: int) -> str:
    """Closed-vocabulary band for telemetry (never a count)."""
    if not total:
        return "none"
    pct = got / total
    return "all" if pct >= 1 else "most" if pct >= 0.5 else "some" if pct > 0 else "none"


def suggested_scopes(cov: Mapping[str, Any]) -> str:
    """The ``scopes`` value ``all`` expands to (``expand_all``), comma-separated."""
    return ",".join(cov.get("candidates") or [])


def pick_scopes(text: Optional[str], candidates: Sequence[str]) -> Tuple[List[str], List[str]]:
    """The CLI picker: numbers (1-based), ranges (``2-5``) and ``catalog.schema`` / ``catalog.*``,
    comma-separated, against the numbered ``candidates``. Blank picks every candidate."""
    cands = list(candidates)
    if not (text or "").strip():
        return cands, []
    out: List[str] = []
    errors: List[str] = []
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", p)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if not (1 <= lo <= hi <= len(cands)):
                errors.append(f"'{p}' is not between 1 and {len(cands)}.")
                continue
            picked = cands[lo - 1:hi]
        else:
            picked, errs = parse_scopes(p)
            errors.extend(errs)
            if SCOPES_ALL in picked:
                picked = cands
        out += [s for s in picked if s not in out]
    return out, errors


def _check(key: str, label: str, status: str, detail: str = "", ask: Optional[str] = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "ask": ask}


def _status(v: Optional[bool]) -> str:
    return GO if v else UNKNOWN if v is None else NO_GO


WORKSPACE_USERS_GROUP = "users"
_FOLDER_EDIT_LEVELS = frozenset({"CAN_EDIT", "CAN_MANAGE"})


def deploy_root(notebook_path: Optional[str]) -> Optional[str]:
    """The bundle's ``root_path`` from a deployed notebook path (``<root>/files/…``), or None."""
    head, sep, _ = str(notebook_path or "").partition("/files/")
    return head if sep and head else None


def folder_open_to_users(entries: Iterable[Mapping[str, Any]]) -> bool:
    """True when the workspace ``users`` group can edit the folder (``{group_name, levels}`` rows,
    inherited levels included), so anyone could change the notebooks the jobs run."""
    return any(str(e.get("group_name") or "").lower() == WORKSPACE_USERS_GROUP
               and _FOLDER_EDIT_LEVELS & {str(lv).upper() for lv in e.get("levels") or ()}
               for e in entries)


def rights_report(probes: Mapping[str, Any], *, door: str = DOOR_NOTEBOOK) -> dict:
    """Turn the installer's probe results into one go / no-go line per check (design section 10).

    ``probes`` keys (each optional; a missing or None value reads UNKNOWN, never GO):
    ``installer``, ``workspace_admin``, ``metastore_admin``, ``estate`` (coverage rows),
    ``control`` ``{catalog, schema, exists, can_grant}``, ``warehouse_can_use``,
    ``jobs`` (``jobs_identity_summary`` + ``active``), ``records`` ``{group, member, can_self_grant}``,
    ``app_can_grant``, ``deploy_folder_open``.
    ``can_install`` is false only on a no-go that stops install: the control catalog cannot be set
    up, or the jobs' identity is unknown or the deployed jobs disagree.
    """
    checks: List[dict] = []
    ws = probes.get("workspace_admin")
    checks.append(_check("workspace_admin", "Workspace admin", GO if ws else WARN if ws is False else UNKNOWN,
                         "" if ws else "needed only to create a service principal or change workspace permissions"))
    ma = probes.get("metastore_admin")
    checks.append(_check("metastore_admin", "Metastore admin", GO if ma else WARN if ma is False else UNKNOWN,
                         "" if ma else "system tables need a metastore admin unless already granted"))
    control = probes.get("control") or {}
    ctl_ok = bool(control.get("exists") or control.get("can_grant"))
    if ctl_ok:
        ctl_status, ctl_ask = GO, None
        ctl_detail = ("the control schema exists" if control.get("exists")
                      else "you can let the jobs' identity create the control schema")
    elif control.get("can_grant") is None:
        ctl_status, ctl_ask, ctl_detail = UNKNOWN, None, "could not read who can grant on the control catalog"
    else:
        ctl_status, ctl_detail = NO_GO, "you cannot grant CREATE SCHEMA on it"
        ctl_ask = control.get("grantor") or METASTORE_ADMIN_GRANTOR
    checks.append(_check("control_catalog", f"Control catalog `{control.get('catalog', '')}`",
                         ctl_status, ctl_detail, ctl_ask))
    wh = probes.get("warehouse_can_use")
    if door != DOOR_NOTEBOOK or wh is not None:
        checks.append(_check("warehouse", "Warehouse", _status(wh), "CAN_USE" if wh else "you need CAN_USE on the SQL warehouse"))
    jobs = probes.get("jobs") or {}
    ident = jobs.get("identity")
    if ident and jobs.get("agree"):
        active = jobs.get("active")
        you = ident["kind"] == KIND_USER and same_principal(ident["name"], probes.get("installer"))
        label = f"you ({ident['name']})" if you else f"{KIND_LABEL.get(ident['kind'], ident['kind'])} {ident['name']}"
        jstatus = GO if active is not False else NO_GO
        jdetail = f"{label}{' (inactive)' if active is False else ''}, all {jobs.get('count', 0)} jobs agree"
        if ident["kind"] == KIND_USER:
            jdetail += ". " + person_identity_note(you)
    elif jobs.get("distinct"):
        jstatus, jdetail = NO_GO, (f"the deployed jobs run as {len(jobs['distinct'])} different identities; "
                                   "redeploy so every job uses jobs_run_as")
    else:
        jstatus, jdetail = UNKNOWN, "could not read the deployed jobs' run_as"
    checks.append(_check("jobs_identity", "Jobs' identity", jstatus, jdetail))
    rec = probes.get("records") or {}
    if rec.get("group"):
        rstatus = _status(rec.get("member"))
        rdetail = f"member of {rec['group']}" if rec.get("member") else f"you are not in {rec['group']}"
        rask = None if rec.get("member") else f"an admin of `{rec['group']}`"
    else:
        rstatus = GO if rec.get("can_self_grant") else (UNKNOWN if rec.get("can_self_grant") is None else NO_GO)
        rdetail = ("no installer_group: you grant yourself access to the install records"
                   if rec.get("can_self_grant") else "no installer_group, and you cannot grant yourself the records")
        rask = None if rec.get("can_self_grant") else control.get("grantor") or METASTORE_ADMIN_GRANTOR
    checks.append(_check("install_records", "Install records", rstatus, rdetail, rask))
    if door == DOOR_APP:
        ag = probes.get("app_can_grant")
        checks.append(_check("app_can_grant", "App can grant for you", _status(ag),
                             "" if ag else "this workspace does not let the app act for you; use the notebook or CLI"))
    if probes.get("deploy_folder_open"):
        checks.append(_check("deploy_folder", "Deploy folder", WARN,
                             "every workspace user can edit the notebooks the jobs run; deploy to a folder "
                             "only the deployers can edit (prod: the prod_root_path variable)"))
    cov = coverage(probes.get("estate") or [])
    can_install = ctl_ok and jstatus == GO
    return {"installer": probes.get("installer"), "checks": checks, "coverage": cov,
            "caveat": None if ma else VISIBILITY_CAVEAT, "can_install": can_install,
            "suggested_scopes": suggested_scopes(cov)}


SP_SWITCH = "jobs_identity=sp:<application-id>"


def person_identity_note(you: bool) -> str:
    """What running the jobs as a person means (stated on the GO line, never a warning)."""
    whose = "your" if you else "that person's"
    return (f"Anyone who can start these jobs runs them with {whose} access. "
            f"To use a service principal: {SP_SWITCH}")


def recorded_identities(audit_rows: Iterable[Mapping[str, Any]]) -> List[str]:
    """The people and service principals the tool granted Look, Read or Change to and has not
    revoked since (groups are skipped: they are a deliberate ``grantee_group``)."""
    out: List[str] = []
    for d in granted_by_tool(audit_rows):
        if d.get("set") not in (SET_LOOK, SET_READ, SET_CHANGE) or d.get("grantee_kind") == KIND_GROUP:
            continue
        g = str(d.get("grantee") or "").strip()
        if g and not any(same_principal(g, o) for o in out):
            out.append(g)
    return sorted(out, key=str.casefold)


def identity_drift(current: Optional[Mapping[str, str]], recorded: Sequence[str],
                   requested: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The stop when the deployed jobs moved to a new identity while this install's grants still
    sit on the old one (an upgrade that changed the default ``run_as``), else None.

    An explicit ``jobs_identity`` (``requested``) is the decision, so it clears the stop."""
    if requested or not current or not recorded:
        return None
    if any(same_principal(current.get("name"), r) for r in recorded):
        return None
    old = recorded[0]
    keep = f"sp:{old}" if _UUID_RE.match(old) else f"user:{old}"
    move = f"{'sp' if current.get('kind') == KIND_SERVICE_PRINCIPAL else current.get('kind')}:{current.get('name')}"
    return (f"the jobs now run as {current.get('name')}, but this install's grants went to "
            f"{', '.join(recorded)}. To keep {old}: jobs_identity={keep}, then redeploy. "
            f"To move to {current.get('name')}: jobs_identity={move}")


def apply_identity_drift(report: Mapping[str, Any], drift: Optional[str]) -> dict:
    """``rights_report`` with the jobs' identity line turned NO-GO by ``drift`` (pure)."""
    out = dict(report)
    if not drift:
        return out
    out["checks"] = [{**c, "status": NO_GO, "detail": drift} if c["key"] == "jobs_identity" else dict(c)
                     for c in report["checks"]]
    out["can_install"] = False
    return out


def rights_text(report: Mapping[str, Any]) -> List[str]:
    """The report as the lines every text door prints."""
    out = [f"Rights and coverage  (as {report.get('installer') or 'you'})"]
    for c in report["checks"]:
        extra = f"   {c['detail']}" if c.get("detail") else ""
        ask = f"  (ask {c['ask']})" if c.get("ask") else ""
        out.append(f"  {c['status']:6s} {c['label']:28s}{extra}{ask}")
    cov = report["coverage"]
    out.append("  " + cov["headline"])
    if cov["uncovered"]:
        parts = [f"{u['catalog']} ({u['schemas']}) ask {' / '.join(u['grantors'])}" for u in cov["uncovered"]]
        out.append(f"  {sum(u['schemas'] for u in cov['uncovered'])} need someone else:  " + "   ".join(parts))
    if report.get("caveat"):
        out.append("  " + report["caveat"])
    return out


# --------------------------------------------------------------------------- #
# Who may issue each line, and verify
# --------------------------------------------------------------------------- #

def line_grantable(line: Mapping[str, Any], *, grantable_schemas: Iterable[str] = (),
                   grantable_catalogs: Iterable[str] = (), metastore_admin: Optional[bool] = False,
                   ) -> Tuple[Optional[bool], str]:
    """Can the installer issue this line, and if not, who can? ``(can, grantor)``."""
    if line.get("executor") != EXECUTOR_INSTALLER:
        return None, "setup"
    if str(line.get("securable_type")).upper() == JOBS_SECURABLE:
        return None, "the jobs' owner or a workspace admin"
    if str(line.get("securable_type")).upper() == FOLDER_SECURABLE:
        return None, FOLDER_GRANTOR
    if metastore_admin:
        return True, ""
    stype, fqn = str(line["securable_type"]).upper(), str(line["securable"]).lower()
    cat = fqn.split(".")[0]
    if cat == "system":
        return False, METASTORE_ADMIN_GRANTOR
    cats = {c.lower() for c in grantable_catalogs}
    schemas = {s.lower() for s in grantable_schemas}
    if cat in cats:
        return True, ""
    if stype == "CATALOG":
        return False, f"the owner of catalog `{cat}`, or {METASTORE_ADMIN_GRANTOR}"
    sch = ".".join(fqn.split(".")[:2])
    if sch in schemas:
        return True, ""
    return False, f"the owner of `{sch}` or its catalog, or {METASTORE_ADMIN_GRANTOR}"


def held_covers(held: Optional[Iterable[str]], privilege: str) -> Optional[bool]:
    """Does a set of effective privileges cover ``privilege``? None when unreadable."""
    if held is None:
        return None
    norm = {" ".join(str(p).upper().replace("_", " ").split()) for p in held}
    want = " ".join(str(privilege).upper().replace("_", " ").split())
    if want == "CAN MANAGE RUN":
        return bool(norm & {"CAN MANAGE RUN", "CAN MANAGE", "IS OWNER"})
    return want in norm or "ALL PRIVILEGES" in norm or "OWNER" in norm


def verify(lines: Iterable[Mapping[str, Any]], held_by_line: Mapping[Tuple, Optional[Iterable[str]]]) -> List[dict]:
    """Read-backs → per-line ``status`` (``present`` / ``missing`` / ``unknown``).

    ``held_by_line`` maps ``line_key(line)`` to the grantee's effective privileges on the
    securable (``OWNER`` when it owns it or a parent), or None when the read failed. An unreadable
    grant is never ``present``.
    """
    out = []
    for ln in lines:
        covered = held_covers(held_by_line.get(line_key(ln)), ln["privilege"])
        status = STATUS_UNKNOWN if covered is None else STATUS_PRESENT if covered else STATUS_MISSING
        out.append({**ln, "status": status})
    return out


def verify_summary(results: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    out = {STATUS_PRESENT: 0, STATUS_MISSING: 0, STATUS_UNKNOWN: 0}
    for r in results:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def person_grantee_line(lines: Iterable[Mapping[str, Any]], installer: Optional[str] = None) -> Optional[str]:
    """When Look, Read and Change go to a person (the jobs run as them), say so before the grants
    (SR17 condition 3), else None."""
    who = next((ln["grantee"] for ln in lines if ln.get("set") in (SET_LOOK, SET_READ, SET_CHANGE)
                and ln.get("grantee_kind") == KIND_USER), None)
    if not who:
        return None
    if same_principal(who, installer):
        return "The jobs run as you, so Look, Read and Change go to you."
    return f"The jobs run as {who}, a person, so Look, Read and Change go to {who}."


def plan_text(lines: Iterable[Mapping[str, Any]], *, statuses: Optional[Mapping[Tuple, str]] = None,
              grantors: Optional[Mapping[Tuple, str]] = None, limit: Optional[int] = None,
              dry_run: bool = True, installer: Optional[str] = None) -> List[str]:
    """The plan as the lines every text door prints."""
    lines = list(lines)
    pid = plan_id(lines)
    head = (f"Grant plan {pid}  (dry run — set confirm_grants=true to apply; plan_id={pid} pins it)"
            if dry_run else f"Grant plan {pid}")
    out = [head]
    person = person_grantee_line(lines, installer)
    if person:
        out.append("  " + person)
    shown = 0
    for ln in lines:
        if limit is not None and shown >= limit:
            break
        st = (statuses or {}).get(line_key(ln))
        who = "app SP" if ln["executor"] == EXECUTOR_SETUP and ln["grantee_kind"] == KIND_SERVICE_PRINCIPAL else ln["grantee"]
        stmt = "(granted by setup as table owner)" if ln["executor"] == EXECUTOR_SETUP else ln["statement"]
        tag = f"[{st}] " if st else ""
        g = (grantors or {}).get(line_key(ln))
        out.append(f"  {ln['set']:8s} {tag}{who}: {ln['reason']}" + (f"  — ask {g}" if g else ""))
        out.append(f"           {stmt}")
        shown += 1
    rest = len(lines) - shown
    if rest > 0:
        out.append(f"  … {rest} more lines.")
    return out


# --------------------------------------------------------------------------- #
# Hand-back and uninstall
# --------------------------------------------------------------------------- #

def ledger_key(line: Mapping[str, Any]) -> str:
    """``INSTALL|<TYPE> <securable>|<privilege>|<grantee>`` — never an Authorize key."""
    return (f"{LEDGER_PREFIX}|{str(line['securable_type']).upper()} {line['securable']}|"
            f"{line['privilege']}|{line['grantee']}")


def is_installer_ledger_row(row: Mapping[str, Any]) -> bool:
    return str(row.get("ledger_key") or "").startswith(LEDGER_PREFIX + "|")


def handback_action_key_target(grantee: str) -> str:
    return f"{HANDBACK_TARGET_PREFIX}{grantee}"


def handback_action_key(row: Mapping[str, Any]) -> str:
    """The ``action_key`` of the hand-back ``handback_action_rows`` opens for a ledger row."""
    from . import actions as _ac  # noqa: PLC0415

    cat, _, sch = str(row.get("securable_name") or row.get("securable") or "").partition(".")
    return _ac.action_key(_ac.KIND_RESTORE_OWNERSHIP, f"{cat}.{sch or '*'}",
                          handback_action_key_target(str(row.get("grantee"))))


def handback_reopen_keys(ledger_rows: Iterable[Mapping[str, Any]], ready_keys: Iterable[str]) -> List[str]:
    """Hand-back action keys to reopen: every installer row ready to hand back (marked now or by
    an earlier run). A ready row next to a closed action means its ``REVOKE`` never ran, since a
    hand-back flips the row to ``REVOKED``."""
    ready = set(ready_keys or ())
    return sorted({handback_action_key(r) for r in ledger_rows or []
                   if _held(r) and (r.get("ledger_key") in ready or r.get("status") == LEDGER_STATUS_HANDBACK_READY)})


def handback_reopen_sql(catalog: str, schema: str, keys: Sequence[str], *, now) -> str:
    """Reopen a hand-back action an earlier grant cycle closed. The action key is stable per
    scope and grantee, and a MATCHed merge keeps the operator-owned ``DONE``, so a schema granted
    again after a hand-back would never be offered again."""
    from . import actions as _ac  # noqa: PLC0415

    quoted = ", ".join(sql_lit(k) for k in keys)
    return (f"UPDATE {catalog}.{schema}.{_ac.ACTIONS_TABLE} SET status = '{_ac.STATUS_OPEN}', "
            f"resolution = NULL, completed_by = NULL, completed_at = NULL, updated_at = {_ts_lit(now)} "
            f"WHERE action_key IN ({quoted}) AND status IN ('{_ac.STATUS_DONE}', '{_ac.STATUS_OBSOLETE}')")


def handback_done_sql(catalog: str, schema: str, keys: Sequence[str], *, now, by: Optional[str] = None) -> str:
    """Close hand-back actions once their ``REVOKE`` ran (operator-owned columns only)."""
    from . import actions as _ac  # noqa: PLC0415

    quoted = ", ".join(sql_lit(k) for k in keys)
    return (f"UPDATE {catalog}.{schema}.{_ac.ACTIONS_TABLE} SET status = '{_ac.STATUS_DONE}', "
            f"resolution = 'installer convert rights handed back', completed_by = {sql_lit(by)}, "
            f"completed_at = {_ts_lit(now)}, updated_at = {_ts_lit(now)} "
            f"WHERE action_key IN ({quoted}) AND status NOT IN ('{_ac.STATUS_DONE}', '{_ac.STATUS_OBSOLETE}')")


def is_handback_action_key(key: Optional[str]) -> bool:
    """The schema-scoped RESTORE_OWNERSHIP 07 opens for an installer MANAGE (never regenerated by
    the profiler, so obsolete detection must leave it alone)."""
    parts = str(key or "").split("|")
    return len(parts) >= 3 and parts[0] == "RESTORE_OWNERSHIP" and parts[2].startswith(HANDBACK_TARGET_PREFIX)


def _schemas_of(row: Mapping[str, Any], states_by_schema: Mapping[str, Any]) -> List[str]:
    """The ``catalog.schema`` keys a ledger row's securable covers (a catalog covers all of its)."""
    fqn = str(row.get("securable_name") or "").lower()
    if str(row.get("securable_type") or "").upper() == "CATALOG":
        return [s for s in states_by_schema if s.split(".")[0] == fqn]
    return [fqn]


def _held(row: Mapping[str, Any]) -> bool:
    return (is_installer_ledger_row(row) and not row.get("dry_run") and bool(row.get("added"))
            and not row.get("prior_held")
            and row.get("status") in (LEDGER_STATUS_GRANTED, LEDGER_STATUS_HANDBACK_READY))


def schema_states(manifest_rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """``{catalog.schema: {state: count}}`` from manifest rows ``{catalog, schema, state}``."""
    out: Dict[str, Dict[str, int]] = {}
    for r in manifest_rows or []:
        k = f"{r.get('catalog')}.{r.get('schema')}".lower()
        st = r.get("state") or ""
        out.setdefault(k, {})
        out[k][st] = out[k].get(st, 0) + 1
    return out


def in_window(states: Mapping[str, int]) -> bool:
    return any(states.get(s) for s in IN_WINDOW_STATES)


def all_terminal(states: Mapping[str, int]) -> bool:
    return bool(states) and all(s in TERMINAL_STATES for s, n in states.items() if n)


def handback_ready_keys(ledger_rows: Iterable[Mapping[str, Any]],
                        states_by_schema: Mapping[str, Mapping[str, int]]) -> List[str]:
    """Installer MANAGE rows 07 marks ready: every table in their schema(s) is CLEANED or
    ROLLED_BACK (and at least one table exists)."""
    out = []
    for r in ledger_rows or []:
        if not _held(r) or r.get("status") != LEDGER_STATUS_GRANTED:
            continue
        if " ".join(str(r.get("privilege") or "").upper().split()) != "MANAGE":
            continue
        schemas = _schemas_of(r, states_by_schema)
        if schemas and all(all_terminal(states_by_schema.get(s) or {}) for s in schemas):
            out.append(r["ledger_key"])
    return sorted(out)


def handback_action_rows(ledger_rows: Iterable[Mapping[str, Any]], ready_keys: Iterable[str], *,
                         source: str = "07_post_convert_cleanup", now=None) -> List[dict]:
    """One schema-scoped ``RESTORE_OWNERSHIP`` per ready installer MANAGE row, carrying the exact
    ``REVOKE`` and the installer command (never blocking; a person runs it)."""
    from . import actions as _ac  # noqa: PLC0415 — actions imports provision lazily too

    ready = set(ready_keys or ())
    out = []
    for r in ledger_rows or []:
        if r.get("ledger_key") not in ready:
            continue
        stype = str(r.get("securable_type") or "").upper()
        fqn = str(r.get("securable_name") or "")
        cat, _, sch = fqn.partition(".")
        stmt = revoke_sql(r.get("privilege"), stype, fqn, r.get("grantee"))
        out.append(_ac.build_action_row(
            action_kind=_ac.KIND_RESTORE_OWNERSHIP, scope=_ac.SCOPE_SCHEMA, phase=_ac.PHASE_POST_CONVERT,
            title=f"Hand back convert rights on {fqn}",
            catalog=cat, schema=sch or "*", target_kind=_ac.TARGET_NONE,
            target_id=handback_action_key_target(str(r.get("grantee"))), blocking=False,
            detail=("Every table here is CLEANED or ROLLED_BACK. The jobs' identity cannot give up "
                    "its own MANAGE, so a person with authority on the schema hands it back. Only "
                    "what the installer granted is revoked."),
            remediation_sql=stmt + ";",
            remediation_api=("Run the installer with action=revoke_convert and confirm_grants=true "
                             "(or press Hand back in the app)"),
            confidence=_ac.CONF_HIGH, source=source, detected_at=now, updated_at=now,
        ))
    return out


def handback_plan(ledger_rows: Iterable[Mapping[str, Any]],
                  states_by_schema: Mapping[str, Mapping[str, int]],
                  scopes: Sequence[str] = (), window_ends: Optional[Mapping[str, str]] = None) -> dict:
    """What ``revoke_convert`` revokes (pure).

    With no ``scopes``: every installer Change row 07 marked ready, and ``waiting`` names the held
    rows 07 has not marked yet. With ``scopes``: those scopes' rows. Either way a schema with a table
    inside its convert → 14-day window is refused with that reason (and the date it becomes
    possible, when ``window_ends`` knows it). Only rows the tool recorded as added are ever revoked.
    """
    revokes, refused, waiting = [], [], []
    for r in ledger_rows or []:
        if not _held(r):
            continue
        fqn = str(r.get("securable_name") or "").lower()
        stype = str(r.get("securable_type") or "").upper()
        if scopes:
            wanted = any(scope_covers(s, *(fqn.split(".") + [""])[:2]) if stype == "SCHEMA"
                         else scope_parts(s)[0] == fqn and scope_parts(s)[1] is None for s in scopes)
            if not wanted:
                continue
        elif r.get("status") != LEDGER_STATUS_HANDBACK_READY:
            waiting.append(str(r.get("securable_name")))
            continue
        busy = [s for s in _schemas_of(r, states_by_schema) if in_window(states_by_schema.get(s) or {})]
        stmt = revoke_sql(r.get("privilege"), stype, r.get("securable_name"), r.get("grantee"))
        item = {"ledger_key": r["ledger_key"], "securable_type": stype, "securable": r.get("securable_name"),
                "privilege": r.get("privilege"), "grantee": r.get("grantee"), "statement": stmt}
        if busy:
            ends = sorted(v for v in ((window_ends or {}).get(s) for s in busy) if v)
            when = f" It becomes possible after {ends[-1]}." if ends else ""
            refused.append({**item, "reason": (f"{', '.join(busy)} has a table inside its 14-day rollback "
                                              f"window; rollback needs these rights.{when}")})
        else:
            revokes.append(item)
    return {"revokes": _revoke_order(revokes), "refused": refused, "waiting": sorted(set(waiting))}


_PRIV_UNDO_ORDER = {"MODIFY": 0, "MANAGE": 1, "SELECT": 2, "BROWSE": 3, "USE SCHEMA": 4,
                    "CREATE SCHEMA": 5, "USE CATALOG": 6}
_TYPE_UNDO_ORDER = {"TABLE": 0, "SCHEMA": 1, "CATALOG": 2, "METASTORE": 3}


def _revoke_order(items: List[dict]) -> List[dict]:
    """Undo order: table privileges first, then schema MANAGE / data, catalog usage last."""
    return sorted(items, key=lambda i: (_TYPE_UNDO_ORDER.get(str(i["securable_type"]).upper(), 9),
                                        _PRIV_UNDO_ORDER.get(" ".join(str(i["privilege"]).upper().split()), 9),
                                        str(i["securable"]), str(i["grantee"])))


def audit_detail(line: Mapping[str, Any], *, door: str, pid: str, added: Optional[bool] = None,
                 prior_held: Optional[bool] = None, grantor: Optional[str] = None) -> str:
    d = {"front_door": door, "plan_id": pid, "set": line.get("set"), "grantee": line.get("grantee"),
         "grantee_kind": line.get("grantee_kind"), "privilege": line.get("privilege"),
         "securable_type": line.get("securable_type"), "securable": line.get("securable"),
         "scope": line.get("scope"), "added": added, "prior_held": prior_held}
    if grantor:
        d["grantor"] = grantor
    if line.get("job_ids"):
        d["job_ids"] = [str(j) for j in line["job_ids"]]
    return json.dumps(d, sort_keys=True)


def granted_by_tool(audit_rows: Iterable[Mapping[str, Any]]) -> List[dict]:
    """Grants the installer ran (``INSTALL_GRANT`` SUCCESS with ``added``) and has not revoked since
    (no later ``INSTALL_REVOKE`` SUCCESS for the same grant). Newest record per grant wins."""
    latest: Dict[Tuple, Tuple[Any, str, dict]] = {}
    for r in audit_rows or []:
        if r.get("action") not in (AUDIT_INSTALL_GRANT, AUDIT_INSTALL_REVOKE) or r.get("phase") != _au.PHASE_SUCCESS:
            continue
        try:
            d = json.loads(r.get("detail") or "{}")
        except (TypeError, ValueError):
            continue
        if not d.get("privilege"):
            continue
        k = line_key(d)
        ts = r.get("created_at") or ""
        prev = latest.get(k)
        if prev is None or str(ts) >= str(prev[0]):
            latest[k] = (ts, r["action"], d)
    return [d for _, act, d in latest.values() if act == AUDIT_INSTALL_GRANT and d.get("added")]


def uninstall_plan(audit_rows: Iterable[Mapping[str, Any]], ledger_rows: Iterable[Mapping[str, Any]],
                   states_by_schema: Mapping[str, Mapping[str, int]], *,
                   installer: Optional[str] = None) -> dict:
    """Every grant the tool made that is still held, in undo order; refused while any table is in
    its convert → 14-day window. Never touches data.

    The installer's own access to the install records (their records self-grant) is ``kept``, not
    revoked: every revoke writes its audit row, so it cannot remove the access that writes it. Its
    statements are returned for the person to run last.
    """
    busy = sorted(s for s, st in states_by_schema.items() if in_window(st))
    seen = set()
    items = []
    for r in ledger_rows or []:
        if _held(r):
            d = {"privilege": r.get("privilege"), "securable_type": r.get("securable_type"),
                 "securable": r.get("securable_name"), "grantee": r.get("grantee"), "set": SET_CHANGE}
            seen.add(line_key(d))
            items.append({**d, "ledger_key": r["ledger_key"]})
    for d in granted_by_tool(audit_rows):
        if str(d.get("securable_type")).upper() in API_SECURABLES:
            continue  # job and folder permissions go with the deployment at `bundle destroy`
        if line_key(d) not in seen:
            seen.add(line_key(d))
            items.append({k: d.get(k) for k in ("privilege", "securable_type", "securable", "grantee", "set")})
    kept = []
    for i in items:
        i["securable_type"] = str(i["securable_type"]).upper()
        i["statement"] = revoke_sql(i["privilege"], i["securable_type"], i["securable"], i["grantee"])
    if installer:
        kept = [i for i in items if same_principal(i["grantee"], installer) and i.get("set") == SET_CONTROL]
        items = [i for i in items if i not in kept]
    reason = (f"{len(busy)} schema(s) have a table inside its 14-day rollback window "
              f"({', '.join(busy[:5])}{'…' if len(busy) > 5 else ''}); uninstall waits until every "
              "table is CLEANED or ROLLED_BACK.") if busy else None
    return {"revokes": _revoke_order(items), "kept": _revoke_order(kept), "refused": reason}


def revoke_text(plan: Mapping[str, Any], pid: str, *, title: str, dry_run: bool = True) -> List[str]:
    """A hand-back or uninstall plan as the lines every text door prints."""
    head = (f"{title} {pid}  (dry run — set confirm_grants=true to apply; plan_id={pid} pins it)"
            if dry_run else f"{title} {pid}")
    out = [head]
    refused = plan.get("refused")
    if isinstance(refused, str) and refused:
        out.append(f"  Refused: {refused}")
    for r in (refused if isinstance(refused, list) else []):
        out.append(f"  Not yet: {r['statement']}  — {r['reason']}")
    for r in plan.get("revokes") or []:
        out.append(f"  {r['statement']}")
    if not plan.get("revokes"):
        out.append("  Nothing to revoke.")
    if plan.get("waiting"):
        out.append(f"  Still held, not ready yet: {', '.join(plan['waiting'])}. Cleanup marks a schema ready")
        out.append("  once every table in it is CLEANED or ROLLED_BACK. To hand one back now, name it in scopes.")
    if plan.get("kept"):
        out.append("  Kept (your own access to the install records; run these yourself last):")
        out += [f"    {r['statement']};" for r in plan["kept"]]
    return out


# --------------------------------------------------------------------------- #
# SQL builders for the install records (same rows from every door)
# --------------------------------------------------------------------------- #

def sql_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ts_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    s = v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v)
    return f"TIMESTAMP {sql_lit(s)}"


_AUDIT_TYPES = dict(_au.AUDIT_COLUMNS)


def audit_insert_sql(catalog: str, schema: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """One INSERT for audit rows built with ``audit.build_audit_row``."""
    cols = list(_au.AUDIT_COLUMN_NAMES)
    vals = []
    for r in rows:
        parts = []
        for c in cols:
            t = _AUDIT_TYPES[c]
            v = r.get(c)
            parts.append(_ts_lit(v) if t == "TIMESTAMP" else
                         ("NULL" if v is None else f"CAST({sql_lit(v)} AS {t})") if t in ("BIGINT", "INT")
                         else sql_lit(v))
        vals.append("(" + ", ".join(parts) + ")")
    return (f"INSERT INTO {catalog}.{schema}.{_au.AUDIT_TABLE} ({', '.join(cols)}) VALUES "
            + ", ".join(vals))


def install_audit_row(line: Mapping[str, Any], *, action: str, phase: str, door: str, pid: str,
                      operator: Optional[str], now, added: Optional[bool] = None,
                      prior_held: Optional[bool] = None, error: Optional[str] = None,
                      grantor: Optional[str] = None, statement: Optional[str] = None) -> dict:
    return _au.build_audit_row(
        full_name=line.get("securable"), action=action, phase=phase, operator=operator,
        sql_command=statement or line.get("statement"),
        error_class=("INSTALL_GRANT_FAILED" if error else None), error_message=error,
        detail=audit_detail(line, door=door, pid=pid, added=added, prior_held=prior_held, grantor=grantor),
        start_time=now, end_time=now, created_at=now, wave_id=line.get("wave_id"),
    )


SCOPE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("scope", "STRING"),            # catalog.schema or catalog.* — the MERGE key
    ("scope_kind", "STRING"),       # SCHEMA | CATALOG
    ("added_by", "STRING"),
    ("added_at", "TIMESTAMP"),
    ("front_door", "STRING"),       # notebook | cli | app
    ("sets_granted", "STRING"),     # comma list of Read / Change applied on it
    ("convert_rights", "STRING"),   # all_at_once | per_wave
    ("removed_at", "TIMESTAMP"),    # flagged, never deleted
    ("removed_by", "STRING"),
)
SCOPE_COLUMN_NAMES = tuple(c for c, _ in SCOPE_COLUMNS)


def scope_table_fqn(catalog: str, schema: str) -> str:
    return f"{catalog}.{schema}.{SCOPE_TABLE}"


def create_scope_table_sql(catalog: str, schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in SCOPE_COLUMNS)
    return f"CREATE TABLE IF NOT EXISTS {scope_table_fqn(catalog, schema)} (\n  {cols}\n) USING DELTA"


def scope_rows(scopes: Sequence[str], *, added_by: Optional[str], door: str, sets: Sequence[str],
               convert_rights: str, now) -> List[dict]:
    return [{"scope": s, "scope_kind": scope_kind(s), "added_by": added_by, "added_at": now,
             "front_door": door, "sets_granted": ",".join(sets), "convert_rights": convert_rights,
             "removed_at": None, "removed_by": None} for s in scopes]


def scope_merge_sql(catalog: str, schema: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """MERGE on ``scope``: a re-added scope keeps who first added it, refreshes the sets granted,
    and is un-flagged if it had been removed."""
    sel = " UNION ALL ".join(
        "SELECT " + ", ".join(
            f"{_ts_lit(r.get(c)) if t == 'TIMESTAMP' else sql_lit(r.get(c))} AS {c}"
            if not (t == "TIMESTAMP" and r.get(c) is None) else f"CAST(NULL AS TIMESTAMP) AS {c}"
            for c, t in SCOPE_COLUMNS) for r in rows)
    return (f"MERGE INTO {scope_table_fqn(catalog, schema)} t USING ({sel}) s ON t.scope = s.scope "
            "WHEN MATCHED THEN UPDATE SET t.sets_granted = s.sets_granted, "
            "t.convert_rights = s.convert_rights, t.removed_at = NULL, t.removed_by = NULL "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(SCOPE_COLUMN_NAMES)}) "
            f"VALUES ({', '.join('s.' + c for c in SCOPE_COLUMN_NAMES)})")


def scope_remove_sql(catalog: str, schema: str, scopes: Sequence[str], *, removed_by: Optional[str], now) -> str:
    keys = ", ".join(sql_lit(s) for s in scopes)
    return (f"UPDATE {scope_table_fqn(catalog, schema)} SET removed_at = {_ts_lit(now)}, "
            f"removed_by = {sql_lit(removed_by)} WHERE scope IN ({keys}) AND removed_at IS NULL")


def stored_scopes_sql(catalog: str, schema: str) -> str:
    return (f"SELECT {', '.join(SCOPE_COLUMN_NAMES)} FROM {scope_table_fqn(catalog, schema)} "
            "WHERE removed_at IS NULL ORDER BY scope")


def ledger_row(line: Mapping[str, Any], *, installer: Optional[str], door: str, pid: str, now,
               added: bool, prior_held: Optional[bool]) -> dict:
    """The ownership_ledger GRANT row for one installer Change line."""
    stype = str(line["securable_type"]).upper()
    cat, _, rest = str(line["securable"]).partition(".")
    return {
        "ledger_key": ledger_key(line), "action_key": None, "action_id": None, "op_kind": "GRANT",
        "scope": stype, "catalog": cat, "schema": rest or None, "table_name": None, "full_name": None,
        "prior_owner": None, "new_owner": None, "dry_run": False,
        "status": LEDGER_STATUS_GRANTED, "leased_at": now, "leased_by": installer,
        "restored_at": None, "restored_by": None, "plan_run_id": None, "wave_id": line.get("wave_id"),
        "evidence": json.dumps({"front_door": door, "plan_id": pid}, sort_keys=True),
        "notes": "installer convert rights (handed back with revoke_convert)", "updated_at": now,
        "securable_type": stype, "securable_name": line["securable"], "privilege": line["privilege"],
        "grantee": line["grantee"], "prior_held": prior_held, "added": added, "granted_at": now,
        "revoked_at": None,
    }


def ledger_merge_sql(catalog: str, schema: str, rows: Sequence[Mapping[str, Any]],
                     columns: Sequence[Tuple[str, str]], mutable: Iterable[str]) -> str:
    """MERGE installer ledger rows on ``ledger_key`` (IDENTITY kept, MUTABLE refreshed).

    A re-apply that finds the grant already in place (``prior_held``) never overwrites a live row
    that recorded the installer adding it: that record is what hand-back and uninstall revoke.
    """
    def lit(v, t):
        if v is None:
            return f"CAST(NULL AS {t})"
        return _ts_lit(v) if t == "TIMESTAMP" else sql_lit(v)
    sel = " UNION ALL ".join("SELECT " + ", ".join(f"{lit(r.get(c), t)} AS {c}" for c, t in columns)
                             for r in rows)
    names = [c for c, _ in columns]
    upd = ", ".join(f"t.{c} = s.{c}" for c in names if c in set(mutable))
    return (f"MERGE INTO {catalog}.{schema}.ownership_ledger t USING ({sel}) s "
            f"ON t.ledger_key = s.ledger_key WHEN MATCHED AND NOT {_KEEP_ADDED} THEN UPDATE SET {upd} "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) VALUES ({', '.join('s.' + c for c in names)})")


_KEEP_ADDED = ("(coalesce(s.prior_held, false) AND coalesce(t.added, false) "
               "AND NOT coalesce(t.prior_held, false) "
               f"AND t.status IN ({sql_lit(LEDGER_STATUS_GRANTED)}, {sql_lit(LEDGER_STATUS_HANDBACK_READY)}))")


def ledger_status_sql(catalog: str, schema: str, keys: Sequence[str], *, status: str, now,
                      by: Optional[str] = None, from_status: Sequence[str] = (LEDGER_STATUS_GRANTED,)) -> str:
    quoted = ", ".join(sql_lit(k) for k in keys)
    froms = ", ".join(sql_lit(s) for s in from_status)
    extra = (f", revoked_at = {_ts_lit(now)}, restored_by = {sql_lit(by)}"
             if status == LEDGER_STATUS_REVOKED else "")
    return (f"UPDATE {catalog}.{schema}.ownership_ledger SET status = {sql_lit(status)}, "
            f"updated_at = {_ts_lit(now)}{extra} WHERE ledger_key IN ({quoted}) AND status IN ({froms})")


def installer_ledger_sql(catalog: str, schema: str) -> str:
    return (f"SELECT * FROM {catalog}.{schema}.ownership_ledger WHERE ledger_key LIKE "
            f"'{LEDGER_PREFIX}|%' AND dry_run = false")


def install_audit_sql(catalog: str, schema: str) -> str:
    return (f"SELECT action, phase, detail, created_at FROM {catalog}.{schema}.{_au.AUDIT_TABLE} "
            f"WHERE action IN ('{AUDIT_INSTALL_GRANT}', '{AUDIT_INSTALL_REVOKE}')")


def manifest_states_sql(catalog: str, schema: str) -> str:
    return f"SELECT catalog, schema, state FROM {catalog}.{schema}.table_manifest"


def window_ends_sql(catalog: str, schema: str) -> str:
    """Per schema, 14 days after its latest successful convert (when hand-back becomes possible)."""
    return (f"SELECT lower(concat(split(full_name, '[.]')[0], '.', split(full_name, '[.]')[1])) AS scope, "
            f"date_format(max(end_time) + INTERVAL 14 DAYS, 'yyyy-MM-dd') AS ends "
            f"FROM {catalog}.{schema}.{_au.AUDIT_TABLE} WHERE action = '{_au.ACTION_CONVERT}' "
            f"AND phase = '{_au.PHASE_SUCCESS}' GROUP BY 1")


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #

def exit_code(*, errors: Sequence[str] = (), can_install: bool = True, plan_mismatch: bool = False,
              redeploy: bool = False, gaps: bool = False) -> int:
    if errors:
        return EXIT_INVALID
    if plan_mismatch:
        return EXIT_PLAN_MISMATCH
    if redeploy:
        return EXIT_REDEPLOY
    if not can_install:
        return EXIT_NO_GO
    if gaps:
        return EXIT_GAPS
    return EXIT_OK


def json_document(*, rights: Optional[Mapping[str, Any]] = None, lines: Sequence[Mapping[str, Any]] = (),
                  results: Optional[Sequence[Mapping[str, Any]]] = None, action: str = ACTION_INSTALL,
                  exit: int = EXIT_OK, messages: Sequence[str] = (), extra: Optional[Mapping[str, Any]] = None) -> dict:
    """The CLI's ``--output json`` document (stable keys for scripts and change tickets)."""
    doc = {"action": action, "exit_code": exit, "rights": rights,
           "coverage": (rights or {}).get("coverage"), "plan": [dict(ln) for ln in lines],
           "plan_id": plan_id(lines) if lines else None, "results": list(results) if results is not None else None,
           "messages": list(messages)}
    if extra:
        doc.update(extra)
    return doc
