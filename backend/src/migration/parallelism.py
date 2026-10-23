"""Pure parallel-batching logic for bulk convert.

Parallel-safety is a DERIVED property of the plan the wave planner already produced — this
module never changes how tables map to waves. It reduces `wave_plan` + `compaction_sources`
+ `streaming_plan` rows into per-wave fingerprints, builds the conflict graph, and colors
waves into WITHIN-TIER, conflict-free batches (streaming isolated to singletons), each
capped at a serverless-budget max. All functions are pure and unit-tested (no Spark).

Two waves conflict iff: either is streaming, OR same group_key (catalog.schema), OR they
share a compaction JOB actor_id, OR they share a streaming job_or_pipeline_id.
"""

from __future__ import annotations

import itertools
import re
from typing import Iterable, Optional

from . import job_schedule as _js
from . import compat as _compat
from . import conductor as _cd
from . import eligibility as _elig
from . import lineage as _lin

JOB_ACTOR_KIND = "JOB"
STREAM_ATTRIBUTED_TYPES: tuple[str, ...] = ("DLT", "STRUCTURED_STREAMING")
DEFAULT_BULK_MAX_PARALLEL = 4

# Batch ordering mirrors the wave sequence (sequence_waves): pilot -> normal -> streaming ->
# compat -> share -> final_large. Lower rank = earlier campaign; pilot batches always first.
_DEFAULT_TIER_RANK: dict[str, int] = {
    _lin.BUCKET_PILOT: 0,
    _lin.BUCKET_NORMAL: 1,
    _lin.BUCKET_STREAMING: 2,
    _lin.BUCKET_COMPAT: 3,
    _lin.BUCKET_SHARE: 4,
    _lin.BUCKET_FINAL_LARGE: 5,
}


