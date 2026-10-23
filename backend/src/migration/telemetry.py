"""Opt-out usage telemetry: closed-vocabulary signals on the SDK ``User-Agent``.

**What this sends.** Nothing to any Databricks-owned endpoint. A signal is a set of
``key/value`` extras on the ``User-Agent`` of one cheap call (``current_user.me()``) to the
customer's *own* workspace control plane, which already logs every request it receives.
The same mechanism as Databricks Labs DQX/UCX and Genie Workbench.

**What it may contain.** Only values this toolkit defines itself: stage ids, modes,
outcomes, selector kinds, manifest states, blocker tokens, action kinds, authority
verdicts, reader-readiness / path-scan / writer-schedule verdicts, wave buckets, freeze
op kinds, gate kinds, Databricks error classes, banded counts and a random per-run nonce.
Every key and value is checked against a closed vocabulary (or a band) and the SDK's
``User-Agent`` character set before it is sent; anything else is dropped or counted as
``other``. It NEVER contains catalog/schema/table names, paths or locations, share or
recipient names, job/pipeline/notebook names or ids, identities (including the
``operator`` parameter), SQL text, error messages or any free-text field.

**Opt out** with the ``telemetry_enabled`` bundle variable (``false``), which reaches every
stage notebook as a deploy-fixed parameter, or ``UC_EXT2MANAGED_TELEMETRY=false`` in the
environment. When the preference cannot be read, nothing is sent.

**Telemetry, not control flow.** Like ``gate_eval.try_append_gate_eval_rows``: every edge is
best-effort and never raises, the ping is bounded by a short timeout, and a failure is
logged and swallowed so it can never change, delay for long, or mask a stage's outcome.

This module is PURE at import (no Spark, no SDK). The only SDK/Spark touchpoints are the
thin edges at the bottom (``activate`` / ``send`` / ``start`` / ``estate_snapshot``).
"""

from __future__ import annotations

import os
import re
import secrets
import time
from typing import Callable, Iterable, Mapping, Optional, Sequence

import uc_ext2managed as _product

from migration import actions as _ac
from migration import audit as _au
from migration import authority as _auth
from migration import compat as _cp
from migration import conductor as _cd
from migration import convert as _cv
from migration import eligibility as _el
from migration import executor as _ex
from migration import external_writes as _xw
from migration import gate_eval as _ge
from migration import job_schedule as _js
from migration import lineage as _ln
from migration import locations as _loc
from migration import manifest as _mf
from migration import parallelism as _par
from migration import path_access as _pa
from migration import reconcile as _rc

PRODUCT = _product.PRODUCT
VERSION = _product.__version__

KEY_PREFIX = "e2m."
OTHER = "other"

WIDGET = "telemetry_enabled"
ENV_SWITCH = "UC_EXT2MANAGED_TELEMETRY"
_FALSY = frozenset({"false", "0", "no", "off"})

PING_TIMEOUT_SECONDS = 5
SEND_DEADLINE_SECONDS = 10
MAX_UA_EXTRA_CHARS = 900
MAX_CHUNKS = 6

# The SDK's own User-Agent character set (databricks.sdk.useragent.alphanum_pattern). Checked
# here because Config.with_user_agent_extra does NOT validate, and a newline in a header value
# makes the HTTP client reject the request outright.
_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_.+-]{1,96}$")
# A Databricks error class (optionally with a sub-class), e.g.
# DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED. "/" is mapped to "+" first.
_ERROR_CLASS_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}(?:[.+][A-Z][A-Z0-9_]{2,80}){0,2}$")
# A Python exception class name (a code identifier, never data).
_EXC_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_CHECK_RE = re.compile(r"^[0-9]{1,3}$")

# ---------------------------------------------------------------------------------------------
# Closed vocabularies (imported from their owning modules; never copied)
# ---------------------------------------------------------------------------------------------

