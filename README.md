# External → Managed Table Migration

[![Release](https://img.shields.io/github/v/release/databricks-solutions/managed-table-migration-accelerator)](https://github.com/databricks-solutions/managed-table-migration-accelerator/releases)
[![License](https://img.shields.io/badge/license-Databricks-blue)](./LICENSE.md)
· [Changelog](./CHANGELOG.md)

Convert Unity Catalog **EXTERNAL** tables to **MANAGED** tables with
`ALTER TABLE ... SET MANAGED`, safely and at estate scale. One Databricks Asset Bundle discovers
your external tables, finds what would break, plans the conversion in waves, converts them, and
lets you roll back for 14 days. A person approves every change. Drive it from the app, the
notebooks, or in plain language with **Genie Code**: the app doubles as an MCP server.

![The app's Overview page: migration progress at the top, and the recommended next step (convert one schema) below it](docs/images/overview.png)

**[Install](#install)** · **[Your first migration](#your-first-migration)** ·
**[Drive it with Genie Code](#drive-it-with-genie-code)** · **[Documentation](./docs/README.md)**

## Who it is for

Platform and data teams that own a Unity Catalog estate with many external Delta tables and want
them managed: Predictive Optimization, automatic liquid clustering, and less storage to look after.

Converting one table is a single statement. Converting thousands is not: streams must be stopped
and restarted, compaction and `VACUUM` jobs paused, Delta shares re-shared, external engines and
path-based readers found, and every table tracked through a 14-day rollback window. This tool does
that bookkeeping from one control table (the **manifest**) and records every action in an audit
log.

> [!IMPORTANT]
> **Nothing converts on its own.** The tool recommends; a person triggers each conversion from a
> deployed job or the app, and every job re-checks its safety gates before it changes a table.
> Never run `SET MANAGED` against production from an editor.

## How it works

Each table moves through the same lifecycle. Every step is a Databricks job; the read-only steps
are safe to re-run at any time.

```mermaid
flowchart LR
  discover["Discover<br/>find external tables"] --> assess["Assess<br/>what would break?"]
  assess --> plan["Plan waves<br/>pilot first"]
  plan --> dry["Dry run<br/>go / no-go per schema"]
  dry --> freeze["Freeze<br/>pause compaction,<br/>stop streams"]
  freeze --> authorize["Authorize<br/>grant missing privileges"]
  authorize --> convert(["Convert<br/>SET MANAGED"])
  convert --> validate["Validate<br/>and unfreeze"]
  validate --> finish["Finish steps<br/>re-share, Compatibility Mode,<br/>optimization"]
  finish --> cleanup["Cleanup<br/>after day 14"]
  convert -. "within 14 days" .-> rollback(["Rollback<br/>UNSET MANAGED"])
  validate -. "within 14 days" .-> rollback
```

Freeze and Authorize are optional: the tool recommends them only when a table has compaction,
streams or writers to pause, or when the job identity is missing a privilege. The architecture
behind it is in [Architecture](./docs/02-architecture.md).

There is one engine and three ways to drive it. All three run the same jobs and read the same
control tables.

| Drive it from | Best for | Guide |
|---|---|---|
| **The app** (a Databricks App) | Most teams. It shows where the migration stands, recommends the next step, and launches it. | [Using the app](./docs/12-using-the-app.md) |
| **Notebooks and jobs** | Running without the app, scripting, or CI. `09_cockpit` is a guided notebook. | [Using the notebooks](./docs/11-using-notebooks.md) |
| **Genie Code** (MCP), built into the app | Asking in plain language: "what's next?", "what blocks this schema?", "convert this wave". Changes still need `confirm=true`. | [Drive it with Genie Code](#drive-it-with-genie-code) |

## Prerequisites

| You need | Why |
|---|---|
| A Unity Catalog-enabled workspace | The tables, grants and system tables all come from Unity Catalog. |
| Serverless compute, or Databricks Runtime 17.3 LTS+ | `SET MANAGED` and `UNSET MANAGED` require it. The jobs run on serverless. |
| A SQL warehouse | The app, the AI/BI dashboard and the Genie space query through it. |
| A managed storage location on each target schema, its catalog, or the metastore | Converted data needs somewhere to go. Tables without one are blocked, not guessed. |
| Workspace admin, for the one-time install | Creating the app needs it. Day-to-day users do not. |
| The Databricks CLI, 1.3.0 or later (CLI install only) | The bundle uses the direct deployment engine. |

Readers and writers of the converted tables should run Databricks Runtime 15.4 LTS or later; the
profiler flags any that do not.

## Permissions

```mermaid
flowchart LR
  installer(["Installer"]) -- "installs once" --> app["Migration app"]
  operator(["Operators"]) -- "use day to day" --> app
  app -- "launches" --> jobs["Migration jobs"]
  jobs -- "convert" --> tables[("Your tables")]
```

- **Installer, once:** a workspace admin who owns, or has `MANAGE` on, the control catalog.
- **Operators, day to day:** `CAN_USE` on the app. Nothing else.
- **The jobs, per table:** ownership **or** `MANAGE`, plus `SELECT` + `MODIFY`. Ownership never
  changes; **Authorize** grants only what is missing.
- **At install** the installer grants the jobs read access and, by default, `MANAGE` on each schema
  you migrate, in one reviewed plan. A person hands `MANAGE` back after cleanup. Granting it per
  wave instead is the stricter option.

Every identity, privilege and grant statement, step by step:
[Who needs which permissions](./docs/00-installation.md#who-needs-which-permissions).

## Install

Deploy the bundle, then run the **installer**. It checks your rights up front, shows exactly what
it will grant, grants it after you confirm, reads every grant back and verifies the app. Nothing
is granted until you confirm, and if you cannot grant something it says who can.

```mermaid
flowchart LR
  deploy["Deploy"] --> check["Check rights"] --> plan["Preview grants"] --> apply["Apply + verify"]
  apply --> migrate["Profile, plan,<br/>convert"] --> handback["Hand convert<br/>rights back"]
```

| Front door | How |
|---|---|
| **Notebook** (no local tools) | Clone into a **Git folder**, open `databricks.yml`, click **Deployments → Deploy**, then open `backend/notebooks/install.py` and **Run all** |
| **CLI** | `databricks bundle deploy`, then `databricks bundle run install` (below) |
| **The app**, after the first install | **Connection & policy → Permissions**: add schemas, grant as you, hand rights back |

Run all first with `action=check`: one line per requirement, and how much of the estate you can
grant on.

![The installer's rights and coverage report: go lines for workspace admin, the control catalog, the jobs' identity and the install records, a warning for metastore admin, and the coverage headline](docs/images/install/notebook/rights.png)

Then set `confirm_grants=true` and the printed `plan_id`, and run all again. The run ends with
`VERIFY PASSED` and the app URL.

```bash
git clone https://github.com/databricks-solutions/managed-table-migration-accelerator.git
cd managed-table-migration-accelerator

databricks bundle deploy -t dev --profile <profile> --var="warehouse_id=<warehouse-id>"
MTM_ACTION=check databricks bundle run install -t dev --profile <profile>        # read-only
MTM_SCOPES='sales.*' databricks bundle run install -t dev --profile <profile>    # preview the plan
MTM_SCOPES='sales.*' MTM_PLAN_ID=<plan_id> MTM_CONFIRM_GRANTS=true \
  databricks bundle run install -t dev --profile <profile>                        # apply it
```

![The CLI check: the rights and coverage report, the stored scopes, and the grant plan with one Change line missing](docs/images/install/cli/check.png)

Before deploying, set the `dev` target's workspace `host` in `databricks.yml` (it ships as a
placeholder), and the catalog and schema for the control tables if `main.migration` is not right
for you. Every variable is listed in [Configure the bundle](./docs/00-installation.md#configure-the-bundle).

> [!NOTE]
> Run `job_08_conductor` after **every** deploy. It refreshes the views the app reads, so the app
> never shows a stale step. Deploy only with `databricks bundle deploy`, never
> `databricks apps deploy`.

**Check that it worked.** Open the app and go to **Connection & policy** (Settings). Every check
should be green: the control plane is provisioned, the app's grants are in place, and the
warehouse answers. If something is red, see
[Troubleshooting](./docs/00-installation.md#troubleshooting).

The full guide, with every option, each step's screenshots, Genie Code, handing convert rights back,
production (`-t prod` with a service principal), the e2e target, and uninstalling, is
[Installation](./docs/00-installation.md).

## Your first migration

Start with one small **pilot** schema. The app's Overview page always shows the one step it
recommends next; following it walks you through these steps in order.

1. **Profile the estate.** Click **Rescan estate**, or run `job_00_profiler`. It finds every
   external table and marks each one eligible, blocked, or needing review.
2. **Plan waves.** `job_02_wave_planner` groups tables into waves and picks a pilot schema of up to
   15 tables. Streaming tables always get their own waves.
3. **Clear what blocks.** The **Workstreams** page groups the problems (streams, path-based
   readers, external engines, missing privileges), each with the steps that fix it.
4. **Dry run.** `job_03_dry_run` gives a go or no-go for the schema and changes nothing.
5. **Freeze and authorize, if recommended.** `job_03c_freeze` pauses compaction jobs and stops
   streams for the wave; `job_03d_lease_ownership` (Authorize) grants any missing privilege. Both
   default to a dry run until you set `confirm_execute=true`.
6. **Convert.** `job_04_convert` converts the schema's ready tables. It refuses to start while
   anything unsafe is still open.
7. **Validate and finish.** `job_05_validate` checks every converted table and
   `job_05b_unfreeze_verify` restarts what the freeze stopped. Then re-share Delta shares, set up
   external readers, and turn on optimization if the app recommends it.

![The Workstreams page: one card per kind of problem that can stop a table converting, each with a count and a link to its fix](docs/images/workstreams.png)

The same steps from the CLI:

```bash
databricks bundle run job_00_profiler     -t dev --profile <profile>
databricks bundle run job_02_wave_planner -t dev --profile <profile>
databricks bundle run job_03_dry_run      -t dev --profile <profile> \
  --params catalog_filter=<catalog>,schema_filter=<schema>
databricks bundle run job_04_convert      -t dev --profile <profile> \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true
databricks bundle run job_05_validate     -t dev --profile <profile> \
  --params catalog_filter=<catalog>,schema_filter=<schema>
```

After the pilot, scale out wave by wave, or convert several non-conflicting waves at once with
[Bulk convert](./docs/14-bulk-convert.md). The end-to-end runbook is the
[Estate migration playbook](./docs/13-estate-migration-playbook.md).

## Drive it with Genie Code

The app is also an **MCP server**, so Genie Code, the Databricks assistant, can run the migration
for you in plain language. It ships with the bundle; there is nothing else to install.

```mermaid
flowchart LR
  you(["You, in Genie Code"]) -- "plain language" --> genie["Genie Code"]
  genie -- "MCP tools" --> app["Migration app<br/>/mcp"]
  app -- "reads" --> control[("Control tables")]
  app -- "launches, after confirm=true" --> jobs["Migration jobs<br/>re-check every gate"]
```

**Connect it** (no account admin needed): in Genie Code, open **Settings → Custom MCP server**,
choose `mcp-managed-table-migration` (or paste the app's `/mcp` URL) and save. It signs in as you,
so anyone with `CAN_USE` on the app can connect, and each run records who asked for it.

**Then ask**, for example:

- "Where does the migration stand, and what should I do next?"
- "What is blocking `main.sales`, and who needs to fix it?"
- "Can I convert `main.sales`?" (it runs a preflight first and tells you what is still open)
- "Freeze, convert and validate the next wave." (each step waits for your confirmation)

Reading is free. Every tool that changes something needs an explicit `confirm=true`, and the job
re-checks every safety gate exactly as it does when you click in the app. To allow reads and
non-mutating stages only, deploy with `--var="mcp_tier=v1"`. Full guide:
[Genie Code MCP](./docs/15-genie-code-mcp.md).

> [!TIP]
> Genie Code is not the app's **Ask Genie** button. Ask Genie answers questions about the control
> tables from a Genie space; Genie Code calls the migration's tools and can launch its jobs.

## Rollback and the 14-day window

A converted table can go back to EXTERNAL with `ALTER TABLE ... UNSET MANAGED` for **14 days**
after the convert. `job_06_rollback` does it for one table or a whole wave, with a reason you
supply, and puts any jobs and streams the freeze paused back exactly as they were. After day 14, Predictive Optimization removes
the old external files (or `job_07_post_convert_cleanup` runs `VACUUM` if it is off), and rollback
is no longer possible. The tool never runs `VACUUM` inside the window.

![The Convert page's undo panels: roll back one table or one wave within 14 days, and clean up old files only after the window has passed](docs/images/rollback-and-cleanup.png)

More in [Rollback and cleanup](./docs/09-rollback-and-cleanup.md).

## Documentation

| I want to... | Read |
|---|---|
| Install, configure, or troubleshoot | [Installation](./docs/00-installation.md) |
| Understand what the migration is and its safety rules | [Overview](./docs/01-overview.md) |
| Migrate a whole estate step by step | [Estate migration playbook](./docs/13-estate-migration-playbook.md) |
| Use the app, the notebooks, or Genie Code | [App](./docs/12-using-the-app.md) · [Notebooks](./docs/11-using-notebooks.md) · [Genie Code](./docs/15-genie-code-mcp.md) |
| Know what the profiler detects and why a table is blocked | [Discovery and signals](./docs/03-discovery-and-signals.md) · [Streaming](./docs/04-streaming.md) · [External access](./docs/05-external-access-and-buckets.md) |
| Understand waves, freezing, permissions, and the convert gates | [Wave planning](./docs/06-wave-planning.md) · [Freeze](./docs/07-freeze-bracket.md) · [Permissions](./docs/07b-convert-permissions.md) · [Convert and validate](./docs/08-conversion-and-validation.md) |
| Finish, roll back, or clean up | [Post-convert maintenance](./docs/08c-post-convert-maintenance.md) · [Rollback and cleanup](./docs/09-rollback-and-cleanup.md) |
| Use the dashboard and Genie space | [Dashboard and Genie](./docs/16-dashboard-and-genie.md) |
| Look up every job parameter or control-table column | [Jobs contract](./backend/docs/contracts/jobs.md) · [Control tables contract](./backend/docs/contracts/control-tables.md) |

The full table of contents is [docs/README.md](./docs/README.md).

## Repository layout

```text
databricks.yml       the one bundle: variables, targets (dev / e2e / prod), includes both halves
backend/             the migration tooling; runs on its own, with no app deployed
  resources/         one job per stage (file number = run order), plus the dashboard
  notebooks/         stage notebooks and install.py
  src/migration/     pure, unit-tested logic used by the notebooks
  src/tests/         pytest suite; needs no cluster or Spark
  docs/              contracts (jobs, control tables), architecture, operations, reference
app/                 the Databricks App (React + TypeScript on AppKit) and its MCP server
docs/                the operator documentation
scripts/release.py   local release tooling
```

The app talks to the tooling only through the control tables and job parameters, documented in
[backend/docs/contracts/](./backend/docs/contracts/).

## Contributing

```bash
make test   # backend unit tests (fast, no cluster)
make ci     # the full pre-merge gate: backend tests plus app typecheck, tests and lint
```

There is no hosted CI, so run `make ci` before opening a pull request. Read [AGENTS.md](./AGENTS.md)
first: it holds the safety rules and engineering conventions for people and coding agents alike.
Add a line under `[Unreleased]` in [CHANGELOG.md](./CHANGELOG.md) for any user-visible change.
Releases follow "Cutting a release" in AGENTS.md. Usage telemetry and how to turn it off are
described in [backend/docs/operations/telemetry.md](./backend/docs/operations/telemetry.md).

## How to get help

Databricks support doesn't cover this content. For questions or bugs, please open a
[GitHub issue](https://github.com/databricks-solutions/managed-table-migration-accelerator/issues)
and the team will help on a best effort basis. To report a security vulnerability, see
[SECURITY.md](./SECURITY.md).

## License

&copy; 2026 Databricks, Inc. All rights reserved. The source in this repository is provided
subject to the [Databricks License](https://databricks.com/db-license-source) (see
[LICENSE.md](./LICENSE.md)). All included or referenced third party libraries are subject to the
licenses set forth below.

<details>
<summary>Third-party libraries</summary>

| library | description | license | source |
|---------|-------------|---------|--------|
| `databricks-sdk` | Databricks SDK for Python (provided by the Databricks Runtime) | Apache-2.0 | https://github.com/databricks/databricks-sdk-py |
| `pyspark` | Apache Spark Python API (provided by the Databricks Runtime) | Apache-2.0 | https://github.com/apache/spark |
| `@databricks/appkit` | Databricks Apps framework (server) | Apache-2.0 | https://github.com/databricks/appkit |
| `@databricks/appkit-ui` | Databricks Apps framework (UI components) | Apache-2.0 | https://github.com/databricks/appkit |
| `@databricks/sdk-experimental` | Databricks SDK for JavaScript | Apache-2.0 | https://github.com/databricks/databricks-sdk-js |
| `@modelcontextprotocol/sdk` | Model Context Protocol TypeScript SDK | MIT | https://github.com/modelcontextprotocol/typescript-sdk |
| `clsx` | Conditional className strings | MIT | https://github.com/lukeed/clsx |
| `driver.js` | Guided product tours | MIT | https://github.com/nilbuild/driver.js |
| `embla-carousel-react` | Carousel component | MIT | https://github.com/davidjerleke/embla-carousel |
| `lucide-react` | Icon library | ISC | https://github.com/lucide-icons/lucide |
| `next-themes` | Light/dark theme switching | MIT | https://github.com/pacocoursey/next-themes |
| `react` | UI library | MIT | https://github.com/facebook/react |
| `react-dom` | React DOM renderer | MIT | https://github.com/facebook/react |
| `react-resizable-panels` | Resizable panel layouts | MIT | https://github.com/bvaughn/react-resizable-panels |
| `react-router` | Client-side routing | MIT | https://github.com/remix-run/react-router |
| `tailwind-merge` | Tailwind class merging | MIT | https://github.com/dcastil/tailwind-merge |
| `tailwindcss-animate` | Tailwind animation plugin | MIT | https://github.com/jamiebuilds/tailwindcss-animate |
| `tw-animate-css` | Tailwind v4 animation utilities | MIT | https://github.com/Wombosvideo/tw-animate-css |
| `zod` | Schema validation | MIT | https://github.com/colinhacks/zod |

</details>
