"""Downstream-consumer analysis, blast-radius scoring, and wave sequencing.

Turns the manifest into ordered migration WAVES plus a machine-readable comms feed.
Pure, unit-testable logic only; the notebook collects ``system.access.table_lineage``
events via Spark and passes plain Python dicts into these functions.

Lineage caveat (same as elsewhere): lineage is a *subset* of events — absence of a
consumer is not proof there is none. Owners/consumers are best-effort.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from . import eligibility as _elig
from . import external_writes as _ew
from . import ownership as _own
from . import comms as _comms

# --------------------------------------------------------------------------- #
# Consumer classification
# --------------------------------------------------------------------------- #

# entity_type -> coarse consumer "kind" (Databricks-native kinds; external handled below).
_ENTITY_KIND = {
    "JOB": "job",
    "WORKFLOW": "job",
    "PIPELINE": "pipeline",
    "DLT_PIPELINE": "pipeline",
    "LAKEFLOW_PIPELINE": "pipeline",
    "DASHBOARD": "dashboard",
    "DBSQL_DASHBOARD": "dashboard",
    "LEGACY_DASHBOARD": "dashboard",
    "QUERY": "query",
    "DBSQL_QUERY": "query",
    "SQL_QUERY": "query",
    "NOTEBOOK": "notebook",
    "MODEL": "model",
    "ALERT": "alert",
    "GENIE_SPACE": "genie",
}


def classify_consumer_kind(entity_type: Optional[str]) -> str:
    """Coarse kind for a lineage entity_type: external | job | pipeline | dashboard |
    query | notebook | model | ... | other."""
    e = (entity_type or "").strip().upper()
    if not e:
        return "other"
    if _ew.classify_entity_type(e) == "external":
        return "external"
    return _ENTITY_KIND.get(e, "other")


def downstream_summary(events: Optional[Iterable[dict]]) -> dict:
    """Aggregate READ-side lineage events for one table into a consumer summary.

    Each event dict may carry: entity_type, entity_id, created_by, downstream_table
    (the target_table_full_name produced from reading this table).

    Returns counts, owner set, external flag, and human labels for the comms feed.
    ``downstream_consumer_count`` = distinct downstream tables + distinct consumer
    entities (jobs/pipelines/dashboards/queries/notebooks/models/external/...).
    """
    events = list(events or [])
    downstream_tables: set[str] = set()
    consumers: set[tuple[str, str]] = set()   # (kind, id)
    owners: set[str] = set()
    external: set[str] = set()

    for ev in events:
        dt = ev.get("downstream_table")
        if dt:
            downstream_tables.add(str(dt))
        kind = classify_consumer_kind(ev.get("entity_type"))
        ident = ev.get("entity_id")
        ident = str(ident) if ident is not None else None
        if kind != "other" or ident:
            consumers.add((kind, ident or "?"))
        if kind == "external":
            external.add(ident or (ev.get("entity_type") or "external"))
        ob = ev.get("created_by")
        if ob:
            owners.add(str(ob))

    # Build human labels: "kind:id" for entities, plus downstream tables.
    labels = [f"{k}:{i}" for (k, i) in sorted(consumers)]
    labels += [f"table:{t}" for t in sorted(downstream_tables)]

    return {
        "downstream_tables": sorted(downstream_tables),
        "consumers": sorted(consumers),
        "owners": sorted(owners),
        "external_consumers": sorted(external),
        "has_external_consumer": bool(external),
        "downstream_consumer_count": len(downstream_tables) + len(consumers),
        "consumer_labels": labels,
    }


# --------------------------------------------------------------------------- #
# Blast-radius score
# --------------------------------------------------------------------------- #

# Tunable weights. Higher score => migrate later / with more coordination.
STREAMING_WEIGHT = 5.0
EXTERNAL_CONSUMER_WEIGHT = 5.0
CROSS_REGION_WEIGHT = 3.0


def blast_radius_score(
    downstream_consumer_count: int,
    has_streaming: bool = False,
    has_external_consumer: bool = False,
    cross_region_risk: bool = False,
) -> float:
    """blast_radius = downstream_consumer_count + weighted risk flags (per the spec)."""
    return (
        float(downstream_consumer_count or 0)
        + (STREAMING_WEIGHT if has_streaming else 0.0)
        + (EXTERNAL_CONSUMER_WEIGHT if has_external_consumer else 0.0)
        + (CROSS_REGION_WEIGHT if cross_region_risk else 0.0)
    )


# --------------------------------------------------------------------------- #
# Wave bucketing + sequencing
# --------------------------------------------------------------------------- #

BUCKET_PILOT = "pilot"
BUCKET_NORMAL = "normal"
BUCKET_STREAMING = "streaming"
BUCKET_COMPAT = "compat"
BUCKET_SHARE = "share"
BUCKET_FINAL_LARGE = "final_large"

# Canonical, ordered set of valid bucket values (used to validate a selector's `bucket`
# and to drive the 04_select_waves dropdown). Keep in sync with the constants above.
ALL_BUCKETS = (
    BUCKET_PILOT, BUCKET_NORMAL, BUCKET_STREAMING,
    BUCKET_COMPAT, BUCKET_SHARE, BUCKET_FINAL_LARGE,
)

# Precedence rank for the first-match bucketing in ``assign_bucket`` (lower =
# more specialized / earlier wave). Single source of truth for the "a live flag
# outranks this bucket" coherence rule; the SQL views mirror this ordering.
_BUCKET_RANK = {
    BUCKET_STREAMING: 1, BUCKET_COMPAT: 2, BUCKET_SHARE: 3,
    BUCKET_FINAL_LARGE: 4, BUCKET_PILOT: 5, BUCKET_NORMAL: 6,
}
_UNKNOWN_RANK = 99


def bucket_rank(bucket: Optional[str]) -> int:
    """Precedence rank (lower = more specialized / earlier wave). Unknown sorts last."""
    return _BUCKET_RANK.get((bucket or "").strip().lower(), _UNKNOWN_RANK)


def live_flags_outrank_bucket(
    bucket: Optional[str],
    *,
    has_streaming_writer: bool,
    needs_compatibility_mode: bool,
    is_delta_shared: bool,
) -> bool:
    """True when a LIVE manifest special flag maps to a bucket of strictly higher
    precedence than the wave's planned ``bucket`` — i.e. re-planning would move this
    table to a more-specialized wave. This is the exact "badge contradicts bucket"
    condition the Waves board must never render silently.

    Uses the NARROW manifest signals the UI actually shows (``has_streaming_writer``),
    not the planner's broader ``wave_plan.has_streaming`` snapshot. ``has_path_based_access``
    is intentionally excluded: it is not a bucketing input, so pilot+path is coherent.
    """
    cur = bucket_rank(bucket)
    live = _UNKNOWN_RANK
    if has_streaming_writer:
        live = min(live, _BUCKET_RANK[BUCKET_STREAMING])
    if needs_compatibility_mode:
        live = min(live, _BUCKET_RANK[BUCKET_COMPAT])
    if is_delta_shared:
        live = min(live, _BUCKET_RANK[BUCKET_SHARE])
    return live < cur

LARGE_BANDS = {"~10TB", ">10TB"}
SMALL_BAND = "<=100GB"
# A pilot table must have near-zero blast radius (tunable).
PILOT_MAX_SCORE = 2.0

# Wave-planner v2 defaults (all overridable via 02_wave_planner widgets).
PILOT_MAX_TABLES = 15                     # cap the pilot so it stays a true proof-of-tooling
WAVE_MAX_TABLES = 300                     # count cap per non-pilot wave (schema slice chunking)
WAVE_MAX_BYTES = 25 * (1024 ** 4)         # 25 TB byte cap per wave (chunk large schemas)
GRAIN_CATALOG_SCHEMA = "catalog_schema"
GRAIN_SCHEMA = "schema"
GRAIN_CATALOG = "catalog"

# Advisory scheduling guidance per bucket — a recommendation for WHEN to convert this class
# of wave, not a detected fact about any team's calendar. Rendered inline as the wave's
# "planned window" descriptor, so each reads as clear, self-explanatory advice.
_WINDOW_HINTS = {
    BUCKET_PILOT: "low blast radius — safe to convert during business hours",
    BUCKET_NORMAL: "routine tables — best converted during the owning team's off-peak hours",
    BUCKET_STREAMING: "has streaming — needs a coordinated window; restart every stream reader/writer immediately after convert",
    BUCKET_COMPAT: "has external (non-Databricks) readers — coordinated window; after convert move them to UC open APIs (Compatibility Mode only if they cannot)",
    BUCKET_SHARE: "Delta-shared — coordinated window; re-share to recipients after convert (not auto-updated)",
    BUCKET_FINAL_LARGE: "largest tables — schedule for a low-traffic window (overnight or weekend)",
}


def assign_bucket(
    *,
    size_band: Optional[str],
    blast_radius: float,
    has_streaming: bool,
    has_external_consumer: bool,
    is_delta_shared: bool,
    needs_compatibility_mode: bool,
) -> str:
    """Assign a sequencing bucket.

    Precedence (highest first): streaming tables are ALWAYS isolated into their own
    wave (they need a coordinated restart), then tables needing special coordination
    (Compatibility Mode, Delta-Sharing), then the largest tables, then the pilot
    (simple, small, low-blast-radius, no external consumer), else normal.
    """
    if has_streaming:
        return BUCKET_STREAMING
    if needs_compatibility_mode:
        return BUCKET_COMPAT
    if is_delta_shared:
        return BUCKET_SHARE
    if size_band in LARGE_BANDS:
        return BUCKET_FINAL_LARGE
    if (
        not has_external_consumer
        and size_band == SMALL_BAND
        and blast_radius <= PILOT_MAX_SCORE
    ):
        return BUCKET_PILOT
    return BUCKET_NORMAL


def wave_window_hint(bucket: str) -> str:
    return _WINDOW_HINTS.get(bucket, _WINDOW_HINTS[BUCKET_NORMAL])


# Buckets that are otherwise "routine" (no coordinated window planned). An ad-hoc writer on
# one of these is worth flagging for coordination; the specialized buckets already coordinate.
_ROUTINE_BUCKETS = {BUCKET_NORMAL, BUCKET_PILOT}


def needs_coordination(bucket: Optional[str], *, has_adhoc_writer: bool) -> bool:
    """True when an otherwise-routine wave carries an ad-hoc writer (notebook/user/external).

    Advisory only: ad-hoc writers cannot be paused (in-window writes fail and retry — no
    loss). This surfaces "coordinate an off-peak window" without changing bucket precedence.
    """
    return bool(has_adhoc_writer) and (bucket or "").strip().lower() in _ROUTINE_BUCKETS


def grain_key(table: dict, grain: str = GRAIN_CATALOG_SCHEMA) -> str:
    """Slice key for a table at the requested grain.

    Defaults to ``catalog.schema`` (falls back to schema-only when catalog is absent,
    which keeps older callers/tests that pass schema-only dicts working).
    """
    cat = (table.get("catalog") or "").strip()
    sch = (table.get("schema") or "").strip()
    if grain == GRAIN_CATALOG:
        return cat or sch
    if grain == GRAIN_SCHEMA:
        return sch
    return f"{cat}.{sch}" if cat else sch


def _wave_label(bucket: str, group_key: str, seq: int = 1, n: int = 1) -> str:
    suffix = f" ({seq}/{n})" if n > 1 else ""
    if bucket == BUCKET_PILOT:
        return "Pilot — prove the tooling"
    if bucket == BUCKET_FINAL_LARGE:
        return f"Final — largest tables (>=10TB){suffix}"
    if bucket == BUCKET_STREAMING:
        return f"Streaming — {group_key}{suffix} (restart consumers)"
    if bucket == BUCKET_COMPAT:
        return f"Compatibility Mode — {group_key}{suffix}"
    if bucket == BUCKET_SHARE:
        return f"Delta Sharing re-share — {group_key}{suffix}"
    return f"{group_key}{suffix} (domain)"


def _sort_members(ts: list[dict]) -> list[dict]:
    return sorted(
        ts, key=lambda x: (x["blast_radius"], x.get("table_size_bytes") or 0, x["full_name"])
    )


def _chunk(members: list[dict], max_tables: int, max_bytes: Optional[int]) -> list[list[dict]]:
    """Split an ordered member list into chunks bounded by table count and/or total bytes.

    A single table larger than ``max_bytes`` becomes its own chunk (never dropped).
    ``max_bytes`` of 0/None disables the byte cap; ``max_tables`` of 0/None the count cap.
    """
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_bytes = 0
    for m in members:
        b = m.get("table_size_bytes") or 0
        over_count = bool(max_tables) and len(cur) >= max_tables
        over_bytes = bool(max_bytes) and cur and (cur_bytes + b) > max_bytes
        if cur and (over_count or over_bytes):
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(m)
        cur_bytes += b
    if cur:
        chunks.append(cur)
    return chunks


def _sizing_rollup(members: list[dict]) -> dict:
    """Cluster-sizing rollup for a wave: driven by the member needing the LARGEST cluster.

    Uses the per-member recommendation and keeps the largest, so an UNKNOWN (None) size —
    which ``eligibility.recommended_cores`` treats conservatively as the top band — is not
    silently down-sized to 32 cores by coercing ``None`` to ``0`` (an empty 0-byte table).
    """
    if not members:
        return {
            "recommended_cores": _elig.recommended_cores(0),
            "recommended_warehouse": _elig.recommended_warehouse(0),
            "max_size_band": _elig.size_band(0),
        }
    driver = None          # table_size_bytes of the member needing the biggest cluster
    best_cores = -1
    for m in members:
        b = m.get("table_size_bytes")   # may be None (unknown) -> conservative top band
        c = _elig.recommended_cores(b)
        if c > best_cores:
            best_cores, driver = c, b
    return {
        "recommended_cores": _elig.recommended_cores(driver),
        "recommended_warehouse": _elig.recommended_warehouse(driver),
        "max_size_band": _elig.size_band(driver),
    }


def pick_pilot(
    pilot_pool: list[dict],
    *,
    pilot_max_tables: int = PILOT_MAX_TABLES,
    pilot_schema: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Choose the pilot wave from all pilot-eligible tables.

    The pilot is constrained to a SINGLE ``catalog.schema`` (we are only proving the
    tooling) and capped at ``pilot_max_tables``. When ``pilot_schema`` is given it is
    honoured; otherwise the schema with the most pilot candidates is auto-picked
    (ties broken by fewest bytes, then name). Returns ``(chosen, demoted)`` — demoted
    pilot-eligible tables fall back to the normal bucket.
    """
    if not pilot_pool:
        return [], []
    by_key: dict[str, list[dict]] = defaultdict(list)
    for t in pilot_pool:
        by_key[grain_key(t, GRAIN_CATALOG_SCHEMA)].append(t)
    if pilot_schema:
        if pilot_schema not in by_key:
            raise ValueError(
                f"pilot_schema {pilot_schema!r} has no pilot-eligible candidates "
                f"(available: {sorted(by_key)})"
            )
        key = pilot_schema
    else:
        key = min(
            by_key.items(),
            key=lambda kv: (-len(kv[1]), sum((x.get("table_size_bytes") or 0) for x in kv[1]), kv[0]),
        )[0]
    ranked = _sort_members(by_key[key])
    chosen = ranked[:pilot_max_tables]
    chosen_names = {t["full_name"] for t in chosen}
    demoted = [t for t in pilot_pool if t["full_name"] not in chosen_names]
    return chosen, demoted


