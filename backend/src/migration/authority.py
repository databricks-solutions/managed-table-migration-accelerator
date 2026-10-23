"""Convert authority: can the convert identity ``SET MANAGED`` this table, and if not, what exactly
must be granted, and by whom? (pure rule + thin Spark edges).

This is the ONE place the verdict is computed. Every writer (profiler, dry run, authorize,
convert, conductor, bulk cockpit) evaluates through :func:`evaluate` and persists the result to
the ``convert_authority`` control table; the app, MCP and Genie only read that table.

The rule is empirical (live scratch matrix on serverless SQL warehouses, DBSQL 2026.38/2026.39 —
``backend/src/tests/fixtures/convert_authority_cases.json``). ``SET MANAGED`` / ``UNSET MANAGED``
need ALL of:

- **Usage** — USE CATALOG and USE SCHEMA. Owning the catalog implies USE CATALOG but NOT USE SCHEMA
  on another principal's schema; owning the schema implies USE SCHEMA; metastore admin implies
  neither.
- **Authority** — owner of the table, schema or catalog (directly or through a group), or MANAGE on
  any of them (direct, inherited or group). Metastore admin does NOT count by itself. ``ALL
  PRIVILEGES`` never includes MANAGE.
- **Data** — SELECT and MODIFY (any level, ``ALL PRIVILEGES`` covers both), unless the identity owns
  the table.

The ``SET MANAGED`` error "User is not an owner" is misleading — it is raised for a missing
authority even when ownership is not required (:func:`explain_failure`).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

STATUS_AUTHORIZED = "AUTHORIZED"                # every requirement already held
STATUS_SELF_AUTHORIZABLE = "SELF_AUTHORIZABLE"  # missing only what the identity can grant itself
STATUS_NEEDS_GRANT = "NEEDS_GRANT"              # someone else must grant at least one privilege
STATUS_UNKNOWN = "UNKNOWN"                      # could not prove it either way — blocks the convert
ALL_STATUSES = (STATUS_AUTHORIZED, STATUS_SELF_AUTHORIZABLE, STATUS_NEEDS_GRANT, STATUS_UNKNOWN)

BASIS = ("TABLE_OWNER", "SCHEMA_OWNER", "CATALOG_OWNER", "MANAGE_TABLE", "MANAGE_SCHEMA", "MANAGE_CATALOG")
MISSING = ("USE_CATALOG", "USE_SCHEMA", "OWNER_OR_MANAGE", "SELECT", "MODIFY")

LEVEL_TABLE, LEVEL_SCHEMA, LEVEL_CATALOG = "TABLE", "SCHEMA", "CATALOG"

_ALL = "ALL PRIVILEGES"
_GRANTOR_FALLBACK = "any principal with MANAGE on it, or a metastore admin"
_UNREAD = "could not be read by the convert identity"


@dataclass(frozen=True)
class AuthorityFacts:
    """Everything the rule needs, as read by the convert identity itself (pure input)."""

    full_name: str
    actor: str
    table_owner: Optional[str]
    schema_owner: Optional[str]
    catalog_owner: Optional[str]
    actor_groups: frozenset = field(default_factory=frozenset)  # groups the actor is a member of
    grants: tuple = ()  # (securable_level, privilege, grantee) visible to the actor
    metastore_admin: Optional[bool] = None
    reads_ok: bool = True
    note: Optional[str] = None


def facts_from_dict(d: dict) -> AuthorityFacts:
    return AuthorityFacts(
        full_name=d.get("full_name") or "",
        actor=d.get("actor") or "",
        table_owner=d.get("table_owner"),
        schema_owner=d.get("schema_owner"),
        catalog_owner=d.get("catalog_owner"),
        actor_groups=frozenset(d.get("actor_groups") or ()),
        grants=tuple(tuple(g) for g in (d.get("grants") or ())),
        metastore_admin=d.get("metastore_admin"),
        reads_ok=bool(d.get("reads_ok", True)),
        note=d.get("note"),
    )


def _norm(p) -> str:
    return str(p or "").strip().casefold()


def _priv(p) -> str:
    return " ".join(str(p or "").replace("_", " ").upper().split())


def _split(full_name: str) -> Optional[tuple[str, str, str]]:
    parts = (full_name or "").split(".")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        return None
    return parts[0], parts[1], parts[2]


class _Ctx:
    def __init__(self, f: AuthorityFacts):
        self.f = f
        cat, sch, _tbl = _split(f.full_name)
        self.names = {LEVEL_TABLE: f.full_name, LEVEL_SCHEMA: f"{cat}.{sch}", LEVEL_CATALOG: cat}
        self.owners = {LEVEL_TABLE: f.table_owner, LEVEL_SCHEMA: f.schema_owner, LEVEL_CATALOG: f.catalog_owner}
        self.me = {_norm(f.actor)} | {_norm(g) for g in f.actor_groups}
        self.me.discard("")
        self.ms_admin = bool(f.metastore_admin)

    def is_me(self, principal) -> bool:
        return _norm(principal) in self.me

    def owns(self, level) -> bool:
        return self.owners[level] is not None and self.is_me(self.owners[level])

    def held(self, privs: set, levels: Iterable[str]):
        """The first matching grant (level, privilege, grantee) the actor holds, or None."""
        levels = set(levels)
        for lvl, priv, grantee in self.f.grants:
            if str(lvl).upper() in levels and _priv(priv) in privs and self.is_me(grantee):
                return (str(lvl).upper(), _priv(priv), grantee)
        return None

    def can_grant(self, level) -> bool:
        """Can the actor issue GRANT on a securable at ``level``? (owner up-chain, MANAGE, ms admin)."""
        chain = {LEVEL_TABLE: (LEVEL_TABLE, LEVEL_SCHEMA, LEVEL_CATALOG),
                 LEVEL_SCHEMA: (LEVEL_SCHEMA, LEVEL_CATALOG),
                 LEVEL_CATALOG: (LEVEL_CATALOG,)}[level]
        if self.ms_admin:
            return True
        if any(self.owns(lv) for lv in chain):
            return True
        return self.held({"MANAGE"}, chain) is not None


def _basis(c: _Ctx) -> tuple[Optional[str], Optional[str]]:
    for lvl, code in ((LEVEL_TABLE, "TABLE_OWNER"), (LEVEL_SCHEMA, "SCHEMA_OWNER"), (LEVEL_CATALOG, "CATALOG_OWNER")):
        if c.owns(lvl):
            owner = c.owners[lvl]
            return code, (None if _norm(owner) == _norm(c.f.actor) else owner)
    for lvl, code in ((LEVEL_TABLE, "MANAGE_TABLE"), (LEVEL_SCHEMA, "MANAGE_SCHEMA"), (LEVEL_CATALOG, "MANAGE_CATALOG")):
        g = c.held({"MANAGE"}, (lvl,))
        if g:
            return code, (None if _norm(g[2]) == _norm(c.f.actor) else g[2])
    return None, None


def _missing(c: _Ctx, basis: Optional[str]) -> list[str]:
    out: list[str] = []
    if not (c.owns(LEVEL_CATALOG) or c.held({"USE CATALOG", _ALL}, (LEVEL_CATALOG,))):
        out.append("USE_CATALOG")
    if not (c.owns(LEVEL_SCHEMA) or c.held({"USE SCHEMA", _ALL}, (LEVEL_SCHEMA, LEVEL_CATALOG))):
        out.append("USE_SCHEMA")
    if basis is None:
        out.append("OWNER_OR_MANAGE")
    if not c.owns(LEVEL_TABLE):
        every = (LEVEL_TABLE, LEVEL_SCHEMA, LEVEL_CATALOG)
        if not c.held({"SELECT", _ALL}, every):
            out.append("SELECT")
        if not c.held({"MODIFY", _ALL}, every):
            out.append("MODIFY")
    return out


_MISSING_TO_GRANT = {
    "USE_CATALOG": (LEVEL_CATALOG, "USE CATALOG"),
    "USE_SCHEMA": (LEVEL_SCHEMA, "USE SCHEMA"),
    "OWNER_OR_MANAGE": (LEVEL_TABLE, "MANAGE"),
    "SELECT": (LEVEL_TABLE, "SELECT"),
    "MODIFY": (LEVEL_TABLE, "MODIFY"),
}


def _q(principal: str) -> str:
    return "`" + str(principal).replace("`", "``") + "`"


def grant_request(securable_type: str, securable_name: str, privilege: str, grantee: str) -> dict:
    req = {"securable_type": securable_type, "securable_name": securable_name,
           "privilege": privilege, "grantee": grantee}
    req["statement"] = grant_statement(req)
    return req


def grant_statement(req: dict) -> str:
    """Executable GRANT (no trailing ``;``). Securable names are stored identifiers, as elsewhere."""
    return f"GRANT {req['privilege']} ON {req['securable_type']} {req['securable_name']} TO {_q(req['grantee'])}"


def revoke_statement(req: dict) -> str:
    return f"REVOKE {req['privilege']} ON {req['securable_type']} {req['securable_name']} FROM {_q(req['grantee'])}"


def grantors(f: AuthorityFacts, request: dict) -> dict:
    """Who can run ONE grant request: ``grantors`` (narrowest scope first), ``grantor_hint``,
    ``ask_text`` (the verbatim sentence every interface renders) and ``self_grantable``.

    Only principals that can grant at the request's level are named (a catalog-level grant never
    names a table or schema owner). Other principals' MANAGE grants are invisible to the actor, so
    they are described, never invented. An owner that cannot be read is never guessed."""
    c = _Ctx(f)
    level = request["securable_type"]
    stmt = request["statement"]
    if c.can_grant(level):
        return {"self_grantable": True, "grantors": [f.actor], "grantor_hint": "Authorize can do this",
                "ask_text": f"Authorize can do this: {stmt}"}
    chain = {LEVEL_TABLE: (LEVEL_TABLE, LEVEL_SCHEMA, LEVEL_CATALOG),
             LEVEL_SCHEMA: (LEVEL_SCHEMA, LEVEL_CATALOG),
             LEVEL_CATALOG: (LEVEL_CATALOG,)}[level]
    named: list[tuple[str, str]] = []
    seen: set = set()
    for lv in chain:
        owner = c.owners[lv]
        if owner is None or not str(owner).strip() or c.is_me(owner) or _norm(owner) in seen:
            continue
        seen.add(_norm(owner))
        named.append((lv, str(owner).strip()))
    if named:
        lv, who = named[0]
        hint = _GRANTOR_FALLBACK
        ask = (f"Ask the owner of {lv.lower()} `{c.names[lv]}` ({_q(who)}) to run: {stmt}; "
               f"or ask {hint}.")
    else:
        hint = f"the owner of catalog `{c.names[LEVEL_CATALOG]}` ({_UNREAD}), {_GRANTOR_FALLBACK}"
        ask = f"Ask {hint} to run: {stmt}"
    return {"self_grantable": False, "grantors": [w for _, w in named], "grantor_hint": hint, "ask_text": ask}


def grant_plan(f: AuthorityFacts, missing: Iterable[str]) -> list[dict]:
    """The narrowest grants that close ``missing``, in executable order (catalog, schema, MANAGE,
    data), each annotated with :func:`grantors`."""
    c = _Ctx(f)
    out = []
    for m in MISSING:
        if m not in missing:
            continue
        level, priv = _MISSING_TO_GRANT[m]
        req = grant_request(level, c.names[level], priv, f.actor)
        req.update(grantors(f, req))
        out.append(req)
    return out


_REVOKE_RANK = {(LEVEL_TABLE, "MODIFY"): 0, (LEVEL_TABLE, "SELECT"): 1, (LEVEL_TABLE, "MANAGE"): 2}
_LEVEL_RANK = {LEVEL_TABLE: 2, LEVEL_SCHEMA: 3, LEVEL_CATALOG: 4}


def revoke_order(rows: Iterable[dict]) -> list[dict]:
    """Undo order: table data privileges, then table MANAGE, then schema, then catalog usage."""
    def key(r):
        lvl = str(r.get("securable_type") or "").upper()
        return _REVOKE_RANK.get((lvl, _priv(r.get("privilege"))), _LEVEL_RANK.get(lvl, 5))
    return sorted(rows or [], key=key)


def evaluate(f: AuthorityFacts) -> dict:
    """The verdict (pure). Keys: status, basis, basis_via, missing, self_grantable, metastore_admin,
    grant_requests, grant_sql, reason."""
    v = {"actor": f.actor, "full_name": f.full_name, "status": STATUS_UNKNOWN, "basis": None,
         "basis_via": None, "missing": [], "self_grantable": False,
         "metastore_admin": f.metastore_admin, "grant_requests": [], "grant_sql": [],
         "owners_known": f.table_owner is not None, "note": f.note}
    if not f.reads_ok or _split(f.full_name) is None or not _norm(f.actor):
        if _split(f.full_name) is None:
            v["note"] = f.note or "not a catalog.schema.table name"
        v["reason"] = authority_text(v)
        return v
    c = _Ctx(f)
    basis, via = _basis(c)
    missing = _missing(c, basis)
    requests = grant_plan(f, missing)
    v.update(basis=basis, basis_via=via, missing=missing, grant_requests=requests,
             grant_sql=[r["statement"] for r in requests],
             self_grantable=all(r["self_grantable"] for r in requests))
    if not missing:
        v["status"] = STATUS_AUTHORIZED
    elif v["self_grantable"]:
        v["status"] = STATUS_SELF_AUTHORIZABLE
    elif f.table_owner is None:
        v["status"] = STATUS_UNKNOWN
    else:
        v["status"] = STATUS_NEEDS_GRANT
    v["reason"] = authority_text(v)
    return v


# --------------------------------------------------------------------------- copy

_BASIS_PHRASE = {
    "TABLE_OWNER": "it owns the table", "SCHEMA_OWNER": "it owns the schema",
    "CATALOG_OWNER": "it owns the catalog", "MANAGE_TABLE": "it holds MANAGE on the table",
    "MANAGE_SCHEMA": "it holds MANAGE on the schema", "MANAGE_CATALOG": "it holds MANAGE on the catalog",
}

MISSING_LABELS = {
    "USE_CATALOG": "USE CATALOG on the catalog",
    "USE_SCHEMA": "USE SCHEMA on the schema",
    "OWNER_OR_MANAGE": "ownership or MANAGE on the table",
    "SELECT": "SELECT on the table",
    "MODIFY": "MODIFY on the table",
}


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def missing_phrase(missing: Iterable[str]) -> str:
    """``["SELECT", "MODIFY"]`` -> ``"SELECT and MODIFY on the table"`` (grouped by securable)."""
    missing = [m for m in MISSING if m in set(missing or ())]
    groups: list[str] = []
    if "USE_CATALOG" in missing:
        groups.append(MISSING_LABELS["USE_CATALOG"])
    if "USE_SCHEMA" in missing:
        groups.append(MISSING_LABELS["USE_SCHEMA"])
    table = [{"OWNER_OR_MANAGE": "ownership or MANAGE", "SELECT": "SELECT", "MODIFY": "MODIFY"}[m]
             for m in missing if m in ("OWNER_OR_MANAGE", "SELECT", "MODIFY")]
    if table:
        groups.append(f"{_and(table)} on the table")
    return _and(groups)


def _basis_clause(v: dict) -> str:
    phrase = _BASIS_PHRASE.get(v.get("basis") or "")
    if not phrase:
        return ""
    if v.get("basis_via"):
        phrase += f" through group `{v['basis_via']}`"
    return phrase


def authority_text(v: dict) -> str:
    """The one sentence every interface renders verbatim for a verdict."""
    who = f"`{v.get('actor') or 'the convert identity'}`"
    status = v.get("status")
    if status == STATUS_AUTHORIZED:
        return f"{who} can convert this table: {_basis_clause(v)}."
    if status == STATUS_UNKNOWN and not v.get("missing"):
        note = v.get("note") or "read failed"
        return f"Permissions for {who} could not be read ({note}); the convert stays blocked until they can be."
    gap = missing_phrase(v.get("missing") or ())
    if status == STATUS_SELF_AUTHORIZABLE:
        clause = _basis_clause(v)
        if not clause and v.get("metastore_admin"):
            clause = "it is a metastore admin (which does not count by itself)"
        return f"{who} is missing {gap}; {clause}, so Authorize can grant it."
    if status == STATUS_UNKNOWN:
        return (f"{who} is missing {gap} and cannot read who owns the table; ask the catalog owner or a "
                "metastore admin to run the grant script.")
    return f"{who} is missing {gap}; someone who can grant it must run the grant script."


# --------------------------------------------------------------------------- failure text

_NOT_OWNER = re.compile(r"not an owner", re.IGNORECASE)
_DOES_NOT_HAVE = re.compile(r"does not have (.+?) on (table|schema|catalog)\b", re.IGNORECASE)


def explain_failure(error_text: Optional[str]) -> Optional[str]:
    """Translate a SET/UNSET MANAGED permission error into the rule (None if unrelated)."""
    if not error_text:
        return None
    if _NOT_OWNER.search(error_text):
        return ("SET MANAGED reports \"not an owner\" even when the real gap is a missing privilege: the "
                "convert identity needs USE CATALOG, USE SCHEMA, ownership or MANAGE, and SELECT and MODIFY "
                "on the table. Re-check permissions, then run Authorize.")
    m = _DOES_NOT_HAVE.search(error_text)
    if m:
        return (f"SET MANAGED failed because the convert identity does not have {m.group(1)} on the "
                f"{m.group(2).lower()}. Re-check permissions, then run Authorize.")
    return None


# --------------------------------------------------------------------------- #
# Facts edge — read everything AS the convert identity (information_schema is permission
# filtered to the caller, so the identity that will run SET MANAGED must be the one reading).
# --------------------------------------------------------------------------- #

FACTS_CHUNK = 200
NOTE_ACTOR_MISMATCH = "evaluated identity differs from the convert identity"

_INHERITED_LEVEL = {"SCHEMA": LEVEL_SCHEMA, "CATALOG": LEVEL_CATALOG, "METASTORE": LEVEL_CATALOG}


def _lit(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _in_list(values: Iterable[str]) -> str:
    return ", ".join(_lit(v) for v in values)


def _chunks(items: list, n: int = FACTS_CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def grant_level(own_level: str, inherited_from) -> str:
    """The securable level a privilege row actually lives at (pure).

    ``information_schema.*_privileges`` lists an inherited privilege on the child with
    ``inherited_from`` naming the ancestor — either a level keyword or the ancestor's name."""
    inh = str(inherited_from or "").strip()
    if not inh or inh.upper() == "NONE":
        return own_level
    if inh.upper() in _INHERITED_LEVEL:
        return _INHERITED_LEVEL[inh.upper()]
    return LEVEL_CATALOG if "." not in inh else LEVEL_SCHEMA


