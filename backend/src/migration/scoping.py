"""Scope-predicate helpers for control-plane DETAIL tables.

Detail tables (``streaming_plan``, ``compaction_sources``, ...) are keyed by ``full_name``.
Some of them carry ``catalog``/``schema`` columns and some (notably ``streaming_plan``) do
NOT — so the manifest-oriented ``catalog = ... AND schema = ...`` predicate must NEVER be
applied to a detail read. On a column-less table that predicate throws, the caller's
best-effort ``except`` swallows it, and every row is silently dropped (this is exactly the
bug that made ``03b`` lose all streaming actions when it ran with a schema-scoped predicate).

These helpers scope detail reads by ``full_name`` membership only, which is correct for
EVERY detail table. ``full_name_in_predicate`` is pure; ``read_detail_by_full_name`` is the
single thin Spark edge (unit-testable with a minimal fake ``spark``).
"""

from __future__ import annotations

from typing import Iterable, Optional


def _q(v) -> str:
    """SQL single-quote escape."""
    return str(v).replace("'", "''")


def full_name_in_predicate(names: Optional[Iterable[str]]) -> str:
    """Build a ``full_name IN (...)`` SQL predicate from ``names`` (empty/None -> ``FALSE``).

    Single quotes are escaped. ``FALSE`` (not ``TRUE``) is the empty default so an empty
    scope reads nothing rather than the entire table.
    """
    cleaned = [n for n in (names or []) if n]
    if not cleaned:
        return "FALSE"
    quoted = ", ".join("'" + _q(n) + "'" for n in cleaned)
    return f"full_name IN ({quoted})"


def wave_member_predicate(
    full_names: Optional[Iterable[str]],
    wave_schemas: Optional[Iterable[Iterable[str]]],
) -> str:
    """Select a wave's rows by MEMBER identity, for control tables that carry
    ``scope``/``catalog``/``schema``/``full_name`` (``migration_actions``, ``freeze_ledger``).

    Wave membership can NOT be matched on ``plan_run_id``/``wave_id``: those tables are keyed
    by table/schema and generated before (and independently of) the wave plan, so their
    ``plan_run_id``/``wave_id`` are NULL and ``plan_run_id = X AND wave_id = Y`` matches NOTHING
    (NULL comparison) — the silent-no-op bug. Instead a row belongs to a wave iff:

      * it is TABLE-scoped and its ``full_name`` is a wave member (``full_name`` is NULL on
        SCHEMA-scoped rows, so this clause never catches them), OR
      * it is SCHEMA-scoped (schema-wide Predictive Optimization) and its ``(catalog, schema)``
        contains a wave member.

    ``wave_schemas`` is an iterable of ``(catalog, schema)`` pairs. Empty scope -> ``FALSE``
    (reads nothing rather than the whole table). Single quotes are escaped.
    """
    names = [n for n in (full_names or []) if n]
    pairs = [
        (parts[0], parts[1])
        for parts in (tuple(s) for s in (wave_schemas or []))
        if len(parts) >= 2 and parts[0] and parts[1]
    ]
    clauses: list = []
    if names:
        quoted = ", ".join("'" + _q(n) + "'" for n in names)
        clauses.append(f"(full_name IN ({quoted}))")
    if pairs:
        schema_ors = " OR ".join(
            f"(catalog = '{_q(c)}' AND schema = '{_q(s)}')" for c, s in pairs
        )
        # 'SCHEMA' is the stored scope value in both migration_actions and freeze_ledger.
        clauses.append(f"(scope = 'SCHEMA' AND ({schema_ors}))")
    if not clauses:
        return "FALSE"
    return "(" + " OR ".join(clauses) + ")"