_VALID_GRAINS = {GRAIN_CATALOG_SCHEMA, GRAIN_CATALOG}


def sequence_waves(
    tables: list[dict],
    *,
    pilot_max_tables: int = PILOT_MAX_TABLES,
    pilot_schema: Optional[str] = None,
    wave_max_tables: int = WAVE_MAX_TABLES,
    wave_max_bytes: Optional[int] = WAVE_MAX_BYTES,
    grain: str = GRAIN_CATALOG_SCHEMA,
) -> tuple[list[dict], dict]:
    """Order tables into waves (wave-planner v2).

    ``tables`` items need: full_name, catalog, schema, bucket, blast_radius,
    table_size_bytes. Buckets come from :func:`assign_bucket`.

    Order: pilot (single capped catalog.schema) -> normal -> streaming -> compat ->
    share -> final_large. Non-pilot buckets are sliced by ``grain`` (default
    ``catalog.schema``, optional coarser ``catalog``) and each slice is chunked to stay
    within ``wave_max_tables`` and ``wave_max_bytes`` so no single wave is overloaded. A
    single table above the byte cap forms an explicitly flagged (``exceeds_wave_byte_cap``)
    singleton wave; a wave never exceeds the count cap. Slices are ordered by ascending
    average blast-radius then total bytes; within a wave tables are sorted by ascending
    blast_radius then size. Each wave carries group metadata + a cluster-sizing rollup
    (driven by its largest table).

    Returns (waves, assignments) where assignments maps full_name -> wave fields.
    Note: pilot-eligible tables not selected for the pilot are demoted to ``normal``
    (this mutates their ``bucket`` in ``tables``).

    Raises ``ValueError`` on an invalid grain, non-positive caps, or a malformed
    explicit ``pilot_schema`` (fail loud rather than silently fall back).
    """
    if grain not in _VALID_GRAINS:
        raise ValueError(f"grain must be one of {sorted(_VALID_GRAINS)}; got {grain!r}")
    if pilot_max_tables <= 0 or wave_max_tables <= 0:
        raise ValueError("pilot_max_tables and wave_max_tables must be positive")
    if wave_max_bytes is not None and wave_max_bytes <= 0:
        raise ValueError("wave_max_bytes must be positive or None")
    if pilot_schema is not None and len(pilot_schema.split(".")) != 2:
        raise ValueError(f"pilot_schema must be 'catalog.schema'; got {pilot_schema!r}")

    chosen_pilot, demoted = pick_pilot(
        [t for t in tables if t["bucket"] == BUCKET_PILOT],
        pilot_max_tables=pilot_max_tables,
        pilot_schema=pilot_schema,
    )
    for t in demoted:
        t["bucket"] = BUCKET_NORMAL

    def ordered_slices(bucket: str) -> list[tuple[str, str, int, int, list[dict]]]:
        d: dict[str, list[dict]] = defaultdict(list)
        for t in tables:
            if t["bucket"] == bucket:
                d[grain_key(t, grain)].append(t)

        def keyf(item):
            _key, ts = item
            avg = sum(x["blast_radius"] for x in ts) / len(ts)
            tot = sum((x.get("table_size_bytes") or 0) for x in ts)
            return (avg, tot, _key)

        out: list[tuple[str, str, int, int, list[dict]]] = []
        for key, ts in sorted(d.items(), key=keyf):
            chunks = _chunk(_sort_members(ts), wave_max_tables, wave_max_bytes)
            n = len(chunks)
            for i, ch in enumerate(chunks, start=1):
                out.append((bucket, key, i, n, ch))
        return out

    groups: list[tuple[str, str, int, int, list[dict]]] = []
    if chosen_pilot:
        # The pilot is a single catalog.schema (pick_pilot keys by GRAIN_CATALOG_SCHEMA);
        # carry that real schema as group_key so it is a valid `target_schema` for the
        # campaign/convert selectors. The generic "Pilot — prove the tooling" branding
        # lives in wave_label, not group_key.
        pilot_key = grain_key(chosen_pilot[0], GRAIN_CATALOG_SCHEMA)
        groups.append((BUCKET_PILOT, pilot_key, 1, 1, chosen_pilot))
    for bucket in (BUCKET_NORMAL, BUCKET_STREAMING, BUCKET_COMPAT, BUCKET_SHARE, BUCKET_FINAL_LARGE):
        groups.extend(ordered_slices(bucket))

    waves: list[dict] = []
    assignments: dict[str, dict] = {}
    for order, (bucket, key, seq, n, ts) in enumerate(groups):
        wave_id = f"wave_{order:02d}"
        members = _sort_members(ts)
        total_bytes = sum((m.get("table_size_bytes") or 0) for m in members)
        exceeds = bool(wave_max_bytes) and total_bytes > wave_max_bytes
        fields = {
            "wave_id": wave_id,
            "wave_order": order,
            "bucket": bucket,
            "wave_label": _wave_label(bucket, key, seq, n),
            "planned_window": wave_window_hint(bucket),
            "group_key": key,
            "wave_seq_in_group": seq,
            "wave_count_in_group": n,
            "planned_table_count": len(members),
            "planned_total_bytes": total_bytes,
            "exceeds_wave_byte_cap": exceeds,
            **_sizing_rollup(members),
        }
        waves.append({**fields, "members": [m["full_name"] for m in members]})
        for m in members:
            assignments[m["full_name"]] = dict(fields)
    return waves, assignments


