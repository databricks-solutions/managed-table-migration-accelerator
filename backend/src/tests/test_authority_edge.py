"""convert_authority edges: the facts reader (fake Spark), the control table, and the grant script."""

from datetime import datetime, timezone

from migration import authority as au


class _Row(dict):
    def asDict(self, recursive=False):  # noqa: ARG002 — pyspark Row signature
        return dict(self)


class _DF:
    def __init__(self, rows):
        self._rows = [_Row(r) for r in rows]

    def collect(self):
        return self._rows


class _FakeSpark:
    """Answers the information_schema reads the way UC does for ONE caller."""

    def __init__(self, *, me="sp", tables=(), schemata=(), catalogs=(), tprivs=(), sprivs=(), cprivs=(),
                 groups=(), ms_owner="ms-admins", fail_on=None, fail_membership=False):
        self.me, self.groups, self.ms_owner = me, set(groups), ms_owner
        self.data = {"tables": tables, "schemata": schemata, "catalogs": catalogs,
                     "table_privileges": tprivs, "schema_privileges": sprivs, "catalog_privileges": cprivs}
        self.fail_on, self.fail_membership = fail_on, fail_membership
        self.queries = []

    def sql(self, q):
        import re

        self.queries.append(q)
        if "current_user()" in q:
            return _DF([{"u": self.me}])
        if "is_account_group_member" in q:
            if self.fail_membership:
                raise RuntimeError("is_account_group_member not supported")
            names = re.findall(r"is_account_group_member\('((?:[^']|'')*)'\) AS (m\d+)", q)
            return _DF([{alias: (n.replace("''", "'") in self.groups) for n, alias in names}])
        if "information_schema.metastores" in q:
            return _DF([{"metastore_owner": self.ms_owner}])
        for key in ("table_privileges", "schema_privileges", "catalog_privileges", "schemata", "catalogs", "tables"):
            if f"information_schema.{key} " in q:
                if self.fail_on == key:
                    raise RuntimeError("PERMISSION_DENIED: information_schema")
                return _DF(self.data[key])
        raise AssertionError(q)


def _tier2_spark(**kw):
    """Today's Tier-2 grants on a foreign-owned table (case c15 of the golden fixture)."""
    base = dict(
        tables=[{"table_catalog": "c", "table_schema": "s", "table_name": "t", "table_owner": "ann"}],
        schemata=[{"catalog_name": "c", "schema_name": "s", "schema_owner": "ann"}],
        catalogs=[{"catalog_name": "c", "catalog_owner": "ops"}],
        tprivs=[{"grantee": "sp", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                 "privilege_type": "SELECT", "inherited_from": "SCHEMA"}],
        sprivs=[{"grantee": "sp", "catalog_name": "c", "schema_name": "s", "privilege_type": "USE SCHEMA",
                 "inherited_from": "NONE"},
                {"grantee": "sp", "catalog_name": "c", "schema_name": "s", "privilege_type": "SELECT",
                 "inherited_from": "NONE"},
                {"grantee": "sp", "catalog_name": "c", "schema_name": "s", "privilege_type": "MANAGE",
                 "inherited_from": "NONE"}],
        cprivs=[{"grantee": "sp", "catalog_name": "c", "privilege_type": "USE_CATALOG", "inherited_from": None}],
    )
    base.update(kw)
    return _FakeSpark(**base)


def test_reader_produces_tier2_verdict_self_grant_modify():
    facts = au.read_authority_facts(_tier2_spark(), ["c.s.t"])
    v = au.evaluate(facts["c.s.t"])
    assert v["status"] == au.STATUS_SELF_AUTHORIZABLE
    assert v["basis"] == "MANAGE_SCHEMA"
    assert v["missing"] == ["MODIFY"]
    assert v["grant_sql"] == ["GRANT MODIFY ON TABLE c.s.t TO `sp`"]


def test_reader_counts_group_grants_only_for_groups_the_caller_belongs_to():
    sp = _tier2_spark(
        sprivs=[{"grantee": "sp", "catalog_name": "c", "schema_name": "s", "privilege_type": "USE SCHEMA",
                 "inherited_from": "NONE"}],
        tprivs=[{"grantee": "eng", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                 "privilege_type": "MANAGE", "inherited_from": "NONE"},
                {"grantee": "eng", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                 "privilege_type": "ALL PRIVILEGES", "inherited_from": "NONE"},
                {"grantee": "strangers", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                 "privilege_type": "MODIFY", "inherited_from": "NONE"}],
        groups={"eng"},
    )
    v = au.evaluate(au.read_authority_facts(sp, ["c.s.t"])["c.s.t"])
    assert v["status"] == au.STATUS_AUTHORIZED
    assert (v["basis"], v["basis_via"]) == ("MANAGE_TABLE", "eng")


