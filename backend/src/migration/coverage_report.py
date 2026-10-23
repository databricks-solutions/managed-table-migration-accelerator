"""Coverage self-diagnostic — turn the run ledger into an honest coverage answer (WS2).

Estate coverage is bounded by two Unity Catalog filters (per-principal privilege filtering
+ per-workspace catalog bindings), so a run only ever sees "what this identity, in this
workspace, may access". This module makes each run SAY that: it assembles a per-run
coverage report (identity + catalogs visible vs. scanned + per-catalog status + an explicit
disclaimer) for the run log, and owns the read-only ``coverage_report`` view over
``profiler_run_ledger`` (+ ``table_manifest``) that the dashboard/App read. Realizes
design-delta D6.

Pure and unit-testable: no Spark import at module load. The only Spark touch is the view
DDL string (a pure builder); the view itself is created by ``ensure_control_plane``.
"""

from __future__ import annotations

from typing import Optional

from . import run_ledger as rl

# Per-catalog discovery status. NOT_SCANNED is deliberately honest: "visible to this
# identity but never queried this run" -> we cannot claim we covered it.
CATALOG_SCANNED = "scanned"           # produced discovered rows this run
CATALOG_EMPTY = "empty"               # queried OK, no in-scope EXTERNAL tables
CATALOG_ERROR = "error"               # per-catalog information_schema read failed
CATALOG_NOT_SCANNED = "not_scanned"   # visible to the identity but never queried this run

# The standing disclaimer. Intentionally apostrophe-free: it is also embedded as a SQL
# string literal in the coverage_report view, where doubling quotes would be error-prone.
COVERAGE_DISCLAIMER = (
    "Coverage is privilege- and workspace-binding-filtered: this is what this identity, "
    "in this workspace, could see. Absence of a table is not proof it does not exist."
)


def classify_catalogs(
    *,
    visible: Optional[list],
    scanned: list,
    errored: Optional[list] = None,
    empty: Optional[list] = None,
) -> dict:
    """Map each catalog -> a status constant (pure).

    ``scanned`` (produced rows) and ``errored`` are authoritative; a ``visible`` catalog
    that is neither scanned, errored, nor explicitly ``empty`` is NOT_SCANNED (honest: we
    cannot claim coverage of it). ``visible`` may be ``None`` when ``SHOW CATALOGS`` failed
    — then only scanned/errored/empty catalogs are classified. Error wins over empty/
    not-scanned for the same catalog.
    """
    scanned_s = set(scanned or [])
    empty_s = set(empty or [])
    errored_s = set(errored or [])
    out: dict = {}
    for c in scanned_s:
        out[c] = CATALOG_SCANNED
    for c in empty_s:
        out.setdefault(c, CATALOG_EMPTY)
    for c in errored_s:
        out[c] = CATALOG_ERROR
    for c in (visible or []):
        out.setdefault(c, CATALOG_NOT_SCANNED)
    return out


def build_coverage_report(
    *, run_ledger_row: dict, per_catalog_status: Optional[dict] = None
) -> dict:
    """Assemble the per-run coverage report (pure).

    Reads counters straight off the ledger row so the printed report and the persisted
    ledger never disagree. ``catalogs_unscanned`` is the honest gap (visible but not
    covered) or ``None`` when either counter is unknown; ``per_catalog_counts`` summarizes
    the status map when supplied.
    """
    r = run_ledger_row
    visible = r.get("catalogs_visible")
    scanned = r.get("catalogs_scanned")
    unscanned = (visible - scanned) if (visible is not None and scanned is not None) else None

    status = dict(per_catalog_status or {})
    counts = {
        CATALOG_SCANNED: 0, CATALOG_EMPTY: 0, CATALOG_ERROR: 0, CATALOG_NOT_SCANNED: 0,
    }
    for st in status.values():
        if st in counts:
            counts[st] += 1

    return {
        "run_id": r.get("run_id"),
        "workspace_id": r.get("workspace_id"),
        "metastore_id": r.get("metastore_id"),
        "principal": r.get("principal"),
        "catalog_filter": r.get("catalog_filter"),
        "schema_filter": r.get("schema_filter"),
        "discovery_source": r.get("discovery_source"),
        "system_information_schema_ok": r.get("system_information_schema_ok"),
        "catalogs_visible": visible,
        "catalogs_scanned": scanned,
        "catalogs_unscanned": unscanned,
        "tables_discovered": r.get("tables_discovered"),
        "per_catalog_status": status,
        "per_catalog_counts": counts,
        "disclaimer": COVERAGE_DISCLAIMER,
    }