def resolve_wave_targets(
    wave_rows: list[dict],
    *,
    wave_ids: Optional[Iterable[str]] = None,
    target_schema: Optional[str] = None,
    bucket: Optional[str] = None,
    include_streaming: bool = False,
) -> list[dict]:
    """Resolve a selector into an ordered list of ``{plan_run_id, wave_id}`` targets.

    ``wave_rows`` are current ``wave_plan`` rows enriched with the table's manifest
    ``state`` (one row per table). Selection rules (see plan):

    - Exactly one of ``wave_ids`` OR (``target_schema`` and/or ``bucket``) — no selector
      is an error (there is no implicit "convert all"); ``wave_ids`` is exclusive with
      ``target_schema``/``bucket``.
    - ``bucket`` is a FILTER, not a standalone selector — it must accompany a
      ``target_schema`` (or ``wave_ids``). The ONE exception is ``bucket='streaming'``,
      which is a valid standalone selector because streaming waves are otherwise excluded
      from a ``target_schema`` scope (they are the only bucket a schema scope skips).
    - Only waves with a remaining ``DRY_RUN_OK`` or ``CONVERTING`` member are runnable
      (this lets an interrupted conversion resume).
    - Streaming waves are excluded from a schema/bucket selection unless
      ``include_streaming=True`` or ``bucket='streaming'``. Explicit streaming
      ``wave_ids`` are always honoured.
    - Unknown explicit wave IDs raise; an unknown ``bucket`` raises (must be one of
      ``ALL_BUCKETS``); a valid scope with no runnable waves returns ``[]``.
    - All rows must share one ``plan_run_id`` (guards against selector/convert drift).

    Returns objects ordered by ``wave_order``.
    """
    # Dedupe explicit ids preserving first-seen order (a repeated id must not convert twice).
    ids: list[str] = []
    for w in (wave_ids or []):
        w = (w or "").strip()
        if w and w not in ids:
            ids.append(w)
    has_explicit = bool(ids)
    has_scope = bool(target_schema) or bool(bucket)
    if not has_explicit and not has_scope:
        raise ValueError("no selector: pass wave_ids OR target_schema/bucket (no implicit convert-all)")
    if has_explicit and has_scope:
        raise ValueError("wave_ids is exclusive with target_schema/bucket")
    # Reject an unknown bucket up front with a clear message, rather than letting it fall
    # through to the generic "matched no waves" error (which reads like a done scope). This
    # runs before the filter-rule below so a typo'd bucket reports the typo, not the rule.
    if bucket and bucket not in ALL_BUCKETS:
        raise ValueError(
            f"unknown bucket {bucket!r}; valid buckets are {list(ALL_BUCKETS)}"
        )
    # bucket is a filter, not a standalone selector: it narrows a target_schema scope, so a
    # bucket ALONE (no schema, no explicit ids) would silently convert that bucket across
    # EVERY schema in the plan. The sole exception is streaming, which a target_schema scope
    # deliberately excludes (so bucket='streaming' is the documented way to convert it).
    if bucket and bucket != BUCKET_STREAMING and not target_schema and not has_explicit:
        raise ValueError(
            "bucket is a filter (except 'streaming'): pair it with target_schema, "
            "or use wave_ids / bucket=streaming"
        )

    rows = list(wave_rows or [])
    plan_ids = {r.get("plan_run_id") for r in rows if r.get("plan_run_id")}
    if len(plan_ids) > 1:
        raise ValueError(f"wave_plan spans multiple plan_run_ids {sorted(plan_ids)}; re-run the planner")
    plan_run_id = next(iter(plan_ids), None)
    # A current plan MUST carry a non-empty plan_run_id — otherwise convert's plan-drift
    # guard (which matches on plan_run_id) cannot bind. Fail rather than emit null-pinned
    # targets that would silently never match.
    if rows and plan_run_id is None:
        raise ValueError("wave_plan rows have no plan_run_id; re-run the planner to stamp one")

    # Group current plan rows into waves, tracking state membership + metadata.
    waves: dict[str, dict] = {}
    for r in rows:
        wid = r.get("wave_id")
        if not wid:
            continue
        w = waves.setdefault(wid, {
            "wave_id": wid, "wave_order": r.get("wave_order", 0),
            "bucket": r.get("bucket"), "group_key": r.get("group_key"),
            "states": [],
        })
        w["states"].append((r.get("state") or "").upper())

    def _runnable(w: dict) -> bool:
        return any(s in ("DRY_RUN_OK", "CONVERTING") for s in w["states"])

    if has_explicit:
        unknown = [i for i in ids if i not in waves]
        if unknown:
            raise ValueError(f"unknown wave_ids: {sorted(unknown)}")
        selected = [waves[i] for i in ids]
    else:
        selected = list(waves.values())
        if target_schema:
            selected = [w for w in selected if w.get("group_key") == target_schema]
        if bucket:
            selected = [w for w in selected if w.get("bucket") == bucket]
        # A scope that matches ZERO waves is almost always a typo (e.g. wrong
        # catalog.schema / bucket). Fail loudly rather than silently returning [] — that
        # empty-no-op is reserved for a valid scope whose waves are simply all done.
        if not selected:
            raise ValueError(
                f"selector matched no waves in the current plan "
                f"(target_schema={target_schema!r}, bucket={bucket!r}). "
                "Check the value against wave_plan.group_key / bucket."
            )
        # Exclude streaming from a schema/bucket scope unless explicitly allowed.
        if not include_streaming and bucket != BUCKET_STREAMING:
            selected = [w for w in selected if w.get("bucket") != BUCKET_STREAMING]

    runnable = [w for w in selected if _runnable(w)]
    runnable.sort(key=lambda w: (w.get("wave_order", 0), w["wave_id"]))
    return [{"plan_run_id": plan_run_id, "wave_id": w["wave_id"]} for w in runnable]


