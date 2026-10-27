# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is `0.x`, a minor
release may change job parameters or control-table columns; every such change is called out under
**Changed** with the matching contract in `backend/docs/contracts/`.

## [Unreleased]

### Added

- **One permissioning engine for every install door.** `migration.provision` (pure) holds the
  install options, the rights and coverage report, the four-set grant plan (Control / Look /
  Read / Change) with its `plan_id` fingerprint, read-back verification, and the hand-back and
  uninstall plans; `migration.provision_io` is the thin edge the notebook and the CLI share. New
  control table `migration_scope` (which scopes the jobs' identity was granted, by whom, through
  which door; flagged on removal, never deleted). New `audit_log` actions `INSTALL_GRANT`,
  `INSTALL_GRANT_SKIPPED` and `INSTALL_REVOKE`, and installer `INSTALL|…` rows in
  `ownership_ledger` that `07` never revokes (contract change: see
  `backend/docs/contracts/control-tables.md`).
- **`installer_group` bundle variable.** `job_99_setup` grants that group read access to the
  control schema and `MODIFY` on the install records, so installers never grant themselves.
  Blank (the default) keeps today's behavior: each installer grants themselves that access.
- **The install notebook checks your rights and shows the grant plan.** Its widgets now come from
  the shared option list (`action`, `confirm_grants`, `jobs_identity`, `scopes`, `convert_rights`,
  `wave_id`, `plan_id`, `grantee_group`, plus the existing ones). A default run still runs setup,
  data and verify; it now also prints the rights and coverage report and the grant plan, and
  applies the grants (each audited first, then read back) only with `confirm_grants=true`.
  `action=check`, `grant_convert`, `revoke_convert` and `uninstall` cover the rest of the journey.
  Run as a job, it keeps what it printed: a cell that prints a plan or a fix and then stops hands
  its exit to the next cell, so the run output still shows the plan. It checks this deployment's
  own app, named by the `99 setup` job's `app_name` parameter.
- **An install CLI.** `databricks bundle run install -t <target>` runs the same engine from a
  terminal or a pipeline: options as `MTM_*` environment variables through the bundle (a bundle
  script takes no arguments) or as flags when run directly, `MTM_OUTPUT=json` for one document
  with the rights, the plan and its `plan_id`, an interactive scope picker, and exit codes 0–5.
  A dry run runs nothing. `make install` is the shortcut.
- **Read-backs see privileges held through groups.** A grant held through one of the grantee's
  groups (or `account users`) now reads as present instead of missing.