def _i(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _truthy(v) -> bool:
    """A view boolean that may arrive as a real bool or as a 'true'/'false' string."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return bool(v)


def wave_fingerprints(
    wave_plan_rows: Iterable[dict],
    compaction_rows: Iterable[dict],
    stream_rows: Iterable[dict],
    *,
    job_actor_kind: str = JOB_ACTOR_KIND,
    stream_types: tuple[str, ...] = STREAM_ATTRIBUTED_TYPES,
) -> list[dict]:
    """One fingerprint per (plan_run_id, wave_id). Pure."""
    # Index resource ids by table full_name so we can attribute them to each wave.
    jobs_by_table: dict[str, set] = {}
    for r in compaction_rows:
        if str(r.get("actor_kind")) == job_actor_kind and r.get("actor_id"):
            jobs_by_table.setdefault(r["full_name"], set()).add(str(r["actor_id"]))
    streams_by_table: dict[str, set] = {}
    for r in stream_rows:
        if str(r.get("type")) in stream_types and r.get("job_or_pipeline_id"):
            streams_by_table.setdefault(r["full_name"], set()).add(str(r["job_or_pipeline_id"]))

    grouped: dict[tuple, dict] = {}
    for w in wave_plan_rows:
        key = (w.get("plan_run_id"), w["wave_id"])
        fp = grouped.get(key)
        if fp is None:
            fp = {
                "plan_run_id": w.get("plan_run_id"), "wave_id": w["wave_id"],
                "wave_order": _i(w.get("wave_order")), "bucket": w.get("bucket"),
                "group_key": w.get("group_key"), "is_streaming": False,
                "compaction_job_ids": set(), "stream_ids": set(),
                "table_count": 0, "size_bytes": 0, "est_copy_min": 0.0,
                "max_downtime_min": 0.0, "recommended_warehouse": w.get("recommended_warehouse"),
            }
            grouped[key] = fp
        fp["table_count"] += 1
        fp["size_bytes"] += _i(w.get("table_size_bytes"))
        fp["est_copy_min"] += float(w.get("est_data_copy_minutes") or 0.0)
        fp["max_downtime_min"] = max(fp["max_downtime_min"], float(w.get("est_downtime_minutes") or 0.0))
        if w.get("has_streaming") or w.get("bucket") == _lin.BUCKET_STREAMING:
            fp["is_streaming"] = True
        fp["compaction_job_ids"].update(jobs_by_table.get(w["full_name"], ()))
        fp["stream_ids"].update(streams_by_table.get(w["full_name"], ()))

    out = []
    for fp in grouped.values():
        fp["compaction_job_ids"] = frozenset(fp["compaction_job_ids"])
        fp["stream_ids"] = frozenset(fp["stream_ids"])
        fp["est_copy_min"] = round(fp["est_copy_min"], 2)
        out.append(fp)
    out.sort(key=lambda f: (f["wave_order"], str(f["wave_id"])))
    return out


def wave_conflict_edges(fingerprints: list[dict]) -> set[frozenset]:
    """Undirected conflict edges between wave_ids. Pure."""
    edges: set[frozenset] = set()
    for a, b in itertools.combinations(fingerprints, 2):
        pair = frozenset((a["wave_id"], b["wave_id"]))
        if a["is_streaming"] or b["is_streaming"]:
            edges.add(pair); continue
        if a["group_key"] is not None and a["group_key"] == b["group_key"]:
            edges.add(pair); continue
        if a["compaction_job_ids"] & b["compaction_job_ids"]:
            edges.add(pair); continue
        if a["stream_ids"] & b["stream_ids"]:
            edges.add(pair)
    return edges


def conflict_reasons(fingerprints: list[dict], edges: set[frozenset]) -> dict[str, list[str]]:
    """Per wave_id, the specific, human reasons it conflicts with any peer. Pure."""
    by_id = {f["wave_id"]: f for f in fingerprints}
    reasons: dict[str, set] = {f["wave_id"]: set() for f in fingerprints}
    for edge in edges:
        a_id, b_id = tuple(edge)
        a, b = by_id[a_id], by_id[b_id]
        pair_reasons: list[str] = []
        if a["is_streaming"] or b["is_streaming"]:
            pair_reasons.append("streaming_isolated")
        if a["group_key"] is not None and a["group_key"] == b["group_key"]:
            pair_reasons.append(f"shared_schema:{a['group_key']}")
        for jid in sorted(a["compaction_job_ids"] & b["compaction_job_ids"]):
            pair_reasons.append(f"shared_compaction_job:{jid}")
        for sid in sorted(a["stream_ids"] & b["stream_ids"]):
            pair_reasons.append(f"shared_stream:{sid}")
        reasons[a_id].update(pair_reasons)
        reasons[b_id].update(pair_reasons)
    return {wid: sorted(rs) for wid, rs in reasons.items()}


def safe_batches(
    fingerprints: list[dict],
    edges: set[frozenset],
    *,
    max_parallel: int = DEFAULT_BULK_MAX_PARALLEL,
    tier_rank: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Within-tier greedy graph coloring -> ordered, capped, conflict-free batches. Pure."""
    ranks = tier_rank or _DEFAULT_TIER_RANK
    cap = max(1, int(max_parallel))
    adjacency: dict[str, set] = {f["wave_id"]: set() for f in fingerprints}
    for edge in edges:
        a, b = tuple(edge)
        adjacency[a].add(b)
        adjacency[b].add(a)

    # Partition waves by tier, ordered by (tier_rank, wave_order, wave_id).
    tiers: dict[int, list[dict]] = {}
    for f in fingerprints:
        tiers.setdefault(ranks.get(f["bucket"], 99), []).append(f)

    batches: list[dict] = []
    order = 0
    for rank in sorted(tiers):
        members = sorted(tiers[rank], key=lambda f: (f["wave_order"], str(f["wave_id"])))
        streaming = members and members[0]["is_streaming"]
        colors: list[list[str]] = []   # each color = one batch of mutually non-conflicting waves
        for f in members:
            wid = f["wave_id"]
            placed = False
            if not streaming:
                for grp in colors:
                    if len(grp) < cap and not (adjacency[wid] & set(grp)):
                        grp.append(wid); placed = True; break
            if not placed:
                colors.append([wid])
        for grp in colors:
            bucket = next(m["bucket"] for m in members if m["wave_id"] == grp[0])
            batches.append({
                "batch_id": f"batch_{order:02d}", "batch_order": order,
                "bucket": bucket, "wave_ids": grp, "parallel_degree": len(grp),
            })
            order += 1
    return batches


# --------------------------------------------------------------------------- #
# Control table: wave_parallel_groups (one row per wave, rewritten per plan_run_id)
# --------------------------------------------------------------------------- #

WAVE_PARALLEL_GROUPS_TABLE = "wave_parallel_groups"
WAVE_PARALLEL_GROUPS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("wave_order", "INT"),
    ("bucket", "STRING"),
    ("group_key", "STRING"),
    ("batch_id", "STRING"),
    ("batch_order", "INT"),
    ("parallel_degree", "INT"),
    ("blocked_from_parallel", "BOOLEAN"),
    ("conflict_reason", "STRING"),
    ("table_count", "INT"),
    ("size_bytes", "BIGINT"),
    ("est_copy_min", "DOUBLE"),
    ("max_downtime_min", "DOUBLE"),
    ("recommended_warehouse", "STRING"),
    ("computed_at", "TIMESTAMP"),
)
WAVE_PARALLEL_GROUPS_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in WAVE_PARALLEL_GROUPS_COLUMNS)


def wave_parallel_groups_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{WAVE_PARALLEL_GROUPS_TABLE}"


def create_wave_parallel_groups_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in WAVE_PARALLEL_GROUPS_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{wave_parallel_groups_fqn(manifest_catalog, manifest_schema)} (\n  {cols}\n) USING DELTA"
    )


def wave_parallel_groups_struct_type():
    from pyspark.sql.types import (
        BooleanType, DoubleType, IntegerType, LongType, StringType,
        StructField, StructType, TimestampType,
    )
    sql_to_spark = {
        "STRING": StringType(), "BIGINT": LongType(), "INT": IntegerType(),
        "BOOLEAN": BooleanType(), "DOUBLE": DoubleType(), "TIMESTAMP": TimestampType(),
    }
    return StructType([StructField(n, sql_to_spark[t], True) for n, t in WAVE_PARALLEL_GROUPS_COLUMNS])