def wave_summary(rows: list[dict], parallelism: int = 4) -> dict:
    """Per-wave rollup. ``rows`` carry table_size_bytes, est_data_copy_minutes,
    est_downtime_minutes, owners (list). Copy time respects parallelism (wall-clock ~
    sum/parallelism); downtime is per-table so we report the max."""
    p = max(1, int(parallelism))
    total_bytes = sum((r.get("table_size_bytes") or 0) for r in rows)
    sum_copy = sum((r.get("est_data_copy_minutes") or 0.0) for r in rows)
    max_downtime = max((r.get("est_downtime_minutes") or 0.0 for r in rows), default=0.0)
    owners = sorted({o for r in rows for o in (r.get("owners") or [])})
    max_bytes = max((r.get("table_size_bytes") or 0 for r in rows), default=0)
    return {
        "total_tables": len(rows),
        "total_bytes": total_bytes,
        "sum_est_data_copy_minutes": round(sum_copy, 2),
        "parallel_est_data_copy_minutes": round(sum_copy / p, 2),
        "max_est_downtime_minutes": round(max_downtime, 2),
        "owners": owners,
        "recommended_cores": _elig.recommended_cores(max_bytes),
        "recommended_warehouse": _elig.recommended_warehouse(max_bytes),
        "max_size_band": _elig.size_band(max_bytes),
    }


