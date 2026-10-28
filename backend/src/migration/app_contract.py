"""Before/after contract snapshots of the deployed app (pure; no network).

``backend/.e2e/e2e_app_contract.py`` reads every read-only app route and MCP read tool, passes
each body through :func:`normalize`, and stores the result. :func:`compare` then reports every
difference between two snapshots, so a dependency upgrade can prove the app still returns the
same data on the same estate.

Two modes per entry:

- ``value``: keys and values are kept, except volatile ones (timestamps, run ids, run URLs,
  durations, refresh tokens), which become their type, and ISO timestamps anywhere, which become
  ``<timestamp>``.
- ``shape``: every scalar becomes its type and a list becomes the sorted set of its element
  shapes, plus a row-count band (``0``, ``1`` or ``many``). Used where rows legitimately change
  between captures (run lists, query rows) but their columns, types and nullability must not.
"""
from __future__ import annotations

import json
import re
from typing import Any

VALUE = "value"
SHAPE = "shape"
MODES = (VALUE, SHAPE)

# Keys whose values change from one capture to the next on an unchanged estate.
_VOLATILE_EXACT = frozenset({
    "as_of", "run_id", "run_page_url", "start_time", "end_time", "duration", "elapsedms",
    "elapsed_ms", "refresh_token", "request_id", "now", "latest_run_id",
})
_VOLATILE_SUFFIX = ("_at", "_ts", "_time", "_timestamp", "_ms")
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def is_volatile_key(key: str) -> bool:
    k = key.lower()
    return k in _VOLATILE_EXACT or k.endswith(_VOLATILE_SUFFIX)


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def row_band(n: int) -> str:
    return "0" if n == 0 else "1" if n == 1 else "many"


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        elems = sorted({json.dumps(_shape(v), sort_keys=True) for v in value})
        return {"<list>": [json.loads(e) for e in elems], "<rows>": row_band(len(value))}
    return f"<{type_name(value)}>"


def _value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (f"<{type_name(value[k])}>" if is_volatile_key(k) else _value(value[k]))
                for k in sorted(value)}
    if isinstance(value, list):
        return [_value(v) for v in value]
    if isinstance(value, str) and _ISO_TS.match(value):
        return "<timestamp>"
    return value


def normalize(value: Any, mode: str = VALUE) -> Any:
    """The stable form of a response body for ``mode`` (``value`` or ``shape``)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    return _shape(value) if mode == SHAPE else _value(value)


def entry(status: int, body: Any, mode: str = VALUE) -> dict:
    """One snapshot entry: the HTTP (or tool) status, the mode and the normalised body."""
    return {"status": status, "mode": mode, "body": normalize(body, mode)}


def _diff(a: Any, b: Any, path: str, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                out.append(f"{p}: added {json.dumps(b[k], sort_keys=True)[:200]}")
            elif k not in b:
                out.append(f"{p}: removed {json.dumps(a[k], sort_keys=True)[:200]}")
            else:
                _diff(a[k], b[k], p, out, limit)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out, limit)
        return
    if a != b:
        out.append(f"{path}: {json.dumps(a, sort_keys=True)[:200]} -> {json.dumps(b, sort_keys=True)[:200]}")


def diff(a: Any, b: Any, *, limit: int = 50) -> list[str]:
    """Every difference between two normalised values, as ``path: before -> after`` lines."""
    out: list[str] = []
    _diff(a, b, "", out, limit)
    return out


def compare(before: dict, after: dict, *, limit_per_entry: int = 20) -> list[str]:
    """Differences between two snapshots (``{"entries": {name: entry}, "checks": {name: bool}}``).

    An entry missing from either side, a different status or mode, or any body difference is
    reported. A check that passed before and does not pass after is a regression; a check that
    newly passes is not.
    """
    out: list[str] = []
    eb, ea = before.get("entries", {}), after.get("entries", {})
    for name in sorted(set(eb) | set(ea)):
        if name not in ea:
            out.append(f"{name}: missing after")
            continue
        if name not in eb:
            out.append(f"{name}: new after (no baseline)")
            continue
        x, y = eb[name], ea[name]
        if x.get("status") != y.get("status"):
            out.append(f"{name}: status {x.get('status')} -> {y.get('status')}")
        if x.get("mode") != y.get("mode"):
            out.append(f"{name}: mode {x.get('mode')} -> {y.get('mode')}")
            continue
        out.extend(f"{name}: {d}" for d in diff(x.get("body"), y.get("body"), limit=limit_per_entry))
    cb, ca = before.get("checks", {}), after.get("checks", {})
    for name in sorted(cb):
        if cb[name] is True and ca.get(name) is not True:
            out.append(f"check {name}: passed before, {'missing' if name not in ca else 'fails'} after")
    return out


_PARAM = re.compile(r"^--\s*@param\s+(\w+)\s+(\w+)(?:\s*=\s*(.*))?$")


def query_params(sql_text: str) -> list[tuple[str, str, str | None]]:
    """``(name, type, default)`` for each ``-- @param`` header line of an app query file."""
    out = []
    for line in sql_text.splitlines():
        m = _PARAM.match(line.strip())
        if m:
            default = m.group(3)
            out.append((m.group(1), m.group(2).upper(), default.strip() if default is not None else None))
    return out


_NUMBER_TYPES = frozenset({"INT", "BIGINT", "FLOAT", "DOUBLE", "NUMERIC"})


def query_parameters(sql_text: str, *, catalog: str, schema: str) -> dict:
    """The analytics-route ``parameters`` the UI sends for a query: catalog and schema from the
    app config, an empty refresh token, and every other declared param at its default (empty
    string when it has none), as AppKit SQL markers."""
    params = {}
    for name, typ, default in query_params(sql_text):
        value = {"catalog": catalog, "schema": schema, "refresh_token": ""}.get(name, default or "")
        kind = typ if typ in _NUMBER_TYPES or typ == "BOOLEAN" else "STRING"
        if kind in _NUMBER_TYPES and value == "":
            continue
        params[name] = {"__sql_type": kind, "value": str(value)}
    return params


def sse_result(text: str) -> tuple[str, Any]:
    """The outcome of one analytics SSE stream: ``("result", rows)``, ``("error", message)``, or
    ``("empty", None)`` when no result or error event arrived."""
    error = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "result":
            return "result", msg.get("data")
        if msg.get("type") == "error" or "error" in msg:
            error = msg.get("error") or msg.get("message") or msg
    return ("error", error) if error is not None else ("empty", None)