def facts_sql(full_names: list[str]) -> dict:
    """The read-only information_schema statements for one chunk of tables (pure; tested)."""
    triples = [_split(fn) for fn in full_names]
    triples = [t for t in triples if t]
    tables = sorted({".".join(t) for t in triples})
    schemas = sorted({f"{t[0]}.{t[1]}" for t in triples})
    catalogs = sorted({t[0] for t in triples})
    tl, sl, cl = _in_list(tables) or "''", _in_list(schemas) or "''", _in_list(catalogs) or "''"
    snl = _in_list(sorted({t[1] for t in triples})) or "''"
    # The bare catalog/schema predicates are what information_schema can prune on; the concat
    # filter alone enumerates the whole metastore (minutes per statement on a large one).
    t_scope = f"table_catalog IN ({cl}) AND table_schema IN ({snl})"
    s_scope = f"catalog_name IN ({cl}) AND schema_name IN ({snl})"
    isc = "system.information_schema"
    return {
        "tables": (f"SELECT table_catalog, table_schema, table_name, table_owner FROM {isc}.tables "
                   f"WHERE {t_scope} AND concat_ws('.', table_catalog, table_schema, table_name) IN ({tl})"),
        "schemata": (f"SELECT catalog_name, schema_name, schema_owner FROM {isc}.schemata "
                     f"WHERE {s_scope} AND concat_ws('.', catalog_name, schema_name) IN ({sl})"),
        "catalogs": f"SELECT catalog_name, catalog_owner FROM {isc}.catalogs WHERE catalog_name IN ({cl})",
        "table_privileges": (
            f"SELECT grantee, table_catalog, table_schema, table_name, privilege_type, inherited_from "
            f"FROM {isc}.table_privileges "
            f"WHERE {t_scope} AND concat_ws('.', table_catalog, table_schema, table_name) IN ({tl})"),
        "schema_privileges": (
            f"SELECT grantee, catalog_name, schema_name, privilege_type, inherited_from "
            f"FROM {isc}.schema_privileges "
            f"WHERE {s_scope} AND concat_ws('.', catalog_name, schema_name) IN ({sl})"),
        "catalog_privileges": (
            f"SELECT grantee, catalog_name, privilege_type, inherited_from "
            f"FROM {isc}.catalog_privileges WHERE catalog_name IN ({cl})"),
    }


