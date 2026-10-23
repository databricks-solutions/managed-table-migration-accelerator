import { createTaggedWorkspaceClient } from '../telemetry/client.js';
import { resolveMigrationCatalog, resolveMigrationSchema } from './catalog.js';

export interface ControlConfig {
  /** Genie space id from the handshake row; null until the profiler populates it. */
  genieSpaceId: string | null;
  /** AI/BI dashboard id from the handshake row; null until the profiler populates it. */
  dashboardId: string | null;
}

/**
 * Hard upper bound on the handshake read. A cold SQL warehouse can take 30–90s to start,
 * and this read runs during app boot (and per /api/config request) — Databricks Apps have
 * a startup health timeout, so a slow read can FAIL the deploy, not just be slow. If a
 * result doesn't arrive within this window we bail and degrade to {null, null}, exactly as
 * on any other error. A cold warehouse costs a Genie panel, not a failed deploy.
 *
 * KNOWN SYMPTOM — not a bug: because the BOOT read is what decides whether genie() registers,
 * a cold warehouse at boot makes /api/config report `genie_available: false` even when a
 * genie_space_id exists. `genie_space_configured` (re-read live per request, warehouse now warm)
 * still shows true. A restart WHILE THE WAREHOUSE IS WARM flips genie_available true. See the
 * README "genie_available can read false right after a restart (cold warehouse)".
 */
const CONTROL_READ_TIMEOUT_MS = 4000;

/**
 * One-shot startup read of the migration_control_config handshake row — done BEFORE
 * createApp, because genie_space_id must be known to construct genie({ spaces }), which
 * happens before the AppKit query layer exists. Uses the SDK statement-execution path
 * (createWorkspaceClient + the bundle warehouse) — the same warehouse the write route uses.
 *
 * GRACEFUL BY DESIGN. On a fresh install the app deploys before the profiler has ever
 * run, so the table may be missing, the row absent, or genie_space_id NULL (the column
 * itself materialises on the next profiler run via overwriteSchema). That is the expected
 * FIRST-RUN state, not a config error: every failure here degrades to nulls and the app
 * renders "run the profiler" empty states. Only a missing MIGRATION_CATALOG is fatal, and
 * that throw comes from resolveMigrationCatalog() OUTSIDE the try below.
 *
 * Read once at boot: the profiler creates the space once and updates it thereafter, so
 * the id is stable — a value that changes takes effect on the next app restart.
 */
export async function readControlConfig(): Promise<ControlConfig> {
  const catalog = resolveMigrationCatalog(); // fatal if MIGRATION_CATALOG unset — intended
  const schema = resolveMigrationSchema();
  const empty: ControlConfig = { genieSpaceId: null, dashboardId: null };

  try {
    const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
    if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID is not set');

    const client = createTaggedWorkspaceClient();
    // SELECT * (not the named columns) so a not-yet-materialised genie_space_id column
    // does not error — columns are picked by name from the result manifest. wait_timeout
    // is the API minimum (5s) + CANCEL so the server-side statement can't linger after we
    // stop waiting.
    const exec = client.statementExecution.executeStatement({
      warehouse_id: warehouseId,
      statement: `SELECT * FROM ${catalog}.${schema}.migration_control_config LIMIT 1`,
      wait_timeout: '5s',
      on_wait_timeout: 'CANCEL',
    });
    exec.catch(() => undefined); // swallow a late rejection if the timeout wins the race
    // Bound the wait: if the warehouse is cold, bail at CONTROL_READ_TIMEOUT_MS and degrade.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const resp = await Promise.race([
      exec,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`timed out after ${CONTROL_READ_TIMEOUT_MS}ms (warehouse likely cold)`)),
          CONTROL_READ_TIMEOUT_MS,
        );
      }),
    ]).finally(() => {
      if (timer) clearTimeout(timer);
    });
    if (resp.status?.state !== 'SUCCEEDED') {
      throw new Error(resp.status?.error?.message ?? `statement ${resp.status?.state ?? 'unknown'}`);
    }

    const row = resp.result?.data_array?.[0];
    if (!row) return empty; // no row yet — profiler hasn't run

    const cols = resp.manifest?.schema?.columns ?? [];
    const at = (name: string): string | null => {
      const col = cols.find((c) => c.name === name);
      if (!col || col.position === undefined) return null; // column not present yet
      const v = row[col.position];
      return v === undefined || v === null || v === '' ? null : v;
    };
    return { genieSpaceId: at('genie_space_id'), dashboardId: at('dashboard_id') };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(
      `[control-config] Could not read migration_control_config (${msg}). ` +
        `This is expected before the first profiler run — Genie and the dashboard show ` +
        `first-run empty states until the profiler populates the handshake row.`,
    );
    return empty;
  }
}