def test_reader_failure_is_unknown_never_all_clear():
    rows = au.read_authority_facts(_tier2_spark(fail_on="table_privileges"), ["c.s.t"])
    v = au.evaluate(rows["c.s.t"])
    assert v["status"] == au.STATUS_UNKNOWN
    assert "permissions read failed" in v["reason"]


def test_membership_failure_only_makes_the_verdict_stricter():
    sp = _tier2_spark(tprivs=[{"grantee": "eng", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                               "privilege_type": "MODIFY", "inherited_from": "NONE"}],
                      groups={"eng"}, fail_membership=True)
    f = au.read_authority_facts(sp, ["c.s.t"])["c.s.t"]
    assert "group membership unreadable" in f.note
    assert "MODIFY" in au.evaluate(f)["missing"]


def test_reader_for_a_different_identity_is_unknown():
    f = au.read_authority_facts(_tier2_spark(), ["c.s.t"], expected_actor="prod-sp")["c.s.t"]
    v = au.evaluate(f)
    assert v["status"] == au.STATUS_UNKNOWN and v["actor"] == "prod-sp"
    assert au.NOTE_ACTOR_MISMATCH in v["reason"]


def test_reader_matches_expected_actor_case_insensitively():
    f = au.read_authority_facts(_tier2_spark(), ["c.s.t"], expected_actor=" SP ")["c.s.t"]
    assert f.reads_ok


def test_metastore_admin_detected_via_owner_group():
    sp = _tier2_spark(groups={"ms-admins"})
    assert au.read_authority_facts(sp, ["c.s.t"])["c.s.t"].metastore_admin is True
    assert au.read_authority_facts(_tier2_spark(), ["c.s.t"])["c.s.t"].metastore_admin is False


def test_invisible_table_has_no_owner():
    f = au.read_authority_facts(_tier2_spark(tables=[]), ["c.s.t"])["c.s.t"]
    assert f.table_owner is None and f.schema_owner == "ann"


def test_schema_scope_keeps_planned_and_unreadable_tables_only():
    rows = [
        {"full_name": "c.s.planned", "state": "DRY_RUN_OK", "blockers": None},
        {"full_name": "c.s.unreadable", "state": "DISCOVERED", "blockers": "convert_identity_cannot_read"},
        {"full_name": "c.s.unreadable_parquet", "state": "DISCOVERED",
         "blockers": "convert_identity_cannot_read, non_delta_format"},
        {"full_name": "c.s.corrupt", "state": "BLOCKED", "blockers": "corrupted_or_inaccessible"},
        {"full_name": "c.s.unplanned", "state": "DISCOVERED", "blockers": None},
    ]
    keep, excluded = au.schema_scope_rows(rows, {"c.s.planned"})
    assert [r["full_name"] for r in keep] == ["c.s.planned", "c.s.unreadable"]
    assert excluded == ["c.s.corrupt", "c.s.unplanned", "c.s.unreadable_parquet"]


def test_facts_sql_is_chunked_and_quoted():
    q = au.facts_sql(["c.s.t", "c.s.o'brien", "bad"])
    assert "'c.s.o''brien'" in q["tables"]
    assert "'bad'" not in q["tables"]
    assert q["schemata"].endswith("IN ('c.s')")
    assert q["catalogs"].endswith("IN ('c')")
    for key in ("tables", "table_privileges"):
        assert "table_catalog IN ('c') AND table_schema IN ('s')" in q[key]
    for key in ("schemata", "schema_privileges"):
        assert "catalog_name IN ('c') AND schema_name IN ('s')" in q[key]
    names = [f"c.s.t{i}" for i in range(450)]
    sp = _tier2_spark()
    au.read_authority_facts(sp, names)
    assert sum(1 for q in sp.queries if "information_schema.tables " in q) == 3


