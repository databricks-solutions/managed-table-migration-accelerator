"""Unit tests for the profiler run ledger + discovery-provenance contract. No Spark."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import manifest as mf  # noqa: E402
from migration import run_ledger as rl  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0)


def test_new_run_id_is_32_hex_and_unique():
    a, b = rl.new_run_id(), rl.new_run_id()
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)
    assert a != b


def test_resolve_discovery_source():
    assert rl.resolve_discovery_source(system_ok=True, used_fallback=False) == rl.SOURCE_SYSTEM
    assert rl.resolve_discovery_source(system_ok=False, used_fallback=True) == rl.SOURCE_FALLBACK
    assert rl.resolve_discovery_source(system_ok=True, used_fallback=True) == rl.SOURCE_MIXED


def test_provenance_columns_registered_and_split():
    # every provenance col is declared in the manifest
    for col in rl.PROVENANCE_COLUMN_NAMES:
        assert col in mf.MANIFEST_COLUMN_NAMES
    # exactly first_seen_at is write-once; the rest refresh each run
    assert set(rl.PROVENANCE_REFRESH_COLUMNS) | {"first_seen_at"} == set(rl.PROVENANCE_COLUMN_NAMES)
    assert "first_seen_at" in mf.INSERT_ONLY_COLUMNS
    for col in rl.PROVENANCE_REFRESH_COLUMNS:
        assert col not in mf.INSERT_ONLY_COLUMNS


def test_build_manifest_provenance_stamps_now_for_both_seen_times():
    prov = rl.build_manifest_provenance(
        run_id="r1", workspace_id="999", principal="me@x.com",
        discovery_source=rl.SOURCE_SYSTEM, now=NOW,
    )
    assert prov == {
        "discovery_run_id": "r1",
        "discovered_in_workspace_id": "999",
        "discovered_by_principal": "me@x.com",
        "discovery_source": rl.SOURCE_SYSTEM,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
    }


def test_build_run_ledger_row_has_every_column():
    row = rl.build_run_ledger_row(
        run_id="r1", workspace_id="999", metastore_id="m1", principal="me@x.com",
        catalog_filter="*", schema_filter="*", discovery_source=rl.SOURCE_SYSTEM,
        catalogs_visible=5, catalogs_scanned=4, tables_discovered=120,
        system_information_schema_ok=True, started_at=NOW, finished_at=NOW, notes=None,
    )
    assert set(row.keys()) == set(rl.RUN_LEDGER_COLUMN_NAMES)
    assert row["tables_discovered"] == 120
    assert row["system_information_schema_ok"] is True


def test_run_ledger_fqn():
    assert rl.run_ledger_fqn("main", "migration") == "main.migration.profiler_run_ledger"


# --- workspace-id resolution (deterministic, serverless-safe) -------------- #
class _FakeSpark:
    """Minimal spark stub: `SELECT current_workspace_id()` raises (unresolved on serverless),
    and conf keys come from a dict (missing key raises, like real spark.conf.get)."""

    def __init__(self, *, sql_wid=None, conf=None, sql_raises=True):
        self._sql_wid = sql_wid
        self._conf = conf or {}
        self._sql_raises = sql_raises

    def sql(self, _query):
        if self._sql_raises:
            raise RuntimeError("[UNRESOLVED_ROUTINE] current_workspace_id")

        class _R:
            def __init__(self, v):
                self._v = v

            def collect(self):
                return [[self._v]] if self._v is not None else []

        return _R(self._sql_wid)

    class _Conf:
        def __init__(self, d):
            self._d = d

        def get(self, k):
            if k not in self._d:
                raise RuntimeError(f"missing conf {k}")
            return self._d[k]

    @property
    def conf(self):
        return _FakeSpark._Conf(self._conf)


def test_resolve_workspace_id_prefers_explicit():
    # Explicit wins even when every runtime probe would also resolve.
    spark = _FakeSpark(sql_wid="runtime", conf={"spark.databricks.workspaceId": "conf"}, sql_raises=False)
    assert rl.resolve_workspace_id(spark, explicit="  1234567890123456 ") == "1234567890123456"


def test_resolve_workspace_id_blank_explicit_falls_through_to_conf():
    # Serverless: SQL routine unresolved -> use the serverless conf key.
    spark = _FakeSpark(conf={"spark.databricks.workspaceId": "999"})
    assert rl.resolve_workspace_id(spark, explicit="") == "999"
    assert rl.resolve_workspace_id(spark, explicit="   ") == "999"


def test_resolve_workspace_id_uses_sql_probe_when_available():
    spark = _FakeSpark(sql_wid="12345", sql_raises=False)
    assert rl.resolve_workspace_id(spark) == "12345"


def test_resolve_workspace_id_none_when_all_blank():
    # Serverless with no explicit + no conf keys -> the exact bug we hit -> None (caller WARNs).
    spark = _FakeSpark()
    assert rl.resolve_workspace_id(spark) is None


def test_current_workspace_id_delegates_runtime_only():
    spark = _FakeSpark(conf={"spark.databricks.clusterUsageTags.clusterOwnerOrgId": "42"})
    assert rl.current_workspace_id(spark) == "42"
    assert rl.resolve_workspace_id(_FakeSpark()) is None
