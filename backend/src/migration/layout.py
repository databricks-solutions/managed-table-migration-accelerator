"""Table-layout detector — pure parsers for DESCRIBE DETAIL / DESCRIBE TABLE EXTENDED AS JSON.

Feeds the three auto-evolved manifest layout columns (``partition_columns``,
``clustering_columns``, ``cluster_by_auto``) that the M3 auto-liquid eligibility gate
(:func:`migration.optimization.auto_liquid_eligibility`) reads. No Spark at import time — the thin
edges live in ``notebooks/00_profiler.py`` (the profiler already reads ``DESCRIBE DETAIL`` per
table) and ``executor.read_cluster_by_auto_for_table`` (post-``CLUSTER BY AUTO`` verification).

Degrade honestly: a missing/unreadable field parses to ``None`` (NULL in the manifest) so the
eligibility gate treats it as unknown -> MANUAL, never as a clean layout.
"""

from __future__ import annotations

import json
from typing import Optional

LAYOUT_MANIFEST_COLUMNS: tuple[str, ...] = (
    "partition_columns",
    "clustering_columns",
    "cluster_by_auto",
)


def _as_str_list(value) -> Optional[list]:
    """A list-of-strings when ``value`` is a list/tuple (possibly empty); else ``None``."""
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x is not None and str(x) != ""]
    return None


def parse_describe_detail_layout(detail: Optional[dict]) -> dict:
    """Extract ``partition_columns`` / ``clustering_columns`` from a DESCRIBE DETAIL row dict.

    Missing keys / a ``None`` detail -> ``None`` (unknown). An empty list stays ``[]`` (known: the
    table has no partition/clustering columns) so the gate can distinguish "no keys" from "unread".
    """
    d = detail or {}
    return {
        "partition_columns": _as_str_list(d.get("partitionColumns")),
        "clustering_columns": _as_str_list(d.get("clusteringColumns")),
    }


def _find_cluster_by_auto(obj) -> Optional[bool]:
    """Recursively locate a boolean ``clusterByAuto`` in a parsed JSON structure."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if str(key) == "clusterByAuto" and isinstance(val, bool):
                return val
            found = _find_cluster_by_auto(val)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_cluster_by_auto(item)
            if found is not None:
                return found
    return None


def _table_property_cluster_by_auto(obj) -> Optional[bool]:
    """``clusterByAuto`` from the ``table_properties`` map, where the platform reports it.

    Table properties are strings: ``"true"`` / ``"false"`` (any case) -> bool. A properties map
    without the key means the table was never put on automatic clustering -> ``False``. Any other
    value is unknown -> ``None``. Returns ``None`` too when there is no properties map to read.
    """
    props = obj.get("table_properties") if isinstance(obj, dict) else None
    if not isinstance(props, dict):
        return None
    if "clusterByAuto" not in props:
        return False
    val = props["clusterByAuto"]
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def parse_cluster_by_auto(extended_json) -> Optional[bool]:
    """Best-effort ``clusterByAuto`` from ``DESCRIBE TABLE EXTENDED … AS JSON``.

    Accepts a dict (already parsed) or a JSON string. The platform reports the flag as the string
    property ``table_properties.clusterByAuto`` (``"true"`` / ``"false"``); a properties map
    without it means automatic clustering was never turned on (``False``). Without a properties
    map, falls back to a boolean ``clusterByAuto`` anywhere in the document. Returns ``None`` when
    the flag cannot be determined or the string is not valid JSON (degrade honestly).
    """
    obj = extended_json
    if obj is None:
        return None
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(obj, dict) and isinstance(obj.get("table_properties"), dict):
        return _table_property_cluster_by_auto(obj)
    return _find_cluster_by_auto(obj)


def parse_columns_and_props(extended_json):
    """``(columns, table_properties)`` from ``DESCRIBE TABLE EXTENDED … AS JSON``.

    ``columns`` is the list of ``{"name", "type": {"name", ...}}`` entries; ``table_properties``
    the string map. Either is ``None`` when absent or unreadable.
    """
    obj = extended_json
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:  # noqa: BLE001
            return None, None
    if not isinstance(obj, dict):
        return None, None
    cols = obj.get("columns")
    props = obj.get("table_properties")
    return (cols if isinstance(cols, list) else None), (props if isinstance(props, dict) else None)


def live_layout(detail: Optional[dict], extended_json) -> dict:
    """A table's current layout from ``DESCRIBE DETAIL`` plus ``DESCRIBE TABLE EXTENDED … AS JSON``.

    Same three keys as the manifest layout columns. Any part that could not be read stays ``None``
    (unknown), so a caller can fall back to the manifest field by field.
    """
    out = dict(parse_describe_detail_layout(detail))
    out["cluster_by_auto"] = parse_cluster_by_auto(extended_json)
    return out


def build_manifest_layout_update(
    full_name: str, detail: Optional[dict], cluster_by_auto: Optional[bool]
) -> dict:
    """The manifest column-update dict for one table's layout (UPDATE-only MERGE)."""
    parsed = parse_describe_detail_layout(detail)
    return {
        "full_name": full_name,
        "partition_columns": parsed["partition_columns"],
        "clustering_columns": parsed["clustering_columns"],
        "cluster_by_auto": cluster_by_auto,
    }