def test_grant_level_resolves_inheritance():
    assert au.grant_level("TABLE", None) == "TABLE"
    assert au.grant_level("TABLE", "NONE") == "TABLE"
    assert au.grant_level("TABLE", "SCHEMA") == "SCHEMA"
    assert au.grant_level("SCHEMA", "CATALOG") == "CATALOG"
    assert au.grant_level("TABLE", "c.s") == "SCHEMA"
    assert au.grant_level("TABLE", "c") == "CATALOG"


def test_inherited_catalog_grant_is_rekeyed_onto_the_catalog():
    sp = _tier2_spark(tprivs=[{"grantee": "sp", "table_catalog": "c", "table_schema": "s", "table_name": "t",
                               "privilege_type": "MODIFY", "inherited_from": "CATALOG"}])
    f = au.read_authority_facts(sp, ["c.s.t"])["c.s.t"]
    assert ("CATALOG", "MODIFY", "sp") in f.grants
    assert au.evaluate(f)["status"] == au.STATUS_AUTHORIZED


# ---------------------------------------------------------------- table


def test_columns_cover_the_verdict_and_key_is_deterministic():
    names = set(au.AUTHORITY_COLUMN_NAMES)
    assert {"authority_key", "status", "missing", "grant_requests", "grant_sql", "reason", "source"} <= names
    assert len(names) == len(au.AUTHORITY_COLUMN_NAMES)
    assert au.authority_key("c.s.t", "sp") == "AUTHORITY|c.s.t|sp"


def test_authority_row_has_every_column_and_request_fields():
    f = au.read_authority_facts(_tier2_spark(), ["c.s.t"])["c.s.t"]
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    row = au.authority_row(au.evaluate(f), source=au.SOURCE_DRY_RUN, evaluated_at=now, facts=f,
                           plan_run_id="p1", wave_id="w1")
    assert tuple(row) == au.AUTHORITY_COLUMN_NAMES
    assert row["catalog"] == "c" and row["schema"] == "s" and row["table_name"] == "t"
    assert row["table_owner"] == "ann" and row["catalog_owner"] == "ops"
    assert row["status"] == "SELF_AUTHORIZABLE" and row["reads_ok"] is True
    (req,) = row["grant_requests"]
    assert tuple(req) == au.REQUEST_FIELDS
    assert req["ask_text"].startswith("Authorize can do this")


def test_ddl_declares_every_column_once():
    ddl = au.create_convert_authority_table_sql("m", "mig")
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS m.mig.convert_authority")
    for name, typ in au.AUTHORITY_COLUMNS:
        assert f"  {name} {typ}" in ddl
    assert "grant_requests ARRAY<STRUCT<statement: STRING" in ddl


def test_merge_refreshes_every_non_key_column():
    q = au.merge_authority_sql("m.mig.convert_authority", "_src")
    assert "ON t.authority_key = s.authority_key" in q
    for c in au.AUTHORITY_COLUMN_NAMES[1:]:
        assert f"t.{c} = s.{c}" in q
    assert "t.authority_key = s.authority_key," not in q


def test_sources_are_unique():
    assert len(set(au.ALL_SOURCES)) == len(au.ALL_SOURCES)


def test_summarize_counts_and_lists_blocked():
    rows = [{"full_name": "a", "status": "AUTHORIZED"}, {"full_name": "b", "status": "NEEDS_GRANT"},
            {"full_name": "c", "status": "UNKNOWN"}]
    s = au.summarize(rows)
    assert s["counts"]["AUTHORIZED"] == 1 and s["not_authorized"] == ["b", "c"]


def test_grant_script_sections_by_grantor_with_fallback_last():
    req = lambda stmt, self_, g: {"statement": stmt, "self_grantable": self_, "grantors": g}  # noqa: E731
    rows = [
        {"grant_requests": [req("GRANT MODIFY ON TABLE c.s.a TO `sp`", True, ["sp"])]},
        {"grant_requests": [req("GRANT USE CATALOG ON CATALOG x TO `sp`", False, []),
                            req("GRANT MANAGE ON TABLE c.s.b TO `sp`", False, ["ann", "ops"]),
                            req("GRANT MANAGE ON TABLE c.s.b TO `sp`", False, ["ann", "ops"])]},
    ]
    script = au.grant_script(rows)
    sections = script.strip().split("\n\n")
    assert sections[0].startswith("-- Authorize can do this")
    assert sections[1].startswith("-- Run as `ann` (owner)")
    assert sections[1].count("GRANT MANAGE ON TABLE c.s.b TO `sp`;") == 1
    assert sections[-1].startswith("-- Run as a metastore admin")
    assert au.grant_script([]) == ""