- **Choose who the jobs run as.** New complex bundle variable `jobs_run_as`, set as `run_as` on
  all 24 stage jobs (default: the deployer, on every target). The installer's
  `jobs_identity` option writes it to `variable-overrides.json`, and the CLI offers to redeploy
  (`--yes` skips the prompt). The grant plan gains a line that gives the jobs' identity
  `CAN_MANAGE_RUN` on every job, so the cockpits and conductor tails can start other jobs. New
  `prod_root_path` variable (default unchanged) and a WARN when every workspace user can edit
  the deployment folder. Deploying as a service principal: set `jobs_run_as`, since the default
  names a user. Moving the jobs to a service principal needs the deployer to hold its Service
  principal: User role (workspace admins too). The installer says so before it redeploys, and
  names the fix when the deploy is refused for it. The dev-only `e2e` helper jobs are not counted
  in the "all jobs agree" check. When the jobs' identity is not the owner of the deployment folder
  (on `dev` and `e2e` it sits in the deployer's home), the plan adds a bootstrap line that gives
  it `CAN_READ` on that folder, so setup can open its notebooks. The app cannot grant it and
  shows the two CLI commands instead.
- **Install records follow the account group.** The `installer_group` membership check uses
  `is_account_group_member`, so an account group not assigned to the workspace still counts
  (the workspace group list is the fallback).
- **Convert rights are handed back at the end.** `07` now marks the installer's schema `MANAGE`
  `HANDBACK_READY` once every table in the schema is `CLEANED` or `ROLLED_BACK`, and opens a
  schema-scoped `RESTORE_OWNERSHIP` action with the exact `REVOKE` (in dry-run too: it changes no
  grant). The installer's `revoke_convert` runs it and closes the action. New installer option
  `plan_run_id` pins `grant_convert` to the wave plan you reviewed: a newer plan stops it before
  any grant.
- **Hand-back rules.** A hand-back or uninstall plan has its own `plan_id`, so a grant plan id can
  never apply a revoke. A re-install keeps `added = true` on a grant the tool already added, so it is
  still handed back. Re-running `07` reopens a closed hand-back action when a schema is ready again.
  An empty hand-back now names what is still held and why ("Still held, not ready yet"), with a
  new `waiting` list on `provision.handback_plan` and one line in Settings → Permissions.
- **Grant permissions from the app.** Settings → Permissions shows your rights, who the jobs run as,
  the schemas in scope, and how convert rights are given, then grants the plan as the signed-in
  admin (their forwarded token, never the app's identity) after a preview pinned by `plan_id`. The
  convert gate offers **Grant convert rights for this wave**, and Do by hand offers **Hand back** on
  the installer's hand-back rows. New routes `GET /api/permissions`, `POST /api/permissions/grant`
  and `POST /api/permissions/hand-back`. The app now writes the installer's `INSTALL_*` audit rows,
  `migration_scope` and `INSTALL|…` ledger rows (safety rules SR15–SR17 in
  `app/docs/control_plane.md`). Job permissions and a jobs identity change stay CLI or notebook
  steps; the app shows the commands.
- **Permissions over MCP.** `mtm_get_permissions` (v1 and v2) reads the same plan as the Settings
  page, as the caller. The v2 tools `mtm_grant_permissions` and `mtm_hand_back_convert_rights`
  run it as the caller too, behind `confirm=true` and the previewed `plan_id`. With no sign-in they
  answer with the CLI command. `backend/.e2e/e2e_mcp.py --permissions` checks them live.
- **One deploy.** The app looks the Genie space up per request instead of once at boot, so the
  space the installer creates works without a second deploy. The install notebook's "deploy
  again" step is gone, and every installer run (notebook or CLI) now ends with the conductor, so
  the app's views and next step reflect it: to upgrade, deploy and re-run the installer.
- **An under-privileged banner on every page.** When the jobs are missing permissions, every page
  says so ("The migration is missing permissions on 2 schemas.") and links to Settings →
  Permissions. It reads the same plan as that page and never grants.
- **The Settings grants panel addresses schema grants to the jobs' identity.** Step 2 now reads
  who the deployed jobs run as (`jobs_identity` on `GET /api/grants/plan`), instead of assuming the
  app service principal.
- **A documentation site.** `docs/site/` is a Docusaurus site built from the existing `docs/`
  pages plus six backend reference pages (the two contracts, architecture, state machine, install
  and permissions, privileges), with search, Mermaid diagrams and image zoom. The markdown is
  unchanged: links that leave a page's folder are rewritten at build time to a site page or to the
  file on GitHub, and a broken link fails the build. Run it with `make docs-dev`; `make ci` now
  builds it too.

### Fixed

- **A fresh install recommends the profiler, not "Plan the waves again".** Before the first
  profile the conductor's `replan` row (no planned schema, no discovered table) is now titled
  **Run the profiler first**, with the `job_00_profiler` command and the `profiler` alias; the kind
  stays `replan`. The Overview shows the first-run card for that row instead of 0 of 0 tables, the
  `09` cockpit's `advance` launches the profiler, and Genie Code's `mtm_get_next_step` returns
  `mtm_run_profiler`. All three re-check the manifest, so once a profile has found tables they
  plan the waves.
- **Settings no longer reports "0 of 0" or asks for permissions it can't see.** Convert rights
  says **No tables profiled yet** (with **Run the profiler**) on an empty manifest and **No tables
  left to convert** once everything is converted. The Plan counts `missing` and `not checked`
  apart; not-checked lines get their own box and a dry-run CLI check, and **Copy request** and the
  downloaded plan ask only for missing lines. The jobs-permission line is one shell loop plus the
  Jobs UI path, labelled "Not SQL". Because the app can't read the jobs' access lists, it now
  treats that line as held when a notebook or CLI install recorded it for the current jobs, or
  when the jobs' identity created every job, and says which. `audit_log.detail` gains `job_ids`
  on a jobs-permission line, and `GET /api/grants/plan` `authority_summary` gains `profiled`
  (additive; see `backend/docs/contracts/control-tables.md`).
- **The install commands work as written.** The README passed `--var warehouse_id` only to
  `bundle deploy`, so every `bundle run install` after it failed with `dashboard warehouse_id is
  required`: `bundle run` resolves the variables again. The instructions now set them once for
  every command. The installer (CLI and notebook) also stops, naming the deployed control schema,
  when a run resolves a different `manifest_catalog` / `manifest_schema` from the deploy, instead
  of working on the wrong control plane. From a Git folder, the notebook now resolves its own
  target's jobs (the `target` widget), not the first deployment it finds.

- **The e2e control-plane wipe empties every control table.** `--wipe-control-plane` now also
  empties `convert_authority` and `migration_scope`, so a fresh install no longer shows old convert
  rights or old stored scopes.
- **Settings → Permissions explains a sign-in with no SQL access.** When your sign-in to the app
  has no `sql` scope (it predates the app asking for it), the panel now says so and how to fix it
  (open the app in a new private window and approve the prompt). It no longer shows `can't read` on
  grants you already hold or names guessed grantors: every line reads "not checked". Its CLI
  command is a dry run without a `plan_id`, because a plan built from failed reads is not the plan
  the CLI computes and the CLI would refuse it.
- **A sequence diagram in the install and permissions design did not render.** A `;` in a message
  ends the statement in Mermaid; it now reads "Writes control tables, app reads them back".

- **The `prod` target validates.** A target-level `run_as` also applied to the app, which the
  Apps platform refuses ("apps do not support a setting a run_as user that is different from the
  owner"), and the e2e DLT pipeline pinned `development: true`, which production mode rejects.
  `run_as` now sits on each job, and the pipeline takes development mode from its target.

- **A fresh install's control config is complete before the first profile.** `job_99_setup` now
  writes `migration_control_config.warehouse_id` from the deploy's `warehouse_id`, so the install
  check no longer reports it missing until the profiler runs.

- **The profiler skips `information_schema`.** An unfiltered profile listed each catalog's
  `information_schema` views (reported as `EXTERNAL`, format `UNITY_CATALOG`) as tables to
  migrate. Both discovery queries now exclude that schema.

- **The `prod` target deploys.** It set `host: ${var.prod_host}`, which the CLI refuses
  ("Variable interpolation is not supported for fields that configure authentication"), so
  `-t prod` could neither validate nor deploy. The workspace now comes from `--profile`, as on
  `dev` and `e2e`: `databricks bundle deploy -t prod --profile <prod-profile>`. The `prod_host`
  variable is gone (remove it from any `BUNDLE_VAR_prod_host` export), and `make deploy-prod`
  requires `PROFILE=`.

- **Settings → Permissions reads more accurately.** A saved `catalog.*` scope covers every schema
  of that catalog in **Add schemas**, so they are not offered again. Convert rights says "All N
  tables still to convert are authorized" when none is blocked, instead of empty counts. The toast
  after **Grant** counts the lines the app cannot read as not checked, apart from missing ones.

### Changed

- **The jobs run as you on every target, prod included.** The `prod` target no longer overrides
  `jobs_run_as`, and the `prod_run_as_sp` variable is gone (contract change: see
  `backend/docs/contracts/jobs.md` and `backend/docs/integration/job-api.md`). A person-run install
  says so on the jobs' identity line ("Anyone who can start these jobs runs them with your
  access") and in the grant plan's header, with the switch: `jobs_identity=sp:<application-id>`,
  then redeploy. An upgrade that moves the jobs off the identity holding this install's grants
  stops with NO-GO and both fixes (keep the old identity, or move to the new one), instead of
  granting a second identity silently. If your prod jobs ran as a service principal, keep it with
  `jobs_identity=sp:<application-id>` before you redeploy.
- **Settings → Permissions → Where the jobs have access** (was Migration scope). The part lists
  where the jobs can read and convert, and now says that this grants access only: the profiler
  finds the tables, and nothing converts until you launch a wave. A line under it points to Waves.
- **Blank scopes cover your catalogs.** `scopes` takes the new token `all`, and a blank value with
  no stored scopes now means `all`: every catalog with external tables you can grant on, as
  `catalog.*`, or the schemas you can grant on where you cannot grant on the whole catalog
  (`system`, `hive_metastore`, `samples` and shared or foreign catalogs are left out). Stored scopes
  still win, so an earlier narrowing is never widened. Every door prints a note on catalog-level
  grants and how to narrow (`scopes=<catalog.schema,...>` or `convert_rights=per_wave`). The CLI no
  longer refuses a non-interactive install without scopes, the notebook no longer asks you to
  paste a list, and Settings → Permissions starts with the expansion as removable rows.
  `GET /api/permissions` and `mtm_get_permissions` gain `scopes_from: "all"` and `scope_note`, the
  CLI's JSON gains both, and `PLAN_RULES_VERSION` is `2`, so a `plan_id` reviewed before this
  release is re-reviewed.
- **Recommended installer.** [Installation](docs/00-installation.md#recommended-installer) and
  the README now recommend installing as a workspace admin who is also a metastore admin, say what
  each role covers and that account admin is not needed, and correct "creating the app needs
  workspace admin": any user can create an app, but binding the jobs and the warehouse to it needs
  Can Manage on each.
- **Clearer install instructions.** The README and [Installation](docs/00-installation.md) now
  separate the deploy from the installer, give the workspace path first and the CLI second, and
  say what you see at each step (the GO / WARN / NO-GO lines, the app's sign-in prompt, how long
  profiling takes). The app's Permissions page is described as what carries on after the first
  install, not a way to do it. New: an Upgrade section and troubleshooting by the exact message.
- **The `dev` target has no placeholder host.** The workspace comes from `--profile`, or from the
  Git folder you deploy from, so no tracked file needs editing for the host. The warehouse ID is
  the one setting you add (`targets.dev.variables.warehouse_id`). `dataset_catalog` /
  `dataset_schema` now default to `manifest_catalog` / `manifest_schema`, so the dashboard and
  Genie follow the control schema without setting both.
- **Your own bundle targets stay local.** The bundle includes an optional, git-ignored
  `databricks.local.yml` for targets such as a long-lived demo deployment, and `.gitignore` now
  ignores every `.env.<name>` file except the tracked `*.example` templates.
- **Settings has one grant panel per identity.** "Permissions the app needs" is now **The app's
  own access**: the app service principal's control-table grants, checked and copyable. Its
  copy-paste grants for the jobs identity duplicated the Permissions plan and are gone; its schema
  picker moved into Permissions → Where the jobs have access, where a ticked schema joins the plan directly,
  and the per-table convert permission summary and script moved into Convert rights. Picks are no
  longer saved in the browser. `GET /api/grants/plan` takes `tier2=0` to skip the per-schema reads;
  without it the response is unchanged (the MCP tool `mtm_get_grants_plan` still gets Tier 2).
- **The profiler preflight no longer offers `READ METADATA` for enrichment.** A live test showed
  `DESCRIBE DETAIL` and `DESCRIBE HISTORY` still need `SELECT` with `READ METADATA` held on the
  metastore or catalog. An enrichment gap with no known catalogs now prints `USE CATALOG`,
  `USE SCHEMA` and `SELECT` on a `<catalog>` placeholder, and the broad alternatives are labelled
  discovery-only. `backend/docs/operations/privileges.md` records the result, and the new design
  note `backend/docs/install-and-permissions.md` plans notebook and CLI installs over one
  permissioning engine.

- **Shorter README, fuller docs.** `README.md` is now a short front door: what the tool does, how it
  works (a lifecycle diagram and app screenshots), prerequisites, the corrected convert
  permissions (ownership **or** `MANAGE`, plus `SELECT` + `MODIFY`; ownership never changes), two
  install paths with a success check, a first migration, and rollback. The detail moved into
  `docs/`, including two new pages: `docs/00-installation.md` (install, permissions, configuration,
  troubleshooting) and `docs/16-dashboard-and-genie.md`. `app/docs/app-sp-grants.md` drops the
  stale claim that `SET MANAGED` requires table ownership.
- **Permissions, pictured.** The README's permissions section is now a one-glance diagram and three
  bullets. `docs/00-installation.md` adds a diagram of which identity acts on what and a
  step-by-step table of what each step needs; `docs/07b-convert-permissions.md` adds the lifecycle
  of an Authorize grant through the 14-day window to its revoke at cleanup.
- **Settings → "What you can see" now generates grants.** Tick a discovered schema, or type one,
  and its Tier 2 grant SQL appears before the first profiler run. Previously "Add scope" only
  added a badge. Discovered schemas are unticked suggestions (Tier 2 includes `MANAGE`), and the
  selection is kept in the browser. The scan lists each catalog's table summaries eight at a time
  within a 20-second deadline instead of walking every schema one call at a time; a stopped scan
  says how much it covered instead of looking complete. "No scopes yet." no longer shows while
  the scan is still running, invalid typed names explain why, and typed scopes can be removed.
- **Settings → "What you can see" marks schemas the profiler already lists.** Their chips read
  "already covered by the scan" instead of offering a tick that changed nothing, because their
  grants are already in Step 2. "Select all" and the selected count skip them.
- **Settings → "Permissions the app needs" stays usable on large estates.** Step 2 opens on a
  summary and shows only missing grants by default, grouped by catalog with `USE CATALOG` once per
  catalog, and **Copy all missing**, **Copy all** and **Download .sql** take the whole list. The
  schema picker is grouped by catalog with a tick-all per catalog, has a filter, lists the
  schemas the scan does not cover by default, and scrolls inside a fixed height. The grant check
  reads each catalog and schema once, at most eight at a time, and reuses a result for a minute
  (Refresh checks again), so a 400-schema estate costs about 430 reads instead of 1,600.
  `GET /api/grants/plan` accepts an optional `scopes` parameter (validated server-side, max 50).
- **Install docs, with screenshots.** The README's Install section and `docs/00-installation.md`
  now walk the three front doors (notebook, CLI, Settings → Permissions) and Genie Code, with
  redacted screenshots under `docs/images/install/` from a live install. `docs/07b`, `docs/11`,
  `docs/12`, `docs/15`, `app/docs/app-sp-grants.md` and `backend/docs/contracts/jobs.md` (new
  section 26) cover the installer, and the design note records where the build differs.
- **Genie Code, front and center.** The README gains a "Drive it with Genie Code" section (what the
  app's MCP server is, how to connect it, example prompts, the `confirm=true` safety model).
  `docs/15-genie-code-mcp.md` adds a "What you can ask" table mapping prompts to tools; the
  overview, app, installation (connect step and the `mcp_tier` variable) and Genie knowledge
  pages now name Genie Code as the third way to drive the migration and separate it from the
  app's Ask Genie button.

## [0.1.0] - 2026-10-22

First public release of the Unity Catalog EXTERNAL → MANAGED table migration accelerator, packaged
as one Databricks Asset Bundle.

### Added

- **Manifest-driven stage jobs** (`backend/`): profiler (`00`), optional source scan (`01`), wave
  planner (`02`), dry run (`03`), convert (`04`, `ALTER TABLE ... SET MANAGED`, selector-pinned per
  wave), validate (`05`), rollback (`06`, `UNSET MANAGED` within the 14-day window) and cleanup
  (`07`), all driven by the `table_manifest` control table with a full `audit_log`.
- **Pre-convert safety gates**: name-based-access holds (path-based access with per-consumer
  remediation and an eligibility-gated redirect opt-in), recent OPTIMIZE / VACUUM / write
  recency checks, machine-verified compaction and stream stop coverage, a fail-closed
  managed-location gate, and convert-authority checks with an optional, verified Authorize step
  (`03d`).
- **Freeze / unfreeze bracket** (`03c` / `05b`) with a never-deleted `freeze_ledger`, exact
  restore on rollback and forward-only unfreeze.
- **Action queue and runbook**: `migration_actions` work queue (15 action kinds), the
  `migration_runbook` view, and a read-only cockpit export.
- **Finish steps**: Delta Sharing re-share (`08`), external readers / Compatibility Mode (`08b`),
  and Predictive Optimization plus liquid clustering (`08c`).
- **Wave planner v2** with risk buckets, a capped pilot, and parallel-safe batches for the bulk
  campaign (`11*` jobs, `11e` app-free cockpit).
- **Read-only conductor** (`08_conductor`) that recommends the next step after every stage, with
  per-scope focus and team pins.
- **Admin control-plane app** (`app/`, Databricks App on AppKit) over the same control tables, plus
  a Genie Code MCP server at `POST /mcp`.
- **AI/BI scoping dashboard and Genie space**.
- **Opt-out usage telemetry** with a closed vocabulary (no names, paths, IDs or SQL).
- **Generic `e2e` bundle target** and environment-driven live e2e drivers (`.env.e2e.example`).
- Local release tooling: `make ci` (backend pytest, app typecheck / tests / lint) and
  `make release TAG=vX.Y.Z` (`scripts/release.py`), which checks the tag matches the version in
  every versioned file and publishes the GitHub Release from this changelog.

[Unreleased]: https://github.com/databricks-solutions/managed-table-migration-accelerator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/databricks-solutions/managed-table-migration-accelerator/releases/tag/v0.1.0