def membership_sql(principals: list[str]) -> str:
    """One row of booleans ``m0..mN`` — is the caller a member of each principal (pure)."""
    cols = ", ".join(f"is_account_group_member({_lit(p)}) AS m{i}" for i, p in enumerate(principals))
    return f"SELECT {cols}"


METASTORE_OWNER_SQL = "SELECT metastore_owner FROM system.information_schema.metastores"


def assemble_facts(
    full_names: Iterable[str],
    actor: str,
    *,
    table_rows: Iterable[dict] = (),
    schema_rows: Iterable[dict] = (),
    catalog_rows: Iterable[dict] = (),
    priv_rows: Iterable[tuple] = (),
    member_of: Iterable[str] = (),
    metastore_admin: Optional[bool] = None,
    reads_ok: bool = True,
    note: Optional[str] = None,
) -> dict:
    """``{full_name: AuthorityFacts}`` from already-read rows (pure).

    ``priv_rows`` are ``(level, securable_name, privilege, grantee)`` with the level already
    resolved through :func:`grant_level`."""
    t_own = {f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}": r.get("table_owner")
             for r in table_rows}
    s_own = {f"{r['catalog_name']}.{r['schema_name']}": r.get("schema_owner") for r in schema_rows}
    c_own = {r["catalog_name"]: r.get("catalog_owner") for r in catalog_rows}
    groups = frozenset(member_of or ())
    by_securable: dict = {}
    for level, name, priv, grantee in priv_rows:
        by_securable.setdefault((level, name), []).append((level, priv, grantee))
    out: dict = {}
    for fn in full_names:
        t = _split(fn)
        if not t:
            out[fn] = AuthorityFacts(full_name=fn, actor=actor, table_owner=None, schema_owner=None,
                                     catalog_owner=None, reads_ok=reads_ok, note=note)
            continue
        sch, cat = f"{t[0]}.{t[1]}", t[0]
        grants = tuple(
            by_securable.get((LEVEL_TABLE, fn), []) + by_securable.get((LEVEL_SCHEMA, sch), [])
            + by_securable.get((LEVEL_CATALOG, cat), [])
        )
        out[fn] = AuthorityFacts(
            full_name=fn, actor=actor, table_owner=t_own.get(fn), schema_owner=s_own.get(sch),
            catalog_owner=c_own.get(cat), actor_groups=groups, grants=grants,
            metastore_admin=metastore_admin, reads_ok=reads_ok, note=note,
        )
    return out