class _PersistSpark(_FakeSpark):
    """Records writes and can fail them."""

    def __init__(self, *, fail_write=False, **kw):
        super().__init__(**kw)
        self.fail_write = fail_write
        self.created = []

    def sql(self, q):
        if q.startswith(("CREATE TABLE", "MERGE INTO", "DESCRIBE TABLE", "ALTER TABLE")):
            self.queries.append(q)
            if self.fail_write:
                raise RuntimeError("no MODIFY on the control schema")
            if q.startswith("DESCRIBE TABLE"):
                return _DF([{"col_name": n} for n in au.AUTHORITY_COLUMN_NAMES])
            return _DF([])
        return super().sql(q)

    def createDataFrame(self, rows, schema=None):  # noqa: N802
        self.created.append(rows)
        spark = self

        class _Tmp:
            def createOrReplaceTempView(self, _name):  # noqa: N802
                spark.queries.append("TEMPVIEW")
        return _Tmp()


def _patch_struct(monkeypatch):
    monkeypatch.setattr(au, "convert_authority_struct_type", lambda: None)


def test_refresh_persists_and_returns_rows(monkeypatch):
    _patch_struct(monkeypatch)
    sp = _PersistSpark(tables=_tier2_spark().data["tables"], schemata=_tier2_spark().data["schemata"],
                       catalogs=_tier2_spark().data["catalogs"], sprivs=_tier2_spark().data["schema_privileges"],
                       cprivs=_tier2_spark().data["catalog_privileges"])
    rows = au.refresh_authority(sp, "m", "mig", ["c.s.t", "c.s.t"], source=au.SOURCE_PROFILER)
    assert [r["full_name"] for r in rows] == ["c.s.t"]
    assert any(q.startswith("MERGE INTO m.mig.convert_authority") for q in sp.queries)
    assert sp.created and sp.created[0][0]["source"] == "00_profiler"


def test_refresh_never_raises_when_persist_fails(monkeypatch):
    _patch_struct(monkeypatch)
    logs = []
    sp = _PersistSpark(fail_write=True, tables=_tier2_spark().data["tables"])
    rows = au.refresh_authority(sp, "m", "mig", ["c.s.t"], source=au.SOURCE_CONVERT, logger=logs.append)
    assert rows and rows[0]["full_name"] == "c.s.t"
    assert any("could not persist" in m for m in logs)


def test_refresh_with_no_tables_reads_nothing():
    sp = _FakeSpark()
    assert au.refresh_authority(sp, "m", "mig", [], source=au.SOURCE_PROFILER) == []
    assert sp.queries == []


# --------------------------------------------------------------------------- dry-run Check 4


def _row(status, **kw):
    v = {"actor": "migration-sp", "full_name": "c.s.t", "status": status, "missing": [], "grant_requests": []}
    v.update(kw)
    v.setdefault("reason", au.authority_text(v))
    return au.authority_row(v, source=au.SOURCE_DRY_RUN, evaluated_at=None)


def test_dry_run_check_is_info_when_authorized_and_never_fail():
    status, detail = au.dry_run_check(_row(au.STATUS_AUTHORIZED, basis="TABLE_OWNER"))
    assert status == "INFO"
    assert "can convert this table" in detail
    for s in au.ALL_STATUSES:
        assert au.dry_run_check(_row(s))[0] in ("INFO", "WARN")


def test_dry_run_check_warns_with_each_statement_and_who_to_ask():
    req = {"statement": "GRANT USE SCHEMA ON SCHEMA c.s TO `migration-sp`", "securable_type": "SCHEMA",
           "securable_name": "c.s", "privilege": "USE SCHEMA", "self_grantable": False,
           "grantors": ["data-owner@example.com"], "grantor_hint": None,
           "ask_text": "Ask `data-owner@example.com` (owner of schema `c.s`)"}
    status, detail = au.dry_run_check(_row(au.STATUS_NEEDS_GRANT, missing=["USE_SCHEMA"], grant_requests=[req]))
    assert status == "WARN"
    assert "GRANT USE SCHEMA ON SCHEMA c.s TO `migration-sp`;" in detail
    assert "Ask `data-owner@example.com`" in detail