def build_wave_parallel_group_row(**kwargs) -> dict:
    return {name: kwargs.get(name) for name in WAVE_PARALLEL_GROUPS_COLUMN_NAMES}


def build_parallel_group_rows(
    fingerprints: list[dict],
    batches: list[dict],
    reasons: dict[str, list[str]],
    *,
    plan_run_id: str,
    computed_at,
) -> list[dict]:
    """One typed wave_parallel_groups row per wave. Pure."""
    batch_of: dict[str, dict] = {}
    for b in batches:
        for wid in b["wave_ids"]:
            batch_of[wid] = b
    rows: list[dict] = []
    for f in fingerprints:
        wid = f["wave_id"]
        b = batch_of[wid]
        degree = b["parallel_degree"]
        wave_reasons = reasons.get(wid, [])
        if degree > 1:
            reason = None
        elif wave_reasons:
            reason = "; ".join(wave_reasons)
        else:
            reason = "sole_wave_in_tier"
        rows.append(build_wave_parallel_group_row(
            plan_run_id=plan_run_id, wave_id=wid, wave_order=f["wave_order"],
            bucket=f["bucket"], group_key=f["group_key"], batch_id=b["batch_id"],
            batch_order=b["batch_order"], parallel_degree=degree,
            blocked_from_parallel=(degree == 1), conflict_reason=reason,
            table_count=f["table_count"], size_bytes=f["size_bytes"],
            est_copy_min=f["est_copy_min"], max_downtime_min=f["max_downtime_min"],
            recommended_warehouse=f["recommended_warehouse"], computed_at=computed_at,
        ))
    rows.sort(key=lambda r: (r["batch_order"], r["wave_order"], str(r["wave_id"])))
    return rows


def wave_ids_for_batch(group_rows: Iterable[dict], batch_id: str) -> list[str]:
    """Member wave_ids of one batch_id, de-duplicated, ordered by (wave_order, wave_id). Pure.

    Reads wave_parallel_groups rows (as dicts). Used by 03d to expand a batch selector into the
    exact set of waves to lease — the bulk mirror of the single-wave (plan_run_id, wave_id) path.
    """
    matched = [g for g in group_rows if str(g.get("batch_id")) == str(batch_id)]
    matched.sort(key=lambda g: (_i(g.get("wave_order")), str(g.get("wave_id"))))
    out: list[str] = []
    seen: set = set()
    for g in matched:
        wid = g.get("wave_id")
        if wid is not None and wid not in seen:
            seen.add(wid)
            out.append(wid)
    return out


# --------------------------------------------------------------------------- #
# WS5.0: shared readiness rollup + next-step recommendation (pure, no Spark)
# Consumed identically by the 11_bulk_cockpit notebook and the app bulk screen.
# --------------------------------------------------------------------------- #

STEP_BULK_DRY_RUN = "bulk_dry_run"
STEP_BULK_FREEZE = "bulk_freeze"
STEP_BULK_LEASE = "bulk_lease"
STEP_BULK_CONVERT = "bulk_convert"
STEP_BULK_VALIDATE = "bulk_validate"
STEP_BULK_UNFREEZE = "bulk_unfreeze"
STEP_BULK_RESHARE = "bulk_reshare"
STEP_BULK_COMPAT = "bulk_compat"
STEP_BULK_OPTIMIZE = "bulk_optimize"
STEP_BULK_DONE = "bulk_done"

# The one-wave finish order (conductor.post_convert_step), applied to the whole batch.
BULK_FINISH_ORDER: tuple[str, ...] = (
    STEP_BULK_UNFREEZE, STEP_BULK_RESHARE, STEP_BULK_COMPAT, STEP_BULK_OPTIMIZE)
_WAVE_TO_BULK_FINISH: dict[str, str] = {
    _cd.STEP_UNFREEZE: STEP_BULK_UNFREEZE,
    _cd.STEP_RESHARE: STEP_BULK_RESHARE,
    _cd.STEP_COMPAT: STEP_BULK_COMPAT,
    _cd.STEP_OPTIMIZE: STEP_BULK_OPTIMIZE,
}

_READY = "READY"
_DONE = "DONE"
_BLOCKED = "BLOCKED"
_AWAITING = "AWAITING_ACTIONS"
_ROLLED_BACK = "ROLLED_BACK"


def dry_run_filters(group_key) -> Optional[tuple[str, str]]:
    """``(catalog_filter, schema_filter)`` for ``job_03_dry_run`` over one wave's group key. Pure.

    A ``catalog_schema`` grain key (``cat.sch``) scopes one schema; a ``catalog`` grain key scopes
    the catalog (``schema_filter='*'``). A blank key returns ``None``: never an estate-wide dry run."""
    gk = str(group_key or "").strip()
    if not gk:
        return None
    cat, _, sch = gk.partition(".")
    return (cat, sch or "*") if cat else None


def _member_to_convert(s: dict) -> bool:
    """A member that still has tables to SET MANAGED: READY, or DRY_RUN_OK/CONVERTING rows."""
    return (str(s.get("wave_state") or "") == _READY
            or _i(s.get("dry_run_ok")) + _i(s.get("converting")) > 0)


