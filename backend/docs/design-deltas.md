# Backend design deltas — estate coverage & federation

Permanent record of the **why** behind the tooling's estate-coverage design: the two
filters that bound what any single profiler run can see, and the deliberate choice to
**federate** the tooling per workspace rather than centralize a shared control plane.

This is the backend companion to [`app/docs/design-deltas.md`](../../app/docs/design-deltas.md)
(which records where the App diverges from its mockup). Where those are app↔mockup
deltas, these are architecture decisions for the discovery/coverage surface. Column and
table contracts introduced by these deltas are specified in
[`contracts/control-tables.md`](./contracts/control-tables.md); the design rationale lives
here so the contract stays a dry reference.

---

## The two filters that bound coverage (the problem)

`job_00_profiler` discovers EXTERNAL tables from `system.information_schema.tables`
(falling back to per-catalog `<catalog>.information_schema.tables`). Both are subject to
**two orthogonal, non-negotiable Unity Catalog filters** — and so are the enrichment
passes (`DESCRIBE DETAIL`/`DESCRIBE HISTORY`, `system.access.table_lineage`) **and the
`ALTER TABLE … SET MANAGED` itself**:

1. **Privilege filter (per principal).** `information_schema` implements *privilege-aware
   auto-filtering*: a principal sees **only the objects it has UC privileges to access**,
   with **no explicit `SELECT` grant** required, and the result set changes as privileges
   change. (`BROWSE` on a catalog is enough to *see* metadata in `information_schema`
   without `USE CATALOG`/`USE SCHEMA`/`SELECT`; `READ METADATA` at the metastore inherits
   metadata visibility to all objects.)
   Source: <https://learn.microsoft.com/en-us/azure/databricks/sql/language-manual/sql-ref-information-schema>

2. **Workspace–catalog binding filter (per workspace).** When a catalog is bound to
   specific workspaces, "**Information schema queries return only the catalogs accessible
   in the current workspace**," and access from an unbound workspace is **denied even for
   users with explicit grants**. Lineage and Catalog Explorer are scoped the same way.
   Source: <https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/access-control/workspace-catalog-binding>

**Consequence:** a single profiler run answers only *"what this `run_as` identity, sitting
in this workspace, is allowed to see."* Full-estate coverage is the **union over
(principals × workspaces)** that collectively reach every bound catalog and every
privileged object. Neither filter can be engineered around — binding in particular
**overrides grants**, so no amount of privilege makes an unbound catalog visible.

---

## D1 — Multi-run union is the coverage model (already true at the data layer)

**Decision.** Treat "run the profiler repeatedly — as different principals, in different
workspaces, with different `catalog_filter`/`schema_filter` — and accrete into one
manifest" as the *supported, intended* coverage pattern.

**Why it already works.** `manifest.merge_manifest_rows` is
`MERGE … WHEN MATCHED UPDATE … WHEN NOT MATCHED INSERT` on `full_name` and **never deletes
rows it did not see**. A filtered/partial run therefore only ever *adds or refreshes*;
it cannot erase another run's coverage. The union is already correct; what was missing is
**provenance** (which run/workspace/principal produced a row) and **observability** (what a
run could *not* see). WS1 adds that (see D2).

**Rejected alternative.** Requiring a single "sees-everything" profiler run. That would
demand one identity with estate-wide privilege *and* a workspace bound to every catalog —
which the binding model exists to prevent, and which many customers will not grant.

- **Status update (surface):** staleness is now also a read-only `stale_manifest` view
  (created by `bootstrap.ensure_control_plane`), so the App/dashboard can show stale rows
  without re-running the pure logic. Semantics match `reconcile.stale_full_names`
  (NULL/`< covering_cutoff`; nothing emitted without a covering run). Never deletes.

---

## D2 — Discovery provenance is first-class (WS1)

**Decision.** Every manifest row records **who/where/when** it was discovered, and every
profiler run appends one row to a new append-only `profiler_run_ledger`.

- New `table_manifest` columns: `discovery_run_id`, `discovered_in_workspace_id`,
  `discovered_by_principal`, `discovery_source`, `first_seen_at` (**write-once**),
  `last_seen_at` (refreshed each run).
- New control table `profiler_run_ledger` (one row per run): identity (`workspace_id`,
  `metastore_id`, `principal`), scope (`catalog_filter`/`schema_filter`), and coverage
  counters (`catalogs_visible`, `catalogs_scanned`, `tables_discovered`,
  `system_information_schema_ok`, `discovery_source`, timestamps, notes).

**Why.** Turns "did we cover the estate?" from a guess into a queryable fact, and becomes
the **dedupe/join key** if per-workspace manifests are ever rolled up (D5). Follows the
existing "each module owns its table's DDL/StructType/row-builder/column tuple" convention
(`run_ledger.py`), and the manifest **auto-evolution** convention (declare in
`MANIFEST_COLUMNS`; existing rows backfill NULL via `ALTER TABLE ADD COLUMNS`).