def test_dry_run_check_warns_when_not_evaluated():
    status, detail = au.dry_run_check(None)
    assert status == "WARN" and "not evaluated" in detail
    assert au.CHECK_NUMBER == 4 and au.CHECK_NAME == "convert_authority"


# --------------------------------------------------------------------------- conductor / cockpit edge


class _WaveSpark(_PersistSpark):
    """Adds the plan-member read and the stored-verdict read."""

    def __init__(self, *, members=(), stored=(), fail_members=False, fail_stored=False, **kw):
        super().__init__(**kw)
        self.members, self.stored = list(members), list(stored)
        self.fail_members, self.fail_stored = fail_members, fail_stored

    def sql(self, q):
        if q.startswith("SELECT p.wave_id"):
            self.queries.append(q)
            if self.fail_members:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND wave_plan")
            return _DF(self.members)
        if q.startswith("SELECT * FROM m.mig.convert_authority"):
            self.queries.append(q)
            if self.fail_stored:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND convert_authority")
            return _DF(self.stored)
        return super().sql(q)


def _tier2_kw():
    d = _tier2_spark().data
    return dict(tables=d["tables"], schemata=d["schemata"], catalogs=d["catalogs"],
                sprivs=d["schema_privileges"], cprivs=d["catalog_privileges"])


def test_apply_authority_counts_only_tables_still_to_convert():
    waves = [{"wave_id": "w1", "open_lease": 9}, {"wave_id": "w2", "open_lease": 9}, {"wave_id": "w3"}]
    members = [
        {"wave_id": "w1", "full_name": "c.s.a", "state": "DRY_RUN_OK"},
        {"wave_id": "w1", "full_name": "c.s.b", "state": "DRY_RUN_OK"},
        {"wave_id": "w1", "full_name": "c.s.c", "state": "CONVERTED"},   # migrated: never counted
        {"wave_id": "w1", "full_name": "c.s.d", "state": "DROPPED"},     # gone: never counted
        {"wave_id": "w2", "full_name": "c.s.e", "state": "ROLLED_BACK"},  # will convert again
    ]
    by_fn = {"c.s.a": {"status": au.STATUS_AUTHORIZED}, "c.s.b": {"status": au.STATUS_NEEDS_GRANT},
             "c.s.c": {"status": au.STATUS_UNKNOWN}}
    au.apply_authority_to_waves(waves, members, by_fn)
    assert [(w["lease_total"], w["open_lease"], w["authority_unevaluated"]) for w in waves] == [
        (2, 1, 0), (1, 1, 1), (0, 0, 0)]


def test_authority_for_rereads_when_the_caller_is_the_convert_identity(monkeypatch):
    _patch_struct(monkeypatch)
    sp = _WaveSpark(**_tier2_kw())
    by_fn, note = au.authority_for(sp, "m", "mig", ["c.s.t"], source=au.SOURCE_CONDUCTOR, convert_identity="SP")
    assert by_fn["c.s.t"]["status"] == au.STATUS_SELF_AUTHORIZABLE
    assert any(q.startswith("MERGE INTO m.mig.convert_authority") for q in sp.queries)
    assert "re-read as `sp`" in note


def test_authority_for_uses_stored_verdicts_for_another_identity_and_never_writes(monkeypatch):
    _patch_struct(monkeypatch)
    stored = [{"full_name": "c.s.t", "actor": "convert-sp", "status": au.STATUS_AUTHORIZED}]
    sp = _WaveSpark(stored=stored, **_tier2_kw())
    by_fn, note = au.authority_for(sp, "m", "mig", ["c.s.t", "c.s.u"], source=au.SOURCE_BULK_COCKPIT,
                                   convert_identity="convert-sp")
    assert by_fn == {"c.s.t": stored[0]}
    assert not any(q.startswith(("MERGE INTO", "CREATE TABLE")) for q in sp.queries)
    assert not any("information_schema" in q for q in sp.queries)
    assert "lower(actor) = lower('convert-sp')" in next(q for q in sp.queries if "convert_authority" in q)
    assert "1 of 2 evaluated" in note


def test_authority_for_degrades_to_unevaluated_when_the_store_is_unreadable():
    sp = _WaveSpark(fail_stored=True, **_tier2_kw())
    by_fn, note = au.authority_for(sp, "m", "mig", ["c.s.t"], source=au.SOURCE_BULK_COCKPIT,
                                   convert_identity="convert-sp")
    assert by_fn == {} and "not evaluated" in note


