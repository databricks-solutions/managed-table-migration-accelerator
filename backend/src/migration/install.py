"""Pure helpers for the first-time-operator notebook installer (``backend/notebooks/install.py``).

The installer runs **after** the bundle has been deployed from the workspace (phase 2 in the
notebook's own docstring): the bundle — not this notebook — creates the 15 operator jobs, the
setup job, the dashboard, and the app. These functions carry the only logic worth unit-testing,
so the notebook stays a thin Spark/SDK edge (same discipline as ``gate_eval`` / ``genie_space`` /
``grants``):

* **Job + app resolution by suffix.** DABs *development* mode prefixes resource names with
  ``[dev <user>]``, and the bundle-name segment can differ across renames, so we match a deployed
  job only by its stable `` :: <stage label>`` tail — never the full name. Apps can't carry those
  characters, so a deployed app is matched by an exact name or a unique suffix.
* **Preflight completeness.** Given what the workspace returned, compute exactly which required
  pieces are missing so the notebook can STOP and point at phase 2 (deploy) rather than trying to
  create anything.
* **App-SP grant verification.** Parse ``SHOW GRANTS`` rows (via the existing ``grants`` module)
  into a present/missing summary the notebook prints, plus the exact GRANT statements to hand an
  admin for any miss.
* **Data-mode plan.** Map the ``real | fixture | none`` widget to the ordered job sequence to run,
  and gate ``fixture`` on the dev allowlist (reusing ``fixture_safety``) BEFORE launching.
* **Config-handshake completeness.** Check ``migration_control_config`` has every field the app's
  boot read needs.
* **No app restart.** The app looks ``genie_space_id`` up per request, so the installer never asks
  for a second deploy; every run ends with the conductor (``CONDUCTOR_JOB_KEY``).

No Spark/SDK import here; every function is a pure transform over plain data.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from . import fixture_safety as fs
from . import grants as gr

# --------------------------------------------------------------------------- #
# Deployed-name suffixes (the stable ` :: <label>` tail — bundle-name & dev-prefix independent)
# --------------------------------------------------------------------------- #

# The DABs display name is ``[<target>] <bundle.name> :: <stage label>`` (dev mode further
# prefixes ``[dev <user>] ``). Only the `` :: <label>`` tail is invariant, so we match on it.
STAGE_SEP = " :: "

# The 19 OPERATOR jobs the bundle deploys AND the app binds (see app/resources/app.yml — "19
# operator jobs + the SQL warehouse", exactly at the 20-resource cap). Preflight proves every one
# exists; any miss means the bundle was not deployed (phase 2), not something the notebook should
# create. This map is the single source the installer resolves by suffix; a guard test
# (test_install.py) asserts it stays in lockstep with the app's job bindings, so a newly-bound job
# can't silently escape preflight.
# NOTE: three jobs are deployed by the bundle but NOT app-bound, so they are intentionally absent
# here (all CLI-runnable, not app-triggered): 03b_compaction_scan (its slot went to the SQL
# warehouse), 01_source_scan (folded into the 00 profiler "discovery chunk" behind the
# run_source_scan toggle, triggered via the profiler alias — its freed slot now holds 03d), and
# 09_get_started (un-bound in M5 to free the 20-cap slot for 08c_enable_optimization; still
# bundle-deployed + resolved on demand for the ``real`` seed via ORCHESTRATOR_JOB_LABELS).
OPERATOR_JOB_LABELS: Dict[str, str] = {
    "00_profiler": "00 profiler",
    "02_wave_planner": "02 wave planner",
    "03_dry_run": "03 dry run",
    "03c_freeze": "03c freeze",
    "03d_lease_ownership": "03d lease ownership",
    "04_convert": "04 convert",
    "05_validate": "05 validate",
    "05b_unfreeze_verify": "05b unfreeze + verify",
    "06_rollback": "06 rollback (manual)",
    "07_cleanup": "07 post-convert cleanup",
    "08_conductor": "08 conductor",
    "08_reshare": "08 re-share (D2D)",
    "08b_enable_compat": "08b enable compatibility mode",
    "08c_enable_optimization": "08c enable predictive optimization",
    "10_campaign": "10 campaign (gated)",
    "11_bulk_convert": "11 bulk convert",
    "11b_bulk_freeze": "11b bulk freeze",
    "11c_bulk_validate": "11c bulk validate",
    "11d_bulk_unfreeze": "11d bulk unfreeze + verify",
}

# Install-time provisioning job. NOT one of the 19 and NOT app-bound, but the bundle deploys it and
# Step 2 runs it, so it is part of the preflight required set.
SETUP_JOB_KEY = "99_setup"
SETUP_JOB_LABELS: Dict[str, str] = {SETUP_JOB_KEY: "99 setup"}

# Dev-only fixture seeder (allowlist-guarded). Only Step 4 in ``fixture`` mode needs it, so it is
# resolved on demand, never required by the core preflight.
FIXTURE_JOB_KEY = "e2e_seed_fixture_estate"
FIXTURE_JOB_LABELS: Dict[str, str] = {FIXTURE_JOB_KEY: "e2e seed fixture estate (dev-only)"}

# Bundle-deployed + resolved for the install-time ``real`` seed, but NOT app-bound (un-bound in M5
# to free the 20-cap slot for 08c enable optimization). Like the fixture seeder it is resolved on
# demand (real data mode) via ALL_JOB_LABELS, never part of the app-bound operator set or the
# by-app preflight.
ORCHESTRATOR_JOB_KEY = "09_get_started"
ORCHESTRATOR_JOB_LABELS: Dict[str, str] = {
    ORCHESTRATOR_JOB_KEY: "09 get started (discover→plan→conduct)",
}

# Everything the notebook may resolve.
ALL_JOB_LABELS: Dict[str, str] = {
    **OPERATOR_JOB_LABELS,
    **SETUP_JOB_LABELS,
    **FIXTURE_JOB_LABELS,
    **ORCHESTRATOR_JOB_LABELS,
}

# Preflight requires the 19 app-bound operator jobs AND the setup job (Step 2 depends on it).
PREFLIGHT_JOB_LABELS: Dict[str, str] = {**OPERATOR_JOB_LABELS, **SETUP_JOB_LABELS}


def job_suffix(label: str) -> str:
    """The endswith-suffix for a stage ``label`` (e.g. ``" :: 04 convert"``)."""
    return f"{STAGE_SEP}{label}"


# --------------------------------------------------------------------------- #
# Job resolution by suffix
# --------------------------------------------------------------------------- #

def deployment_qualifier(bundle_name: Optional[str], target: Optional[str]) -> Optional[str]:
    """The ``[<target>] <bundle.name>`` segment every job of one deployment carries, or ``None``.

    Two deployments in one workspace (dev and prod, or two targets) share every `` :: <label>``
    tail, so a caller that knows its own target passes this to resolve only its own jobs.
    """
    if not bundle_name or not target:
        return None
    return f"[{target.strip()}] {bundle_name.strip()}"


def deployment_from_path(path: Optional[str]) -> Optional[Tuple[str, str]]:
    """``(bundle_name, target)`` from a path inside a deployed bundle (``…/.bundle/<name>/<target>/…``).

    The installer reads its own notebook path, so it resolves its own deployment's jobs without
    the operator re-typing the target. ``None`` for a Git folder or any other non-bundle path.
    """
    parts = [p for p in (path or "").split("/") if p]
    for i, part in enumerate(parts):
        if part == ".bundle" and i + 2 < len(parts):
            return parts[i + 1], parts[i + 2]
    return None


def deployment_of_path(path: Optional[str]) -> Optional[str]:
    """``deployment_qualifier`` for the deployment a notebook path lives in, or ``None``."""
    found = deployment_from_path(path)
    return deployment_qualifier(*found) if found else None


def name_matches(name: Optional[str], suffix: str, deployment: Optional[str]) -> bool:
    """``name`` ends with ``suffix``, preceded by ``deployment`` when one is given.

    ``suffix`` may carry its own leading space or not (``" :: 04 convert"`` / ``":: 04 convert"``),
    and may already start with the bundle name (``"managed-table-migration :: 04 convert"``).
    """
    if not name:
        return False
    if not deployment:
        return name.endswith(suffix)
    tail = suffix.lstrip(" ")
    target, _, bundle = deployment.partition("] ")
    if bundle and tail.startswith(bundle + " "):
        return name.endswith(target + "] " + tail)
    return name.endswith(deployment + " " + tail)


def resolve_job_id(
    jobs: Iterable[Tuple[Optional[str], object]], label: str, deployment: Optional[str] = None
):
    """Return the id of the first ``(name, id)`` whose name ends with ``label``'s stage suffix.

    ``jobs`` is an iterable of ``(display_name, job_id)`` pairs (what ``w.jobs.list`` yields once
    projected). Matching on the `` :: <label>`` tail tolerates the ``[dev <user>]`` prefix and any
    bundle-name segment. With ``deployment`` (see ``deployment_qualifier``) only names ending in
    ``<deployment> :: <label>`` match, so another deployment's job is never picked. Returns
    ``None`` when no job matches.
    """
    suffix = job_suffix(label)
    for name, job_id in jobs:
        if name_matches(name, suffix, deployment):
            return job_id
    return None


def resolve_job_ids(
    jobs: Iterable[Tuple[Optional[str], object]],
    labels: Dict[str, str],
    deployment: Optional[str] = None,
) -> Dict[str, object]:
    """Resolve a whole ``{key: label}`` map to ``{key: job_id-or-None}`` (``jobs`` read once)."""
    materialized = list(jobs)
    return {key: resolve_job_id(materialized, label, deployment) for key, label in labels.items()}


def missing_job_keys(resolved: Dict[str, object]) -> List[str]:
    """Keys whose id did not resolve (a falsy value), in the map's own order."""
    return [key for key, job_id in resolved.items() if not job_id]