# --------------------------------------------------------------------------- #
# Control tables: wave_plan + comms_feed
# --------------------------------------------------------------------------- #

WAVE_PLAN_TABLE = "wave_plan"
WAVE_PLAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("plan_run_id", "STRING"),
    ("planned_at", "TIMESTAMP"),
    ("wave_id", "STRING"),
    ("wave_order", "INT"),
    ("wave_label", "STRING"),
    ("group_key", "STRING"),
    ("wave_seq_in_group", "INT"),
    ("wave_count_in_group", "INT"),
    ("bucket", "STRING"),
    ("blast_radius", "DOUBLE"),
    ("downstream_consumer_count", "INT"),
    ("size_band", "STRING"),
    ("table_size_bytes", "BIGINT"),
    ("est_data_copy_minutes", "DOUBLE"),
    ("est_downtime_minutes", "DOUBLE"),
    ("planned_table_count", "INT"),
    ("planned_total_bytes", "BIGINT"),
    ("recommended_cores", "INT"),
    ("recommended_warehouse", "STRING"),
    ("max_size_band", "STRING"),
    ("exceeds_wave_byte_cap", "BOOLEAN"),
    ("has_streaming", "BOOLEAN"),
    ("has_external_consumer", "BOOLEAN"),
    ("is_delta_shared", "BOOLEAN"),
    ("needs_compatibility_mode", "BOOLEAN"),
    ("has_adhoc_writer", "BOOLEAN"),
    ("cross_region_risk", "BOOLEAN"),
    ("planned_window", "STRING"),
    ("updated_at", "TIMESTAMP"),
)
WAVE_PLAN_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in WAVE_PLAN_COLUMNS)