STAGES = (
    "00_profiler",
    "02_wave_planner",
    "03_dry_run",
    "03c_freeze",
    "03d_lease_ownership",
    "04_convert",
    "05_validate",
    "05b_unfreeze_verify",
    "06_rollback",
    "07_post_convert_cleanup",
    "08_reshare",
    "08b_enable_compat",
    "08c_enable_optimization",
    "11_bulk_cockpit",
    "99_setup",
    "install",
)
MODE_DRY_RUN = "dry_run"
MODE_EXECUTE = "execute"
MODES = (MODE_DRY_RUN, MODE_EXECUTE)

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"     # the stage ran and some item failed (fail-loud raise follows)
OUTCOME_ERROR = "error"       # the stage died before reporting (uncaught exception)
OUTCOME_NOOP = "noop"         # nothing in scope / already done
OUTCOME_REFUSED = "refused"   # a guard refused to act (selector, gate, confirmation)
OUTCOMES = (OUTCOME_OK, OUTCOME_FAILED, OUTCOME_ERROR, OUTCOME_NOOP, OUTCOME_REFUSED)

SELECTORS = ("schema", "bucket", "wave", "batch", "table", "none")

COUNT_METRICS = (
    "batches", "converted", "discovered", "failed", "frozen", "hint_fallback", "hinted", "manual",
    "ops", "pending", "planned", "reconciled", "refused", "restored", "rolled_back", "skipped",
    "switched", "targets", "validated", "verified", "waves",
)

# Literal blocker tokens written to the manifest ``blockers`` CSV that have no constant in their
# owning module, plus the convert/rollback/cleanup failure markers. Registered here (not added to
# the upstream modules) and guarded by test_notebook_wiring_telemetry.py so a new literal fails CI.
_LITERAL_BLOCKERS = frozenset({
    "non_delta_format",
    "recent_compaction_activity",
    "recent_write_activity",
    "recent_vacuum_activity",
    "rollback_failed",
    "VERIFICATION_FAILED",
    "ROLLBACK_FAILED",
    "VACUUM_FAILED",
    "REVOKE_FAILED",
    "RESTORE_FAILED",
})


def _constants(module, prefix: str) -> frozenset:
    return frozenset(
        v for n, v in vars(module).items() if n.startswith(prefix) and isinstance(v, str)
    )


# The Databricks error classes classify_convert_error can return (it writes them to blockers).
CONVERT_ERROR_CLASSES = frozenset({
    "DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR",
    "DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED",
    "DELTA_TRUNCATED_TRANSACTION_LOG/DELTA_TXN_LOG_FAILED_INTEGRITY",
    "DELTA_STATE_RECOVER_ERRORS",
    "UNKNOWN",
})

BLOCKER_TOKENS = frozenset(
    set(_loc.DRY_RUN_OWNED_BLOCKERS)
    | _constants(_el, "BLOCKER_")
    | _constants(_xw, "BLOCKER_")
    | _constants(_rc, "BLOCKER_")
    | _constants(_pa, "BLOCKER_")
    | {_cv.BLOCKER_UNEXPECTED_TABLE_TYPE}
    | CONVERT_ERROR_CLASSES
    | _LITERAL_BLOCKERS
)

STATES = _constants(_au, "STATE_")
AUTHORITY_STATUSES = frozenset(_auth.ALL_STATUSES)
READER_STATES = frozenset(_cp.READER_STATES)
SCAN_STATUSES = frozenset(_pa.SCAN_STATUSES) | frozenset(_pa.REDIRECT_VERDICTS)
SCHEDULE_VERDICTS = _constants(_js, "VERDICT_")
BUCKETS = frozenset(_ln.ALL_BUCKETS)
OP_KINDS = _constants(_ex, "OP_") | _constants(_ex, "RESTORE_")
# The persisted gate kinds plus the 04_convert refusals that have no gate_eval row (fixed names,
# recorded with StageRun.trip just before the matching raise).
LOCAL_GATES = frozenset({
    "RUNTIME", "PLAN_DRIFT", "STREAMING_LEAK", "PRE_CONVERT_ACTIONS", "MAX_TABLES",
    "OWNING_WORKSPACE", "STREAMING_PLAN_UNAVAILABLE",
})
GATE_KINDS = frozenset(_ge.ALL_GATE_KINDS) | LOCAL_GATES
ACTION_KINDS = frozenset(_ac.ALL_KINDS)
CHECK_STATUSES = frozenset({"FAIL", "WARN"})

