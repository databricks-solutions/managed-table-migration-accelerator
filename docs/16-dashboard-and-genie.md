# 16 - AI/BI dashboard and Genie space

Two read-only views over the persisted `<catalog>.migration.*` control tables ship with the bundle:
an AI/BI (Lakeview) **dashboard** for scoping and progress, and a **Genie space** for asking
questions about the migration in plain language. Neither changes anything.

The dashboard deploys with the bundle (direct engine, pinned by `bundle.engine: direct` in
`databricks.yml`). The Genie space is **not** a bundle resource: the install-time setup job
(`job_99_setup`) creates or updates it through the API, so a fresh `bundle deploy` never fails on
control tables that do not exist yet. The profiler stays free of any Genie or app provisioning, so a
customer can run it against their estate with no app installed.

## Deploy resilience: the control plane is pre-created empty

The dashboard and Genie space read tables that later stages populate (`wave_plan`, `comms_feed`,
`audit_log`, `migration_next_actions`, and the `migration_progress` / `wave_status` views). To keep
them from failing on "table or view does not exist", the profiler and the setup job call
`bootstrap.ensure_control_plane(...)` first. It idempotently creates **every** control table and
view, empty, reusing the per-module creators so no DDL is duplicated. Re-running never clobbers real
data (`CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE VIEW`).

## The dashboard

The source is the inline `serialized_dashboard` in `backend/resources/dashboard_scope.yml`. It is the
**only** dashboard source and the only deploy source; there is no compiled `.lvdash.json` in the
repo. It has 11 pages, ordered by the migration lifecycle:

| Page | What it shows |
|---|---|
| Actions & Comms | The open action queue and the comms feed (owners, downstream consumers, recommended window). |
| Filters | Estate-wide filters. |
| Scope | Counts, bytes and effort; eligibility and size bands. |
| Risk | UniForm, Delta Sharing, external writers, Compatibility Mode, streaming, lifecycle state, blocked tables. |
| Effort & waves | Estimated copy time and downtime, effort by size band, and the wave plan with the recommended warehouse and cores. |
| Landscape (table / schema) | Table- and schema-level breakdowns. |
| Consumers & Runtime | Who reads and writes each table and on which runtime. |
| Storage & Region | Storage locations and regions. |
| Coverage & Estate | Signal coverage across the estate (`coverage_report`, `coverage_matrix`). |
| Next action | The conductor's current recommendation. |
| Freeze & actions | The freeze ledger and the open freeze and restore actions. |

Table names are fully qualified through `${var.dataset_catalog}.${var.dataset_schema}`, which the
bundle substitutes into the inlined JSON, so nothing hardcodes a catalog or schema. (The JSON is
inlined rather than referenced by `file_path` because AI/BI does not reliably apply the
`dataset_catalog` / `dataset_schema` resource fields as the query context.) Re-publish after the
profiler runs: `databricks lakeview publish <id> --warehouse-id <warehouse-id>`.

### Editing the dashboard

- **Do not hand-edit the roughly 6,000-line `serialized_dashboard`.** Page structure is transformed
  by the tested pure library `backend/src/migration/dashboard_layout.py` and applied with the
  idempotent script `python backend/scripts/rationalize_dashboard.py` (a no-op on an
  already-rationalized file). Widgets are moved, never deleted, so every dataset stays referenced.
- `backend/src/tests/test_dashboard_scope.py` guards the structure: at most 16 pages (the Lakeview
  cap; the page budget is 12), at most 100 datasets, unique page slugs and widget names, every
  dataset referenced by a widget, and no widget referencing an undefined dataset.
- For a click-through cell, use the version-2 `templatedURL` convention described under "Editing the
  dashboard" in [AGENTS.md](../AGENTS.md), and keep the catalog and schema parameterized.
- The rationale for each page merge is recorded in
  [backend/docs/design-deltas.md](../backend/docs/design-deltas.md).
- To round-trip a dashboard edited in the UI back to source, use
  `databricks bundle generate dashboard --existing-path ...`, then deploy with `--force` on drift.

## The Genie space

The space is built in code by `backend/src/migration/genie_space.py` (`build_serialized_space`) and
created or updated by `job_99_setup` through the low-level Genie REST API (`w.api_client.do` against
`/api/2.0/genie/spaces`), not the SDK's `w.genie.create_space` / `update_space`. Those convenience
methods exist only on newer SDK builds, and the serverless runtime ships an older SDK without
`update_space`; `api_client.do` exists on every version. Setup is idempotent (it matches an existing
space by title, then updates it), and each step is best-effort: a Genie or permission error is
logged and never fails the setup job.

- **Sources** span the operator surface: `table_manifest`, `wave_plan`, `migration_progress`,
  `wave_status`, `audit_log`, and the action, runbook, freeze and next-step views
  (`action_queue_open`, `migration_runbook`, `freeze_ledger_open`, `migration_next_actions`,
  `action_summary`), with `join_specs` and `sql_snippets` to steer SQL generation.
- **Strict `serialized_space` shape (v2).** `join_specs[].left` / `right` are objects
  (`{identifier, alias}`), each `sql` array is
  `[condition, "--rt=FROM_RELATIONSHIP_TYPE_<CARDINALITY>--"]`, and `data_sources.tables` must be
  pre-sorted by identifier (other id collections likewise). The builder enforces this; the API
  rejects violations. Pass `serialized_space` as a JSON string, not a dict.
- **Knowledge library.** The operator docs in [backend/docs/genie/](../backend/docs/genie/) (registry
  `genie_space.KNOWLEDGE_DOCS`, each topic written once) are uploaded by `job_99_setup` to the
  `genie_knowledge` Unity Catalog volume, which is attached to the space. The same run removes
  retired doc files by exact name. The agent reads them in Agent mode (Beta); see
  [backend/docs/genie/00_setup.md](../backend/docs/genie/00_setup.md). Answer quality is scored by
  `backend/.e2e/e2e_genie_knowledge.py`.
- **Permissions.** `genie_grant_users` (default `true`) grants the `users` group `CAN_RUN`; set it to
  `false`, as the `e2e` target does, for a creator-only space.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `warehouse_id` | blank | The SQL warehouse behind the dashboard and Genie. Required for both; leave blank to skip Genie. |
| `ensure_genie` | `true` | Whether `job_99_setup` creates or updates the Genie space. |
| `genie_grant_users` | `true` | Grant the `users` group `CAN_RUN` on the space (`false` = creator-only). |
| `dataset_catalog` / `dataset_schema` | `main` / `migration` | Where the dashboard and Genie read the control tables. Match the manifest catalog and schema. |

```bash
databricks bundle deploy -t <target> --profile <profile> --var="warehouse_id=<warehouse-id>"
```

The app picks up a new Genie space only after a restart, and a restart means a bundle redeploy, not
`databricks apps stop` / `start`. See [Restarting the app](./00-installation.md#restarting-the-app)
and the cold-warehouse note under [Troubleshooting](./00-installation.md#troubleshooting).

The embedded dashboard in the app needs no grant for the app's service principal: with basic
embedding it renders under the viewing user's identity.