def _collect(spark, q: str) -> list[dict]:
    return [r.asDict() for r in spark.sql(q).collect()]


def current_identity(spark) -> str:
    return spark.sql("SELECT current_user() AS u").collect()[0]["u"]


def read_authority_facts(
    spark, full_names: Iterable[str], *, expected_actor: Optional[str] = None,
    logger: Callable[[str], None] = print,
) -> dict:
    """``{full_name: AuthorityFacts}`` read AS ``current_user()`` (thin edge; degrades honestly).

    A failed owner/privilege read yields ``reads_ok=False`` (→ ``UNKNOWN``, which blocks the
    convert). A failed group-membership read is recorded in ``note`` and treated as "not a
    member" (can only make the verdict stricter). When ``expected_actor`` (the convert identity)
    differs from the caller, the caller's grants prove nothing about it: every table is
    ``UNKNOWN`` with :data:`NOTE_ACTOR_MISMATCH` — never a guess."""
    names = sorted({fn for fn in (full_names or []) if fn})
    actor = current_identity(spark)
    if expected_actor and _norm(expected_actor) != _norm(actor):
        note = f"{NOTE_ACTOR_MISMATCH} (`{actor}` read, convert runs as `{expected_actor}`)"
        return assemble_facts(names, expected_actor, reads_ok=False, note=note)
    table_rows: list = []
    schema_rows: list = []
    catalog_rows: list = []
    priv_rows: list = []
    try:
        for chunk in _chunks(names):
            q = facts_sql(chunk)
            table_rows += _collect(spark, q["tables"])
            schema_rows += _collect(spark, q["schemata"])
            catalog_rows += _collect(spark, q["catalogs"])
            for r in _collect(spark, q["table_privileges"]):
                priv_rows.append((grant_level(LEVEL_TABLE, r.get("inherited_from")),
                                  f"{r['table_catalog']}.{r['table_schema']}.{r['table_name']}",
                                  r.get("privilege_type"), r.get("grantee")))
            for r in _collect(spark, q["schema_privileges"]):
                priv_rows.append((grant_level(LEVEL_SCHEMA, r.get("inherited_from")),
                                  f"{r['catalog_name']}.{r['schema_name']}",
                                  r.get("privilege_type"), r.get("grantee")))
            for r in _collect(spark, q["catalog_privileges"]):
                priv_rows.append((LEVEL_CATALOG, r["catalog_name"], r.get("privilege_type"), r.get("grantee")))
    except Exception as ex:  # noqa: BLE001
        note = f"permissions read failed: {str(ex)[:160]}"
        logger(f"convert authority: {note}")
        return assemble_facts(names, actor, reads_ok=False, note=note)

    # An inherited row names the ancestor; re-key it onto that ancestor's securable name.
    rekeyed = []
    for level, name, priv, grantee in priv_rows:
        parts = name.split(".")
        if level == LEVEL_SCHEMA and len(parts) == 3:
            name = ".".join(parts[:2])
        elif level == LEVEL_CATALOG and len(parts) >= 2:
            name = parts[0]
        rekeyed.append((level, name, priv, grantee))

    principals = sorted(
        {str(p).strip() for p in [*(g for *_x, g in rekeyed),
                                  *(r.get("table_owner") for r in table_rows),
                                  *(r.get("schema_owner") for r in schema_rows),
                                  *(r.get("catalog_owner") for r in catalog_rows)]
         if p and _norm(p) != _norm(actor)}
    )
    member_of: list = []
    note = None
    try:
        for chunk in _chunks(principals, 100):
            row = spark.sql(membership_sql(chunk)).collect()[0]
            member_of += [p for i, p in enumerate(chunk) if row[f"m{i}"]]
    except Exception as ex:  # noqa: BLE001
        note = f"group membership unreadable: {str(ex)[:120]}"
        logger(f"convert authority: {note}")

    ms_admin: Optional[bool] = None
    try:
        rows = spark.sql(METASTORE_OWNER_SQL).collect()
        owner = rows[0]["metastore_owner"] if rows else None
        if owner:
            ms_admin = _norm(owner) == _norm(actor) or bool(
                spark.sql(membership_sql([owner])).collect()[0]["m0"])
    except Exception:  # noqa: BLE001
        ms_admin = None

    return assemble_facts(names, actor, table_rows=table_rows, schema_rows=schema_rows,
                          catalog_rows=catalog_rows, priv_rows=rekeyed, member_of=member_of,
                          metastore_admin=ms_admin, note=note)