COMMS_FEED_TABLE = "comms_feed"
COMMS_FEED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("wave_order", "INT"),
    ("wave_label", "STRING"),
    ("full_name", "STRING"),
    ("owners", "STRING"),
    ("prior_owner", "STRING"),
    ("ownership_change", "STRING"),
    ("owner_notice", "STRING"),
    ("downstream_consumers", "STRING"),
    ("downstream_consumer_count", "INT"),
    ("blast_radius", "DOUBLE"),
    ("est_downtime_minutes", "DOUBLE"),
    ("recommended_cores", "INT"),
    ("recommended_warehouse", "STRING"),
    ("max_size_band", "STRING"),
    ("planned_window", "STRING"),
    ("updated_at", "TIMESTAMP"),
)
COMMS_FEED_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in COMMS_FEED_COLUMNS)


def wave_plan_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{WAVE_PLAN_TABLE}"


def comms_feed_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{COMMS_FEED_TABLE}"


def _create_sql(fqn: str, columns: tuple[tuple[str, str], ...]) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {fqn} (\n  {cols}\n) USING DELTA"


def create_wave_plan_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    return _create_sql(wave_plan_fqn(manifest_catalog, manifest_schema), WAVE_PLAN_COLUMNS)


def create_comms_feed_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    return _create_sql(comms_feed_fqn(manifest_catalog, manifest_schema), COMMS_FEED_COLUMNS)