# token category -> membership test. Values outside the vocabulary are counted as ``other``.
TOKEN_CATEGORIES: dict[str, Callable[[str], bool]] = {
    "blk": BLOCKER_TOKENS.__contains__,
    "st": STATES.__contains__,
    "auth": AUTHORITY_STATUSES.__contains__,
    "rdr": READER_STATES.__contains__,
    "scan": SCAN_STATUSES.__contains__,
    "wsv": SCHEDULE_VERDICTS.__contains__,
    "bkt": BUCKETS.__contains__,
    "op": OP_KINDS.__contains__,
    "gate": GATE_KINDS.__contains__,
    # Databricks error classes are Databricks' own vocabulary; accepted when structurally valid.
    "err": lambda t: t in CONVERT_ERROR_CLASSES or error_class_token(t) is not None,
    # Dry-run checks: "<check_num>.<FAIL|WARN>".
    "chk": lambda t: (
        "." in t
        and bool(_CHECK_RE.match(t.split(".", 1)[0]))
        and t.split(".", 1)[1] in CHECK_STATUSES
    ),
}

# Closed single-valued extras (key -> allowed values) and boolean flags.
EXTRA_VOCAB: dict[str, frozenset] = {
    "bucket": BUCKETS,
    "cockpit_action": frozenset({"show", "advance"}),  # 11_bulk_cockpit `action` dropdown
    "finish_action": _constants(_ac, "FINISH_"),
    "step": frozenset(_cd.ALL_STEP_KINDS) | _constants(_par, "STEP_BULK_"),
}
BOOL_FLAGS = frozenset({
    "app_bound", "confirm_convert", "discovery_error", "enable_po", "force_vacuum", "genie",
    "location_supplied", "open_apis", "restore_prior_state", "streaming", "truncate_uniform",
    "warehouse_set",
})

# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------


def safe_token(value) -> Optional[str]:
    """``value`` as a User-Agent-safe token, or ``None`` (``/`` maps to ``+``)."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().replace("/", "+")
    return s if _TOKEN_RE.match(s) else None


def band(n) -> Optional[str]:
    """A count as a coarse band: ``0 | 1-9 | 10-99 | 100-999 | 1k-9k | 10k+``.

    ``None``, a bool, a negative or a non-integer gives ``None`` (no signal).
    """
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    v = n
    if v == 0:
        return "0"
    if v < 10:
        return "1-9"
    if v < 100:
        return "10-99"
    if v < 1_000:
        return "100-999"
    if v < 10_000:
        return "1k-9k"
    return "10k+"


_TIB = 1024 ** 4


def tb_band(size_bytes) -> Optional[str]:
    """Total bytes as a TB band: ``lt1tb | 1-10tb | 10-100tb | 100tb-1pb | 1pb+``."""
    if size_bytes is None or isinstance(size_bytes, bool):
        return None
    try:
        b = float(size_bytes)
    except (TypeError, ValueError):
        return None
    if b < 0 or b != b:  # negative or NaN
        return None
    tib = b / _TIB
    if tib < 1:
        return "lt1tb"
    if tib < 10:
        return "1-10tb"
    if tib < 100:
        return "10-100tb"
    if tib < 1024:
        return "100tb-1pb"
    return "1pb+"


def error_class_token(value) -> Optional[str]:
    """A structurally valid Databricks error class as a token, else ``None``."""
    if not isinstance(value, str):
        return None
    s = value.strip().replace("/", "+")
    return s if _ERROR_CLASS_RE.match(s) else None


def error_class(exc) -> Optional[str]:
    """The structured Databricks error class of ``exc``, never parsed from its message.

    Precedence: ``getCondition()`` (PySpark 4) > ``getErrorClass()`` > ``error_code`` (SDK).
    """
    if exc is None:
        return None
    for attr in ("getCondition", "getErrorClass"):
        fn = getattr(exc, attr, None)
        if callable(fn):
            try:
                tok = error_class_token(fn())
            except Exception:  # noqa: BLE001 — a broken accessor is just "no class"
                tok = None
            if tok:
                return tok
    return error_class_token(getattr(exc, "error_code", None))


def exception_type(exc) -> Optional[str]:
    """The exception's class name when it is a plain identifier, else ``None``."""
    if exc is None:
        return None
    name = type(exc).__name__
    return name if _EXC_TYPE_RE.match(name) else None


def new_nonce() -> str:
    """A random 8-hex per-run id that stitches chunked pings together (no meaning)."""
    return secrets.token_hex(4)


def parse_enabled(raw) -> bool:
    """The opt-out switch: only ``false``/``0``/``no``/``off`` disable. Blank means on."""
    if raw is None:
        return True
    return str(raw).strip().lower() not in _FALSY


