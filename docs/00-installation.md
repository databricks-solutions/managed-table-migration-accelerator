# 00 - Installation

This page takes you from a fresh clone to a running control plane: the jobs, the dashboard, the
Genie space and the admin app, all deployed by one Databricks Asset Bundle. The root
[README](../README.md#install) has the short version; this is the full one, including the
permissions behind each step and what to do when something does not come up.

Installing changes no table. Nothing here runs `SET MANAGED`; a person does that later, from the
deployed jobs.

## Before you start

| You need | Why |
|---|---|
| A Unity Catalog workspace | Every table this tool converts is a Unity Catalog EXTERNAL table. |
| Databricks Runtime 17.3 LTS+ or serverless | `SET MANAGED`, `UNSET MANAGED` and the day-14 `VACUUM` need it. Every stage job runs on serverless; on a classic-cluster workspace, give each job a cluster pinned to one 17.3+ runtime so convert and rollback never drift. |
| A SQL warehouse | Powers the app, the AI/BI dashboard and the Genie space (`warehouse_id`). |
| A managed storage location on each target schema, catalog or metastore | Converted data is written there. The convert refuses a table without one (`no_managed_location`). |
| A control-plane catalog and schema | Where the manifest, audit log and plans live (`manifest_catalog` / `manifest_schema`, default `main.migration`). |
| The Databricks CLI, CLI path only | 1.3.0 or later: the bundle uses the direct deployment engine (`bundle.engine: direct`). |

## Who needs which permissions

Elevated rights are needed **once, by whoever installs**. People who run the migration day to day
need only `CAN_USE` on the app. Four identities are involved:

- **The installer** (deploys the bundle and provisions the app's service principal):
  - **Workspace side**: creating the app is usually the gate, so in practice a workspace admin.
    Also `CAN_USE` (or `CAN_MANAGE`) on the SQL warehouse and write access to the deploy path.
  - **Unity Catalog side**: owner of, or `MANAGE` on, the control catalog (to create the control
    schema and grant the app SP read access) and on each target catalog or schema (to grant the
    convert identity what it needs). For a whole-estate migration a metastore admin is simplest.
    Account admin is not needed unless you create or rotate the service principal.
- **The app service principal** (the app's own identity). `job_99_setup` grants it least-privilege
  access to the control plane (`USE CATALOG`, `USE SCHEMA`, `SELECT` on the control schema, and
  `MODIFY` on `migration_actions` only) and verifies each grant with `SHOW GRANTS`.
- **The convert identity** (the job's `run_as`, which actually runs `SET MANAGED`). In `dev` and
  `e2e` (development mode) that is **you**, the deployer. In `prod` it is the service principal in
  `prod_run_as_sp`, normally the app SP. On each table it needs:
  - `USE CATALOG` and `USE SCHEMA`;
  - ownership **or** `MANAGE` on the table, its schema or its catalog (direct, inherited or through
    a group);
  - `SELECT` and `MODIFY`, unless it owns the table.

  **Ownership never changes.** The Authorize step (`job_03d_lease_ownership`) grants only what is
  missing when the identity can grant it itself, and the app generates a grant script, grouped by
  who must run each statement, for the rest. See
  [Convert permissions and Authorize](./07b-convert-permissions.md).
- **Operators** (the people using the app): `CAN_USE` on the app. Nothing else; the jobs run as the
  convert identity, and the operator is recorded for attribution only.

Some prerequisites are not Unity Catalog grants:

- Pausing a scheduled OPTIMIZE or VACUUM job, or stopping a pipeline, during the freeze needs
  `CAN_MANAGE` on that job or pipeline.
- Re-sharing a Delta Sharing (Databricks-to-Databricks) table needs ownership of the share, or a
  provider admin runs the two `ALTER SHARE` statements from the runbook.

### The app service principal's grants

`job_99_setup` resolves the app's service principal at run time and issues exactly these four
grants, then verifies each with `SHOW GRANTS` and prints the statement for any it could not land:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<app-sp-client-id>`;
GRANT SELECT      ON SCHEMA  <catalog>.<schema> TO `<app-sp-client-id>`;
GRANT MODIFY      ON TABLE   <catalog>.<schema>.migration_actions TO `<app-sp-client-id>`;
```

The set is deliberately narrow: never `ALL PRIVILEGES` on the schema, which would let the app
rewrite `audit_log`, `wave_plan` or `freeze_ledger`. `USE CATALOG` is the one grant that can exceed
the setup identity's authority on a shared catalog; when it is missing, a catalog owner or metastore
admin runs the printed statement once. The grantee is the service principal's client ID (a UUID in
backticks; a display name grants nothing), shown on the app's page in the workspace.

### The Settings page generates the rest

The app's **Settings** page has a **Grants the App SP needs** panel. It generates copy-paste SQL
and checks with `SHOW GRANTS` which grants have landed:

- **Tier 1**, control-plane access: the four grants above (also automated by `job_99_setup`).
- **Tier 2**, per target schema: `USE CATALOG`, `USE SCHEMA`, `SELECT` and `MANAGE` at schema grain,
  which covers every table in the schema, including ones nobody has listed yet.
- **Convert permission**: a downloadable `convert-permissions.sql` with the grants the convert
  identity still needs per table, grouped by who must run them.
- **Scope pre-fill**: the external tables the signed-in admin can see, next to a manual
  "add catalog/schema" input. That is the admin's visibility, not the whole estate; the profiler
  finds the authoritative set once the grants land.

The app generates and verifies this SQL; it never runs a `GRANT` or changes an owner. The full grant
contract, including the `run_as` model and the reasoning behind each tier, is in
[App service principal grants](../app/docs/app-sp-grants.md).

## Configure the bundle

Set these bundle variables before deploying, either in `databricks.yml` under your target or as
`BUNDLE_VAR_<name>` environment variables:

| Variable | Default | Set it to |
|---|---|---|
| `warehouse_id` | blank | Your SQL warehouse ID. Required for the dashboard and Genie. |
| `manifest_catalog` / `manifest_schema` | `main` / `migration` | Where the control tables should live. |
| `dataset_catalog` / `dataset_schema` | `main` / `migration` | The same catalog and schema; the dashboard and Genie read from here. |
| `catalog_filter` / `schema_filter` | `*` / `*` | Glob filters that limit which catalogs and schemas the profiler scans. |
| `workspace_id` | blank | Your workspace ID, so links from the app open in the right workspace. Optional. |
| `prod_host` / `prod_run_as_sp` | placeholder / blank | `prod` target only: the prod workspace URL and the service principal application ID that runs the jobs. |
| `telemetry_enabled` | `true` | `false` to turn off usage telemetry (see below). |

The `dev` target ships with a placeholder host (`https://CHANGE-ME-dev.cloud.databricks.com`):
replace it with your workspace URL. Every other knob (wave sizes, scan caps, parallelism) has a
safe default and is documented in [the jobs contract](../backend/docs/contracts/jobs.md).

## Install from the workspace (recommended, no local CLI)

Installation has three phases. The bundle creates every resource; the installer notebook only
orchestrates the runtime steps, and it never creates jobs, the app or the dashboard.

1. **Clone** this repository into a Databricks **Git folder** from the workspace UI.
2. **Deploy the bundle.** Open `databricks.yml`, click the **Deployments** icon, pick a target and
   click **Deploy**. This creates the stage jobs, the setup job, the dashboard and the app.
3. **Open `backend/notebooks/install.py` and click Run all.** It runs five idempotent steps
   (re-running is safe and reports "already done"):
   - **Preflight**: finds the app and its jobs by name and stops with a pointer back to step 2 if
     anything is missing.
   - **Provision**: runs `job_99_setup` (control tables, Genie space, app SP grants) and waits.
   - **Restart**: asks you to **Deploy again**, so the app picks up the new Genie space. Use a
     bundle deploy, never `databricks apps stop` / `start` (see
     [Restarting the app](#restarting-the-app)).
   - **Data**: per the `data_mode` widget: `real` runs the profiler, the wave planner and the
     conductor against your estate; `fixture` seeds a synthetic estate on an allowlisted dev
     control plane only; `none` skips it.
   - **Verify**: checks the control-plane config, the app SP grants, that the app is running, and
     a representative read. It prints the app URL.

## Install with the CLI

```bash
databricks bundle validate -t dev --profile <profile>
databricks bundle deploy   -t dev --profile <profile>   # jobs, dashboard, Genie handshake and the app
databricks bundle run job_99_setup     -t dev --profile <profile>   # control tables, Genie space, app SP grants
databricks bundle run job_08_conductor -t dev --profile <profile>   # refresh the read views (see below)
```

`databricks bundle deploy` is the only deploy command: it also deploys the app, which the Apps
platform builds from `app/`. Do not run `databricks apps deploy`. The `Makefile` wraps the common
commands (`make validate`, `make deploy`, `make test`).

`job_99_setup` and the profiler can run in either order. The app SP grants, however, land only on
the first `job_99_setup` run **after** the app exists; on a fresh workspace a run before the deploy
logs that the app is not deployed yet and applies nothing. Run it again after deploying.

For production, set `prod_host` and `prod_run_as_sp`, then `databricks bundle deploy -t prod`. Runs
are approved through job-run permissions and a person triggering each run.

## After every deploy: refresh the read views

`bundle deploy` ships new app code but does not recreate the control-plane **views**. Until
`wave_status` is recreated, app queries that read its newer columns fail with an unresolved column,
which breaks the Waves and Convert boards, the Overview wave lookups, the scope pickers and the
Settings query health list. Right after every deploy, run the read-only conductor once:

```bash
databricks bundle run job_08_conductor -t <target> --profile <profile>
```

or press **Overview > Refresh the next step** in the app. The conductor converts nothing and never
rewrites the wave plan: it adds any new manifest columns, recreates the read views and appends a
recommendation.

## Check that it worked

Open the app URL and go to **Settings**. Every control-plane query should report healthy, and the
**Grants the App SP needs** panel should show the control-plane grants as landed. After the
profiler has run, **Overview** shows your estate and the recommended next step.

## Restarting the app

The app reads its whole environment (control catalog and schema, the warehouse and every job
binding) from the bundle, applied at deploy time. `databricks apps stop` followed by `start`
restarts the container **without** that environment, and the app crashes at boot with
`MIGRATION_CATALOG is missing or invalid`. To restart the app, for example to pick up a new
`genie_space_id`, run `databricks bundle deploy -t <target>` again.

## Troubleshooting

- **The app shows `genie_available: false` right after a restart.** The app reads the Genie
  handshake once at boot with a short timeout so a cold warehouse cannot fail the deploy. If the
  warehouse was cold, Genie is not registered until the next restart. Warm the warehouse with any
  query, then deploy again. `genie_space_configured`, read live, already shows `true`.
- **Control-plane reads fail with `INSUFFICIENT_PERMISSIONS`.** The app SP grants have not landed.
  Run `job_99_setup` again after the app exists. If `USE CATALOG` still fails, the catalog is a
  shared one the setup identity cannot grant on: the job prints the exact `GRANT` statement for a
  catalog owner or metastore admin to run once.
- **The profiler finds fewer tables than expected.** The profiler can only see what its identity
  has been granted. Grant `USE CATALOG`, `USE SCHEMA` and `SELECT` on the missing schemas (schema
  grain covers tables nobody has listed yet), then run the profiler again. The Settings page lists
  the tables the signed-in admin can see as a starting scope.
- **A convert refuses with a permission verdict.** Read the table's `convert_authority` row (the app
  shows it on the convert gate), run **Authorize**, and send the generated grant script for anything
  it could not grant itself. See [Convert permissions and Authorize](./07b-convert-permissions.md).
- **A convert refuses with `no_managed_location` or `managed_location_unresolved`.** Configure a
  managed location on the schema, catalog or metastore, then run the dry run again. See
  [Conversion and validation](./08-conversion-and-validation.md).

## Usage telemetry

Each stage run and app job launch tags one call to the workspace's own control plane with
closed-vocabulary signals (stage, outcome, banded counts and the toolkit's own codes). It never
contains catalog, schema or table names, paths, identities, SQL or error messages, and it never
changes a gate. Turn it off with `telemetry_enabled: "false"` and a redeploy, or set
`DO_NOT_TRACK=1`. The full catalogue is in
[backend/docs/operations/telemetry.md](../backend/docs/operations/telemetry.md).

## Testing against your own workspace (e2e target)

The `e2e` target carries no workspace details: the host comes from `--profile` and everything else
from `BUNDLE_VAR_*` environment variables. Copy `.env.e2e.example` to `.env.e2e`, fill it in, load
it with `set -a; source .env.e2e; set +a`, and deploy with
`databricks bundle deploy -t e2e --profile <profile>`. The live e2e drivers are described in
[backend/.e2e/README.md](../backend/.e2e/README.md).

## Uninstalling

`databricks bundle destroy -t <target>` removes the jobs, the dashboard and the app. It does not
drop the control-plane schema or its tables (the jobs create those at run time) or the Genie space
(created by `job_99_setup`), and it never touches your tables. Roll back any converted table you
want to keep EXTERNAL **before** the 14-day window closes; see
[Rollback and cleanup](./09-rollback-and-cleanup.md).
