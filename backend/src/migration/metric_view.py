"""Genie metric view builder — governed measures over ``table_manifest`` (pure).

A Unity Catalog *metric view* gives the Genie Agent ONE consistent definition of
each migration KPI (table counts by eligibility/state, total size, estimated copy
minutes) instead of re-deriving ``GROUP BY`` aggregations per question — the main
source of subtly-inconsistent free-form answers. It is registered as a
``data_sources.metric_views`` source in the Genie space (see
``genie_space.build_space_dict``); querying it uses ``MEASURE(`<name>`)`` grouped
by dimensions (``SELECT *`` is not supported on a metric view).

This module is PURE (no Spark, no SDK): it builds the YAML body, the
fully-qualified name, and the ``CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE
YAML`` DDL string. The setup job (``99_setup``) runs the DDL as a best-effort thin
edge and only references the view in the space when the create SUCCEEDED.

Why 99_setup and not ``bootstrap.ensure_control_plane``: metric views require DBR
16.4+ (YAML v0.1) / 17.2+ (v1.1) or serverless. The profiler is a standalone
deliverable a customer can run against an older-runtime cluster, so it must never
be blocked by a metric-view create. Genie provisioning already lives in 99_setup
(which runs on the Genie warehouse / serverless), so the metric view is created
there and the space degrades gracefully (``include_metric_view=False``) if the
create fails.
"""

from __future__ import annotations

METRIC_VIEW = "migration_metrics"


def metric_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    """Fully-qualified name of the migration metric view."""
    return f"{manifest_catalog}.{manifest_schema}.{METRIC_VIEW}"


# Dimensions + measures over ``table_manifest``. Display names are backtick-quoted
# at query time; measures MUST be wrapped in ``MEASURE()``. Keep the names STABLE —
# they are part of the Genie contract (the space instructions reference them).
_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("Catalog", "catalog", "Unity Catalog catalog"),
    ("Schema", "schema", "Schema within the catalog"),
    ("Eligibility", "eligibility", "ELIGIBLE | BLOCKED | NEEDS_REVIEW"),
    ("Lifecycle State", "state", "DISCOVERED .. CONVERTED .. VALIDATED .. CLEANED"),
    ("Size Band", "size_band", "Coarse table-size bucket"),
)

# Conditional counts use the documented ``COUNT(1) FILTER (WHERE ...)`` pattern (not
# ``COUNT_IF``) — it is the canonical conditional-aggregation form in the metric-view
# re-aggregation semantic layer (see the create-metric-view docs, which use
# ``SUM(...) FILTER (WHERE ...)``). Plain scalars: valid unquoted YAML in block context.
_MEASURES: tuple[tuple[str, str, str | None], ...] = (
    ("Table Count", "COUNT(1)", "Number of tables in scope"),
    ("Eligible Tables", "COUNT(1) FILTER (WHERE eligibility = 'ELIGIBLE')", "Tables assessed ELIGIBLE"),
    ("Blocked Tables", "COUNT(1) FILTER (WHERE eligibility = 'BLOCKED')", "Tables assessed BLOCKED"),
    ("Needs Review Tables", "COUNT(1) FILTER (WHERE eligibility = 'NEEDS_REVIEW')", "Tables held for review"),
    ("Converted Tables", "COUNT(1) FILTER (WHERE state = 'CONVERTED')", "Tables in CONVERTED state"),
    ("Validated Tables", "COUNT(1) FILTER (WHERE state = 'VALIDATED')", "Tables in VALIDATED state"),
    ("Total Size Bytes", "SUM(table_size_bytes)", "Total on-disk size in bytes"),
    ("Estimated Copy Minutes", "SUM(est_data_copy_minutes)", "Sum of estimated data-copy minutes"),
)


def dimension_names() -> list:
    """Display names of the metric-view dimensions (for docs/tests)."""
    return [n for n, _, _ in _DIMENSIONS]


def measure_names() -> list:
    """Display names of the metric-view measures (queried via ``MEASURE(`name`)``)."""
    return [n for n, _, _ in _MEASURES]


def build_metric_view_yaml(manifest_catalog: str, manifest_schema: str) -> str:
    """Return the metric-view YAML body (v1.1) sourced from ``table_manifest``."""
    src = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    lines = [
        "version: 1.1",
        f"source: {src}",
        'comment: "Governed migration KPIs over table_manifest for consistent Genie answers"',
        "dimensions:",
    ]
    for name, expr, comment in _DIMENSIONS:
        lines.append(f"  - name: {name}")
        lines.append(f"    expr: {expr}")
        if comment:
            lines.append(f'    comment: "{comment}"')
    lines.append("measures:")
    for name, expr, comment in _MEASURES:
        lines.append(f"  - name: {name}")
        lines.append(f"    expr: {expr}")
        if comment:
            lines.append(f'    comment: "{comment}"')
    return "\n".join(lines) + "\n"


def create_metric_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Return the ``CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE YAML`` DDL."""
    fqn = metric_view_fqn(manifest_catalog, manifest_schema)
    yaml = build_metric_view_yaml(manifest_catalog, manifest_schema)
    return (
        f"CREATE OR REPLACE VIEW {fqn}\n"
        f"WITH METRICS\n"
        f"LANGUAGE YAML\n"
        f"AS $$\n{yaml}$$"
    )