def _pre_convert_split(s: dict) -> tuple[int, int]:
    """``(freeze_owed, lease_actions)`` for one member's open PRE_CONVERT holds. Pure.

    ``open_pre_convert`` lumps the freeze/stop actions with ``LEASE_OWNERSHIP``, which only
    ``03d`` closes. With the wave_status split (``open_pre_convert_auto`` / ``_manual``, manual
    already excludes the lease) the remainder is the lease. A row without the split keeps the
    legacy reading — everything owes the freeze — never a silent green light (as the conductor).
    """
    pre = _i(s.get("open_pre_convert"))
    auto, manual = s.get("open_pre_convert_auto"), s.get("open_pre_convert_manual")
    if auto is None and manual is None:
        return pre, 0
    auto, manual = _i(auto), _i(manual)
    return auto + manual, max(0, pre - auto - manual)


def batch_finish_step(member_status_rows: Iterable[dict], open_freeze: int = 0) -> Optional[str]:
    """The earliest finish step any member of a batch still owes, or ``None``. Pure.

    Only an ``AWAITING_ACTIONS`` + ``fully_migrated`` member owes a finish step (the
    ``conductor.pick_finishing_wave`` gate): POST_CONVERT actions exist from the plan onward, so an
    unconverted member must owe nothing yet. Each member's own ``conductor.post_convert_step`` is
    mapped to its bulk step, and the batch returns the first owed step in ``BULK_FINISH_ORDER``.
    A FROZEN ledger row (``open_freeze > 0``) always owes the unfreeze.
    """
    owed: set[str] = {STEP_BULK_UNFREEZE} if _i(open_freeze) > 0 else set()
    for s in member_status_rows or []:
        if str(s.get("wave_state") or "").upper() != _AWAITING or not _truthy(s.get("fully_migrated")):
            continue
        step = _cd.post_convert_step(s)
        if step in _WAVE_TO_BULK_FINISH:
            owed.add(_WAVE_TO_BULK_FINISH[step])
    return next((st for st in BULK_FINISH_ORDER if st in owed), None)