# --------------------------------------------------------------------------- #
# convert_authority control table — single source of truth for DDL + StructType + row.
# --------------------------------------------------------------------------- #

CONVERT_AUTHORITY_TABLE = "convert_authority"

SOURCE_PROFILER = "00_profiler"
SOURCE_DRY_RUN = "03_dry_run"
SOURCE_AUTHORIZE = "03d_authorize"
SOURCE_CONVERT = "04_convert"
SOURCE_CONDUCTOR = "08_conductor"
SOURCE_BULK_COCKPIT = "11e_bulk_cockpit"
ALL_SOURCES = (SOURCE_PROFILER, SOURCE_DRY_RUN, SOURCE_AUTHORIZE, SOURCE_CONVERT,
               SOURCE_CONDUCTOR, SOURCE_BULK_COCKPIT)

_REQUEST_STRUCT = (
    "STRUCT<statement: STRING, securable_type: STRING, securable_name: STRING, privilege: STRING, "
    "self_grantable: BOOLEAN, grantors: ARRAY<STRING>, grantor_hint: STRING, ask_text: STRING>"
)
REQUEST_FIELDS = ("statement", "securable_type", "securable_name", "privilege", "self_grantable",
                  "grantors", "grantor_hint", "ask_text")

AUTHORITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("authority_key", "STRING"),       # AUTHORITY|<full_name>|<actor> (MERGE key)
    ("full_name", "STRING"),
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("table_name", "STRING"),
    ("actor", "STRING"),               # the identity the verdict is FOR (the convert identity)
    ("status", "STRING"),              # AUTHORIZED | SELF_AUTHORIZABLE | NEEDS_GRANT | UNKNOWN
    ("basis", "STRING"),               # TABLE_OWNER ... MANAGE_CATALOG, or NULL
    ("basis_via", "STRING"),           # the group the basis comes through, or NULL
    ("missing", "ARRAY<STRING>"),      # subset of USE_CATALOG, USE_SCHEMA, OWNER_OR_MANAGE, SELECT, MODIFY
    ("grant_sql", "ARRAY<STRING>"),    # flat, executable, in grant order
    ("grant_requests", f"ARRAY<{_REQUEST_STRUCT}>"),
    ("self_grantable", "BOOLEAN"),
    ("metastore_admin", "BOOLEAN"),
    ("table_owner", "STRING"),
    ("schema_owner", "STRING"),
    ("catalog_owner", "STRING"),
    ("reason", "STRING"),              # authority_text — rendered verbatim by every interface
    ("reads_ok", "BOOLEAN"),
    ("source", "STRING"),
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("job_run_id", "STRING"),
    ("evaluated_at", "TIMESTAMP"),
)
AUTHORITY_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in AUTHORITY_COLUMNS)


def convert_authority_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{CONVERT_AUTHORITY_TABLE}"


def authority_key(full_name: str, actor: str) -> str:
    return f"AUTHORITY|{full_name}|{actor}"


def authority_row(verdict: dict, *, source: str, evaluated_at, plan_run_id: Optional[str] = None,
                  wave_id: Optional[str] = None, job_run_id: Optional[str] = None,
                  facts: Optional[AuthorityFacts] = None) -> dict:
    """One ``convert_authority`` row (every column present) from a verdict (pure)."""
    fn = verdict.get("full_name") or ""
    t = _split(fn) or (None, None, None)
    row = {
        "authority_key": authority_key(fn, verdict.get("actor") or ""),
        "full_name": fn, "catalog": t[0], "schema": t[1], "table_name": t[2],
        "actor": verdict.get("actor"), "status": verdict.get("status"),
        "basis": verdict.get("basis"), "basis_via": verdict.get("basis_via"),
        "missing": list(verdict.get("missing") or []),
        "grant_sql": list(verdict.get("grant_sql") or []),
        "grant_requests": [{k: r.get(k) for k in REQUEST_FIELDS} for r in verdict.get("grant_requests") or []],
        "self_grantable": bool(verdict.get("self_grantable")),
        "metastore_admin": verdict.get("metastore_admin"),
        "table_owner": facts.table_owner if facts else None,
        "schema_owner": facts.schema_owner if facts else None,
        "catalog_owner": facts.catalog_owner if facts else None,
        "reason": verdict.get("reason"),
        "reads_ok": bool(facts.reads_ok) if facts else verdict.get("status") != STATUS_UNKNOWN,
        "source": source, "plan_run_id": plan_run_id, "wave_id": wave_id, "job_run_id": job_run_id,
        "evaluated_at": evaluated_at,
    }
    return {name: row.get(name) for name in AUTHORITY_COLUMN_NAMES}


def create_convert_authority_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in AUTHORITY_COLUMNS)
    return (f"CREATE TABLE IF NOT EXISTS {convert_authority_fqn(manifest_catalog, manifest_schema)} (\n"
            f"  {cols}\n) USING DELTA")


def convert_authority_struct_type():
    from pyspark.sql.types import (ArrayType, BooleanType, StringType, StructField, StructType,
                                   TimestampType)

    req = StructType([
        StructField("statement", StringType(), True), StructField("securable_type", StringType(), True),
        StructField("securable_name", StringType(), True), StructField("privilege", StringType(), True),
        StructField("self_grantable", BooleanType(), True),
        StructField("grantors", ArrayType(StringType(), True), True),
        StructField("grantor_hint", StringType(), True), StructField("ask_text", StringType(), True),
    ])
    by_sql = {"STRING": StringType(), "BOOLEAN": BooleanType(), "TIMESTAMP": TimestampType(),
              "ARRAY<STRING>": ArrayType(StringType(), True), f"ARRAY<{_REQUEST_STRUCT}>": ArrayType(req, True)}
    return StructType([StructField(n, by_sql[t], True) for n, t in AUTHORITY_COLUMNS])