def test_refresh_never_persists_an_actor_mismatch(monkeypatch):
    _patch_struct(monkeypatch)
    logs = []
    sp = _WaveSpark(**_tier2_kw())
    rows = au.refresh_authority(sp, "m", "mig", ["c.s.t"], source=au.SOURCE_CONDUCTOR,
                                expected_actor="convert-sp", logger=logs.append)
    assert rows[0]["status"] == au.STATUS_UNKNOWN
    assert not any(q.startswith("MERGE INTO") for q in sp.queries)
    assert any("not persisted" in m for m in logs)


def test_refresh_waves_sets_open_lease_from_fresh_verdicts(monkeypatch):
    _patch_struct(monkeypatch)
    members = [{"wave_id": "w1", "full_name": "c.s.t", "state": "DRY_RUN_OK"},
               {"wave_id": "w1", "full_name": "c.s.done", "state": "VALIDATED"}]
    sp = _WaveSpark(members=members, **_tier2_kw())
    waves = [{"wave_id": "w1", "open_lease": 0}]
    note = au.refresh_waves(sp, "m", "mig", waves, wave_plan_fqn="m.mig.wave_plan",
                            manifest_fqn="m.mig.table_manifest", plan_run_id="p'1",
                            source=au.SOURCE_CONDUCTOR)
    assert waves[0]["open_lease"] == 1 and waves[0]["lease_total"] == 1
    assert "SELF_AUTHORIZABLE': 1" in note
    member_q = next(q for q in sp.queries if q.startswith("SELECT p.wave_id"))
    assert "p.plan_run_id = 'p''1'" in member_q
    table_q = next(q for q in sp.queries if "information_schema.tables " in q)
    assert "c.s.done" not in table_q  # migrated tables are never re-read


def test_refresh_waves_leaves_the_view_count_when_members_are_unreadable():
    sp = _WaveSpark(fail_members=True)
    waves = [{"wave_id": "w1", "open_lease": 3}]
    note = au.refresh_waves(sp, "m", "mig", waves, wave_plan_fqn="m.mig.wave_plan",
                            manifest_fqn="m.mig.table_manifest", plan_run_id="p1",
                            source=au.SOURCE_CONDUCTOR)
    assert waves == [{"wave_id": "w1", "open_lease": 3}]
    assert "as the view computed it" in note


# --------------------------------------------------------------------------- 04_convert gate


def test_gate_blocks_every_table_that_is_not_authorized_and_prints_who_to_ask():
    req = {"statement": "GRANT MODIFY ON TABLE c.s.b TO `migration-sp`", "ask_text": "Authorize can do this"}
    rows = [_row(au.STATUS_AUTHORIZED, full_name="c.s.a", basis="TABLE_OWNER"),
            _row(au.STATUS_SELF_AUTHORIZABLE, full_name="c.s.b", missing=["MODIFY"],
                 grant_sql=["GRANT MODIFY ON TABLE c.s.b TO `migration-sp`"], grant_requests=[req]),
            _row(au.STATUS_UNKNOWN, full_name="c.s.c")]
    gate, gaps = au.build_gate_row(rows, evaluated_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
                                   plan_run_id="p1", wave_id="w1", group_key="c.s", source="04_convert")
    assert [g["full_name"] for g in gaps] == ["c.s.b", "c.s.c"]
    assert gate["gate_kind"] == "OWNERSHIP_COVERAGE" and gate["passed"] is False and gate["item_count"] == 2
    assert "GRANT MODIFY ON TABLE c.s.b TO `migration-sp`;  -- Authorize can do this" in gate["reason"]
    assert gaps[0]["remediation_sql"] == "GRANT MODIFY ON TABLE c.s.b TO `migration-sp`;"
    assert gaps[1]["remediation_sql"] is None and "job_03d_lease_ownership" in gaps[1]["remediation_api"]


def test_gate_passes_only_when_every_table_is_authorized():
    rows = [_row(au.STATUS_AUTHORIZED, full_name="c.s.a", basis="SCHEMA_OWNER")]
    gate, gaps = au.build_gate_row(rows, evaluated_at=None, plan_run_id="p1", wave_id="w1",
                                   group_key="c.s", source="04_convert")
    assert gaps == [] and gate["passed"] is True and gate["reason"] == au.GATE_CLEAN_REASON