def batch_readiness(
    group_rows: list[dict],
    wave_status_rows: list[dict],
    open_freeze_by_batch: Optional[dict[str, int]] = None,
) -> list[dict]:
    """One rollup row per batch_id (parallel batches only; blocked_from_parallel excluded).

    Joins each ``wave_parallel_groups`` row to its ``wave_status`` row on
    ``(plan_run_id, wave_id)`` and aggregates per ``batch_id``. Missing wave_status rows
    coalesce to a PLANNED/'' state (no queue == not ready); a missing ``open_freeze`` entry
    coalesces to 0. Pure/deterministic; sorted by ``batch_order``.
    """
    freeze_map = open_freeze_by_batch or {}
    status_by_key: dict[tuple, dict] = {
        (s.get("plan_run_id"), s.get("wave_id")): s for s in wave_status_rows
    }

    batches: dict[str, list[dict]] = {}
    for g in group_rows:
        if g.get("blocked_from_parallel"):
            continue
        batches.setdefault(g["batch_id"], []).append(g)

    out: list[dict] = []
    for batch_id, members in batches.items():
        members = sorted(members, key=lambda g: (_i(g.get("wave_order")), str(g.get("wave_id"))))
        head = members[0]

        member_rows: list[dict] = []
        member_states: list[str] = []
        member_validated: list[bool] = []
        open_pre = open_post = open_blocking = 0
        open_lease = open_lease_to_convert = 0
        open_pre_freeze = open_pre_lease = open_lease_unverified = 0
        readers_undecided = readers_fgac_required = 0
        rolled_back_wave_ids: list[str] = []
        rolled_back_tables = 0
        dry_run_scopes: list[dict] = []
        converted_unvalidated = 0
        waves_need_freeze = 0
        table_count = size_bytes = 0
        est_copy_min = 0.0
        max_downtime_min = 0.0
        sched_members: list[dict] = []
        for g in members:
            table_count += _i(g.get("table_count"))
            size_bytes += _i(g.get("size_bytes"))
            est_copy_min += float(g.get("est_copy_min") or 0.0)
            max_downtime_min = max(max_downtime_min, float(g.get("max_downtime_min") or 0.0))
            s = status_by_key.get((g.get("plan_run_id"), g.get("wave_id"))) or {}
            member_rows.append(s)
            member_states.append(str(s.get("wave_state") or ""))
            # Fully migrated counts as validated even while the wave still owes finish steps
            # (unfreeze / re-share / Compat / optimize read AWAITING_ACTIONS, not DONE).
            member_validated.append(
                str(s.get("wave_state") or "") == _DONE or _truthy(s.get("fully_migrated")))
            pre = _i(s.get("open_pre_convert"))
            open_pre += pre
            open_post += _i(s.get("open_post_convert"))
            open_blocking += _i(s.get("open_blocking"))
            open_lease += _i(s.get("open_lease"))
            # wave_status already counts only members still to convert (advisory, never a hold).
            readers_undecided += _i(s.get("readers_undecided"))
            readers_fgac_required += _i(s.get("readers_fgac_required"))
            freeze_owed, lease_actions = _pre_convert_split(s)
            open_pre_freeze += freeze_owed
            if _member_to_convert(s):
                open_lease_to_convert += _i(s.get("open_lease"))
                open_pre_lease += lease_actions
                open_lease_unverified += _i(s.get("open_pre_convert_lease_unverified"))
            # A rolled-back table is EXTERNAL again and needs a fresh dry run (03 re-selects any
            # ELIGIBLE table) before the batch can convert it.
            rb = _i(s.get("rolled_back"))
            if rb > 0:
                rolled_back_wave_ids.append(g["wave_id"])
                rolled_back_tables += rb
                f = dry_run_filters(g.get("group_key"))
                if f and not any((d["catalog_filter"], d["schema_filter"]) == f for d in dry_run_scopes):
                    dry_run_scopes.append({"wave_id": g["wave_id"], "catalog_filter": f[0],
                                           "schema_filter": f[1]})
            # wave_status.converted counts tables CURRENTLY in state CONVERTED (not yet validated).
            converted_unvalidated += _i(s.get("converted"))
            if freeze_owed > 0:
                waves_need_freeze += 1
            # Schedule-safety rollup: reuse the wave-level rollup so a batch is aggregated
            # exactly like a wave — worst member verdict governs, earliest fire is next write.
            sched_members.append({
                "writer_schedule_verdict": s.get("wave_writer_verdict"),
                "next_scheduled_write_at": s.get("wave_next_scheduled_write_at"),
            })

        sched = _js.wave_schedule_safety(sched_members)

        ready_count = sum(1 for st in member_states if st == _READY)
        total_count = len(members)
        convert_ready = total_count > 0 and all(st == _READY for st in member_states)
        all_validated = total_count > 0 and all(member_validated)
        open_freeze = _i(freeze_map.get(batch_id, 0))
        any_blocked = any(st == _BLOCKED for st in member_states)
        started = any(_cd.wave_started(s) for s in member_rows)
        resume_convert = (started and not any_blocked and any(
            _i(s.get("dry_run_ok")) + _i(s.get("converting")) > 0 for s in member_rows))
        finish_step = batch_finish_step(member_rows, open_freeze) if all_validated else None

        if any_blocked:
            rollup = _BLOCKED
        elif rolled_back_wave_ids:
            rollup = _ROLLED_BACK
        elif open_pre > 0:
            rollup = _AWAITING
        elif all_validated and finish_step:
            # A finish step is still owed: never show DONE beside paused compaction/writers or an
            # un-re-shared table.
            rollup = _AWAITING
        elif all_validated:
            rollup = _DONE
        elif convert_ready:
            rollup = _READY
        else:
            rollup = "IN_PROGRESS"

        out.append({
            "plan_run_id": head.get("plan_run_id"),
            "batch_id": batch_id,
            "batch_order": _i(head.get("batch_order")),
            "parallel_degree": _i(head.get("parallel_degree")),
            "bucket": head.get("bucket"),
            "member_wave_ids": [g["wave_id"] for g in members],
            "table_count": table_count,
            "size_bytes": size_bytes,
            "est_copy_min": round(est_copy_min, 2),
            "max_downtime_min": max_downtime_min,
            "ready_count": ready_count,
            "total_count": total_count,
            "open_pre_convert": open_pre,
            "open_pre_convert_freeze": open_pre_freeze,
            "open_pre_convert_lease": open_pre_lease,
            "open_pre_convert_lease_unverified": open_lease_unverified,
            "open_post_convert": open_post,
            "open_blocking": open_blocking,
            "open_lease": open_lease,
            "open_lease_to_convert": open_lease_to_convert,
            "readers_undecided": readers_undecided,
            "readers_fgac_required": readers_fgac_required,
            "readers_attention": readers_undecided + readers_fgac_required,
            "converted_unvalidated": converted_unvalidated,
            "waves_need_freeze": waves_need_freeze,
            "convert_ready": convert_ready,
            "all_validated": all_validated,
            "open_freeze": open_freeze,
            "started": started,
            "resume_convert": resume_convert,
            "finish_step": finish_step,
            "rolled_back_wave_ids": rolled_back_wave_ids,
            "rolled_back_tables": rolled_back_tables,
            "dry_run_scopes": dry_run_scopes,
            "batch_writer_verdict": sched["wave_verdict"],
            "batch_next_scheduled_write_at": sched["wave_next_scheduled_write_at"],
            "rollup": rollup,
        })
    out.sort(key=lambda r: (r["batch_order"], str(r["batch_id"])))
    return out