def _reconcile_columns(spark, fqn: str) -> list[str]:
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in AUTHORITY_COLUMNS if n not in existing]
    if missing:
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({', '.join(f'{n} {t}' for n, t in missing)})")
    return [n for n, _ in missing]


def ensure_convert_authority_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_convert_authority_table_sql(manifest_catalog, manifest_schema))
    fqn = convert_authority_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def merge_authority_sql(fqn: str, src_view: str) -> str:
    """Upsert the latest verdict per ``authority_key`` (pure; every non-key column refreshed)."""
    update_cols = [c for c in AUTHORITY_COLUMN_NAMES if c != "authority_key"]
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in update_cols)
    cols = ", ".join(AUTHORITY_COLUMN_NAMES)
    vals = ", ".join(f"s.{c}" for c in AUTHORITY_COLUMN_NAMES)
    return (f"MERGE INTO {fqn} AS t USING {src_view} AS s ON t.authority_key = s.authority_key\n"
            f"WHEN MATCHED THEN UPDATE SET\n    {set_clause}\n"
            f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})")


def merge_authority_rows(spark, manifest_catalog: str, manifest_schema: str, rows: list[dict],
                         *, max_attempts: int = 5, sleep=time.sleep) -> int:
    """Idempotent MERGE on ``authority_key``; retries transient row-level conflicts (bulk fan-out)."""
    from . import concurrency as _cc

    if not rows:
        return 0
    fqn = convert_authority_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame([{n: r.get(n) for n in AUTHORITY_COLUMN_NAMES} for r in rows],
                               schema=convert_authority_struct_type())
    src_view = "_convert_authority_src"
    df.createOrReplaceTempView(src_view)
    q = merge_authority_sql(fqn, src_view)

    def _do():
        spark.sql(q)
        return len(rows)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def refresh_authority(
    spark, manifest_catalog: str, manifest_schema: str, full_names: Iterable[str], *,
    source: str, plan_run_id: Optional[str] = None, wave_id: Optional[str] = None,
    job_run_id: Optional[str] = None, expected_actor: Optional[str] = None,
    logger: Callable[[str], None] = print,
) -> list[dict]:
    """Read facts AS the caller, evaluate, persist, and return the ``convert_authority`` rows.

    Never raises: a persist failure is logged and the freshly computed rows are still returned,
    so a gate can act on them (they are UNKNOWN when the facts could not be read)."""
    from datetime import datetime, timezone

    names = sorted({fn for fn in (full_names or []) if fn})
    if not names:
        return []
    facts = read_authority_facts(spark, names, expected_actor=expected_actor, logger=logger)
    now = datetime.now(timezone.utc)
    rows = [authority_row(evaluate(facts[fn]), source=source, evaluated_at=now, plan_run_id=plan_run_id,
                          wave_id=wave_id, job_run_id=job_run_id, facts=facts[fn]) for fn in names]
    if any((facts[fn].note or "").startswith(NOTE_ACTOR_MISMATCH) for fn in names):
        # Another identity's read proves nothing about the convert identity: never overwrite
        # the verdict a matching reader stored.
        logger(f"convert authority: not persisted — {facts[names[0]].note}")
        return rows
    try:
        ensure_convert_authority_table(spark, manifest_catalog, manifest_schema)
        merge_authority_rows(spark, manifest_catalog, manifest_schema, rows)
    except Exception as ex:  # noqa: BLE001
        logger(f"convert authority: could not persist {len(rows)} verdict(s): {str(ex)[:160]}")
    return rows


def stored_authority_sql(fqn: str, full_names: list[str], actor: str) -> str:
    """The stored verdicts for ``full_names`` FOR ``actor`` (pure)."""
    return (f"SELECT * FROM {fqn} WHERE full_name IN ({_in_list(full_names)}) "
            f"AND lower(actor) = lower({_lit(actor)})")


def read_authority_rows(spark, manifest_catalog: str, manifest_schema: str, full_names: Iterable[str],
                        actor: str) -> dict:
    """``{full_name: row}`` of stored verdicts for ``actor``. A table with no row is absent (the
    caller counts it unevaluated). Raises on a failed read so the caller can degrade honestly."""
    names = sorted({fn for fn in (full_names or []) if fn})
    fqn = convert_authority_fqn(manifest_catalog, manifest_schema)
    out: dict = {}
    for chunk in _chunks(names):
        for r in spark.sql(stored_authority_sql(fqn, chunk, actor)).collect():
            d = r.asDict(recursive=True) if hasattr(r, "asDict") else dict(r)
            out[d["full_name"]] = d
    return out


def authority_for(
    spark, manifest_catalog: str, manifest_schema: str, full_names: Iterable[str], *,
    source: str, convert_identity: Optional[str] = None, plan_run_id: Optional[str] = None,
    job_run_id: Optional[str] = None, logger: Callable[[str], None] = print,
) -> tuple[dict, str]:
    """``({full_name: row}, note)`` for the convert identity (thin edge; never raises).

    When this job runs AS the convert identity (or none is named) the facts are re-read and
    persisted. Otherwise the caller's grants prove nothing, so the verdicts a matching reader
    stored are returned instead (absent → unevaluated). A failed stored read returns ``{}``."""
    names = sorted({fn for fn in (full_names or []) if fn})
    if not names:
        return {}, "no tables to evaluate"
    try:
        caller = current_identity(spark)
    except Exception as ex:  # noqa: BLE001
        return {}, f"current_user() unavailable ({type(ex).__name__}); permissions not evaluated"
    target = (convert_identity or "").strip() or caller
    if _norm(target) == _norm(caller):
        rows = refresh_authority(spark, manifest_catalog, manifest_schema, names, source=source,
                                 plan_run_id=plan_run_id, job_run_id=job_run_id, logger=logger)
        return {r["full_name"]: r for r in rows}, f"re-read as `{caller}` (the convert identity)"
    try:
        stored = read_authority_rows(spark, manifest_catalog, manifest_schema, names, target)
    except Exception as ex:  # noqa: BLE001
        return {}, f"stored verdicts unreadable ({type(ex).__name__}); permissions not evaluated"
    return stored, (f"stored verdicts for `{target}` (this run is `{caller}`, so it cannot re-read "
                    f"them): {len(stored)} of {len(names)} evaluated")