# --------------------------------------------------------------------------- #
# App resolution by name
# --------------------------------------------------------------------------- #

def resolve_app_name(app_names: Iterable[Optional[str]], wanted: str) -> Optional[str]:
    """Resolve the deployed app name: exact match first, else the UNIQUE name ending with
    ``wanted`` (tolerating a dev-mode prefix joined by ``-``/``_``).

    Returns the resolved name, or ``None`` when there is no match or the suffix is ambiguous.
    """
    names = [n for n in app_names if n]
    if not wanted:
        return None
    if wanted in names:
        return wanted
    candidates = [
        n for n in names
        if n.endswith(wanted) and (n == wanted or n[: -len(wanted)].endswith(("-", "_")))
    ]
    return candidates[0] if len(candidates) == 1 else None


def deployed_control_schema_mismatch(catalog: str, schema: str,
                                     deployed: Dict[str, str]) -> Optional[str]:
    """``catalog.schema`` the deploy gave 99 setup when it differs from the one this run resolves;
    None when they agree or the deploy is unknown. Unity Catalog names compare case-insensitively."""
    d_catalog = (deployed.get("manifest_catalog") or "").strip()
    d_schema = (deployed.get("manifest_schema") or "").strip()
    if not d_catalog or not d_schema:
        return None
    if (d_catalog.lower(), d_schema.lower()) == ((catalog or "").lower(), (schema or "").lower()):
        return None
    return f"{d_catalog}.{d_schema}"


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def preflight_summary(
    app_resolved: Optional[str],
    resolved_jobs: Dict[str, object],
    required_labels: Dict[str, str] = PREFLIGHT_JOB_LABELS,
) -> dict:
    """Summarize whether the bundle deploy (phase 2) created everything the installer needs.

    Returns a dict with ``app_ok``, ``missing_jobs`` (keys, in required order), ``ok`` (both the
    app and all required jobs present), and ``remediation`` (a phase-2 pointer when not ok).
    """
    missing = [key for key in required_labels if not resolved_jobs.get(key)]
    app_ok = bool(app_resolved)
    ok = app_ok and not missing
    remediation = None
    if not ok:
        parts = []
        if not app_ok:
            parts.append("the app")
        if missing:
            labels = ", ".join(required_labels[k] for k in missing)
            parts.append(f"{len(missing)} job(s): {labels}")
        remediation = (
            "Bundle not fully deployed — missing " + " and ".join(parts) + ". "
            "Open databricks.yml in the workspace, click the Deployments icon, pick your target, "
            "and Deploy (phase 2). Do NOT create these from this notebook."
        )
    return {"app_ok": app_ok, "missing_jobs": missing, "ok": ok, "remediation": remediation}