def order_batches_for_recommendation(
    rollups: Iterable[dict],
    *,
    tier_rank: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Order batch rollups for the single recommended next launch (recommendation ORDER only).

    Risk tiers win first (pilot before normal, streaming isolated, …) — the same tier ordering
    ``batch_order`` already encodes — so a schedule-safe normal batch can never jump ahead of a
    riskier pilot batch. WITHIN a tier the schedule-SAFEST batch is recommended first via
    ``job_schedule.schedule_safety_sort_key`` (applied AFTER the tier key, mirroring how the wave
    sort applies it after ``wave_order``); ``batch_order`` then ``batch_id`` are the final
    deterministic tiebreak. This NEVER re-maps wave→batch membership (``wave_parallel_groups``
    stays the source of truth) — it only reorders the recommendation. Pure/deterministic.
    """
    ranks = tier_rank or _DEFAULT_TIER_RANK

    def _key(r: dict) -> tuple:
        return (
            ranks.get(r.get("bucket"), 99),
            _js.schedule_safety_sort_key(
                r.get("batch_writer_verdict"), r.get("batch_next_scheduled_write_at")),
            _i(r.get("batch_order")),
            str(r.get("batch_id")),
        )

    return sorted(rollups, key=_key)


def next_bulk_step(batch_rollup: dict) -> str:
    """Pure SR11 next-step recommendation for ONE ``batch_readiness`` rollup.

    The one-wave journey, applied to the batch: freeze -> lease -> convert (or resume) ->
    validate -> unfreeze -> re-share -> Compat -> optimize. Only ``open_pre_convert_freeze`` routes
    to the freeze: an open ``LEASE_OWNERSHIP`` action also holds a wave in AWAITING_ACTIONS, but
    ``03d`` closes it, not the freeze, so it routes to the lease. Manual-only holds still route to
    the freeze (no bulk actions step; the operator attests them). A rollup without the split falls
    back to ``open_pre_convert``. A lease is owed only for members that still have tables to convert
    (``open_lease_to_convert`` / ``open_pre_convert_lease``), so a converted batch never loops back.
    A BLOCKED batch never returns LEASE or CONVERT: ``convert_ready`` needs every member READY and
    ``resume_convert`` is false beside a BLOCKED member. Never auto-sequences — the operator
    confirms and launches the returned step.

    A rolled-back member comes first (``bulk_dry_run``): its tables are EXTERNAL again, and
    nothing else in the batch can convert until they are dry-run back to DRY_RUN_OK. The launch is
    the existing per-schema ``job_03_dry_run`` (``dry_run_scopes``); there is no bulk dry-run job.
    """
    if batch_rollup.get("rolled_back_wave_ids"):
        return STEP_BULK_DRY_RUN
    freeze = batch_rollup.get("open_pre_convert_freeze")
    if _i(batch_rollup.get("open_pre_convert") if freeze is None else freeze) > 0:
        return STEP_BULK_FREEZE
    if batch_rollup.get("rollup") != _BLOCKED and (
            _i(batch_rollup.get("open_lease_to_convert")) > 0
            or _i(batch_rollup.get("open_pre_convert_lease")) > 0):
        return STEP_BULK_LEASE
    if batch_rollup.get("convert_ready") or batch_rollup.get("resume_convert"):
        return STEP_BULK_CONVERT
    if batch_rollup.get("all_validated"):
        # batch_finish_step already owes the unfreeze whenever open_freeze > 0.
        return batch_rollup.get("finish_step") or STEP_BULK_DONE
    return STEP_BULK_VALIDATE


def lease_step_note(batch_rollup: dict) -> Optional[str]:
    """Why a batch still routes to Authorize after one ran, or ``None``. Pure.

    ``03d`` leaves a convert-permission action it applied but could not verify IN_PROGRESS, and
    re-running it re-checks the table and closes the action (granting again is a no-op). Only said
    while Authorize is the next step."""
    n = _i(batch_rollup.get("open_pre_convert_lease_unverified"))
    if n <= 0 or next_bulk_step(batch_rollup) != STEP_BULK_LEASE:
        return None
    return _cd.unverified_lease_text(n)


_BULK_PRE_CONVERT_STEPS = (STEP_BULK_DRY_RUN, STEP_BULK_FREEZE, STEP_BULK_LEASE, STEP_BULK_CONVERT)


def reader_step_note(batch_rollup: dict) -> Optional[str]:
    """The pre-convert external-reader warning for a batch still to convert, or ``None``. Pure;
    advisory (the same ``compat.preconvert_reader_warning`` text as every single-wave surface)."""
    if next_bulk_step(batch_rollup) not in _BULK_PRE_CONVERT_STEPS:
        return None
    text = _compat.preconvert_reader_warning(
        _i(batch_rollup.get("readers_undecided")), _i(batch_rollup.get("readers_fgac_required")))
    return text or None


def rolled_back_step_note(batch_rollup: dict) -> Optional[str]:
    """Why a batch routes to the dry run: which member(s) were rolled back, or ``None``. Pure."""
    ids = [str(w) for w in batch_rollup.get("rolled_back_wave_ids") or []]
    if not ids:
        return None
    one = len(ids) == 1
    return (f"{', '.join(ids)} {'was' if one else 'were'} rolled back; re-run "
            f"{'its dry run' if one else 'their dry runs'} (job_03_dry_run, one per schema) before "
            "this batch can convert again.")


def _has_work_to_validate(batch_rollup: dict) -> bool:
    """A member still has tables in state CONVERTED (converted, not yet validated). Pure."""
    return _i(batch_rollup.get("converted_unvalidated")) > 0


def _in_flight(batch_rollup: dict) -> bool:
    if not batch_rollup.get("started") or batch_rollup.get("rollup") == _BLOCKED:
        return False
    step = next_bulk_step(batch_rollup)
    if step in (STEP_BULK_DONE, STEP_BULK_DRY_RUN):
        # A rollback is a deliberate operator choice: it never holds the recommendation.
        return False
    return step != STEP_BULK_VALIDATE or _has_work_to_validate(batch_rollup)


def recommended_batch(rollups: Iterable[dict]) -> Optional[dict]:
    """The one batch to launch next: finish the started batch before starting another (D6). Pure.

    The lowest-ordered in-flight batch wins: started, not BLOCKED, and its ``next_bulk_step`` is
    a real step — freeze, lease, convert/resume, a finish step, or a validate with tables in state
    CONVERTED to validate. A started batch whose step is a no-op validate, or the dry run a
    rolled-back member owes, is not held as in flight, so it never hides a READY batch. Otherwise the first READY batch in
    ``order_batches_for_recommendation`` order. Recommendation only — no gate reads it.
    """
    rollups = list(rollups or [])
    in_flight = [r for r in rollups if _in_flight(r)]
    if in_flight:
        return min(in_flight, key=lambda r: (_i(r.get("batch_order")), str(r.get("batch_id"))))
    return next((r for r in order_batches_for_recommendation(rollups)
                 if r.get("rollup") == _READY), None)


def waves_with_remaining_convert(wave_ids: Iterable[str], member_rows: Iterable[dict]) -> list[str]:
    """The batch waves that still have a table for ``04_convert`` to SET MANAGED (D4). Pure.

    ``04_convert`` runs its coverage gates before it selects targets, so re-running it on a wave
    that is converted and already unfrozen fails the compaction gate. A resumed bulk convert fans
    out only over waves with an ELIGIBLE member in DRY_RUN_OK or CONVERTING — the exact set
    ``04_convert`` selects. ``member_rows`` carry ``wave_id``, ``eligibility`` and ``state``.
    Keeps ``wave_ids`` order.
    """
    remaining = {str(r.get("wave_id")) for r in member_rows or []
                 if str(r.get("eligibility") or "") == _elig.ELIGIBLE
                 and str(r.get("state") or "") in (_elig.STATE_DRY_RUN_OK, _elig.STATE_CONVERTING)}
    return [w for w in wave_ids or [] if str(w) in remaining]


def held_back_members(member_rows: Iterable[dict]) -> list[tuple[str, str]]:
    """Batch tables still in DRY_RUN_OK/CONVERTING whose eligibility is no longer ELIGIBLE. Pure.

    The profiler's path-access pass can downgrade a table to NEEDS_REVIEW after its dry run while
    its state stays DRY_RUN_OK: the bulk rollup still counts it (``resume_convert``) but
    ``waves_with_remaining_convert`` and ``04_convert`` skip it. Returns ``(full_name,
    eligibility)`` sorted by ``full_name`` so ``11_select_batch`` can name them.
    """
    held = {(str(r.get("full_name")), str(r.get("eligibility") or ""))
            for r in member_rows or []
            if str(r.get("state") or "") in (_elig.STATE_DRY_RUN_OK, _elig.STATE_CONVERTING)
            and str(r.get("eligibility") or "") != _elig.ELIGIBLE}
    return sorted(held)


# --------------------------------------------------------------------------- #
# M6.4: the cockpit's joined read, open-freeze count and exit summary (pure, no Spark).
# 11_bulk_cockpit and the e2e harness read a batch through these, so both see the same rollup.
# --------------------------------------------------------------------------- #

# (expression, alias, type): the type lets a statement-API reader (every value a string) re-type
# the row before batch_readiness, which tests blocked_from_parallel by plain truthiness.
BATCH_JOINED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("g.plan_run_id", "plan_run_id", "STRING"),
    ("g.batch_id", "batch_id", "STRING"),
    ("g.batch_order", "batch_order", "INT"),
    ("g.parallel_degree", "parallel_degree", "INT"),
    ("g.blocked_from_parallel", "blocked_from_parallel", "BOOLEAN"),
    ("g.conflict_reason", "conflict_reason", "STRING"),
    ("g.wave_id", "wave_id", "STRING"),
    ("g.wave_order", "wave_order", "INT"),
    ("g.bucket", "bucket", "STRING"),
    ("g.group_key", "group_key", "STRING"),
    ("g.table_count", "table_count", "INT"),
    ("g.size_bytes", "size_bytes", "INT"),
    ("g.est_copy_min", "est_copy_min", "DOUBLE"),
    ("g.max_downtime_min", "max_downtime_min", "DOUBLE"),
    ("g.recommended_warehouse", "recommended_warehouse", "STRING"),
    ("s.wave_state", "wave_state", "STRING"),
    ("s.fully_migrated", "fully_migrated", "BOOLEAN"),
    ("s.open_blocking", "open_blocking", "INT"),
    ("s.open_pre_convert", "open_pre_convert", "INT"),
    ("s.open_pre_convert_auto", "open_pre_convert_auto", "INT"),
    ("s.open_pre_convert_manual", "open_pre_convert_manual", "INT"),
    ("s.open_pre_convert_lease_unverified", "open_pre_convert_lease_unverified", "INT"),
    ("s.open_post_convert", "open_post_convert", "INT"),
    ("s.open_lease", "open_lease", "INT"),
    ("s.total_tables", "total_tables", "INT"),
    ("s.dry_run_ok", "dry_run_ok", "INT"),
    ("s.converting", "converting", "INT"),
    ("s.converted", "converted", "INT"),
    ("s.validated", "validated", "INT"),
    ("s.cleaned", "cleaned", "INT"),
    ("s.converted_external", "converted_external", "INT"),
    ("s.rolled_back", "rolled_back", "INT"),
    ("s.open_post_convert_holding", "open_post_convert_holding", "INT"),
    ("s.open_reshare", "open_reshare", "INT"),
    ("s.open_compat", "open_compat", "INT"),
    ("s.readers_undecided", "readers_undecided", "INT"),
    ("s.readers_fgac_required", "readers_fgac_required", "INT"),
    ("s.optimize_pending", "optimize_pending", "BOOLEAN"),
    ("s.wave_writer_verdict", "wave_writer_verdict", "STRING"),
    ("s.wave_next_scheduled_write_at", "wave_next_scheduled_write_at", "TIMESTAMP"),
    ("(s.wave_state = 'READY')", "convert_ready", "BOOLEAN"),
)
_PLAN_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LEDGER_FROZEN = "FROZEN"
_SUMMARY_KEYS = ("batch_id", "batch_order", "rollup", "member_wave_ids", "table_count",
                 "convert_ready", "resume_convert", "all_validated", "open_pre_convert",
                 "open_pre_convert_freeze", "open_pre_convert_lease",
                 "open_pre_convert_lease_unverified",
                 "open_lease_to_convert", "open_freeze", "finish_step",
                 "readers_undecided", "readers_fgac_required", "readers_attention",
                 "rolled_back_wave_ids", "dry_run_scopes")


def batch_joined_sql(groups_fqn: str, status_fqn: str, plan_run_id: str) -> str:
    """``wave_parallel_groups`` ⋈ ``wave_status`` for one plan, the columns ``batch_readiness``
    reads. Raises on anything that is not a plain plan_run_id (it is interpolated)."""
    if not _PLAN_RUN_ID.match(str(plan_run_id or "")):
        raise ValueError(f"not a plan_run_id: {plan_run_id!r}")
    cols = ",\n  ".join(expr if expr.split(".")[-1] == alias else f"{expr} AS {alias}"
                        for expr, alias, _ in BATCH_JOINED_COLUMNS)
    return (f"SELECT\n  {cols}\nFROM {groups_fqn} g\nLEFT JOIN {status_fqn} s\n"
            "  ON g.plan_run_id = s.plan_run_id AND g.wave_id = s.wave_id\n"
            f"WHERE g.plan_run_id = '{plan_run_id}'\nORDER BY g.batch_order, g.wave_order")


def frozen_ledger_sql(ledger_fqn: str) -> str:
    """The real (non-dry-run) FROZEN ``freeze_ledger`` rows, keyed for ``open_freeze_by_batch``."""
    return (f"SELECT plan_run_id, wave_id FROM {ledger_fqn} "
            f"WHERE status = '{_LEDGER_FROZEN}' AND dry_run = false")


def open_freeze_by_batch(joined_rows: Iterable[dict], frozen_rows: Iterable[dict]) -> dict[str, int]:
    """FROZEN ledger rows per ``batch_id`` via each row's ``(plan_run_id, wave_id)``. Pure."""
    wave_to_batch = {(r.get("plan_run_id"), r.get("wave_id")): r.get("batch_id")
                     for r in joined_rows or []}
    counts: dict[str, int] = {}
    for lr in frozen_rows or []:
        batch_id = wave_to_batch.get((lr.get("plan_run_id"), lr.get("wave_id")))
        if batch_id is not None:
            counts[batch_id] = counts.get(batch_id, 0) + 1
    return counts


def cockpit_summary(plan_run_id: Optional[str], rollups: Iterable[dict], *, batch_id: str = "",
                    lease_note: str = "", action: str = "show") -> dict:
    """The cockpit's exit value (JSON-safe): the recommended batch, each batch's rollup and next
    step, and the selected batch's next step. Output only; no gate reads it."""
    rollups = list(rollups or [])
    reco = recommended_batch(rollups)
    batches = [{**{k: r.get(k) for k in _SUMMARY_KEYS}, "next_step": next_bulk_step(r)} for r in rollups]
    selected = next((b for b in batches if b["batch_id"] == batch_id), None) if batch_id else None
    return {"plan_run_id": plan_run_id or None, "action": action, "lease_note": lease_note,
            "recommended_batch": reco["batch_id"] if reco else None, "batch_id": batch_id or None,
            "next_step": selected["next_step"] if selected else None, "batches": batches}
