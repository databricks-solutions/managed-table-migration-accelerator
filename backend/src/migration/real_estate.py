"""Pure logic for the seed-and-leave REAL-estate builder (dev-only).

No Spark/SDK imports at module load. Everything here is a pure function of its
inputs so it is unit-testable with no cluster. The terminal driver
(backend/.e2e/seed_real_estate.py) and the seed notebooks consume these plans.
"""
from __future__ import annotations
import random
from datetime import datetime, timedelta
from typing import Iterable, Optional

KB, MB, GB, TB = 1024, 1024**2, 1024**3, 1024**4

SIZE_TIERS: tuple[dict, ...] = (
    {"name": "XS", "target_bytes": 10 * KB},
    {"name": "S",  "target_bytes": 1 * MB},
    {"name": "M",  "target_bytes": 50 * MB},
    {"name": "L",  "target_bytes": 500 * MB},
    {"name": "XL", "target_bytes": 1 * GB},
)

SCHEMAS: tuple[dict, ...] = (
    {"name": "e2e_real_bronze",    "purpose": "dense small tables (pilot)", "case": "PILOT_SMALL"},
    {"name": "e2e_real_silver",    "purpose": "mixed real sizes",          "case": "MIXED"},
    {"name": "e2e_real_gold",      "purpose": "large + synthetic-TB",       "case": "LARGE"},
    {"name": "e2e_real_stream",    "purpose": "structured streaming",       "case": "STREAMING"},
    {"name": "e2e_real_dlt",       "purpose": "DLT pipeline output",        "case": "DLT"},
    {"name": "e2e_real_share",     "purpose": "delta-shared (D2D)",         "case": "SHARE"},
    {"name": "e2e_real_compat",    "purpose": "external-writer / compat",   "case": "COMPAT"},
    {"name": "e2e_real_nondelta",  "purpose": "non-Delta (unknown size)",   "case": "NON_DELTA"},
    {"name": "e2e_real_pathaccess","purpose": "path-based reads",           "case": "PATH_ACCESS"},
    # A TINY, papercut-free schema whose sole purpose is to hold ONE isolated scheduled-writer
    # table (the L4 F-WRITER-WAVE fixture points an imminent-WARN_WINDOW writer job at it). Being
    # its own schema, the wave planner slices it into its own small `normal` wave — so the
    # attributed writer is NOT buried in the dense bronze domain wave, and a dry-run + 03c_freeze
    # makes it a runnable wave whose Convert-Gate pause banner names exactly that one job.
    {"name": "e2e_real_writer",    "purpose": "isolated scheduled writer",  "case": "WRITER_WAVE"},
)

# Per-schema weight (share of table_count); pilot schema is densest. e2e_real_writer is tiny by
# design (its wave must contain ~just the one scheduled-writer table).
_WEIGHTS = {
    "e2e_real_bronze": 0.28, "e2e_real_silver": 0.20, "e2e_real_gold": 0.10,
    "e2e_real_stream": 0.08, "e2e_real_dlt": 0.08, "e2e_real_share": 0.06,
    "e2e_real_compat": 0.06, "e2e_real_nondelta": 0.06, "e2e_real_pathaccess": 0.08,
    "e2e_real_writer": 0.02,
}

_CASE_TAGS = {
    "PILOT_SMALL": (), "MIXED": (), "LARGE": (),
    "STREAMING": ("STOP_STREAM",), "DLT": ("STOP_STREAM",),
    "SHARE": ("RESHARE_D2D",), "COMPAT": ("ENABLE_COMPAT",),
    "NON_DELTA": (), "PATH_ACCESS": ("FIX_PATH_ACCESS",),
    # The writer papercut is REAL (a scheduled JOB attributed via lineage into writer_sources),
    # not a synthetic manifest tag — so WRITER_WAVE carries no plan-level papercut tag.
    "WRITER_WAVE": (),
}


def _tier_for(case: str, i: int, rng: random.Random) -> dict:
    if case in ("PILOT_SMALL", "WRITER_WAVE"):
        return SIZE_TIERS[0] if i % 3 else SIZE_TIERS[1]          # XS/S
    if case == "LARGE":
        return SIZE_TIERS[4]                                       # XL (synthetic-TB patched later)
    if case == "NON_DELTA":
        return SIZE_TIERS[1]
    if case == "MIXED":
        return SIZE_TIERS[i % len(SIZE_TIERS)]                     # guarantees all tiers
    return SIZE_TIERS[rng.randrange(len(SIZE_TIERS))]


def build_estate_plan(table_count: int = 120, rng_seed: int = 42) -> list[dict]:
    rng = random.Random(rng_seed)
    plan: list[dict] = []
    for spec in SCHEMAS:
        n = max(1, round(table_count * _WEIGHTS[spec["name"]]))
        for i in range(n):
            tier = _tier_for(spec["case"], i, rng)
            name = f"{spec['case'].lower()}_{i:03d}"
            plan.append({
                "catalog_relative_schema": spec["name"],
                "table_name": name,
                "full_name_suffix": f"{spec['name']}.{name}",
                "kind": spec["case"],
                "size_tier": tier["name"],
                "target_bytes": tier["target_bytes"],
                "size_source": "DESCRIBE_DETAIL",   # real bytes captured by profiler
                "papercut_tags": list(_CASE_TAGS[spec["case"]]),
                "is_delta": spec["case"] != "NON_DELTA",
            })
    return plan


def synthetic_size_rows(schema: str = "e2e_real_gold") -> list[dict]:
    """Post-profile manifest PATCH rows that set table_size_bytes into the true TB bands
    (~1TB, ~10TB, >10TB) so band/copy/cluster/final_large bucketing is visible live.
    Stamped SYNTHETIC — these bytes are NOT from DESCRIBE DETAIL (can't afford real TB)."""
    targets = [("syn_1tb", TB), ("syn_10tb", 10 * TB), ("syn_11tb", 11 * TB)]
    return [{
        "full_name_suffix": f"{schema}.{name}", "table_name": name,
        "catalog_relative_schema": schema, "target_bytes": b,
        "size_source": "SYNTHETIC", "kind": "LARGE", "is_delta": True, "papercut_tags": [],
    } for name, b in targets]


# --------------------------------------------------------------------------- #
# CTAS size mapping (pure; consumed by the provision driver)
# --------------------------------------------------------------------------- #
# The estate's PURPOSE is genuinely DISTINCT real on-disk sizes across tiers, so
# the payload column is INCOMPRESSIBLE (per-row random SHA-512 hex — see
# ``seed_real_estate._payload_expr``). With random hex, real bytes track the
# payload width, so ``rows * pad`` (below) is a faithful proxy for on-disk size.
#
# We scale the nominal tier target DOWN by ``_REAL_SIZE_DIVISOR`` to a REAL size
# that stays affordable (largest REAL table lands in the tens-of-MB range; Task
# 12 patches large SYNTHETIC TB sizes separately). ``rows`` grow with the target
# at a base payload width; once ``rows`` would saturate ``_ROW_CAP`` the payload
# WIDENS instead. Because the nominal tier targets are already >=2x apart, the
# resulting ``rows * pad`` is strictly monotonic with a clear (>=2x) gap between
# every adjacent tier.
_REAL_SIZE_DIVISOR = 16   # nominal target_bytes -> affordable REAL on-disk bytes
_ROW_CAP = 100_000        # cap live range() count; payload widens past this
_MIN_PAD = 64             # base incompressible payload width (chars ~= bytes)