MIGRATED_STATES = ("CONVERTED", "CONVERTED_EXTERNAL", "VALIDATED", "CLEANED")


def is_to_convert(state) -> bool:
    """Mirrors ``progress`` ``to_convert``: active (not DROPPED) and not yet migrated."""
    s = str(state or "").upper()
    return s != "DROPPED" and s not in MIGRATED_STATES


def wave_members_sql(wave_plan_fqn: str, manifest_fqn: str, plan_run_id: str) -> str:
    """The plan's members with their manifest state (pure)."""
    return (f"SELECT p.wave_id AS wave_id, p.full_name AS full_name, m.state AS state "
            f"FROM {wave_plan_fqn} p JOIN {manifest_fqn} m ON p.full_name = m.full_name "
            f"WHERE p.plan_run_id = {_lit(plan_run_id)}")


def to_convert_names(member_rows: Iterable[dict]) -> list[str]:
    return sorted({r["full_name"] for r in member_rows or [] if r.get("full_name") and is_to_convert(r.get("state"))})


def schema_scope_rows(manifest_rows: Iterable[dict], planned_names: Iterable[str]) -> tuple[list[dict], list[str]]:
    """Authorize's ``target_schema`` scope (pure): the schema's tables in the current plan, plus
    the ones held out of it only because the convert identity cannot read them (Authorize is
    their way in; the next profile plans them). Any other held-back table is left out: a grant
    on a table that can never convert is never revoked. Returns ``(rows, excluded_names)``."""
    from .eligibility import BLOCKER_CANNOT_READ  # noqa: PLC0415

    planned = set(planned_names or ())
    keep, excluded = [], []
    for r in manifest_rows or []:
        fn = r.get("full_name")
        if not fn:
            continue
        blockers = {b.strip() for b in str(r.get("blockers") or "").split(",") if b.strip()}
        if fn in planned or blockers == {BLOCKER_CANNOT_READ}:
            keep.append(r)
        else:
            excluded.append(fn)
    return keep, sorted(excluded)


def apply_authority_to_waves(wave_rows: list, member_rows: Iterable[dict], authority_by_fn: dict) -> list:
    """Overwrite each wave's ``open_lease`` from convert authority (pure; mutates in place).

    Per wave, over the members still to convert: ``lease_total`` counts them, ``open_lease``
    counts the ones whose verdict is missing or not ``AUTHORIZED`` (the convert gate blocks
    exactly those), ``authority_unevaluated`` the ones with no verdict at all,
    ``authority_self_grantable`` the blocked ones Authorize can grant itself, and
    ``authority_reason`` is ``"<full_name>: <reason>"`` for the first blocked table (or ``None``)."""
    total: dict = {}
    open_: dict = {}
    unevaluated: dict = {}
    self_ok: dict = {}
    example: dict = {}
    for r in sorted(member_rows or [], key=lambda m: str(m.get("full_name") or "")):
        fn = r.get("full_name")
        if not fn or not is_to_convert(r.get("state")):
            continue
        wid = r.get("wave_id")
        total[wid] = total.get(wid, 0) + 1
        row = (authority_by_fn or {}).get(fn)
        if row is None:
            unevaluated[wid] = unevaluated.get(wid, 0) + 1
        if row is None or row.get("status") != STATUS_AUTHORIZED:
            open_[wid] = open_.get(wid, 0) + 1
            if row is not None and row.get("self_grantable"):
                self_ok[wid] = self_ok.get(wid, 0) + 1
            if row is not None and row.get("reason") and wid not in example:
                example[wid] = f"{fn}: {row['reason']}"
    for w in wave_rows or []:
        wid = w.get("wave_id")
        w["lease_total"] = total.get(wid, 0)
        w["open_lease"] = open_.get(wid, 0)
        w["authority_unevaluated"] = unevaluated.get(wid, 0)
        w["authority_self_grantable"] = self_ok.get(wid, 0)
        w["authority_reason"] = example.get(wid)
    return wave_rows


def unverified_grant_text(n: int) -> str:
    """The one sentence for ``n`` convert-permission actions Authorize applied but could not verify."""
    return (f"{n} convert-permission grant(s) were applied but are not verified yet. Re-run Authorize "
            "to re-check; granting again is a no-op.")


def wave_authority_text(wave: Optional[dict]) -> str:
    """The wave-level permission sentence (conductor directive, cockpit, MCP). Pure.

    Built from the fields :func:`apply_authority_to_waves` stamps; quotes one table's
    :func:`authority_text` verbatim. Fields absent from a row (the raw view) are not invented."""
    w = wave or {}
    n = int(w.get("open_lease") or 0)
    who = (w.get("convert_run_as") or "").strip()
    who = f"`{who}`" if who else "the convert identity"
    of = f" of {int(w['lease_total'])}" if w.get("lease_total") is not None and int(w["lease_total"]) >= n else ""
    text = f"{n}{of} table(s) still to convert are not yet authorized for {who}"
    unev = int(w.get("authority_unevaluated") or 0)
    if unev:
        text += f" ({unev} not checked yet)"
    text += "."
    if w.get("authority_reason"):
        text += f" For example, {w['authority_reason']}"
    if "authority_self_grantable" in w:
        k = int(w.get("authority_self_grantable") or 0)
        checked = max(n - unev, 0)
        if checked and k >= checked:
            text += " Authorize can grant all of the checked ones."
        elif k:
            text += f" Authorize can grant {k}; the rest need their owner's grant (see the grant script)."
        elif checked:
            text += " Authorize cannot grant these; their owners must run the grant script."
    return text


def refresh_waves(spark, manifest_catalog: str, manifest_schema: str, wave_rows: list, *,
                  wave_plan_fqn: str, manifest_fqn: str, plan_run_id: str, source: str,
                  convert_identity: Optional[str] = None, job_run_id: Optional[str] = None,
                  logger: Callable[[str], None] = print) -> str:
    """Evaluate the plan's still-to-convert tables and make ``open_lease`` authority-based in
    place (the one edge shared by ``08_conductor`` and ``11_bulk_cockpit``). Never raises: a
    failed member read leaves ``wave_rows`` untouched and says so. Returns a one-line note."""
    try:
        members = [r.asDict() if hasattr(r, "asDict") else dict(r)
                   for r in spark.sql(wave_members_sql(wave_plan_fqn, manifest_fqn, plan_run_id)).collect()]
    except Exception as ex:  # noqa: BLE001
        return f"permission count as the view computed it (plan members unreadable: {type(ex).__name__})"
    by_fn, note = authority_for(spark, manifest_catalog, manifest_schema, to_convert_names(members),
                                source=source, convert_identity=convert_identity,
                                plan_run_id=plan_run_id, job_run_id=job_run_id, logger=logger)
    apply_authority_to_waves(wave_rows, members, by_fn)
    s = summarize(by_fn.values())
    return f"convert permission: {s['counts']} — {note}"