def tally(rows, key: Optional[str] = None, *, fn: Optional[Callable] = None) -> dict:
    """``{value: count}`` over ``rows`` by ``key`` (dict or Row) or ``fn(row)``. NEVER raises.

    The notebook-side aggregation helper: a stage's ``TM.finish(...)`` arguments are evaluated in
    the notebook, so they must not be able to raise and turn a passing stage into a failure (or
    replace a failing stage's real error). ``None`` values and rows that cannot be read are skipped.
    """
    out: dict = {}
    try:
        for row in rows or ():
            try:
                if fn is not None:
                    value = fn(row)
                elif isinstance(row, Mapping):
                    value = row.get(key)
                else:
                    value = row[key]
            except Exception:  # noqa: BLE001 — an unreadable row is just not counted
                continue
            if value is None:
                continue
            try:
                out[value] = out.get(value, 0) + 1
            except TypeError:  # unhashable value
                continue
    except Exception:  # noqa: BLE001 — a non-iterable / exploding iterator yields what we have
        pass
    return out


def size(rows) -> Optional[int]:
    """``len(rows)`` or ``None`` (no signal) when it cannot be taken. NEVER raises."""
    try:
        return len(rows)
    except Exception:  # noqa: BLE001
        return None


def split_blockers(csv) -> list[str]:
    """Split a manifest ``blockers`` CSV the way ``locations`` writes it (``", "``)."""
    if not csv:
        return []
    return [p.strip() for p in str(csv).split(",") if p.strip()]


def blocker_histogram(rows: Iterable[tuple]) -> dict[str, int]:
    """``{token: tables}`` from ``(blockers_csv, n_tables)`` rows (pure aggregation)."""
    hist: dict[str, int] = {}
    try:
        for csv, n in rows:
            try:
                for tok in set(split_blockers(csv)):
                    hist[tok] = hist.get(tok, 0) + int(n or 0)
            except Exception:  # noqa: BLE001 — skip an unreadable row
                continue
    except Exception:  # noqa: BLE001 — never raises (called from notebook finish arguments)
        pass
    return hist