def rows_pad_for(target_bytes: int) -> tuple[int, int]:
    """Map a target on-disk size to ``(rows, payload_pad)`` for a CTAS ``range()`` build.

    Pure + deterministic. Returns the row count and the incompressible payload
    width (in chars, ~= bytes) whose product ``rows * pad`` approximates the REAL
    on-disk size (``target_bytes`` scaled down by ``_REAL_SIZE_DIVISOR`` to stay
    affordable). ``rows`` grow with the target at ``_MIN_PAD``; once ``rows``
    would exceed ``_ROW_CAP`` the payload widens instead, so ``rows * pad`` stays
    strictly monotonic in ``target_bytes`` across every XS->XL tier. The sizing
    is intentionally approximate — the profiler captures the REAL bytes after.
    """
    real = max(1, int(target_bytes) // _REAL_SIZE_DIVISOR)
    pad = _MIN_PAD
    rows = max(1, real // pad)
    if rows > _ROW_CAP:
        rows = _ROW_CAP
        pad = max(_MIN_PAD, -(-real // rows))  # ceil: widen payload to hit real size
    return rows, pad


# --------------------------------------------------------------------------- #
# Path-based-access fixtures (pure; consumed by seed_real_estate + the notebook)
# --------------------------------------------------------------------------- #
# A FIXED, plan-independent fixture set so the committed read_by_path_real notebook's
# literal ``.load("<base>/e2e_real_pathaccess/<name>")`` paths ALWAYS match tables the
# builder provisions — regardless of ``--table-count`` (which randomizes the estate plan).
# Determinism is the whole point: the source-scan deep link only resolves if the literal in
# the notebook names a table that really exists at that storage path. All three live under
# the ``e2e_real_pathaccess`` schema/prefix so a genuine by-path read edge is established.
PATH_ACCESS_FIXTURES: tuple[str, ...] = ("orders_by_path", "events_by_path", "metrics_by_path")

# The schema (and storage-path segment) the fixtures live under. Matches the PATH_ACCESS
# case schema in SCHEMAS so the profiler attributes the papercut to the same slice.
PATH_ACCESS_SCHEMA = "e2e_real_pathaccess"


def path_access_fixture_location(external_base: str, name: str) -> str:
    """Compose the external storage path for a path-access fixture table.

    ``f"{external_base}/e2e_real_pathaccess/{name}"`` with any trailing slash on
    ``external_base`` normalized away so the separator never doubles up. Pure — the exact
    literal the notebook embeds and the CTAS ``LOCATION`` both derive from this.
    """
    base = (external_base or "").rstrip("/")
    return f"{base}/{PATH_ACCESS_SCHEMA}/{name}"


def expected_papercuts(plan: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in plan:
        out.setdefault(t["catalog_relative_schema"], set()).update(t["papercut_tags"])
    return out


def compaction_candidates(
    catalog: str, plan: list[dict], schemas: Iterable[str]
) -> list[tuple[str, list[str]]]:
    """Ordered OPTIMIZE candidate list per target schema (pure).

    For each schema in ``schemas`` (given order preserved), return
    ``(schema, [fully-qualified Delta table names in plan order])`` where each fq is
    ``f"{catalog}.{t['full_name_suffix']}"`` for plan rows with ``t.get('is_delta', True)``
    and ``t['catalog_relative_schema'] == schema``. Schemas with zero Delta tables are omitted
    (no empty-list entry). The driver iterates each schema's candidates until one OPTIMIZE
    succeeds, so a real compaction source is created as long as ANY Delta table in that schema
    exists — resilient to a first-planned table that failed to provision.
    """
    out: list[tuple[str, list[str]]] = []
    for schema in schemas:
        fqns = [
            f"{catalog}.{t['full_name_suffix']}"
            for t in plan
            if t.get("is_delta", True) and t["catalog_relative_schema"] == schema
        ]
        if fqns:
            out.append((schema, fqns))
    return out


def real_chunking_wave_max_bytes() -> int:
    return 1 * GB


# --------------------------------------------------------------------------- #
# Provision race retry (pure) — settle-wait / retry for the DROP-then-CREATE race
# --------------------------------------------------------------------------- #
# ``--reset`` drops the estate schemas (CASCADE) then immediately re-creates every table at the
# SAME external LOCATION. For tables that were previously converted, the CASCADE data delete is
# ASYNCHRONOUS, so a re-create can race the in-flight delete and fail transiently (the metastore
# briefly cannot resolve the table uuid / still sees stale files at the path). These are safe to
# retry: the delete settles within seconds-to-minutes and the next CREATE OR REPLACE succeeds.

# Number of extra attempts the provision CREATE gets after the first (0 = no retry).
PROVISION_MAX_RETRIES = 6
# Fixed pause after a --reset teardown before re-provisioning, so most creates succeed first try.
RESET_SETTLE_SECONDS = 30

# Case-insensitive substrings that identify a TRANSIENT collision with an in-flight async delete
# (never a schema/permission bug). Kept narrow so a genuinely-broken CREATE still FAILs fast.
_RETRYABLE_PROVISION_SIGNATURES: tuple[str, ...] = (
    "does not exist",            # metastore cannot resolve the mid-delete table uuid
    "table_or_view_not_found",
    "delta_table_not_found",
    "path_already_exists",       # stale data files still present at the location
    "already exists",            # table/schema mid-delete still registered
    "no such file",
    "filenotfound",
    "path does not exist",
    "concurrent",                # concurrent modification against the settling table
)


def is_retryable_provision_error(message: Optional[str]) -> bool:
    """True when a failed provision CREATE looks like the async DROP-then-CREATE race (pure)."""
    if not message:
        return False
    low = message.lower()
    return any(sig in low for sig in _RETRYABLE_PROVISION_SIGNATURES)


def retry_backoff_seconds(attempt: int, base_s: float = 5.0, cap_s: float = 60.0) -> float:
    """Exponential backoff for retry ``attempt`` (0-indexed), capped at ``cap_s`` (pure)."""
    if attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt}")
    return float(min(cap_s, base_s * (2 ** attempt)))


def provision_retry_delays(
    max_retries: int = PROVISION_MAX_RETRIES, base_s: float = 5.0, cap_s: float = 60.0
) -> list[float]:
    """The ordered backoff schedule (seconds) for the provision CREATE retries (pure).

    ``max_retries`` entries, each :func:`retry_backoff_seconds` of its index — e.g. the default
    6/5/60 schedule is ``[5, 10, 20, 40, 60, 60]`` (~195s total settle budget).
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")
    return [retry_backoff_seconds(i, base_s, cap_s) for i in range(max_retries)]


# --------------------------------------------------------------------------- #
# Estate file-clear guard (pure) — safe in-place reset without run-scoped paths
# --------------------------------------------------------------------------- #
# ``DROP SCHEMA … CASCADE`` on EXTERNAL tables removes metadata only; the cloud files stay.
# After a convert-then-drop cycle those files include an orphaned Delta log that collides with a
# same-path re-create. Rather than seed each reset onto a fresh run-scoped subpath (which leaks
# files forever), teardown can physically clear the estate prefix so re-provision reuses the one
# stable base. Because that is a real recursive file removal, it is ONLY ever allowed against the
# dedicated dev test prefix — this pure guard is the gate (the driver + the clear notebook both
# assert it before removing anything).

# The estate prefix must contain this exact PATH SEGMENT (under the bucket, never the bucket
# itself). The builder's default base is ``…/mtm_e2e``; a custom base without this marker is
# never auto-cleared (safe default — teardown WARNs and leaves files rather than guess).
ESTATE_PREFIX_MARKER = "mtm_e2e"


def is_safe_estate_base(external_base: Optional[str]) -> bool:
    """True only when ``external_base`` is unambiguously the dev test estate prefix (pure).

    All of these must hold, so a bucket root / bare scheme / shared location can never be removed:
      * it is a cloud URI (``scheme://bucket/…``);
      * it has at least a bucket AND one path segment under it (never a bucket root);
      * the dedicated ``mtm_e2e`` marker appears as a PATH SEGMENT under the bucket (not as the
        bucket name and not merely as a substring, so ``…/mtm_e2e_other`` does NOT qualify).
    """
    if not external_base:
        return False
    b = external_base.strip().rstrip("/")
    scheme, sep, rest = b.partition("://")
    if not scheme or not sep or not rest:
        return False
    segments = [p for p in rest.split("/") if p]
    if len(segments) < 2:          # need bucket + >=1 path segment
        return False
    path_segments = segments[1:]   # everything under the bucket
    return ESTATE_PREFIX_MARKER in path_segments


def estate_file_clear_path(external_base: str) -> str:
    """The exact prefix teardown removes (pure): the estate base, trailing slash stripped.

    Raises ``ValueError`` for a base that fails :func:`is_safe_estate_base` so an unsafe path can
    never be turned into a removal target.
    """
    if not is_safe_estate_base(external_base):
        raise ValueError(f"refusing to compute a file-clear path for unsafe base {external_base!r}")
    return external_base.strip().rstrip("/")


def estate_file_clear_paths(external_base: str) -> list[str]:
    """The exact per-schema prefixes teardown removes (pure): one dir per estate schema.

    Scoped per ``e2e_real_*`` schema dir (NOT the whole base) so a same-base ``--reset`` can clear
    the estate's files without UC's ``CheckPathAccess`` denying the removal. The reused deployed
    assets ``e2e_stream.src_01`` / ``e2e_dlt.src_bronze`` live under the same ``mtm_e2e`` prefix but
    in sibling ``e2e_stream/`` / ``e2e_dlt/`` dirs; a base-wide ``rm`` overlaps those live external
    tables and is refused. Every estate-owned file lands under an ``e2e_real_*`` dir (main CTAS,
    path-access fixtures, synthetic-size fixtures all use an :data:`SCHEMAS` name), so the per-schema
    list is both complete for the estate and disjoint from the preserved assets. Driven by
    :data:`SCHEMAS` so a new estate schema auto-extends the clear.

    Raises ``ValueError`` for a base that fails :func:`is_safe_estate_base` so an unsafe path can
    never be turned into a removal target.
    """
    base = estate_file_clear_path(external_base)   # validates + strips trailing slash
    return [f"{base}/{spec['name']}" for spec in SCHEMAS]


# --------------------------------------------------------------------------- #
# Control-plane full wipe (pure) — the sanctioned "reset to fresh install" path
# --------------------------------------------------------------------------- #
# A scoped teardown only clears the estate slice, so a control-plane row keyed OUTSIDE that
# slice (e.g. a STEP_DONE ``migration_next_actions`` snapshot with NULL ``next_group_key``, the
# "83% migrated" stale-state class) can survive and keep the app off its first-run screen. The
# full wipe empties EVERY control BASE table so ``migration_next_actions`` has no row and the app
# renders FreshSituation / FirstRunResolution.

# Never wiped: the single-row control handshake.
CONTROL_CONFIG_TABLE = "migration_control_config"

# Control-plane BASE tables emptied by a full fresh-install wipe. Mirrors the canonical list in
# ``seed_real_estate._control_row_deletes``: every base table EXCEPT ``migration_control_config``
# (kept) and every VIEW (migration_progress / wave_status / migration_runbook / wave_gate_latest /
# action_queue_* / coverage_report / stale_manifest — derived, nothing to wipe). Source of truth:
# control-tables.md.
#
# ``profiler_run_ledger`` (append-only, one row per profiler run) MUST be here: the Overview
# landing derives NEVER_PROFILED-vs-ZERO_DISCOVERY solely from whether the ``coverage_report``
# VIEW (a projection of this ledger) has ≥1 row. If the wipe empties ``table_manifest`` but not
# the ledger, the app is stuck on ZERO_DISCOVERY ("profiler ran, found nothing") forever instead
# of the true first-run "run the profiler" card — so a fresh-install wipe would never reproduce a
# brand-new install. Emptying the ledger auto-empties its ``coverage_report`` view.
#
# ``wave_parallel_groups`` is a DERIVED-but-persisted base table (the planner rewrites it in full
# per ``plan_run_id``, alongside ``wave_plan``). It MUST be wiped too: it is not a view, so a wipe
# that empties ``wave_plan`` but not this table strands an orphaned batch snapshot that the Bulk
# Convert screen would render as a ghost plan (batches with no backing ``wave_plan``).
CONTROL_BASE_TABLES: tuple[str, ...] = (
    "table_manifest",
    "wave_plan",
    "wave_parallel_groups",  # derived-but-persisted; unwiped => ghost batches on Bulk Convert
    "comms_feed",
    "audit_log",
    "compaction_sources",
    "streaming_plan",
    "consumer_runtime_plan",
    "path_access_locations",
    "validation_results",
    "dry_run_results",
    "migration_actions",
    "freeze_ledger",
    "ownership_ledger",     # 03d lease/restore rows; unwiped => a re-seeded table reads as LEASED
    "writer_sources",       # profiler writer attributions; unwiped => old writers on same-named tables
    "wave_gate_evaluation",
    "location_map",
    "migration_next_actions",
    "migration_next_actions_by_scope",  # per-scope batches; unwiped => a fresh install shows old focus steps
    "migration_focus_pins",  # team pin events; unwiped => a fresh install inherits the old team's pins
    "coverage_matrix",      # catalog x workspace coverage snapshot; unwiped => a fresh install reads covered
    "profiler_run_ledger",  # append-only run provenance; unwiped => sticky ZERO_DISCOVERY first-run
    "stage_run_ledger",     # append-only stage-run history; unwiped => a fresh install shows old runs
    "convert_authority",    # append-only verdicts; unwiped => a fresh install reads old convert rights
    "migration_scope",      # install scopes; unwiped => a fresh install shows the old migration scope
)


def is_safe_control_wipe(catalog: Optional[str], schema: Optional[str], confirm: bool) -> bool:
    """True only when a full control-plane wipe is explicitly confirmed for a real scope (pure).

    Guards the destructive full wipe: requires an explicit ``confirm`` AND a non-empty
    ``catalog``/``schema`` (never a placeholder/empty target). ``migration_control_config`` is
    out of scope by construction (it is never in :data:`CONTROL_BASE_TABLES`).
    """
    if not confirm:
        return False
    return bool(catalog and catalog.strip()) and bool(schema and schema.strip())


def control_plane_wipe_statements(catalog: str, schema: str) -> list[tuple[str, str]]:
    """(table, DELETE-sql) per control BASE table for a full fresh-install wipe (pure, UNSCOPED).

    Empties every row of each :data:`CONTROL_BASE_TABLES` table — the whole control plane — so
    the app renders its first-run/empty screen. Never targets :data:`CONTROL_CONFIG_TABLE` (the
    handshake) or any view. Caller MUST gate on :func:`is_safe_control_wipe`.
    """
    if not (catalog and catalog.strip() and schema and schema.strip()):
        raise ValueError(f"refusing to build wipe statements for empty scope {catalog!r}.{schema!r}")
    return [(t, f"DELETE FROM {catalog}.{schema}.{t}") for t in CONTROL_BASE_TABLES]


# --------------------------------------------------------------------------- #
# Freeze-ledger outcome classifiers (pure; consumed by the e2e stage)
# --------------------------------------------------------------------------- #
# A pilot schema whose ONLY compaction is a manual OPTIMIZE + a just-enabled Predictive
# Optimization that has not yet produced a PO-attributed commit has nothing auto-freezable.
# The freeze bracket then correctly writes 0 applied FROZEN rows and convert proceeds with no
# coverage gap — so 0 FROZEN/UNFROZEN is CORRECT there and must be a WARN, not a FAIL.


def freeze_frozen_outcome(n_ledger: int, n_frozen: int) -> tuple[str, str]:
    """Classify the post-freeze FROZEN-row assertion for the pilot.

    ``n_ledger`` = ANY-status applied (dry_run=false) ledger rows for the pilot; ``n_frozen`` =
    those actually in status FROZEN. Returns ``(tag, message)``:
      * ``WARN`` when ``n_ledger == 0`` — the pilot had no auto-freezable actor (a manual
        OPTIMIZE / not-yet-run Predictive Optimization freezes nothing), so 0 FROZEN is correct
        and convert proceeds with no coverage gap;
      * ``PASS`` when ``n_frozen > 0`` — the freeze actually froze;
      * ``FAIL`` otherwise — the ledger planned auto-freezable ops but none applied.
    """
    if n_ledger == 0:
        return ("WARN", f"no auto-freezable actor on the pilot (ledger rows=0, frozen={n_frozen}); "
                        f"0 FROZEN is correct and convert proceeds with no coverage gap")
    if n_frozen > 0:
        return ("PASS", f"freeze applied FROZEN ledger rows for the pilot "
                        f"(ledger rows={n_ledger}, frozen={n_frozen})")
    return ("FAIL", f"ledger recorded auto-freezable ops but none applied "
                    f"(ledger rows={n_ledger}, frozen={n_frozen})")


def unfreeze_outcome(n_frozen: int, n_unfrozen: int) -> tuple[str, str]:
    """Classify the post-unfreeze UNFROZEN-row assertion for the pilot.

    Returns ``(tag, message)``:
      * ``WARN`` when ``n_frozen == 0`` — nothing was frozen, so 0 UNFROZEN is correct;
      * ``PASS`` when ``n_unfrozen > 0`` — the ledger flipped to UNFROZEN;
      * ``FAIL`` otherwise — rows were frozen but none flipped to UNFROZEN.
    """
    if n_frozen == 0:
        return ("WARN", f"nothing was frozen (frozen={n_frozen}); 0 UNFROZEN is correct")
    if n_unfrozen > 0:
        return ("PASS", f"ledger flipped to UNFROZEN for the pilot "
                        f"(frozen={n_frozen}, unfrozen={n_unfrozen})")
    return ("FAIL", f"rows were frozen but none flipped to UNFROZEN "
                    f"(frozen={n_frozen}, unfrozen={n_unfrozen})")


def enable_optimization_outcome(
    *, converted: int, po_effective_on: int, al_success: int, al_manual: int,
    po_failed: int = 0, po_unreadable: int = 0,
) -> tuple[str, str]:
    """Classify the post-``08c`` PO + auto-liquid default-on assertions for the pilot schema.

    PO is asserted from the AUTHORITATIVE effective flag (``executor.read_effective_po_for_table``),
    NOT from whether ``08c`` ran an ``ALTER`` — an inherited-on table needs no ALTER yet is correctly
    PO-managed. Counts (scoped to the pilot, read after 08c):
      * ``converted``       — pilot tables in a MIGRATED state (08c's candidate set);
      * ``po_effective_on`` — converted tables whose EFFECTIVE PO is ON (ENABLE, incl. inherited);
      * ``po_unreadable``   — converted tables whose effective flag was unreadable (grants/runtime);
      * ``po_failed``       — ``audit_log`` ENABLE_PREDICTIVE_OPTIMIZATION / FAILED rows (a real
        ALTER that errored — the genuine PO regression signal);
      * ``al_success`` / ``al_manual`` — auto-liquid CLUSTER BY AUTO applied / held back (manual).
    The remainder (``converted - po_effective_on - po_unreadable``) is treated as genuinely PO-OFF.

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * WARN when ``converted == 0`` — nothing to optimize, 08c is a correct no-op;
      * FAIL when ``po_failed > 0`` — a real ENABLE PREDICTIVE OPTIMIZATION ALTER errored;
      * FAIL when any converted table is genuinely PO-OFF (``effective_off > 0``);
      * WARN when none are OFF but not all confirmed ON (``po_unreadable`` remainder) — honest;
      * FAIL when PO all-on but ``al_success == 0 and al_manual == 0`` — auto-liquid silent no-op;
      * WARN when PO all-on but ``al_success == 0 and al_manual > 0`` — every table held back;
      * PASS when all converted tables are effectively PO-ON and auto-liquid applied.
    """
    if converted == 0:
        return ("WARN", "no converted pilot tables — 08c is a no-op (nothing to optimize)")
    if po_failed > 0:
        return ("FAIL", f"08c ENABLE PREDICTIVE OPTIMIZATION failed on {po_failed} of {converted} "
                        f"converted pilot table(s) (see audit_log)")
    effective_off = converted - po_effective_on - po_unreadable
    if effective_off > 0:
        return ("FAIL", f"Predictive Optimization is effectively OFF on {effective_off} of "
                        f"{converted} converted pilot table(s)")
    if po_effective_on < converted:
        return ("WARN", f"PO effectively ON on {po_effective_on}/{converted}; {po_unreadable} "
                        f"unreadable (grants/runtime) — none proven OFF")
    if al_success == 0 and al_manual == 0:
        return ("FAIL", f"PO effectively ON on {po_effective_on}/{converted} but auto-liquid "
                        f"neither applied nor surfaced — the default-on CLUSTER BY AUTO path did nothing")
    if al_success == 0:
        return ("WARN", f"PO effectively ON on {po_effective_on}/{converted}; every converted table "
                        f"held back from auto-liquid ({al_manual} manual) — no clean-eligible table this run")
    return ("PASS", f"PO effectively ON on {po_effective_on}/{converted}; CLUSTER BY AUTO applied to "
                    f"{al_success} eligible table(s), {al_manual} held back")


def auto_liquid_optout_outcome(*, seeded: bool, held_back: int) -> tuple[str, str]:
    """Classify the opt-out (held-back) auto-liquid assertion for the seeded partitioned table.

    ``seeded`` — the partitioned opt-out table was provisioned this run; ``held_back`` — COUNT of
    OPEN ENABLE_AUTO_LIQUID actions for it after 08c (partitioned tables are excluded from the
    default CLUSTER BY AUTO set and surfaced as a manual action, per the post-convert policy).

    Returns ``(tag, message)``:
      * WARN when not ``seeded`` — nothing to assert;
      * PASS when ``held_back > 0`` — the default-on path correctly excluded the partitioned table;
      * WARN when seeded but ``held_back == 0`` — the manifest layout may be unread on this runtime,
        so the table was not classified MANUAL; honest, not a hard failure.
    """
    if not seeded:
        return ("WARN", "opt-out table not seeded — skipping held-back assertion")
    if held_back > 0:
        return ("PASS", f"partitioned opt-out table held back from default auto-liquid "
                        f"({held_back} manual ENABLE_AUTO_LIQUID action)")
    return ("WARN", "opt-out table seeded but no held-back ENABLE_AUTO_LIQUID action — layout may "
                    "be unread on this runtime")


# Two EXTERNAL tables in the pilot schema owned by someone other than the convert identity: one
# by another principal, one by a group. Owners come only from --foreign-owner / --owner-group.
FOREIGN_OWNED_TABLE = "authz_foreign_owned"
GROUP_OWNED_TABLE = "authz_group_owned"


def owner_variants(foreign_owner: Optional[str], owner_group: Optional[str]) -> list[tuple[str, str]]:
    """``(table, owner)`` for each owner variant named on the command line; a blank flag seeds none.

    The owner is written as a backtick-quoted identifier, so a name containing a backtick is
    refused rather than escaped."""
    out = []
    for table, owner in ((FOREIGN_OWNED_TABLE, foreign_owner), (GROUP_OWNED_TABLE, owner_group)):
        name = (owner or "").strip()
        if not name:
            continue
        if "`" in name:
            raise ValueError(f"owner {name!r} contains a backtick")
        out.append((table, name))
    return out


def owner_variant_outcome(*, table: str, owner: str, verdict: Optional[dict]) -> tuple[str, str]:
    """Classify the stored convert-permission verdict for one seeded owner variant (WARN-only).

    PASS when a verdict exists and captured the seeded owner (case-insensitive); the status is
    reported, not asserted, because it depends on what the convert identity holds in this
    workspace. WARN when there is no verdict or the captured owner differs."""
    if not verdict:
        return ("WARN", f"{table}: no stored convert permission verdict after the profile")
    captured = str(verdict.get("table_owner") or "")
    if captured.lower() != owner.lower():
        return ("WARN", f"{table}: verdict captured owner {captured or '(none)'!r}, expected {owner!r}")
    basis = verdict.get("basis") or "no basis"
    return ("PASS", f"{table}: owned by {captured}; convert permission {verdict.get('status')} ({basis})")


def rollback_finalize_reversal_outcome(
    *, po_enabled: int, po_reversed: int, al_enabled: int, al_reversed: int, finalize_obsolete: int
) -> tuple[str, str]:
    """Classify the RB-M2 finalize-reversal assertion after ``06 rollback``.

    Counts (scoped to the pilot, read after rollback ran with reconcile_finalize=true):
      * ``po_enabled``       — 08c ENABLE_PREDICTIVE_OPTIMIZATION / SUCCESS audit rows (durable
        proof 08c ran a per-table PO ALTER this run; append-only, so still present post-rollback);
      * ``po_reversed``      — ROLLBACK audit rows tagged ``finalize-reconcile REVERSE_PO … verified=True``;
      * ``al_enabled``       — 08c ENABLE_AUTO_LIQUID / SUCCESS audit rows (durable proof 08c applied
        CLUSTER BY AUTO to the clean set this run);
      * ``al_reversed``      — ROLLBACK audit rows tagged ``finalize-reconcile REVERSE_AUTO_LIQUID … verified=True``;
      * ``finalize_obsolete``— ENABLE_PREDICTIVE_OPTIMIZATION / ENABLE_AUTO_LIQUID actions now OBSOLETE.

    Two independent finalize deltas 08c can leave — a per-table PO ALTER and a CLUSTER BY AUTO — are
    asserted **independently** so that PO enabled by *inheritance* (no per-table ALTER, ``po_enabled
    == 0``) does not mask a real auto-liquid reversal. Returns ``(tag, message)``, ``tag`` in
    ``{'PASS','FAIL','WARN'}``:
      * WARN only when ``po_enabled == 0`` AND ``al_enabled == 0`` — 08c left no reversible finalize
        delta this run (PO inherited-on, no clean-eligible table), so there is nothing to reverse;
      * FAIL when ``po_enabled > 0`` but ``po_reversed == 0`` — 08c ran a PO ALTER yet rollback
        reversed none (RB-M2 PO finalize-reconcile did not fire);
      * FAIL when ``al_enabled > 0`` but ``al_reversed == 0`` — 08c applied CLUSTER BY AUTO yet
        rollback reversed none (RB-M2 auto-liquid finalize-reconcile did not fire);
      * PASS otherwise — every delta 08c actually left was reversed and the finalize actions obsoleted.
    """
    if po_enabled == 0 and al_enabled == 0:
        return ("WARN", "08c left no reversible finalize delta this run (PO inherited-on, no "
                        "auto-liquid applied) — nothing to reverse")
    if po_enabled > 0 and po_reversed == 0:
        return ("FAIL", f"08c enabled PO on {po_enabled} table(s) but rollback reversed none — "
                        f"RB-M2 PO finalize-reconcile did not fire")
    if al_enabled > 0 and al_reversed == 0:
        return ("FAIL", f"08c applied CLUSTER BY AUTO on {al_enabled} table(s) but rollback reversed "
                        f"none — RB-M2 auto-liquid finalize-reconcile did not fire")
    parts: list[str] = []
    if po_enabled > 0:
        parts.append(f"PO on {po_reversed}")
    elif al_enabled > 0:
        parts.append("PO via inheritance (no per-table ALTER; nothing to reverse)")
    if al_enabled > 0:
        parts.append(f"CLUSTER BY AUTO on {al_reversed}")
    return ("PASS", f"rollback reversed {', '.join(parts)} table(s), obsoleted "
                    f"{finalize_obsolete} finalize action(s)")


def rollback_ledger_closeout_outcome(
    *, applied_freeze: int, manifest_rolled_back: int, ledger_rolled_back: int
) -> tuple[str, str]:
    """Classify the RB-M4 freeze-ledger closeout assertion after ``06 rollback``.

    Counts (pilot-scoped):
      * ``applied_freeze``      — freeze_ledger rows in an APPLIED state (FROZEN / UNFROZEN /
        ROLLED_BACK, dry_run=false): proof the freeze bracket really ran this run;
      * ``manifest_rolled_back``— pilot manifest rows in state ROLLED_BACK (already asserted > 0);
      * ``ledger_rolled_back``  — freeze_ledger rows now stamped ROLLED_BACK.

    Returns ``(tag, message)``:
      * WARN when ``manifest_rolled_back == 0`` — nothing was rolled back, nothing to close out;
      * WARN when ``applied_freeze == 0`` — no freeze bracket ran, so no ledger row to stamp;
      * FAIL when tables rolled back AND a freeze bracket ran but ``ledger_rolled_back == 0`` — the
        RB-M4 closeout did not fire (a rolled-back table would leave a stale FROZEN/UNFROZEN row in
        the runbook / freeze queue);
      * PASS otherwise.
    """
    if manifest_rolled_back == 0:
        return ("WARN", "no tables rolled back — nothing to close out")
    if applied_freeze == 0:
        return ("WARN", "no applied freeze rows this run — nothing to stamp ROLLED_BACK")
    if ledger_rolled_back == 0:
        return ("FAIL", f"{manifest_rolled_back} table(s) rolled back + {applied_freeze} applied "
                        f"freeze row(s) but 0 freeze_ledger rows stamped ROLLED_BACK — RB-M4 "
                        f"closeout did not fire")
    return ("PASS", f"{ledger_rolled_back} freeze_ledger row(s) stamped ROLLED_BACK")


def maintenance_restore_outcome(
    *, job_name: str, resolved: bool, actual_pause_status, retired: bool
) -> tuple[str, str]:
    """Classify a seeded maintenance job's post-``05b`` schedule state against the RB-M3
    forward-only unfreeze contract.

    RB-M3 made ``05b`` forward-only (``post_convert_maintenance_policy=prefer_po``): a job we
    paused at ``03c`` is either
      * RETIRED — Predictive Optimization now maintains the converted table, so ``05b`` does NOT
        resume it and raises a ``RETIRE_MAINTENANCE_JOB`` action; the schedule is LEFT PAUSED; or
      * RESUMED — PO is off/unknown, so ``05b`` resumes the schedule (UNPAUSED).
    ``retired`` is derived by the caller from the ``RETIRE_MAINTENANCE_JOB`` actions' ``target_id``s
    (the job ids). Expected schedule state is PAUSED iff ``retired`` else UNPAUSED.

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * WARN when ``resolved`` is False (job not seeded — grants) or its schedule pause_status is
        not readable — skip, never fail;
      * PASS when the live schedule matches the expected state;
      * FAIL otherwise (the forward-only contract is violated).
    """
    if not resolved:
        return ("WARN", f"{job_name}: not seeded (grants) — skipping pause-status assertion")
    actual = (actual_pause_status or "").strip().upper()
    if not actual:
        return ("WARN", f"{job_name}: no schedule pause_status readable — skipping")
    expected = "PAUSED" if retired else "UNPAUSED"
    if actual == expected:
        why = ("retired — PO-managed converted table, left paused (RETIRE_MAINTENANCE_JOB raised)"
               if retired else "resumed — PO off/unknown, schedule restored")
        return ("PASS", f"{job_name}: schedule {actual} — {why}")
    return ("FAIL",
            f"{job_name}: expected {expected} (retired={retired}) but schedule is {actual!r} — "
            f"RB-M3 forward-only unfreeze contract violated")


def writer_stop_bracket_outcome(
    *, attributed: int, stop_actions_blocking: int
) -> tuple[str, str]:
    """Classify the seeded batch writer job's PRE_CONVERT stop bracket (E2E feature F).

    Counts (pilot-scoped, read after ``profile``):
      * ``attributed``            — ``writer_sources`` rows for the seeded job id with a stoppable
        ``writer_class`` (``SCHEDULED_JOB`` / ``EVENT_JOB``): the profiler attributed the job as a
        live writer from a WRITE lineage edge;
      * ``stop_actions_blocking`` — ``STOP_WRITER_JOB`` actions (``blocking=true``,
        ``phase=PRE_CONVERT``) targeting that job id.

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * WARN when ``attributed == 0`` — the WRITE edge has not landed in
        ``system.access.table_lineage`` yet (sampled, lagging feed; SR7 — absence is NOT "no
        writer"); nothing to assert;
      * FAIL when ``attributed > 0`` but ``stop_actions_blocking == 0`` — an attributed writer job
        with no blocking ``STOP_WRITER_JOB``: the pre-convert writer bracket did not generate;
      * PASS otherwise — the attributed writer job has its blocking pre-convert stop action.
    """
    if attributed == 0:
        return ("WARN", "batch writer job not yet attributed in writer_sources (lineage lag) — "
                        "nothing to assert")
    if stop_actions_blocking == 0:
        return ("FAIL", f"writer job attributed ({attributed} row(s)) but 0 blocking "
                        f"STOP_WRITER_JOB action(s) — pre-convert writer bracket did not generate")
    return ("PASS", f"attributed writer job has {stop_actions_blocking} blocking STOP_WRITER_JOB "
                    f"action(s)")


def writer_pause_phase_outcome(
    *, bracketed: bool, resolved: bool, expected: str, actual: Optional[str]
) -> tuple[str, str]:
    """Classify the seeded writer job's live pause_status at a freeze-bracket phase (feature F).

    The writer job is FORWARD-restarted (never retired — retire is maintenance-only): the freeze
    bracket pauses it at ``03c`` and ``05b`` resumes it (it was UNPAUSED before the freeze).

      * ``bracketed`` — the writer was attributed AND has a blocking ``STOP_WRITER_JOB`` (so the
        bracket is expected to have acted). When ``False`` (lineage lag / not attributed), WARN;
      * ``resolved``  — the job id resolved by exact name;
      * ``expected``  — ``'PAUSED'`` (asserted after ``03c`` freeze) or ``'UNPAUSED'`` (after ``05b``);
      * ``actual``    — ``executor.read_job_pause_status(...)`` live value.

    Returns ``(tag, message)``: WARN when not bracketed or unresolved; PASS when
    ``actual == expected``; FAIL otherwise.
    """
    if not bracketed:
        return ("WARN", f"writer job not bracketed (lineage lag) — skipping {expected} pause check")
    if not resolved:
        return ("WARN", "writer job id did not resolve — skipping pause check")
    if actual == expected:
        return ("PASS", f"writer job schedule {expected} (platform truth)")
    return ("FAIL", f"writer job schedule expected {expected}, got {actual}")


def schedule_verdict_outcome(
    *, label: str, expected: str, actual: Optional[str], next_write_present: bool
) -> tuple[str, str]:
    """Classify a seeded schedule-twin's live ``writer_schedule_verdict`` (E2E feature G).

    The profiler stamps ``writer_schedule_verdict`` + ``next_scheduled_write_at`` on each twin's
    manifest row from the live cron (``job_schedule.writer_verdict``). The far-out twin (daily ~8h
    cron) must read ``SAFE_NOW``; the imminent twin (every-5-min cron) must read ``WARN_WINDOW`` —
    both deterministic because the profiler's 15-min schedule margin sits between the two gaps.

      * ``label``             — 'far-out' | 'imminent' (for the message);
      * ``expected``          — 'SAFE_NOW' | 'WARN_WINDOW';
      * ``actual``            — the manifest ``writer_schedule_verdict`` (``None`` when the row/column
        has no verdict yet);
      * ``next_write_present``— whether ``next_scheduled_write_at`` is non-NULL.

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * WARN when ``actual`` is ``None`` or ``'UNKNOWN'`` — the WRITE edge / schedule read has not
        landed yet (lineage lag / Jobs-API unavailable; SR7 — never a false FAIL);
      * FAIL when ``actual`` is a definite verdict != ``expected`` (a real regression), OR when it
        matches but ``next_scheduled_write_at`` is NULL (the cron next-fire was not computed);
      * PASS otherwise — the twin reads its expected verdict with a live ``next_scheduled_write_at``.
    """
    if actual is None or actual == "UNKNOWN":
        return ("WARN", f"{label}: no definite schedule verdict yet (lineage lag / schedule "
                        f"unreadable) — got {actual}")
    if actual != expected:
        return ("FAIL", f"{label}: expected {expected}, got {actual}")
    if not next_write_present:
        return ("FAIL", f"{label}: verdict {actual} but next_scheduled_write_at is NULL "
                        f"(cron next-fire not computed)")
    return ("PASS", f"{label}: {actual} with next_scheduled_write_at set (computed live from cron)")


# --------------------------------------------------------------------------- #
# L5 notify/comms surfaces (pure oracles for backend/.e2e/e2e_notify.py).
#
# The three "how does an operator learn about pending work" surfaces are PULL/EXPORT, not push:
#   * comms_feed        — per-wave-member owner advisory, written by the wave planner (02);
#   * digest            — open blocking actions + stale freezes, ``digest.build_digest`` (pure);
#   * runbook export    — phase-ordered manual statements, ``runbook.build_runbook`` (pure) +
#                          the ``migration_runbook`` view.
# The e2e driver reads the live control tables via SQL and re-runs the SAME pure builders the
# ``09_cockpit`` (manual=digest / export_runbook) edge uses, then classifies with these helpers.
# All degrade to WARN on an empty/clean surface (never a false FAIL on a quiet estate).
# --------------------------------------------------------------------------- #


def comms_feed_outcome(
    *, total_rows: int, with_notice: int, scope_label: str = "estate"
) -> tuple[str, str]:
    """Classify the ``comms_feed`` surface (L5 N2) — the owner-facing per-wave advisory.

    Counts (read from the ``comms_feed`` table the wave planner writes):
      * ``total_rows``  — comms_feed rows for the scope;
      * ``with_notice`` — of those, how many carry a non-empty ``owner_notice`` (the rendered
        owner advisory: ownership change / planned window / blast radius).

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','WARN'}`` (never FAIL — an empty comms feed is a
    quiet estate, not a defect):
      * WARN when ``total_rows == 0`` — planner has not produced a plan yet (or grants) — nothing to
        assert;
      * WARN when rows exist but none carry an ``owner_notice`` — the advisory did not render;
      * PASS otherwise — the comms feed reflects the planned tables with owner advisories.
    """
    if total_rows <= 0:
        return ("WARN", f"comms_feed empty for {scope_label} — no plan rows yet "
                        f"(planner not run / grants); nothing to assert")
    if with_notice <= 0:
        return ("WARN", f"comms_feed has {total_rows} row(s) for {scope_label} but none carry an "
                        f"owner_notice — advisory not rendered")
    return ("PASS", f"comms_feed reflects {total_rows} planned table(s) for {scope_label}; "
                    f"{with_notice} carry an owner_notice advisory")


def digest_reflects_estate(
    *, blocking_count: int, stale_freeze_count: int, open_blocking_actions: int,
    text_rendered: bool,
) -> tuple[str, str]:
    """Classify the digest surface (L5 N1) — does ``build_digest`` reflect the live estate?

    A directional cross-surface oracle (like the L3 MCP oracle): the driver reads the SAME inputs
    the ``09_cockpit manual=digest`` edge reads (action-queue-open view, freeze_ledger, manifest
    blockers), calls ``digest.build_digest`` in-process, and independently counts open BLOCKING
    actions straight from the action-queue-open view.

    Args:
      * ``blocking_count``        — ``digest['blocking_count']`` (blocking runbook entries);
      * ``stale_freeze_count``    — ``digest['stale_freeze_count']``;
      * ``open_blocking_actions`` — independent count of open blocking rows in the action-queue-open
        view (the ground truth the digest must not under-report);
      * ``text_rendered``         — whether ``digest.to_text(...)`` produced a non-empty string.

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * FAIL when ``to_text`` produced nothing — the delivery artifact is broken;
      * FAIL when the estate has open blocking actions but the digest surfaced 0 blocking entries —
        the digest under-reports blockers (would tell an operator the convert is clear when it is
        not);
      * WARN when there is nothing blocking and no stale freeze — a quiet estate, digest correctly
        empty; nothing to assert;
      * PASS otherwise — the digest surfaced the estate's blockers / stale freezes.
    """
    if not text_rendered:
        return ("FAIL", "digest.to_text produced no output — delivery artifact broken")
    if open_blocking_actions > 0 and blocking_count == 0:
        return ("FAIL", f"estate has {open_blocking_actions} open blocking action(s) but the digest "
                        f"surfaced 0 blocking entries — digest under-reports blockers")
    if blocking_count == 0 and stale_freeze_count == 0:
        return ("WARN", f"quiet estate (0 open blocking actions, 0 stale freezes) — digest correctly "
                        f"reports nothing to attend to")
    return ("PASS", f"digest surfaced {blocking_count} blocking entry(ies) + {stale_freeze_count} "
                    f"stale freeze(s), consistent with {open_blocking_actions} open blocking "
                    f"action(s)")


def runbook_reflects_estate(
    *, builder_entries: int, view_rows: Optional[int], phase_ordered: bool
) -> tuple[str, str]:
    """Classify the runbook-export surface (L5 N3) — does ``build_runbook`` reflect the estate, and
    does the exported script agree with the ``migration_runbook`` view?

    Args:
      * ``builder_entries`` — statements from ``runbook.build_runbook`` over the live SQL reads
        (the same builder the ``export_runbook`` edge writes to a Volume);
      * ``view_rows``       — row count of the ``migration_runbook`` view (``None`` when the view is
        unavailable — grants/runtime);
      * ``phase_ordered``   — whether the builder entries are non-decreasing by ``phase_rank``
        (PRE_CONVERT → CONVERT → POST_CONVERT).

    Returns ``(tag, message)``, ``tag`` in ``{'PASS','FAIL','WARN'}``:
      * WARN when the builder produced 0 statements — clean estate, nothing to export;
      * FAIL when statements exist but are not phase-ordered — the export would run POST before PRE;
      * WARN when the view is unavailable — parity skipped (still proved the builder + ordering);
      * FAIL when the builder has statements but the view is empty — the two surfaces disagree;
      * PASS otherwise — builder and view both reflect the estate's pending statements, phase-ordered.
    """
    if builder_entries <= 0:
        return ("WARN", "runbook builder produced 0 statements — clean estate (nothing pending); "
                        "nothing to export")
    if not phase_ordered:
        return ("FAIL", f"runbook builder produced {builder_entries} statement(s) but they are not "
                        f"phase-ordered (PRE_CONVERT → CONVERT → POST_CONVERT)")
    if view_rows is None:
        return ("WARN", f"runbook builder produced {builder_entries} phase-ordered statement(s); "
                        f"migration_runbook view unavailable — skipped view parity")
    if view_rows <= 0:
        return ("FAIL", f"runbook builder produced {builder_entries} statement(s) but "
                        f"migration_runbook view is empty — surfaces disagree")
    return ("PASS", f"runbook builder ({builder_entries}) and migration_runbook view ({view_rows}) "
                    f"both reflect the estate's pending statements; phase-ordered")


def phase_ranks_nondecreasing(phase_ranks: Iterable) -> bool:
    """True when ``phase_ranks`` (the builder entries' ``phase_rank``s, in order) never decreases.

    ``None``/missing ranks are treated as the sentinel ``9`` (unknown phase sorts last), matching
    ``runbook._phase_rank``. Pure helper so the driver's ordering check is unit-tested here.
    """
    prev = -1
    for r in phase_ranks:
        rank = 9 if r is None else int(r)
        if rank < prev:
            return False
        prev = rank
    return True


def far_out_cron(now: datetime) -> str:
    """A daily Quartz cron whose next fire is ~8h from ``now`` (UTC) — always past the profiler's
    15-min schedule margin, so the seeded far-out schedule twin reads a deterministic ``SAFE_NOW``.

    Fires once/day at ``(now.hour + 8) % 24 : now.minute`` UTC. Because the E2E seeds at ``activity``
    and the profiler stamps the verdict minutes-to-an-hour later, the next fire is still many hours
    out at verdict time (a robust ``SAFE_NOW``); it does NOT fire during the run — only the single
    ``run_now`` writes, for lineage attribution. Pure (no I/O), so it is unit-tested here.
    """
    t = now + timedelta(hours=8)
    return f"0 {t.minute} {t.hour} ? * *"


STAGES: tuple[str, ...] = ("provision", "activity", "lineage_wait", "profile", "scan", "plan", "e2e")
PRESETS: dict[str, tuple[str, ...]] = {
    "provision_only": ("provision",),
    "populate": ("provision", "activity", "lineage_wait", "profile", "scan", "plan"),
    "full": ("provision", "activity", "lineage_wait", "profile", "scan", "plan", "e2e"),
}
_ORDER = {s: i for i, s in enumerate(STAGES)}


def resolve_stages(*, stages: Optional[list[str]], preset: Optional[str], no_lineage: bool) -> list[str]:
    if preset is not None and preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
    chosen = set(PRESETS[preset]) if preset else set()
    for s in (stages or []):
        if s not in _ORDER:
            raise ValueError(f"unknown stage {s!r}; choose from {list(STAGES)}")
        chosen.add(s)
    if not chosen:
        raise ValueError("no stages selected: pass --preset or --stage")
    if no_lineage:
        chosen.discard("lineage_wait")
    return sorted(chosen, key=_ORDER.__getitem__)


# --------------------------------------------------------------------------- #
# Liquid-clustering step: the seeded ZORDER and manual-key tables, checked live in --e2e
# --------------------------------------------------------------------------- #

def layout_from_extended_json(extended_json) -> dict:
    """``{"cluster_by_auto", "clustering_columns"}`` from ``DESCRIBE TABLE EXTENDED … AS JSON``.

    Both come from the string table properties (``clusterByAuto`` = ``"true"``/``"false"``,
    absent = never turned on; ``clusteringColumns`` = a JSON list string, absent = no keys).
    Anything unreadable is ``None`` (unknown), never assumed.
    """
    import json as _json

    from . import layout as _layout

    cba = _layout.parse_cluster_by_auto(extended_json)
    obj = extended_json
    if isinstance(obj, str):
        try:
            obj = _json.loads(obj)
        except Exception:  # noqa: BLE001
            obj = None
    props = obj.get("table_properties") if isinstance(obj, dict) else None
    keys = None
    if isinstance(props, dict):
        raw = props.get("clusteringColumns")
        if raw is None:
            keys = []
        else:
            try:
                val = _json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                val = None
            if isinstance(val, list):
                keys = [".".join(map(str, k)) if isinstance(k, list) else str(k) for k in val]
    return {"cluster_by_auto": cba, "clustering_columns": keys}


def clustering_layout_outcome(
    label: str, *, layout, expect_auto: bool, expect_keys, seeded: bool = True, converted: bool = True,
) -> tuple[str, str]:
    """Classify one seeded table's live clustering layout against what the step should leave.

    After ``08c``: the ZORDER table is AUTO with its ZORDER columns as keys, the manual-key table is
    AUTO with its keys kept. After ``06 rollback``: AUTO is off and the keys are what they were.
    WARN when the table was not seeded / not converted, or the layout could not be read; FAIL when
    the flag or the keys differ; PASS otherwise.
    """
    if not seeded:
        return ("WARN", f"{label}: table not seeded this run — nothing to assert")
    if not converted:
        return ("WARN", f"{label}: table did not convert this run — nothing to assert")
    lay = layout or {}
    cba, keys = lay.get("cluster_by_auto"), lay.get("clustering_columns")
    if cba is None or keys is None:
        return ("WARN", f"{label}: live layout unreadable (cluster_by_auto={cba!r}, keys={keys!r})")
    want = list(expect_keys or [])
    if cba is not expect_auto or list(keys) != want:
        return ("FAIL", f"{label}: expected auto={expect_auto} keys={want}, "
                        f"found auto={cba} keys={list(keys)}")
    return ("PASS", f"{label}: auto={cba} keys={list(keys)}")
