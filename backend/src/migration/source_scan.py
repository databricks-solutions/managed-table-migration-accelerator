"""Tier-2 source scanning: locate path literals in notebook/job source.

Tier 1 (``external_writes.collect_path_access_entities``) answers *who* path-accessed a
table from lineage. This module answers *exactly where* in the code: it scans exported
notebook/job source for storage-path literals and records the file, cell, and line number
so an operator can fix path-based access before conversion.

Pure, unit-testable logic only. The heavy I/O (exporting Workspace/Jobs source via the
Databricks APIs) lives in ``notebooks/01_source_scan.py`` and is OFF by default; that
export step is intended to be ported from the reference workspace-profiler
(see ``references/README.md``). These helpers take plain source text and return plain
Python so they run in CI without a cluster.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from .external_writes import is_path_based_access

# Storage-path schemes worth flagging as potential path-based access. Ordered longest /
# most-specific first so overlapping schemes (e.g. s3a vs s3) match deterministically.
PATH_LITERAL_PATTERNS: tuple[str, ...] = (
    r"abfss://[^\s'\"\)]+",
    r"abfs://[^\s'\"\)]+",
    r"wasbs://[^\s'\"\)]+",
    r"wasb://[^\s'\"\)]+",
    r"s3a://[^\s'\"\)]+",
    r"s3n://[^\s'\"\)]+",
    r"s3://[^\s'\"\)]+",
    r"gs://[^\s'\"\)]+",
    r"adl://[^\s'\"\)]+",
    r"dbfs:/[^\s'\"\)]+",
    r"file:/[^\s'\"\)]+",
    r"/mnt/[^\s'\"\)]+",
)

_COMBINED_RE = re.compile("|".join(f"(?:{p})" for p in PATH_LITERAL_PATTERNS))

# Notebook cell delimiters (Databricks SOURCE export format), per source language.
_PY_CELL_DELIM = re.compile(r"^#\s*COMMAND\s*-+")
_SQL_CELL_DELIM = re.compile(r"^--\s*COMMAND\s*-+")
_SCALA_CELL_DELIM = re.compile(r"^//\s*COMMAND\s*-+")

# path_access_locations control table.
PATH_ACCESS_LOCATIONS_TABLE = "path_access_locations"
PATH_ACCESS_LOCATIONS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),        # the table whose storage_location was matched
    ("entity_kind", "STRING"),      # NOTEBOOK | JOB | FILE
    ("entity_id", "STRING"),        # job id / object id (best-effort)
    ("entity_name", "STRING"),      # notebook path / job name (best-effort)
    ("source_path", "STRING"),      # workspace path of the scanned source
    ("cell_index", "INT"),          # 0-based notebook cell (NULL for non-notebook)
    ("line_number", "INT"),         # 1-based line within the source
    ("snippet", "STRING"),          # the matched line, trimmed
    ("matched_path", "STRING"),     # the storage-path literal found on that line
    ("confidence", "STRING"),       # high (literal under storage_location) | low
    ("updated_at", "TIMESTAMP"),
)
PATH_ACCESS_LOCATIONS_COLUMN_NAMES: tuple[str, ...] = tuple(
    c for c, _ in PATH_ACCESS_LOCATIONS_COLUMNS
)


def path_access_locations_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{PATH_ACCESS_LOCATIONS_TABLE}"


def create_path_access_locations_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in PATH_ACCESS_LOCATIONS_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{path_access_locations_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Pure scanning
# --------------------------------------------------------------------------- #

def find_path_literals(text: Optional[str]) -> list[dict]:
    """Return every storage-path literal in ``text`` as {line_number, snippet, path}.

    ``line_number`` is 1-based; one record per match (a line with two paths yields two).
    """
    if not text:
        return []
    out: list[dict] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for m in _COMBINED_RE.finditer(line):
            out.append({
                "line_number": idx,
                "snippet": line.strip(),
                "path": m.group(0),
            })
    return out


def cell_index_for_lines(text: Optional[str], lang: str = "python") -> list[int]:
    """Map each 1-based source line to its 0-based notebook cell index.

    Cells are delimited by ``# COMMAND ----------`` (python), ``-- COMMAND ----------``
    (sql), or ``// COMMAND ----------`` (scala). The returned list is indexed by
    ``line_number - 1``. Delimiter lines are attributed to the cell they open.
    Non-notebook source returns all-zeros.
    """
    if not text:
        return []
    lang_l = str(lang).lower()
    if lang_l == "sql":
        delim = _SQL_CELL_DELIM
    elif lang_l == "scala":
        delim = _SCALA_CELL_DELIM
    else:
        delim = _PY_CELL_DELIM
    cells: list[int] = []
    current = 0
    for line in text.splitlines():
        if delim.match(line):
            current += 1
            cells.append(current)
        else:
            cells.append(current)
    return cells


def lang_for_source(path: Optional[str], language: Optional[str] = None) -> str:
    """Return the cell-parsing lang token for a source: python | sql | scala | unknown.

    Mirrors the reference profiler's ``get_file_type`` but returns the token
    ``cell_index_for_lines`` expects. Prefers the explicit ``language`` (e.g. the
    Workspace API's ObjectInfo.language) and falls back to the file extension.
    """
    lang = (language or "").strip().upper()
    if lang == "PYTHON":
        return "python"
    if lang == "SQL":
        return "sql"
    if lang == "SCALA":
        return "scala"
    p = (path or "").lower()
    if p.endswith(".py"):
        return "python"
    if p.endswith(".sql"):
        return "sql"
    if p.endswith(".scala"):
        return "scala"
    return "unknown"


def extract_job_notebook_paths(job_settings: Optional[dict]) -> list[str]:
    """Notebook paths referenced by a job's tasks (deduped, order-preserving).

    Walks ``tasks[].notebook_task.notebook_path`` plus the nested
    ``tasks[].for_each_task.task.notebook_task.notebook_path``. Pure: pass
    ``job_details.settings.as_dict()`` from the Jobs SDK in the notebook.
    """
    out: list[str] = []
    seen: set = set()

    def _add(task: Optional[dict]) -> None:
        if not isinstance(task, dict):
            return
        nb = task.get("notebook_task")
        if isinstance(nb, dict):
            path = nb.get("notebook_path")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        fe = task.get("for_each_task")
        if isinstance(fe, dict):
            _add(fe.get("task"))

    for task in (job_settings or {}).get("tasks", []) or []:
        _add(task)
    return out


def job_ids_from_path_access_entities(serialized: Optional[str]) -> list[str]:
    """Distinct JOB entity ids from a tier-1 ``path_access_entities`` JSON string.

    Safe on ``None`` / malformed JSON / unexpected shapes (returns ``[]``). Used by the
    targeted scan mode to resolve only the jobs lineage attributed to path access.
    """
    if not serialized:
        return []
    try:
        entities = json.loads(serialized)
    except (ValueError, TypeError):
        return []
    if not isinstance(entities, list):
        return []
    out: list[str] = []
    seen: set = set()
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("entity_type") or "").strip().upper() != "JOB":
            continue
        eid = ent.get("entity_id")
        if eid is None:
            continue
        eid = str(eid)
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def attribute_to_table(
    matches: Optional[Iterable[dict]],
    storage_location: Optional[str],
) -> list[dict]:
    """Keep only matches whose path literal resolves to ``storage_location``.

    Reuses the tier-1 prefix rule (``is_path_based_access``) so the two tiers agree on
    what "under this table's location" means. Each kept match gets ``confidence='high'``.
    """
    if not storage_location:
        return []
    kept: list[dict] = []
    for m in matches or []:
        if is_path_based_access(m.get("path"), storage_location):
            kept.append({**m, "confidence": "high"})
    return kept


# --------------------------------------------------------------------------- #
# Deep-link builders (pure) — display-only provenance
# --------------------------------------------------------------------------- #
# Turn a scanned source_path / job id into a click-through so an operator can open the
# offending notebook (or job) and verify path-based access. Mirrors the app's
# workspaceLinks.pure.ts so the notebook output, the app panel, and the dashboard agree on
# the link shape (path-based, with an optional `?o=<workspace_id>` for the fully-qualified
# form). Pure: no Spark/SDK; the notebook passes the workspace host + id it reads from its
# runtime context. Every builder returns None when the host or target is missing.

def _ensure_scheme(host: Optional[str]) -> Optional[str]:
    """Normalize a workspace host to ``https://<host>`` (no trailing slash), or None.

    ``spark.conf.get("spark.databricks.workspaceUrl")`` returns a bare host (no scheme); a
    configured value may already carry one. Accept both.
    """
    if not host:
        return None
    h = host.strip().rstrip("/")
    if not h:
        return None
    return h if h.startswith(("http://", "https://")) else f"https://{h}"


def normalize_workspace_path(path: Optional[str]) -> Optional[str]:
    """Root a workspace path at ``/Workspace`` (the form the ``#workspace`` link expects).

    The Workspace SDK returns paths without the prefix (``/Users/...``); fixtures already
    carry it (``/Workspace/...``). Idempotent. Returns None for an empty path.
    """
    if not path:
        return None
    p = path if path.startswith("/") else f"/{path}"
    return p if p == "/Workspace" or p.startswith("/Workspace/") else f"/Workspace{p}"


def _org_param(workspace_id: Optional[str]) -> str:
    """The ``?o=<workspaceId>`` query segment, or ``''`` when the id is missing/blank."""
    wid = workspace_id.strip() if workspace_id else ""
    return f"?o={wid}" if wid else ""


def workspace_object_url(
    host: Optional[str],
    path: Optional[str],
    workspace_id: Optional[str] = None,
) -> Optional[str]:
    """A path-based notebook/file deep link, or None when host/path is missing.

    With ``workspace_id`` the fully-qualified, cross-workspace-safe form is emitted
    (``<host>/?o=<id>#workspace<path>`` — the ``?o=`` goes BEFORE the ``#`` fragment);
    without it, the host-only form (``<host>/#workspace<path>``) still resolves in-session.
    """
    h = _ensure_scheme(host)
    p = normalize_workspace_path(path)
    if not h or not p:
        return None
    return f"{h}/{_org_param(workspace_id)}#workspace{p}"


def job_url(
    host: Optional[str],
    job_id: Optional[str],
    workspace_id: Optional[str] = None,
) -> Optional[str]:
    """A Databricks job-page deep link ``<host>/jobs/<jobId>[?o=<workspaceId>]`` or None."""
    h = _ensure_scheme(host)
    if not h or not job_id:
        return None
    return f"{h}/jobs/{job_id}{_org_param(workspace_id)}"


def build_location_row(
    *,
    full_name: str,
    entity_kind: Optional[str],
    entity_id: Optional[str],
    entity_name: Optional[str],
    source_path: Optional[str],
    cell_index: Optional[int],
    line_number: Optional[int],
    snippet: Optional[str],
    matched_path: Optional[str],
    confidence: str = "high",
) -> dict:
    """Assemble one ``path_access_locations`` row (``updated_at`` set by the notebook)."""
    return {
        "full_name": full_name,
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "source_path": source_path,
        "cell_index": cell_index,
        "line_number": line_number,
        "snippet": snippet,
        "matched_path": matched_path,
        "confidence": confidence,
    }
