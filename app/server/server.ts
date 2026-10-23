import { createApp, analytics, genie, jobs, server } from '@databricks/appkit';
import { registerActionWriteRoute } from './routes/action-write.js';
import { registerConfigRoute } from './routes/app-config.js';
import { registerJobTriggerRoute } from './routes/job-trigger.js';
import { registerJobsActiveRoute } from './routes/jobs-active.js';
import { registerJobsRecentRoute } from './routes/jobs-recent.js';
import { registerJobsHistoryRoute } from './routes/jobs-history.js';
import { registerDiagRoute } from './routes/diag.js';
import { registerReadinessRoute } from './routes/readiness.js';
import { registerGrantsPlanRoute } from './routes/grants-plan.js';
import { registerEstateDiscoveryRoute } from './routes/estate-discovery.js';
import { registerOwnershipSqlRoute } from './routes/ownership-sql.js';
import { registerConvertPreflightRoute } from './routes/convert-preflight.js';
import { registerMcpServer } from './mcp/register-mcp.js';
import { migrationJobs } from './jobs/job-params.js';
import { resolveMigrationCatalog, resolveMigrationSchema } from './config/catalog.js';
import { readControlConfig } from './config/control-config.js';

async function main() {
  // One startup read of the migration_control_config handshake row, BEFORE createApp:
  // genie_space_id must be known to construct genie({ spaces }), and dashboard_id is
  // served via /api/config. Graceful — nulls when the profiler hasn't run yet.
  const control = await readControlConfig();

  // AppKit's genie plugin declares its genie_space resource REQUIRED in its static manifest
  // (@databricks/appkit .../plugins/genie/manifest.js). ResourceRegistry.enforceValidation()
  // checks process.env.DATABRICKS_GENIE_SPACE_ID at createApp and THROWS in production if it
  // is unset — regardless of the `spaces` config. The space id is a runtime handshake value
  // (created by job_99_setup, absent on a fresh workspace), so we do TWO things together,
  // gated on the handshake row carrying a space id: (1) export DATABRICKS_GENIE_SPACE_ID from
  // that row so the gate passes, and (2) register genie() (conditional spread below). Doing
  // only ONE crashes the app — registering genie() without the env trips the resource gate;
  // and the config `spaces` map does NOT satisfy the gate (validate() reads the env var, not
  // the config). When the row has no space id yet, genie() isn't constructed at all: the
  // Genie panel shows its first-run empty state (genie_available:false via /api/config), and
  // a later app restart — after job_99_setup creates the space and writes the handshake —
  // brings Genie online (same restart-to-pick-up sequencing as the SP grants; not
  // hot-reloaded). Restart via `bundle deploy`, NOT `databricks apps start`: a standalone
  // start does not re-apply the bundle's config.env, so the whole env (MIGRATION_CATALOG,
  // warehouse, job ids) drops and the app crashes on the MIGRATION_CATALOG check instead.
  // ── AppKit resource-gate trap (read before adding a plugin) ──────────────────────────
  // AppKit plugins declare manifest.resources.required with a HARDCODED env var name.
  // enforceValidation() checks process.env[<name>] at createApp and throws in production,
  // independent of plugin config. An empty config value satisfies the config schema and
  // still trips the gate. Either bind the env var to a real resource, or do not register
  // the plugin at all.
  //   - genie: gated on DATABRICKS_GENIE_SPACE_ID → registered ONLY when a space id exists
  //     (conditional spread below). CRITICAL: ResourceRegistry.validate() reads the ENV VAR,
  //     not the `spaces` config — spaces:{} does NOT clear the gate — so once a space id is
  //     configured we MUST also export DATABRICKS_GENIE_SPACE_ID or the app crashes at boot
  //     with "Missing required resources: genie_space". The space id is a RUNTIME handshake
  //     value (created by job_99_setup, absent at deploy), so it cannot be bound in
  //     app/resources/app.yml the way the warehouse/jobs are — we set it here from the row we
  //     just read, immediately before constructing genie().
  //   - jobs:  gated on DATABRICKS_JOB_ID (plus one env per configured alias) → bound to a
  //     real job in app/resources/app.yml (value_from: profiler).
  //   - analytics: gated on DATABRICKS_WAREHOUSE_ID → set as a PLAIN env value (value:
  //     ${var.warehouse_id}), NOT a bound resource. The app already binds 19 jobs and Apps cap
  //     an app at 20 resources, so the warehouse can't also be one. The gate only reads the env
  //     var (same as genie above), so the app boots; the SP's CAN_USE is granted out-of-band by
  //     job_99_setup (see app/resources/app.yml).
  // ─────────────────────────────────────────────────────────────────────────────────────
  if (control.genieSpaceId) {
    process.env.DATABRICKS_GENIE_SPACE_ID = control.genieSpaceId;
  }
  const plugins = [
    analytics(),
    ...(control.genieSpaceId ? [genie({ spaces: { default: control.genieSpaceId } })] : []),
    // 19 operator-facing jobs, run-now param schemas from docs/contracts/jobs.md; IDs
    // resolve from DATABRICKS_JOB_<ALIAS> env vars (bundle). See job-params.ts.
    jobs(migrationJobs),
    server(),
  ];

  await createApp({
    plugins,
    onPluginsReady(appkit) {
      // Fail fast at startup: a missing/invalid MIGRATION_CATALOG crashes here (naming
      // the variable), not on first request. (Handshake nulls are NOT fatal — see above.)
      resolveMigrationCatalog();
      resolveMigrationSchema();
      appkit.server.extend((app) => {
        // GET /api/config — the single place the client learns catalog, schema,
        // dashboard_id, and whether Genie is configured yet.
        // genie_available reflects THIS process's plugin (spaces fixed at construct);
        // dashboard_id + genie_space_configured are re-read live per request in the route.
        registerConfigRoute(app, { genieAvailableAtBoot: control.genieSpaceId !== null });
        // POST /api/actions/update — idempotent MERGE UPDATE on migration_actions
        // (notes/assignee) as the service principal. Same catalog origin as reads.
        registerActionWriteRoute(app);
        // POST /api/jobs/:alias/trigger — SDK jobs.runNow with job_parameters +
        // idempotency_token (the plugin can't send job-level params). Plugin keeps
        // /status and /runs. See routes/job-trigger.ts.
        registerJobTriggerRoute(app);
        // GET /api/jobs/active — the shared active-run read (run pill + trigger de-dup
        // awareness). Lists active runs across the migration aliases. See routes/jobs-active.ts.
        registerJobsActiveRoute(app);
        // GET /api/jobs/recent — latest terminal run per alias, so the client can surface
        // a failed run's outcome instead of it silently vanishing. See routes/jobs-recent.ts.
        registerJobsRecentRoute(app);
        // GET /api/jobs/history — a unified, cross-alias feed of the latest completed runs
        // (profiler/planner/freeze/lease/validate/…) that the two control-table ledgers on
        // Runs & history intentionally omit. See routes/jobs-history.ts.
        registerJobsHistoryRoute(app);
        // GET /api/diag/control-read — admin diagnostic returning the VERBATIM Databricks
        // error behind read failures (the plugin sanitizes INSUFFICIENT_PERMISSIONS to
        // BAD_REQUEST client-side). See routes/diag.ts.
        registerDiagRoute(app);
        // GET /api/diag/readiness — SP-run positive readiness verdict (READY /
        // NOT_PROVISIONED / PERMISSION / OTHER) so DataSurface can show a first-run state
        // on a fresh deploy instead of a red error. See routes/readiness.ts.
        registerReadinessRoute(app);
        // GET /api/grants/plan — generates Tier 1/2 grant SQL parameterized from the SP identity +
        // control/target namespaces (manifest targets plus the optional, validated `scopes` the
        // admin ticked), each securable annotated present via SHOW GRANTS. Read-only;
        // the app never executes a GRANT (SR12). See routes/grants-plan.ts.
        registerGrantsPlanRoute(app);
        // GET /api/estate/obo-discovery — enumerates the EXTERNAL tables the SIGNED-IN admin can
        // see (OBO, forwarded token), for scope pre-fill. Labelled admin-visibility, never "the
        // estate"; never triggers a job. See routes/estate-discovery.ts.
        registerEstateDiscoveryRoute(app);
        // GET /api/grants/convert-permissions.sql (alias /api/grants/ownership.sql) — the
        // convert-permission grant script from the stored convert_authority checks, one section
        // per grantor. Generated only; the app never runs a GRANT.
        registerOwnershipSqlRoute(app);
        // GET /api/convert/preflight — the UI's read-only window onto the SAME preflight the MCP
        // tool serves (runPreflight, impacts.authority from convert_authority), so the app convert gate and Genie
        // agree by construction. Triggers nothing; the convert job re-checks every gate.
        registerConvertPreflightRoute(app);
        // POST/GET/DELETE /mcp — Genie Code custom MCP surface. Stateless streamable HTTP;
        // reuses the same reads + triggerAlias() the UI uses (no more-permissive path). v1 =
        // reads + non-mutating stages; MCP_TIER=v2 exposes gated conversions. See mcp/register-mcp.ts.
        registerMcpServer(app);
      });
    },
  });
}

main().catch((err: unknown) => {
  // A config error (e.g. missing MIGRATION_CATALOG) surfaces here — crash loud, non-zero.
  console.error(err);
  process.exit(1);
});