def first_grantor(row: Optional[dict]) -> Optional[str]:
    """The first named grantor of the first request Authorize cannot run (pure; None if unknown)."""
    for r in (row or {}).get("grant_requests") or []:
        if r and not r.get("self_grantable"):
            for g in r.get("grantors") or []:
                if g and str(g).strip():
                    return str(g).strip()
    return None


def summarize(rows: Iterable[dict]) -> dict:
    """Status counts plus the tables that are not yet AUTHORIZED (pure)."""
    rows = list(rows or [])
    counts = {s: 0 for s in ALL_STATUSES}
    for r in rows:
        counts[r.get("status") or STATUS_UNKNOWN] = counts.get(r.get("status") or STATUS_UNKNOWN, 0) + 1
    blocked = sorted(r["full_name"] for r in rows if r.get("status") != STATUS_AUTHORIZED)
    return {"total": len(rows), "counts": counts, "not_authorized": blocked}


def grant_script(rows: Iterable[dict]) -> str:
    """A copy-ready grant script, one section per grantor (pure).

    Self-grantable requests are listed under "Authorize can do this"; requests with no readable
    owner go last under the metastore-admin / MANAGE-holder fallback."""
    sections: dict = {}
    order: list = []
    for r in rows or []:
        for req in r.get("grant_requests") or []:
            if req.get("self_grantable"):
                head = "-- Authorize can do this (the convert identity can grant it itself)"
            elif req.get("grantors"):
                head = (f"-- Run as `{req['grantors'][0]}` (owner), or any principal with MANAGE on it, "
                        "or a metastore admin")
            else:
                head = "-- Run as a metastore admin or a principal with MANAGE (owner could not be read)"
            if head not in sections:
                sections[head] = []
                order.append(head)
            stmt = req.get("statement") + ";"
            if stmt not in sections[head]:
                sections[head].append(stmt)
    fallback = [h for h in order if h.startswith("-- Run as a metastore admin")]
    ordered = [h for h in order if h not in fallback] + fallback
    return "\n\n".join(h + "\n" + "\n".join(sections[h]) for h in ordered) + ("\n" if ordered else "")


GATE_CLEAN_REASON = ("Every table still to convert is AUTHORIZED for the convert identity "
                     "(USE CATALOG, USE SCHEMA, ownership or MANAGE, SELECT and MODIFY).")
GATE_REMEDIATION_API = "Run job_03d_lease_ownership (Authorize) for this wave, or send the grant script."


def gate_gaps(rows: Iterable[dict]) -> list[dict]:
    """The tables the convert gate blocks: every verdict that is not ``AUTHORIZED`` (pure)."""
    gaps = []
    for r in sorted(rows or [], key=lambda x: x.get("full_name") or ""):
        if r.get("status") == STATUS_AUTHORIZED:
            continue
        sql = list(r.get("grant_sql") or [])
        gaps.append({
            "full_name": r.get("full_name"), "unit": f"PERMISSION {r.get('full_name')}",
            "status": r.get("status"), "missing": list(r.get("missing") or []), "grant_sql": sql,
            "self_grantable": bool(r.get("self_grantable")), "reason": r.get("reason") or authority_text(r),
            "remediation_sql": ";\n".join(sql) + ";" if sql else None,
            "remediation_api": None if sql else GATE_REMEDIATION_API,
        })
    return gaps


def gate_report(gaps: Iterable[dict], rows_by_fn: Optional[dict] = None) -> list[str]:
    """The exact lines ``04_convert`` prints for the tables it blocks (pure)."""
    lines = ["Tables the convert identity cannot SET MANAGED yet — run Authorize "
             "(job_03d_lease_ownership) or send the grant script first:"]
    for g in gaps or []:
        lines.append(f"  - {g['full_name']}: {g['reason']}")
        reqs = ((rows_by_fn or {}).get(g["full_name"]) or {}).get("grant_requests") or []
        for req in reqs:
            lines.append(f"      {req.get('statement')};  -- {req.get('ask_text')}")
        if not reqs:
            lines += [f"      {s};" for s in g.get("grant_sql") or []]
    return lines


def build_gate_row(rows: Iterable[dict], *, evaluated_at, plan_run_id: Optional[str], wave_id: Optional[str],
                   group_key: Optional[str], source: str, operator: Optional[str] = None,
                   job_run_id: Optional[str] = None) -> tuple[dict, list[dict]]:
    """ONE ``OWNERSHIP_COVERAGE`` gate row from convert-authority verdicts, plus the gaps (pure)."""
    from . import gate_eval as _ge

    rows = list(rows or [])
    gaps = gate_gaps(rows)
    reason = "\n".join(gate_report(gaps, {r.get("full_name"): r for r in rows})) if gaps else GATE_CLEAN_REASON
    row = _ge.build_gate_eval_row(
        gate_kind=_ge.GATE_OWNERSHIP_COVERAGE, evaluated_at=evaluated_at,
        verdict=(_ge.VERDICT_FAIL if gaps else _ge.VERDICT_PASS), passed=not gaps, reason=reason,
        plan_run_id=plan_run_id, wave_id=wave_id, group_key=group_key, item_count=len(gaps),
        detail=json.dumps({"gaps": gaps}, default=str), source=source, operator=operator,
        job_run_id=job_run_id,
        notes=(None if all(r.get("reads_ok", True) for r in rows) else "permissions unreadable for some tables"),
    )
    return row, gaps


CHECK_NUMBER = 4
CHECK_NAME = "convert_authority"


def dry_run_check(row: Optional[dict]) -> tuple[str, str]:
    """Dry-run Check 4 ``(status, detail)``. Advisory: WARN or INFO, never FAIL — the hard gate
    is ``04_convert``, which re-reads the facts. A missing row WARNs (never a silent all-clear)."""
    from . import eligibility as _elig

    if not row:
        return _elig.CHECK_WARN, ("Convert permission was not evaluated for this table; the convert "
                                  "re-checks it and stays blocked until it can be read.")
    if row.get("status") == STATUS_AUTHORIZED:
        return _elig.CHECK_INFO, row.get("reason") or authority_text(row)
    lines = [row.get("reason") or authority_text(row)]
    for req in row.get("grant_requests") or []:
        lines.append(f"  {req.get('statement')};  -- {req.get('ask_text')}")
    return _elig.CHECK_WARN, "\n".join(lines)


def verdict_json(row: dict) -> str:
    """Compact evidence payload for actions / audit rows (pure)."""
    keep = ("status", "basis", "basis_via", "missing", "grant_sql", "reason")
    return json.dumps({k: row.get(k) for k in keep}, sort_keys=True, default=str)