**Write-once nuance.** `first_seen_at` must survive re-profiling, so it joins `state` and
the deferred/later-owned columns in the MERGE's *preserve-on-update* set
(`INSERT_ONLY_COLUMNS`). The other five provenance columns are discovery-owned and
overwritten each run. This split is exposed as the pure, testable
`manifest.manifest_update_columns()`.

---

## D3 — Federation over a shared control plane (per-workspace install)

**Decision.** When bindings partition the estate across workspaces, **install the tooling
once per workspace that owns bound catalogs** — each with its **own workspace-local
control plane** — and union coverage across installs. Do **not** default to a single shared
control plane spanning workspaces.

**Why.**

- **Bindings force compute presence anyway.** Discovery, enrichment, *and* the convert all
  require a job running *in* a workspace the catalog is bound to. Compute cannot be
  centralized, so the tooling's stages must run in each covering workspace regardless.
- **A shared control plane hits the same wall.** One shared manifest lives in one catalog,
  which would have to be bound to *every* covering workspace — i.e. punch a hole through
  the very binding policy that created the problem. Per-workspace control planes sidestep
  it entirely.
- **Databricks Apps / dashboards / Genie are workspace-scoped by nature.** An App instance
  lives in one workspace and queries that workspace's control plane. "Install the App in
  each workspace" is not a compromise — it is the only model that exists.
- **The bundle already models this.** `dev` / `e2e` / `prod` are targets pointing at
  different hosts; each `bundle deploy -t <target>` stands up its own jobs + App +
  dashboard + Genie + `migration_control_config`. Setting a workspace-local
  `manifest_catalog` per target is the reference sharding pattern.

**Rejected alternative.** A cross-workspace shared control plane as the default. Kept only
as an *optional read-only rollup* (D5), never as the coverage mechanism.

---

## D4 — Two guardrails make federation safe

Federation introduces two failure modes that dedicated guardrails must cover:

1. **`manifest_catalog` collision on a shared metastore.** Workspaces in a region typically
   share a metastore; `dev` and `prod` both default `manifest_catalog: main`. If both
   installs write `main.migration` **and** `main` is reachable from both, they silently
   share one manifest (or one install fails if `main` is bound elsewhere). **Rule:** choose
   `manifest_catalog` deliberately per install — a **workspace-local/bound catalog** for
   true sharding, or a shared governance catalog only if policy
   explicitly allows. A preflight warns on the ambiguous default-on-shared-metastore case.

2. **Overlapping bindings → double-conversion.** A catalog bound to *two* workspaces lands
   in both manifests, and nothing stops both installs from issuing `SET MANAGED` on the
   same table. **Rule:** read-only stages (`00`/`03`/`03b`) may run in every workspace;
   the **mutating stages (`04` convert / `06` rollback / `07` cleanup) get exactly one
   owning workspace per catalog**. `discovered_in_workspace_id` (D2) is the signal a guard
   uses to refuse a convert from a non-owning workspace.

**Status: implemented (WS5).** Pure logic in `migration.reconcile`
(`is_convert_permitted` / `foreign_ownership_blockers` fail-closed; `manifest_catalog_collision_warning`;
`stale_full_names` realizing D1 freshness over the union). Wired as: the **ownership guard**
fails loud before any `SET MANAGED` / `UNSET MANAGED` in `04_convert` / `06_rollback`
(`owning_workspace` + `require_owning_workspace` params, never mutating manifest state); the
**collision preflight** (always-on) and the opt-in **staleness report** (`report_stale`,
print-only) in `00_profiler`. `07_cleanup` inherits ownership safety because it operates only
on already-converted rows the owning workspace produced.

   ---

## D5 — Estate rollup is optional and read-only (deferred)

**Decision.** A single-pane-of-glass across per-workspace installs is a **reporting
nicety, not a correctness requirement**, and is deferred (WS6).

**Why / caveat.** Any aggregator that unions per-workspace manifests must itself be able to
*read* each control catalog — hitting the **same binding wall**. When built, it is
read-only, keyed on the D2 provenance columns (so the same `full_name` seen by two
overlapping-binding workspaces de-dupes cleanly), and it never drives mutation.

---

## D6 — Coverage is reported honestly (WS2), never overstated

**Decision.** Each run emits + persists a coverage report (identity, workspace, metastore;
catalogs visible vs. scanned; per-catalog system-vs-fallback-vs-error; explicit disclaimer)
and, where privilege permits, a **catalog × workspace coverage matrix** from the
workspace-bindings API (WS3).

**Why.** Consistent with the repo's standing "**degrade honestly / no evidence ≠ safe**"
ethos: an object's *absence* from a run is never proof it does not exist — it may be
invisible to this principal or bound to another workspace. The report says exactly that,
in the run log and the App.

**Status: implemented.** `coverage_report` view (WS2, `migration.coverage_report`) + the
opt-in `coverage_matrix` blind-spot table (WS3, `migration.bindings` / `scan_bindings=true`);
both in the column contract.

---

## Privilege contract (tiered) — see WS4

The profiler's passes have different privilege needs; the plan documents and preflights a
tiered least-privilege recipe:

| Pass | Runs | Minimum privilege |
|---|---|---|
| Discovery | `information_schema.tables` | `BROWSE` per catalog (estate-wide via `All account users`) |
| Enrichment | `DESCRIBE DETAIL`/`HISTORY` | `USE CATALOG`+`USE SCHEMA`+`SELECT`. `READ METADATA` does not substitute (tested 2026-10-26; see [`install-and-permissions.md`](./install-and-permissions.md#3-what-a-read-only-profiler-identity-actually-needs-tested)) |
| System tables | `system.access.*`, `system.compute.*`, `system.lakeflow.*` | `USE CATALOG system` + explicit `SELECT` (these do **not** auto-filter) |
| Workspace reach | all of the above | run **in each workspace** with bound catalogs |

**Status: implemented (WS4).** Pure tier classification + exact-GRANT rendering in
`migration.preflight`, surfaced by the opt-in `run_preflight=true` profiler cell (print-only,
never executes a GRANT). Full recipe + setup: [`operations/privileges.md`](./operations/privileges.md).

**Out of scope (permanent):** bypassing bindings; a shared control plane as the default;
running any conversion from the editor.

---

## D7 — Dashboard rationalization surfaces coverage (2026-08-30)

**Decision.** Consolidate the AI/BI scoping dashboard from **16 → 11** lifecycle-ordered pages,
adding a dedicated **Coverage & Estate** page that surfaces the WS2/WS3 coverage self-diagnostic
(D6) and manifest staleness (D1). The dashboard was **at** the 16-page Lakeview cap; the
consolidation frees 5 slots for future signals.

**Why.** The old IA had grown page-per-signal, colliding with the hard cap and scattering related
lifecycle concerns. Grouping by the migration lifecycle (act → filter → scope → risk → effort →
landscape → consumers → storage → coverage → next action → freeze) keeps each page a coherent
decision surface, and gives the honestly-reported coverage work (D6) a first-class home instead of
leaving "what did we *not* see" buried.

**The six merges** performed by `dashboard_layout.rationalize` (each guarded so a completed step is
skipped — the transform is idempotent):

1. `by_catalog_schema` → `custom_insight` (renamed **"Landscape (table / schema)"**).
2. `streaming_restart` → `consumer_runtime` **and** `path_access_page` → `consumer_runtime`
   (renamed **"Consumers & Runtime"**).
3. `comms_feed_page` → `action_queue` (renamed **"Actions & Comms"**).
4. `convert_windows` → `freeze_ledger` (predictive convert-timing folded into the
   convert-execution page).
5. New page **`coverage_estate`** ("Coverage & Estate") added before `next_action`, reading the
   `coverage_report` view + the `coverage_matrix` blind-spot rows (`status = 'bound_not_covered'`)
   plus `recency` staleness from `table_manifest`.
6. `prioritization_staleness` → `coverage_estate` (staleness / `recency` is an observability
   concern, so it belongs with the coverage self-diagnostic).

**Final IA (11 pages, lifecycle-ordered):** Actions & Comms (`action_queue`), Filters
(`global_filters`), Scope (`scope`), Risk (`risk`), Effort & waves (`effort`), Landscape (table /
schema) (`custom_insight`), Consumers & Runtime (`consumer_runtime`), Storage & Region
(`storage_region`), Coverage & Estate (`coverage_estate`), Next action (`next_action`), Freeze &
actions (`freeze_ledger`).

**Move, never delete.** Every merge relocates widgets (offset in `y` below the destination's
existing layout) rather than dropping them, so every declared dataset stays referenced by at least
one widget — a `check_invariants` requirement. Net datasets grew **25 → 27** (the two new
`coverage_report` / `coverage_matrix_blindspots` datasets the Coverage & Estate page reads).

**Idempotent transform tooling + guard.** The page structure is a **tested pure library**,
`backend/src/migration/dashboard_layout.py` (loads the inline `serialized_dashboard` dict,
transforms it, re-emits it — no Spark/SDK), applied to the YAML by
`backend/scripts/rationalize_dashboard.py`. `rationalize` is **idempotent** (every step guarded by
a `_has(slug)` check), so the apply script is a no-op on an already-rationalized file and the
real-dashboard test can run against the rewritten source. `backend/src/tests/test_dashboard_scope.py`
guards the structural invariants: the file parses; `check_invariants` is clean (≤ 16 pages /
`MAX_PAGES`, page budget 12 / `TARGET_MAX_PAGES`, ≤ 100 datasets, unique page slugs +
dashboard-wide unique widget names, every declared dataset referenced by a widget, no widget
referencing an undefined dataset); and `coverage_estate` is present. Do **not** hand-edit the
~6k-line `serialized_dashboard`; the inline block in `backend/resources/dashboard_scope.yml` is the
only dashboard source and the only deploy source — there is no compiled `.lvdash.json` in the repo.

---

## Related

- Column contract: [`contracts/control-tables.md`](./contracts/control-tables.md)
- Architecture (manifest merge, auto-evolution, degrade-honestly): [`architecture.md`](./architecture.md)
