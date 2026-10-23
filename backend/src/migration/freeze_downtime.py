"""Pure freeze-entity safety classification + per-scope downtime read.

Source of truth for the RULES that classify each attributed freeze actor as safe to pause
vs. a continuous writer that goes into downtime until unfreeze. The ``freeze_plan_by_scope``
view (freeze_views.py) mirrors these rules in SQL for the app/dashboard/MCP to read directly;
this module is the Python consumer + the parity anchor. No Spark, no SDK, no network.
"""
from __future__ import annotations

from typing import Iterable, Optional

# Safety classes (how much a paused entity costs while frozen).
SAFETY_SAFE_WINDOW = "SAFE_WINDOW"                 # paused; will not fire — costs nothing
SAFETY_DOWNTIME = "DOWNTIME_UNTIL_UNFREEZE"        # continuous writer stopped — tell consumers
SAFETY_WONT_RUN = "WONT_RUN"                       # opportunistic (PO) — disabling costs nothing
SAFETY_MANUAL = "MANUAL"                           # ad-hoc/external — cannot be auto-paused
SAFETY_UNKNOWN = "UNKNOWN"                         # unclassifiable — never assume safe

# Action-kind vocabulary (actions.py) this classifier keys off.
_KIND_FREEZE_COMPACTION = "FREEZE_COMPACTION"
_KIND_STOP_STREAM = "STOP_STREAM"
_KIND_STOP_WRITER_JOB = "STOP_WRITER_JOB"

_ACTOR_PO = "PREDICTIVE_OPTIMIZATION"
_ACTOR_JOB = "JOB"
_ADHOC_ACTORS = frozenset({"NOTEBOOK", "USER"})
_ADHOC_WRITERS = frozenset({"NOTEBOOK", "USER", "EXTERNAL"})


def classify_freeze_entity(
    *,
    action_kind: str,
    actor_kind: Optional[str] = None,
    streaming_type: Optional[str] = None,   # noqa: ARG001 — reserved; stream kind is always downtime
    writer_class: Optional[str] = None,
) -> str:
    """Classify one attributed freeze actor into a safety class (see module docstring)."""
    ak = (action_kind or "").strip().upper()
    if ak == _KIND_STOP_STREAM:
        return SAFETY_DOWNTIME
    if ak == _KIND_FREEZE_COMPACTION:
        actor = (actor_kind or "").strip().upper()
        if actor == _ACTOR_PO:
            return SAFETY_WONT_RUN
        if actor == _ACTOR_JOB:
            return SAFETY_SAFE_WINDOW
        if actor in _ADHOC_ACTORS:
            return SAFETY_MANUAL
        return SAFETY_UNKNOWN
    if ak == _KIND_STOP_WRITER_JOB:
        wc = (writer_class or "").strip().upper()
        if wc == "EVENT_JOB":
            return SAFETY_DOWNTIME
        if wc == "SCHEDULED_JOB":
            return SAFETY_SAFE_WINDOW
        if wc in _ADHOC_WRITERS:
            return SAFETY_MANUAL
        return SAFETY_UNKNOWN
    return SAFETY_UNKNOWN


def downtime_read(entities: Iterable[dict], *, freeze_window_min: float) -> dict:
    """Aggregate classified freeze entities into the operator downtime read.

    ``entities`` rows carry ``safety_class`` (already classified) and ``entity_name``.
    ``n_safe`` = SAFE_WINDOW; ``n_wont_run`` = WONT_RUN; ``n_heads_up`` = DOWNTIME (continuous
    writers stopped until unfreeze); ``n_manual`` = MANUAL; ``n_unknown`` = the rest.
    ``heads_up_names`` = the DOWNTIME entities' names (what the operator must warn consumers about).
    """
    rows = list(entities or [])
    def _count(cls: str) -> int:
        return sum(1 for r in rows if (r.get("safety_class") or "").strip().upper() == cls)
    heads_up_names = [
        r.get("entity_name")
        for r in rows
        if (r.get("safety_class") or "").strip().upper() == SAFETY_DOWNTIME and r.get("entity_name")
    ]
    return {
        "freeze_window_min": float(freeze_window_min or 0.0),
        "n_safe": _count(SAFETY_SAFE_WINDOW),
        "n_heads_up": _count(SAFETY_DOWNTIME),
        "n_wont_run": _count(SAFETY_WONT_RUN),
        "n_manual": _count(SAFETY_MANUAL),
        "n_unknown": _count(SAFETY_UNKNOWN),
        "heads_up_names": heads_up_names,
    }
