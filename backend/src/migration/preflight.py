"""Privilege preflight — classify the profiler identity's tiered least-privilege coverage
and print the exact GRANTs to close any gaps (pure logic + thin Spark edges).

A profiler run only sees "what this `run_as` identity, in this workspace, may access". Its
passes have DIFFERENT privilege needs (the design-delta "Privilege contract (tiered)" table):

  Discovery       information_schema.tables          BROWSE per catalog (auto-filtered)
  Enrichment      DESCRIBE DETAIL / DESCRIBE HISTORY USE CATALOG+USE SCHEMA+SELECT, or
                                                     READ METADATA at the metastore*
  System tables   system.access/compute/lakeflow.*  USE CATALOG system + explicit SELECT
  Workspace reach all of the above                  run IN each workspace with bound catalogs

This module turns cheap boolean PROBE results into a per-tier satisfied/gap verdict
(``classify_tiers``) and renders the EXACT ``GRANT`` statements for the gaps
(``grant_statements_for_gaps`` / ``broad_grant_alternatives``) + a printable report
(``render_preflight_report``). It NEVER executes a GRANT — it only prints them for an admin.

Pure and unit-testable: no Spark import at module load. The ``probe_*`` / ``collect_probes``
edges at the bottom use local ``spark.sql`` in try/except and degrade honestly (a denial or
missing object -> ``False``, never a crash). (*Enrichment via READ METADATA is flagged
"validate empirically" in the design-delta; see the operations doc.)
"""

from __future__ import annotations

from typing import List, Optional

# --------------------------------------------------------------------------- #
# Tier identifiers (the design-delta privilege table rows)
# --------------------------------------------------------------------------- #
TIER_DISCOVERY = "discovery"
TIER_ENRICHMENT = "enrichment"
TIER_SYSTEM_TABLES = "system_tables"
TIER_WORKSPACE_REACH = "workspace_reach"  # advisory — cannot be proven from one run

# --------------------------------------------------------------------------- #
# Probe contract — the cheap booleans classify_tiers consumes. Single source of
# truth for both the classifier and the collect_probes edge. Default is honest:
# nothing is proven until a probe succeeds.
# --------------------------------------------------------------------------- #
PROBE_KEYS: tuple = (
    "can_list_catalogs",
    "can_read_system_information_schema",
    "can_read_catalog_information_schema",
    "can_describe_detail",
    "can_describe_history",
    "can_read_system_lineage",
    "can_read_system_compute",
    "can_read_system_lakeflow",
)


def default_probes() -> dict:
    """All probes False (pure). Merge real probe results over this to stay total."""
    return {k: False for k in PROBE_KEYS}


# Each tier maps to a tuple of GROUPS; a group is a tuple of probe names. A group is
# satisfied if ANY probe in it is True; a tier is satisfied if ALL its groups are.
# Discovery = one any-of group (system OR per-catalog information_schema). Enrichment =
# two single-probe groups (both DESCRIBE reads required). System tables = three single-
# probe groups (lineage AND compute AND lakeflow). Workspace reach is advisory (below).
TIER_REQUIRED_PROBES: dict = {
    TIER_DISCOVERY: (
        ("can_read_system_information_schema", "can_read_catalog_information_schema"),
    ),
    TIER_ENRICHMENT: (
        ("can_describe_detail",),
        ("can_describe_history",),
    ),
    TIER_SYSTEM_TABLES: (
        ("can_read_system_lineage",),
        ("can_read_system_compute",),
        ("can_read_system_lakeflow",),
    ),
}


def classify_tiers(probe: Optional[dict]) -> dict:
    """Classify each privilege tier from probe results (pure, total).

    Returns ``{tier: {"satisfied": bool|None, "missing_probes": [...], "advisory": bool}}``.
    Missing/unknown probe keys are treated as ``False`` (honest: not proven). Workspace
    reach is ADVISORY — a single run cannot prove it reaches every bound catalog in every
    workspace, so it is always ``satisfied=None`` and emits no gap grants (bindings
    override grants; see ``BINDINGS_CAVEAT``).
    """
    p = default_probes()
    p.update(probe or {})
    out: dict = {}
    for tier, groups in TIER_REQUIRED_PROBES.items():
        missing: List[str] = []
        satisfied = True
        for group in groups:
            if not any(bool(p.get(name)) for name in group):
                satisfied = False
                missing.extend(group)  # report every alternative in an unmet any-of group
        out[tier] = {"satisfied": satisfied, "missing_probes": missing, "advisory": False}
    out[TIER_WORKSPACE_REACH] = {"satisfied": None, "missing_probes": [], "advisory": True}
    return out


# --------------------------------------------------------------------------- #
# GRANT rendering (pure) — exact Databricks SQL for the gaps. NEVER executed here;
# the profiler edge only PRINTS these for an admin to review and run.
# --------------------------------------------------------------------------- #