def _token_pairs(tokens: Optional[Mapping[str, Mapping[str, int]]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for category, counts in (tokens or {}).items():
        member = TOKEN_CATEGORIES.get(category)
        if member is None or not counts:
            continue
        merged: dict[str, int] = {}
        for tok, n in counts.items():
            tok_s = tok if isinstance(tok, str) else None
            key = tok_s if tok_s is not None and member(tok_s) else OTHER
            try:
                merged[key] = merged.get(key, 0) + int(n or 0)
            except (TypeError, ValueError):
                continue
        for tok, n in sorted(merged.items()):
            safe = error_class_token(tok) if category == "err" and tok != OTHER else safe_token(tok)
            b = band(n)
            if safe and b and b != "0":
                out.append((f"{KEY_PREFIX}{category}.{safe}", b))
    return out


def _count_pairs(counts: Optional[Mapping[str, int]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for metric, n in sorted((counts or {}).items()):
        b = band(n)
        if metric in COUNT_METRICS and b:
            out.append((f"{KEY_PREFIX}n.{metric}", b))
    return out


def _extra_pairs(extra: Optional[Mapping[str, object]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in sorted((extra or {}).items()):
        if key in BOOL_FLAGS and isinstance(value, bool):
            out.append((f"{KEY_PREFIX}f.{key}", "true" if value else "false"))
        elif key in EXTRA_VOCAB and isinstance(value, str) and value in EXTRA_VOCAB[key]:
            safe = safe_token(value)
            if safe:
                out.append((f"{KEY_PREFIX}{key}", safe))
    return out


def header_pairs(stage: str, run: str) -> list[tuple[str, str]]:
    """The pairs repeated in every chunk (stage + run nonce), or ``[]`` when invalid."""
    if stage not in STAGES or not (isinstance(run, str) and re.fullmatch(r"[0-9a-f]{8}", run)):
        return []
    return [(f"{KEY_PREFIX}stage", stage), (f"{KEY_PREFIX}run", run)]


def stage_signals(
    *,
    mode: Optional[str] = None,
    outcome: Optional[str] = None,
    selector: Optional[str] = None,
    counts: Optional[Mapping[str, int]] = None,
    tokens: Optional[Mapping[str, Mapping[str, int]]] = None,
    extra: Optional[Mapping[str, object]] = None,
) -> list[tuple[str, str]]:
    """The body signals of one stage run. Anything outside a vocabulary is dropped."""
    out: list[tuple[str, str]] = []
    if mode in MODES:
        out.append((f"{KEY_PREFIX}mode", mode))
    if outcome in OUTCOMES:
        out.append((f"{KEY_PREFIX}outcome", outcome))
    if selector in SELECTORS:
        out.append((f"{KEY_PREFIX}sel", selector))
    out += _extra_pairs(extra)
    out += _count_pairs(counts)
    out += _token_pairs(tokens)
    return out


def estate_signals(
    *,
    state_counts: Optional[Mapping[str, int]] = None,
    total_bytes=None,
    blocker_counts: Optional[Mapping[str, int]] = None,
    action_groups: Iterable[tuple] = (),
    authority_counts: Optional[Mapping[str, int]] = None,
    bucket_counts: Optional[Mapping[str, int]] = None,
) -> list[tuple[str, str]]:
    """Estate-shape signals from pre-aggregated counts (00 profiler / 02 planner only).

    ``action_groups`` are ``(action_kind, is_auto, n_open)`` tuples; the auto-vs-manual split
    comes from ``executor.is_auto_freezable_action`` in ``estate_snapshot``.
    """
    out: list[tuple[str, str]] = []
    total = sum(int(n or 0) for n in (state_counts or {}).values())
    if state_counts is not None:
        b = band(total)
        if b:
            out.append((f"{KEY_PREFIX}est.tables", b))
    tb = tb_band(total_bytes)
    if tb:
        out.append((f"{KEY_PREFIX}est.tb", tb))
    out += _token_pairs({
        "st": state_counts or {},
        "blk": blocker_counts or {},
        "auth": authority_counts or {},
        "bkt": bucket_counts or {},
    })
    merged: dict[str, int] = {}
    for kind, is_auto, n in action_groups:
        k = kind if kind in ACTION_KINDS else OTHER
        key = f"{k}.{'auto' if is_auto else 'manual'}"
        merged[key] = merged.get(key, 0) + int(n or 0)
    for key, n in sorted(merged.items()):
        b = band(n)
        if b and b != "0":
            out.append((f"{KEY_PREFIX}act.{key}", b))
    return out


BANDS = frozenset({"1-9", "10-99", "100-999", "1k-9k", "10k+"})  # "0" is never sent for tokens
TB_BANDS = frozenset({"lt1tb", "1-10tb", "10-100tb", "100tb-1pb", "1pb+"})
_SNAPSHOT_TOKEN_CATEGORIES = frozenset({"st", "blk", "auth", "bkt"})


def vetted_pairs(pairs: Iterable[tuple]) -> list[tuple[str, str]]:
    """Re-check pre-built pairs (``estate_signals`` output, the exception type) before sending.

    Defence in depth for the pairs ``StageRun.finish`` accepts ready-made: only the shapes this
    module produces survive — anything else is dropped, whatever the caller passed.
    """
    out: list[tuple[str, str]] = []
    for pair in pairs:
        try:
            key, value = pair
        except (TypeError, ValueError):
            continue
        if not (isinstance(key, str) and isinstance(value, str)):
            continue
        if safe_token(key) != key or safe_token(value) != value:
            continue
        ok = False
        if key == f"{KEY_PREFIX}est.tables":
            ok = value in BANDS or value == "0"
        elif key == f"{KEY_PREFIX}est.tb":
            ok = value in TB_BANDS
        elif key == f"{KEY_PREFIX}exc":
            ok = bool(_EXC_TYPE_RE.match(value))
        elif key.startswith(f"{KEY_PREFIX}act."):
            kind, _, split = key[len(f"{KEY_PREFIX}act."):].rpartition(".")
            ok = (kind in ACTION_KINDS or kind == OTHER) and split in ("auto", "manual") \
                and value in BANDS
        else:
            category, _, tok = key[len(KEY_PREFIX):].partition(".")
            if key.startswith(KEY_PREFIX) and category in _SNAPSHOT_TOKEN_CATEGORIES:
                ok = (tok == OTHER or TOKEN_CATEGORIES[category](tok)) and value in BANDS
        if ok:
            out.append((key, value))
    return out


def ua_length(pairs: Sequence[tuple[str, str]]) -> int:
    """Characters the pairs add to the User-Agent (``k/v`` joined by spaces)."""
    return sum(len(k) + len(v) + 2 for k, v in pairs)


def chunk_signals(
    header: Sequence[tuple[str, str]],
    body: Sequence[tuple[str, str]],
    *,
    max_len: int = MAX_UA_EXTRA_CHARS,
    max_chunks: int = MAX_CHUNKS,
) -> list[list[tuple[str, str]]]:
    """Split ``body`` into pings that each repeat ``header`` and carry ``e2m.chunk/<i>of<n>``.

    Every chunk stays within ``max_len`` characters; signals beyond ``max_chunks`` chunks are
    dropped (bounded load). ``[]`` when the header is empty (nothing attributable to send).
    """
    if not header:
        return []
    budget = max_len - ua_length(header) - len(f"{KEY_PREFIX}chunk/99of99") - 1
    if budget <= 0:
        return []
    groups: list[list[tuple[str, str]]] = [[]]
    used = 0
    for pair in body:
        size = ua_length([pair])
        if size > budget:
            continue
        if used + size > budget and groups[-1]:
            if len(groups) == max_chunks:
                break
            groups.append([])
            used = 0
        groups[-1].append(pair)
        used += size
    n = len(groups)
    return [
        list(header) + [(f"{KEY_PREFIX}chunk", f"{i}of{n}")] + g
        for i, g in enumerate(groups, start=1)
    ]


# ---------------------------------------------------------------------------------------------
# Thin edges (the only SDK / Spark / IPython code in this module). None of them ever raise.
# ---------------------------------------------------------------------------------------------

_ACTIVATED = False


def activate(enabled: bool) -> bool:
    """Stamp ``uc-ext2managed/<version>`` on every SDK client built after this, process-wide.

    Must run before any ``WorkspaceClient`` is constructed (the SDK computes the User-Agent
    when a client is built). A no-op when disabled or on an SDK without ``useragent``.
    """
    global _ACTIVATED
    if not enabled:
        return False
    try:
        from databricks.sdk import useragent

        useragent.with_extra(PRODUCT, VERSION)
        _ACTIVATED = True
    except Exception:  # noqa: BLE001 — an old SDK just means no process-wide stamp
        return False
    return True


def _default_ws_factory():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def _default_ping(client) -> None:
    client.current_user.me()


def _log(logger, msg: str) -> None:
    try:
        logger(msg)
    except Exception:  # noqa: BLE001 — even the log must not raise
        pass


def send(
    chunks: Sequence[Sequence[tuple[str, str]]],
    *,
    ws_factory: Optional[Callable[[], object]] = None,
    ping: Callable[[object], None] = _default_ping,
    logger=print,
    timeout: int = PING_TIMEOUT_SECONDS,
    deadline: float = SEND_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Send each chunk as the User-Agent of one cheap call. Never raises; ``True`` if all sent.

    Each call is bounded by ``timeout`` (retry window and HTTP timeout) and the whole send by
    ``deadline``; the first failure stops the send (one stalled ping, never one per chunk).
    """
    if not chunks:
        return False
    t0 = clock()
    try:
        ws = (ws_factory or _default_ws_factory)()
        for chunk in chunks:
            if clock() - t0 > deadline:
                _log(logger, "telemetry: deadline reached; remaining signals dropped")
                return False
            cfg = ws.config.copy()
            if not _ACTIVATED:
                cfg.with_user_agent_extra(PRODUCT, VERSION)
            for key, value in chunk:
                if safe_token(key) == key and safe_token(value) == value:
                    cfg.with_user_agent_extra(key, value)
            cfg.retry_timeout_seconds = timeout
            cfg.http_timeout_seconds = timeout
            ping(type(ws)(config=cfg))
        return True
    except Exception as exc:  # noqa: BLE001 — telemetry must never surface in a stage
        _log(logger, f"telemetry: not sent ({type(exc).__name__})")
        return False


def read_enabled(dbutils) -> bool:
    """Read the opt-out switch. Unreadable means OFF (if in doubt, send nothing).

    The generic ``DO_NOT_TRACK=1`` (honoured by AppKit and the app) also turns it off.
    """
    if (os.environ.get("DO_NOT_TRACK") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if not parse_enabled(os.environ.get(ENV_SWITCH)):
        return False
    if dbutils is None:
        return False
    try:
        try:
            dbutils.widgets.text(WIDGET, "true", "Usage telemetry (false = opt out)")
        except Exception:  # noqa: BLE001 — already declared / not declarable
            pass
        return parse_enabled(dbutils.widgets.get(WIDGET))
    except Exception:  # noqa: BLE001
        return False


class StageRun:
    """One stage run's telemetry: a nonce, an "already sent" latch and the send itself."""

    def __init__(self, stage: str, *, enabled: bool, run: Optional[str] = None,
                 ws_factory=None, ping: Callable[[object], None] = _default_ping, logger=print):
        self.stage = stage
        self.run = run or new_nonce()
        self.enabled = bool(enabled) and stage in STAGES
        self.sent = False
        self.delivered = False  # True once every chunk of this run's signals was sent
        self._ws_factory = ws_factory
        self._ping = ping
        self._logger = logger
        self._gates: dict[str, int] = {}

    def trip(self, gate: str) -> None:
        """Record a gate about to refuse, so the error ping names it (no send)."""
        try:
            if gate in GATE_KINDS:
                self._gates[gate] = self._gates.get(gate, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def estate(self, spark, manifest_catalog: str, manifest_schema: str) -> list:
        """Estate-shape signals, read only when enabled (opted out = no extra queries). Never raises."""
        if not self.enabled or self.sent:
            return []
        try:
            return estate_snapshot(spark, manifest_catalog, manifest_schema, logger=self._logger)
        except Exception as exc:  # noqa: BLE001
            _log(self._logger, f"telemetry: estate read skipped ({type(exc).__name__})")
            return []

    def finish(
        self,
        outcome: str,
        *,
        mode: Optional[str] = None,
        selector: Optional[str] = None,
        counts: Optional[Mapping[str, int]] = None,
        tokens: Optional[Mapping[str, Mapping[str, int]]] = None,
        extra: Optional[Mapping[str, object]] = None,
        estate: Optional[Sequence[tuple[str, str]]] = None,
    ) -> None:
        """Send this run's signals once. Later calls (and the error hook) are no-ops.

        Returns ``None`` (it is often the last line of a notebook cell, which would otherwise echo
        the result); ``delivered`` records whether the send went through.
        """
        self._report(outcome, mode=mode, selector=selector, counts=counts, tokens=tokens,
                     extra=extra, estate=estate)

    def _report(self, outcome, *, mode=None, selector=None, counts=None, tokens=None, extra=None,
                estate=None) -> bool:
        if self.sent:
            return False
        self.sent = True
        if not self.enabled:
            return False
        try:
            merged = {k: dict(v) for k, v in (tokens or {}).items()}
            if self._gates:
                merged.setdefault("gate", {}).update(self._gates)
            body = stage_signals(mode=mode, outcome=outcome, selector=selector,
                                 counts=counts, tokens=merged, extra=extra)
            body += vetted_pairs(estate or [])
            chunks = chunk_signals(header_pairs(self.stage, self.run), body)
            self.delivered = send(chunks, ws_factory=self._ws_factory, ping=self._ping,
                                  logger=self._logger)
            return self.delivered
        except Exception as exc:  # noqa: BLE001
            _log(self._logger, f"telemetry: not sent ({type(exc).__name__})")
            return False

    def on_error(self, exc) -> bool:
        """Report a stage that died before ``finish``: type + error class, never the message."""
        if self.sent:
            return False
        tokens: dict[str, dict[str, int]] = {}
        cls = error_class(exc)
        if cls:
            tokens["err"] = {cls: 1}
        etype = exception_type(exc)
        extra_body = [(f"{KEY_PREFIX}exc", etype)] if etype else []
        return self._report(OUTCOME_ERROR, tokens=tokens, estate=extra_body)


_HOOK: Optional[Callable] = None

# dbutils.notebook.exit() surfaces in post_run_cell as an error_in_exec of this type (observed on
# serverless jobs). It is a normal end of a notebook, never a failure, so the hook ignores it; the
# wiring guard makes every stage call ``finish`` before it exits.
_EXIT_EXCEPTION_TYPES = frozenset({"NotebookExit", "SystemExit"})


def on_cell_result(run: StageRun, result) -> bool:
    """The ``post_run_cell`` handler: report an uncaught error, ignore a notebook exit."""
    try:
        err = getattr(result, "error_in_exec", None)
        if err is None or type(err).__name__ in _EXIT_EXCEPTION_TYPES:
            return False
        return run.on_error(err)
    except Exception:  # noqa: BLE001
        return False


def _install_error_hook(run: StageRun) -> None:
    """Send ``outcome=error`` if a cell raises before ``finish`` (replaces any earlier hook)."""
    global _HOOK
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is None:
            return
        if _HOOK is not None:
            try:
                ip.events.unregister("post_run_cell", _HOOK)
            except Exception:  # noqa: BLE001
                pass

        def _hook(result):
            on_cell_result(run, result)

        ip.events.register("post_run_cell", _hook)
        _HOOK = _hook
    except Exception:  # noqa: BLE001 — no IPython, no hook
        pass


def start(stage: str, dbutils=None, *, logger=print, ws_factory=None,
          register_hook: bool = True) -> StageRun:
    """Begin a stage run's telemetry. Call in the import cell, before any SDK client exists.

    Never raises: any failure yields a disabled run whose ``finish`` does nothing.
    """
    try:
        enabled = read_enabled(dbutils)
        activate(enabled)
        run = StageRun(stage, enabled=enabled, ws_factory=ws_factory, logger=logger)
        if run.enabled and register_hook:
            _install_error_hook(run)
        return run
    except Exception:  # noqa: BLE001
        return StageRun(stage, enabled=False, logger=logger)


def estate_snapshot(spark, manifest_catalog: str, manifest_schema: str, *, logger=print) -> list:
    """Estate-shape signals from the control tables (four small aggregates, each best-effort).

    Only counts leave this function — no row-level value (name, path, id) is ever returned.
    """
    base = f"`{manifest_catalog}`.`{manifest_schema}`"
    state_counts: Optional[dict] = None
    total_bytes = None
    blocker_counts: dict = {}
    action_groups: list = []
    authority_counts: dict = {}

    def _rows(sql):
        try:
            return spark.sql(sql).collect()
        except Exception as exc:  # noqa: BLE001 — a missing table just means no signal
            _log(logger, f"telemetry: estate read skipped ({type(exc).__name__})")
            return None

    rows = _rows(
        f"SELECT state, COUNT(*) AS n, SUM(table_size_bytes) AS b "
        f"FROM {base}.{_mf.MANIFEST_TABLE} GROUP BY state"
    )
    if rows is not None:
        state_counts = {r["state"]: r["n"] for r in rows if r["state"]}
        total_bytes = sum(int(r["b"] or 0) for r in rows)
    rows = _rows(
        f"SELECT blockers, COUNT(*) AS n FROM {base}.{_mf.MANIFEST_TABLE} "
        f"WHERE blockers IS NOT NULL AND blockers <> '' GROUP BY blockers"
    )
    if rows is not None:
        blocker_counts = blocker_histogram((r["blockers"], r["n"]) for r in rows)
    rows = _rows(
        f"SELECT action_kind, scope, target_kind, target_id IS NOT NULL AS has_target, "
        f"COUNT(*) AS n FROM {base}.{_ac.ACTIONS_TABLE} "
        f"WHERE status IN ('{_ac.STATUS_OPEN}', '{_ac.STATUS_IN_PROGRESS}') "
        f"GROUP BY action_kind, scope, target_kind, target_id IS NOT NULL"
    )
    for r in rows or []:
        probe = {"action_kind": r["action_kind"], "scope": r["scope"],
                 "target_kind": r["target_kind"], "target_id": "x" if r["has_target"] else None}
        try:
            is_auto = _ex.is_auto_freezable_action(probe)
        except Exception:  # noqa: BLE001
            is_auto = False
        action_groups.append((r["action_kind"], is_auto, r["n"]))
    rows = _rows(
        f"SELECT status, COUNT(*) AS n FROM ("
        f"SELECT status, ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn "
        f"FROM {base}.{_auth.CONVERT_AUTHORITY_TABLE}) WHERE rn = 1 GROUP BY status"
    )
    if rows is not None:
        authority_counts = {r["status"]: r["n"] for r in rows if r["status"]}
    return estate_signals(state_counts=state_counts, total_bytes=total_bytes,
                          blocker_counts=blocker_counts, action_groups=action_groups,
                          authority_counts=authority_counts)