# --------------------------------------------------------------------------- #
# App-SP grant verification (parse SHOW GRANTS)
# --------------------------------------------------------------------------- #

def verify_app_sp_grants(
    catalog: str,
    schema: str,
    sp_uuid: str,
    rows_by_privilege: Dict[str, list],
) -> List[dict]:
    """Verify the four least-privilege App-SP grants from ``SHOW GRANTS`` rows.

    ``rows_by_privilege`` maps a privilege (``"USE CATALOG"`` / ``"USE SCHEMA"`` / ``"SELECT"`` /
    ``"MODIFY"``) to the rows returned by the matching ``SHOW GRANTS`` query. Returns one dict per
    required grant: ``privilege``, ``securable_type``, ``securable_fqn``, ``present`` (bool via the
    tolerant ``grants.sp_has_grant`` read-back), and the exact ``grant_sql`` to run on a miss.
    """
    results: List[dict] = []
    for privilege, sectype, fqn, grant_sql, _show_sql in gr.app_sp_grant_plan(
        catalog, schema, sp_uuid
    ):
        rows = rows_by_privilege.get(privilege, [])
        results.append(
            {
                "privilege": privilege,
                "securable_type": sectype,
                "securable_fqn": fqn,
                "present": bool(gr.sp_has_grant(rows, sp_uuid, privilege)),
                "grant_sql": grant_sql,
            }
        )
    return results