# The system schemas the profiler's enrichment/consumer passes read. system.access
# (lineage/audit), system.compute (clusters), system.lakeflow (job run timeline). These
# do NOT auto-filter by privilege, so each needs an explicit SELECT (plus USE CATALOG
# system). system.information_schema is covered by the Discovery tier, not here.
DEFAULT_SYSTEM_SCHEMAS: tuple = ("access", "compute", "lakeflow")

OPERATIONS_DOC = "backend/docs/operations/privileges.md"

BINDINGS_CAVEAT = (
    "Workspace-catalog bindings OVERRIDE grants: a catalog not bound to this workspace stays "
    "invisible no matter what is granted. Workspace reach can only be satisfied by running the "
    "profiler in each workspace that owns the bound catalogs (D3/D4) — it cannot be granted around."
)

TIER_LABELS = {
    TIER_DISCOVERY: "Discovery (information_schema.tables)",
    TIER_ENRICHMENT: "Enrichment (DESCRIBE DETAIL / HISTORY)",
    TIER_SYSTEM_TABLES: "System tables (access / compute / lakeflow)",
    TIER_WORKSPACE_REACH: "Workspace reach (bound catalogs)",
}


def _bt(ident: str) -> str:
    """Backtick-quote a single identifier (doubling embedded backticks). Principals with
    special chars (SP UUIDs, group names with spaces) MUST be quoted; a bare name is a
    syntax error and a display name would silently grant nothing (mirrors grants.py)."""
    return "`" + str(ident).replace("`", "``") + "`"


def grant_statement(privilege: str, securable_type: str, securable_fqn: str, principal: str) -> str:
    """One additive GRANT. ``securable_fqn`` is already-quoted (or a ``<placeholder>``), and
    is EMPTY for ``METASTORE`` (which takes no securable name)."""
    target = f" {securable_fqn}" if securable_fqn else ""
    return f"GRANT {privilege} ON {securable_type}{target} TO {_bt(principal)}"


def _is_gap(tiers: dict, tier: str) -> bool:
    t = (tiers or {}).get(tier) or {}
    return t.get("satisfied") is False and not t.get("advisory")


def grant_statements_for_gaps(
    *,
    tiers: dict,
    principal: str,
    catalogs: Optional[List[str]] = None,
    system_schemas: Optional[tuple] = None,
) -> List[str]:
    """Render the EXACT ``GRANT`` statements to close each GAPPED tier (pure).

    - Discovery gap -> ``BROWSE`` per catalog (or a ``<catalog>`` placeholder when the
      catalog list is unknown). ``BROWSE`` is catalog-level only and auto-filters
      information_schema without USE CATALOG/USE SCHEMA/SELECT.
    - Enrichment gap -> granular least-privilege ``USE CATALOG`` + ``USE SCHEMA`` + ``SELECT``
      per catalog (all inherit to child schemas/tables). When catalogs are unknown, fall
      back to the metastore-wide ``READ METADATA ON METASTORE`` (inherits to every object;
      "validate empirically" per the design-delta).
    - System-tables gap -> ``USE CATALOG ON CATALOG system`` + one ``SELECT`` per system
      schema (these do NOT auto-filter).
    - Workspace reach is advisory and NEVER emits a grant (bindings can't be granted around).
    """
    cats = list(catalogs or [])
    sys_sch = tuple(system_schemas) if system_schemas is not None else DEFAULT_SYSTEM_SCHEMAS
    out: List[str] = []

    if _is_gap(tiers, TIER_DISCOVERY):
        if cats:
            for c in cats:
                out.append(grant_statement("BROWSE", "CATALOG", _bt(c), principal))
        else:
            out.append(grant_statement("BROWSE", "CATALOG", "<catalog>", principal))

    if _is_gap(tiers, TIER_ENRICHMENT):
        if cats:
            for c in cats:
                out.append(grant_statement("USE CATALOG", "CATALOG", _bt(c), principal))
                out.append(grant_statement("USE SCHEMA", "CATALOG", _bt(c), principal))
                out.append(grant_statement("SELECT", "CATALOG", _bt(c), principal))
        else:
            out.append(grant_statement("READ METADATA", "METASTORE", "", principal))

    if _is_gap(tiers, TIER_SYSTEM_TABLES):
        out.append(grant_statement("USE CATALOG", "CATALOG", _bt("system"), principal))
        for s in sys_sch:
            out.append(grant_statement("SELECT", "SCHEMA", f"{_bt('system')}.{_bt(s)}", principal))

    return out