def _struct_type(columns: tuple[tuple[str, str], ...]):
    from pyspark.sql.types import (
        BooleanType,
        DoubleType,
        IntegerType,
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "BIGINT": LongType(),
        "INT": IntegerType(),
        "BOOLEAN": BooleanType(),
        "DOUBLE": DoubleType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType([StructField(n, sql_to_spark[t], True) for n, t in columns])


def wave_plan_struct_type():
    return _struct_type(WAVE_PLAN_COLUMNS)


def comms_feed_struct_type():
    return _struct_type(COMMS_FEED_COLUMNS)


def build_wave_plan_row(**kwargs) -> dict:
    return {name: kwargs.get(name) for name in WAVE_PLAN_COLUMN_NAMES}


def build_comms_feed_row(**kwargs) -> dict:
    return {name: kwargs.get(name) for name in COMMS_FEED_COLUMN_NAMES}


def build_plan_rows(
    scored: list[dict],
    assignments: dict,
    *,
    plan_run_id: str,
    planned_at,
    updated_at,
    sp_client_id: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """Assemble typed ``wave_plan`` + ``comms_feed`` rows for one plan generation.

    Pure (no Spark). Empty ``scored`` returns two empty lists — the notebook writes them
    unconditionally so a zero-candidate re-plan clears stale current-plan rows.

    Ownership (read-only): ownership never changes, so ``comms_feed.ownership_change`` is
    ``NONE`` for every new plan (older plans may still carry ``LEASE``/``JOIN_GROUP``; readers keep
    accepting them). ``comms.build_owner_notice`` drafts the owner-facing ``owner_notice`` (the
    privileges the migration identity may be granted), ``None`` when ``sp_client_id`` (the App SP)
    owns the table. ``prior_owner`` is the captured owner.
    This function still mutates nothing.
    """
    wave_rows: list[dict] = []
    comms_rows: list[dict] = []
    for t in scored:
        a = assignments[t["full_name"]]
        consumers_str = ", ".join((t.get("consumer_labels") or [])[:50]) or None
        owners_str = ", ".join(t.get("owners") or []) or None

        # --- owner notice (read-only) -----------------------------------------
        prior_owner = t.get("table_owner")
        ownership_change = _own.OWNERSHIP_NONE
        owner_notice = _comms.build_owner_notice(
            full_name=t["full_name"],
            prior_owner=prior_owner,
            planned_window=a["planned_window"],
            owned_by_migration=bool(sp_client_id and prior_owner
                                    and prior_owner.strip() == sp_client_id.strip()),
        )

        wave_rows.append(build_wave_plan_row(
            full_name=t["full_name"], catalog=t.get("catalog"), schema=t.get("schema"),
            plan_run_id=plan_run_id, planned_at=planned_at,
            wave_id=a["wave_id"], wave_order=a["wave_order"], wave_label=a["wave_label"],
            group_key=a["group_key"], wave_seq_in_group=a["wave_seq_in_group"],
            wave_count_in_group=a["wave_count_in_group"],
            bucket=a["bucket"], blast_radius=t["blast_radius"],
            downstream_consumer_count=t["downstream_consumer_count"],
            size_band=t.get("size_band"), table_size_bytes=t.get("table_size_bytes"),
            est_data_copy_minutes=t.get("est_data_copy_minutes"),
            est_downtime_minutes=t.get("est_downtime_minutes"),
            planned_table_count=a["planned_table_count"], planned_total_bytes=a["planned_total_bytes"],
            recommended_cores=a["recommended_cores"], recommended_warehouse=a["recommended_warehouse"],
            max_size_band=a["max_size_band"], exceeds_wave_byte_cap=a["exceeds_wave_byte_cap"],
            has_streaming=t.get("has_streaming"), has_external_consumer=t.get("has_external_consumer"),
            is_delta_shared=t.get("is_delta_shared"), needs_compatibility_mode=t.get("needs_compatibility_mode"),
            has_adhoc_writer=t.get("has_adhoc_writer"),
            cross_region_risk=t.get("cross_region_risk"), planned_window=a["planned_window"],
            updated_at=updated_at,
        ))
        comms_rows.append(build_comms_feed_row(
            plan_run_id=plan_run_id,
            wave_id=a["wave_id"], wave_order=a["wave_order"], wave_label=a["wave_label"],
            full_name=t["full_name"], owners=owners_str,
            prior_owner=prior_owner, ownership_change=ownership_change, owner_notice=owner_notice,
            downstream_consumers=consumers_str,
            downstream_consumer_count=t["downstream_consumer_count"], blast_radius=t["blast_radius"],
            est_downtime_minutes=t.get("est_downtime_minutes"),
            recommended_cores=a["recommended_cores"], recommended_warehouse=a["recommended_warehouse"],
            max_size_band=a["max_size_band"], planned_window=a["planned_window"], updated_at=updated_at,
        ))
    return wave_rows, comms_rows