def render_coverage_report(report: dict) -> str:
    """Render the report as a printable block ENDING with the disclaimer (pure)."""
    counts = report.get("per_catalog_counts") or {}
    lines = [
        "=== Coverage report (this run) ===",
        f"  run_id      : {report.get('run_id')}",
        f"  workspace   : {report.get('workspace_id')}   metastore: {report.get('metastore_id')}",
        f"  principal   : {report.get('principal')}",
        f"  scope       : catalog_filter={report.get('catalog_filter')} "
        f"schema_filter={report.get('schema_filter')}",
        f"  source      : {report.get('discovery_source')} "
        f"(system_information_schema_ok={report.get('system_information_schema_ok')})",
        f"  catalogs    : visible={report.get('catalogs_visible')} "
        f"scanned={report.get('catalogs_scanned')} unscanned={report.get('catalogs_unscanned')}",
    ]
    if any(counts.values()):
        lines.append(
            f"  per-catalog : scanned={counts.get(CATALOG_SCANNED, 0)} "
            f"empty={counts.get(CATALOG_EMPTY, 0)} error={counts.get(CATALOG_ERROR, 0)} "
            f"not_scanned={counts.get(CATALOG_NOT_SCANNED, 0)}"
        )
    lines.append(f"  tables      : {report.get('tables_discovered')} discovered this run")
    lines.append(f"  NOTE: {report.get('disclaimer')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# coverage_report view — latest run per workspace + a manifest rollup (pure SQL builder)
# --------------------------------------------------------------------------- #
COVERAGE_REPORT_VIEW = "coverage_report"


def coverage_report_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{COVERAGE_REPORT_VIEW}"


def create_coverage_report_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """One row per workspace: its LATEST profiler run + a manifest rollup + the disclaimer.

    Latest run per workspace is picked by ``finished_at`` (ties broken by ``run_id``). The
    manifest rollup counts rows attributed to that workspace via
    ``discovered_in_workspace_id``, so the view answers "per workspace, what did the newest
    run see and how much is now in scope". Read-only; the App/dashboard surface it (WS6).
    """
    view = coverage_report_view_fqn(manifest_catalog, manifest_schema)
    ledger = rl.run_ledger_fqn(manifest_catalog, manifest_schema)
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH latest AS (
  SELECT *, ROW_NUMBER() OVER (
      PARTITION BY workspace_id ORDER BY finished_at DESC, run_id DESC
  ) AS rn
  FROM {ledger}
),
manifest_by_ws AS (
  SELECT discovered_in_workspace_id AS workspace_id,
         COUNT(*) AS manifest_tables,
         COUNT_IF(UPPER(state) = 'BLOCKED') AS manifest_blocked
  FROM {manifest}
  GROUP BY discovered_in_workspace_id
)
SELECT
  l.workspace_id, l.metastore_id, l.principal,
  l.run_id AS latest_run_id, l.started_at, l.finished_at,
  l.discovery_source, l.system_information_schema_ok,
  l.catalog_filter, l.schema_filter,
  l.catalogs_visible, l.catalogs_scanned,
  (l.catalogs_visible - l.catalogs_scanned) AS catalogs_unscanned,
  l.tables_discovered AS tables_discovered_latest_run,
  m.manifest_tables, m.manifest_blocked,
  '{COVERAGE_DISCLAIMER}' AS coverage_disclaimer
FROM latest l
LEFT JOIN manifest_by_ws m ON m.workspace_id = l.workspace_id
WHERE l.rn = 1"""
