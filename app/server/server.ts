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
import { registerPermissionsRoutes } from './routes/permissions.js';
import { registerMcpServer } from './mcp/register-mcp.js';
import { migrationJobs } from './jobs/job-params.js';
import { resolveMigrationCatalog, resolveMigrationSchema } from './config/catalog.js';
import { readControlConfig } from './config/control-config.js';
import { BOOT_PLACEHOLDER, armGenieSpaces, liveGenieSpaces, noteGenieSpace } from './config/genieSpace.js';

async function main() {
  // One startup read of the migration_control_config handshake row, BEFORE createApp, to seed
  // the Genie space cache (dashboard_id is served live via /api/config). Graceful — nulls when
  // the installer hasn't created the space yet.
  const control = await readControlConfig();
  noteGenieSpace(control.genieSpaceId);

  // ── AppKit resource-gate trap (read before adding a plugin) ──────────────────────────
  // AppKit plugins declare manifest.resources.required with a HARDCODED env var name.
  // enforceValidation() checks process.env[<name>] at createApp and throws in production,
  // independent of plugin config. An empty config value satisfies the config schema and
  // still trips the gate. Either bind the env var to a real resource, or set it yourself.
  //   - genie: gated on DATABRICKS_GENIE_SPACE_ID. The space is created by the installer
  //     (job_99_setup), after the deploy, so the id cannot be bound in app/resources/app.yml.
  //     The plugin is ALWAYS registered with a live `spaces` getter (config/genieSpace.ts),
  //     looked up per request, so Genie comes online without a second deploy; the env var
  //     carries the boot id or a placeholder purely to clear the gate.
  //   - jobs:  gated on DATABRICKS_JOB_ID (plus one env per configured alias) → bound to a
  //     real job in app/resources/app.yml (value_from: profiler).
  //   - analytics: gated on DATABRICKS_WAREHOUSE_ID → set as a PLAIN env value (value:
  //     ${var.warehouse_id}), NOT a bound resource. The app already binds 19 jobs and Apps cap
  //     an app at 20 resources, so the warehouse can't also be one. The gate only reads the env
  //     var, so the app boots; the SP's CAN_USE is granted out-of-band by job_99_setup.
  // ─────────────────────────────────────────────────────────────────────────────────────
  process.env.DATABRICKS_GENIE_SPACE_ID = control.genieSpaceId ?? BOOT_PLACEHOLDER;
  const plugins = [
    analytics(),
    genie({ spaces: liveGenieSpaces(readControlConfig) }),
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
      armGenieSpaces();
      appkit.server.extend((app) => {
        // GET /api/config — the single place the client learns catalog, schema,
        // dashboard_id, and whether Genie is set up. Re-read live per request; each read also
        // refreshes the Genie space the plugin uses (config/genieSpace.ts).
        registerConfigRoute(app);
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
        // GET /api/permissions, POST /api/permissions/grant, POST /api/permissions/hand-back —
        // the install engine's app door. Every statement runs as the signed-in user (forwarded
        // token, never the app's identity), plan_id-pinned, audit row first, read back after.
        registerPermissionsRoutes(app);
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