def read_wave_members(spark, manifest_catalog: str, manifest_schema: str,
                      plan_run_id: str, wave_id: str):
    """Resolve a wave's MEMBERS from ``wave_plan`` (the single thin Spark edge for it).

    Returns ``(member_full_names, wave_schemas)`` where ``wave_schemas`` is a sorted list of
    ``(catalog, schema)`` tuples. ``wave_plan`` is the plan, so it DOES carry
    ``plan_run_id``/``wave_id`` — scoping the read by them here is correct (unlike the
    action/ledger tables). Raises ``ValueError`` if the selector matches no wave_plan rows
    (drift / typo): a per-wave stage must never silently no-op.
    """
    fqn = f"{manifest_catalog}.{manifest_schema}.wave_plan"
    pred = f"plan_run_id = '{_q(plan_run_id)}' AND wave_id = '{_q(wave_id)}'"
    rows = [
        r.asDict()
        for r in spark.table(fqn).where(pred).select("full_name", "catalog", "schema").collect()
    ]
    if not rows:
        raise ValueError(
            f"no wave_plan members for plan={plan_run_id} wave={wave_id} — plan drift or a "
            "typo'd selector. Re-run the planner + selector."
        )
    member_full_names = [r["full_name"] for r in rows]
    wave_schemas = sorted({(r["catalog"], r["schema"]) for r in rows})
    return member_full_names, wave_schemas


def read_batch_members(spark, manifest_catalog: str, manifest_schema: str,
                       plan_run_id: str, batch_id: str):
    """Resolve a bulk batch's MEMBERS: ``wave_parallel_groups`` -> wave ids -> ``wave_plan``.

    Returns ``(member_full_names, batch_schemas, wave_by_full_name)``: ``batch_schemas`` is a
    sorted list of ``(catalog, schema)`` tuples, and ``wave_by_full_name`` maps each member to its
    own ``wave_id`` (the finish stages stamp it on the rows they emit). Raises ``ValueError`` when
    the batch resolves no waves or no members (drift / typo): a batch stage must never no-op.
    """
    groups_fqn = f"{manifest_catalog}.{manifest_schema}.wave_parallel_groups"
    wave_ids = sorted({
        r.asDict()["wave_id"]
        for r in spark.table(groups_fqn)
        .where(f"plan_run_id = '{_q(plan_run_id)}' AND batch_id = '{_q(batch_id)}'")
        .select("wave_id").collect()
    })
    if not wave_ids:
        raise ValueError(
            f"no wave_parallel_groups waves for plan={plan_run_id} batch={batch_id} — plan drift "
            "or a typo'd batch_id. Re-run the planner, then pick a batch from the cockpit.")
    in_list = ", ".join(f"'{_q(w)}'" for w in wave_ids)
    fqn = f"{manifest_catalog}.{manifest_schema}.wave_plan"
    rows = [
        r.asDict()
        for r in spark.table(fqn)
        .where(f"plan_run_id = '{_q(plan_run_id)}' AND wave_id IN ({in_list})")
        .select("full_name", "catalog", "schema", "wave_id").collect()
    ]
    if not rows:
        raise ValueError(
            f"no wave_plan members for plan={plan_run_id} batch={batch_id} waves={wave_ids} — "
            "plan drift. Re-run the planner.")
    member_full_names = [r["full_name"] for r in rows]
    batch_schemas = sorted({(r["catalog"], r["schema"]) for r in rows})
    wave_by_full_name = {r["full_name"]: r["wave_id"] for r in rows}
    return member_full_names, batch_schemas, wave_by_full_name


def read_detail_by_full_name(spark, fqn: str, cols: list, full_names: Optional[Iterable[str]]) -> dict:
    """``full_name -> list[dict]`` for a detail table, scoped by ``full_name`` membership.

    Best-effort: a missing / unreadable table logs and yields ``{}`` (the caller then just
    generates no actions for that signal). This function NEVER applies a catalog/schema
    predicate — detail tables like ``streaming_plan`` do not have those columns.
    """
    out: dict = {}
    pred = full_name_in_predicate(full_names)
    try:
        rows = spark.table(fqn).where(pred).select("full_name", *cols).collect()
    except Exception as ex:  # noqa: BLE001
        print(f"  {fqn} unavailable for backlog ({str(ex)[:100]})")
        return out
    for r in rows:
        d = r.asDict()
        out.setdefault(d["full_name"], []).append(d)
    return out