def missing_grant_statements(results: Iterable[dict]) -> List[str]:
    """The GRANT statements for any grant the read-back could not confirm (order preserved)."""
    return [r["grant_sql"] for r in results if not r.get("present")]


def all_grants_present(results: List[dict]) -> bool:
    """True iff every required grant verified (and there is at least one to verify)."""
    return bool(results) and all(r.get("present") for r in results)


# --------------------------------------------------------------------------- #
# Data mode (Step 4)
# --------------------------------------------------------------------------- #

DATA_MODE_REAL = "real"
DATA_MODE_FIXTURE = "fixture"
DATA_MODE_NONE = "none"
DATA_MODES: Tuple[str, ...] = (DATA_MODE_REAL, DATA_MODE_FIXTURE, DATA_MODE_NONE)

# ``real`` seeds the control plane from the estate in ONE orchestrated job: ``09 get_started`` runs
# the profiler then the wave planner (which itself tails the conductor, stage 02) — i.e.
# discover->plan->conduct. This supersedes the old ("00_profiler","02_wave_planner","08_conductor")
# trio, whose trailing conductor became a redundant second run once the planner started tailing it.
REAL_JOB_SEQUENCE: Tuple[str, ...] = (ORCHESTRATOR_JOB_KEY,)

# Every installer run ends with the conductor, so the views and the next step reflect it (an
# upgrade is "re-run the installer"). ``real`` gets it from the orchestrator's own tail.
CONDUCTOR_JOB_KEY = "08_conductor"


def normalize_data_mode(value: Optional[str]) -> str:
    """Normalize + validate the data-mode widget to one of ``DATA_MODES`` (raises on unknown)."""
    mode = (value or "").strip().lower()
    if mode not in DATA_MODES:
        raise ValueError(
            f"data_mode must be one of {DATA_MODES}, got {value!r}"
        )
    return mode


def data_mode_job_sequence(mode: str) -> List[str]:
    """Ordered job keys to run for ``mode``.

    ``real`` -> the ``09 get_started`` orchestrator (discover->plan->conduct); ``fixture`` -> the dev
    fixture seeder, then the conductor; ``none`` -> the conductor alone. ``real`` and ``fixture`` are
    mutually exclusive — both fully rewrite ``wave_plan`` — so the notebook only ever runs one
    branch per install.
    """
    mode = normalize_data_mode(mode)
    if mode == DATA_MODE_REAL:
        return list(REAL_JOB_SEQUENCE)
    if mode == DATA_MODE_FIXTURE:
        return [FIXTURE_JOB_KEY, CONDUCTOR_JOB_KEY]
    return [CONDUCTOR_JOB_KEY]


def fixture_allowed(catalog: str, schema: str, extra: str | None = None) -> bool:
    """True iff ``(catalog, schema)`` is in the dev fixture allowlist (``fixture_safety``).

    ``extra`` is the optional deploy-time ``catalog.schema`` entry (``var.e2e_fixture_control_plane``).
    The fixture seed job self-guards, but checking here lets the notebook STOP with guidance
    BEFORE launching a job that would only fail inside its own allowlist assertion.
    """
    return (catalog, schema) in fs.allowed_control_planes(extra)


# --------------------------------------------------------------------------- #
# Control-config handshake completeness (Step 5)
# --------------------------------------------------------------------------- #

# Every field the app's boot read + the dashboard/Genie handshake need populated.
REQUIRED_CONFIG_FIELDS: Tuple[str, ...] = (
    "catalog",
    "schema",
    "warehouse_id",
    "dashboard_id",
    "genie_space_id",
)


def missing_config_fields(
    config_row: Optional[dict], required: Iterable[str] = REQUIRED_CONFIG_FIELDS
) -> List[str]:
    """Required config fields that are absent or blank in ``migration_control_config``."""
    row = config_row or {}
    return [
        field
        for field in required
        if not (row.get(field) is not None and str(row.get(field)).strip())
    ]


def config_complete(
    config_row: Optional[dict], required: Iterable[str] = REQUIRED_CONFIG_FIELDS
) -> bool:
    """True iff every required config field is populated."""
    return not missing_config_fields(config_row, required)