def broad_grant_alternatives(principal: str) -> List[str]:
    """The estate-wide shortcuts recommended for the single-profiler-SP setup (pure).

    Databricks recommends ``BROWSE`` to ``All account users`` for org-wide discovery, and
    a single metastore-wide ``READ METADATA`` covers enrichment everywhere (broad + sensitive
    — grant only to a trusted profiler SP; "validate empirically"). Surfaced in the report as
    an alternative to the per-catalog least-privilege grants above.
    """
    return [
        grant_statement("BROWSE", "CATALOG", "<catalog>", "All account users"),
        grant_statement("READ METADATA", "METASTORE", "", principal),
    ]


def render_preflight_report(tiers: dict, gap_statements: List[str]) -> str:
    """Render a printable preflight report (pure): per-tier OK/GAP/ADVISORY, the exact GRANTs
    to run for the gaps, the bindings caveat, and a pointer to the operations doc."""
    lines = ["=== Privilege preflight (this identity, this workspace) ==="]
    for tier in (TIER_DISCOVERY, TIER_ENRICHMENT, TIER_SYSTEM_TABLES, TIER_WORKSPACE_REACH):
        t = (tiers or {}).get(tier) or {}
        if t.get("advisory"):
            mark = "ADVISORY"
        elif t.get("satisfied") is True:
            mark = "OK"
        else:
            mark = "GAP"
        line = f"  [{mark:8s}] {TIER_LABELS.get(tier, tier)}"
        miss = t.get("missing_probes") or []
        if miss:
            line += f" -- missing: {', '.join(miss)}"
        lines.append(line)

    lines.append("")
    if gap_statements:
        lines.append("Run these GRANT statements to close the gaps (review before running; "
                     "the preflight NEVER executes them):")
        lines.extend(f"  {s}" for s in gap_statements)
    else:
        lines.append("All required tiers are satisfied for this identity in this workspace.")

    lines.append("")
    lines.append(f"NOTE: {BINDINGS_CAVEAT}")
    lines.append(f"See {OPERATIONS_DOC} for the full least-privilege recipe and run_as setup.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Spark edges (thin, best-effort) — local imports keep the pure logic importable
# without pyspark. Each probe returns a boolean; a denial / missing object / older
# runtime -> False (degrade honestly, never crash).
# --------------------------------------------------------------------------- #

def _probe(spark, sql: str) -> bool:
    try:
        spark.sql(sql).collect()
        return True
    except Exception:  # noqa: BLE001 — probes are best-effort; failure means "not proven"
        return False


def probe_list_catalogs(spark) -> bool:
    return _probe(spark, "SHOW CATALOGS")


def probe_system_information_schema(spark) -> bool:
    return _probe(spark, "SELECT 1 FROM system.information_schema.tables LIMIT 1")


def probe_catalog_information_schema(spark, catalog: str) -> bool:
    if not catalog:
        return False
    return _probe(spark, f"SELECT 1 FROM {_bt(catalog)}.information_schema.tables LIMIT 1")


def probe_describe_detail(spark, full_name_bt: str) -> bool:
    if not full_name_bt:
        return False
    return _probe(spark, f"DESCRIBE DETAIL {full_name_bt}")


def probe_describe_history(spark, full_name_bt: str) -> bool:
    if not full_name_bt:
        return False
    return _probe(spark, f"DESCRIBE HISTORY {full_name_bt} LIMIT 1")


def probe_system_lineage(spark) -> bool:
    return _probe(spark, "SELECT 1 FROM system.access.table_lineage LIMIT 1")


def probe_system_compute(spark) -> bool:
    return _probe(spark, "SELECT 1 FROM system.compute.clusters LIMIT 1")


def probe_system_lakeflow(spark) -> bool:
    return _probe(spark, "SELECT 1 FROM system.lakeflow.job_task_run_timeline LIMIT 1")


def collect_probes(spark, *, sample_full_name: Optional[str] = None,
                   sample_catalog: Optional[str] = None) -> dict:
    """Run every cheap probe and return the ``PROBE_KEYS`` dict for ``classify_tiers`` (edge).

    ``sample_full_name`` (already backticked, e.g. from ``manifest.make_full_name``) and
    ``sample_catalog`` let the enrichment/per-catalog probes test a REAL object the identity
    should be able to reach; when absent, those probes stay ``False`` (honest: not proven).
    """
    probes = default_probes()
    probes["can_list_catalogs"] = probe_list_catalogs(spark)
    probes["can_read_system_information_schema"] = probe_system_information_schema(spark)
    if sample_catalog:
        probes["can_read_catalog_information_schema"] = probe_catalog_information_schema(
            spark, sample_catalog)
    if sample_full_name:
        probes["can_describe_detail"] = probe_describe_detail(spark, sample_full_name)
        probes["can_describe_history"] = probe_describe_history(spark, sample_full_name)
    probes["can_read_system_lineage"] = probe_system_lineage(spark)
    probes["can_read_system_compute"] = probe_system_compute(spark)
    probes["can_read_system_lakeflow"] = probe_system_lakeflow(spark)
    return probes
