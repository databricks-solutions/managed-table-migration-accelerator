/**
 * Single source of truth for which Unity Catalog + schema the app reads from and writes
 * to. The value originates in exactly ONE place — the server-side MIGRATION_CATALOG env
 * var (and MIGRATION_SCHEMA, which defaults to 'migration') — and is never taken from a
 * request. The write route calls resolveMigrationCatalog() directly; the read path gets
 * it from the client, which fetches it once from GET /api/config (see
 * server/routes/app-config.ts). Both trace back here, so the app can never read one
 * catalog and write to another.
 *
 * There is NO catalog fallback: an unset/empty MIGRATION_CATALOG THROWS (fail loud). A
 * silent default is right on one workspace and wrong on every other, with no error — the
 * kind of silent failure this project has spent its history removing.
 */

const IDENT_RE = /^[A-Za-z0-9_]+$/;
const DEFAULT_SCHEMA = 'migration';

/**
 * A self-diagnosing config error: names the variable, says where the value comes from in
 * each environment, and states what was expected. At deploy time this crash must not read
 * as a bundle/deploy failure to someone who hasn't seen this code — the text is the fix.
 */
function configError(varName: string, expected: string): Error {
  return new Error(
    `${varName} is missing or invalid — expected ${expected}. ` +
      `Deployed runs get it from config.env in app/resources/app.yml; ` +
      `local dev reads it from app/.env. ` +
      `This is a configuration error at startup, not a deploy failure.`,
  );
}

/** The migration catalog. REQUIRED — no default; throws if unset, empty, or non-identifier. */
export function resolveMigrationCatalog(): string {
  const raw = process.env.MIGRATION_CATALOG;
  if (!raw || !IDENT_RE.test(raw)) {
    throw configError(
      'MIGRATION_CATALOG',
      'a bare-identifier Unity Catalog name (e.g. my_catalog)',
    );
  }
  return raw;
}

/**
 * The migration schema. Defaults to 'migration' — a contract-fixed constant the read
 * queries hardcode; validated as a bare identifier if an override is set.
 *
 * NOTE: the read queries hardcode `.migration.` in IDENTIFIER(:catalog || '.migration.<t>').
 * This resolver feeds only the WRITE path (migrationTableFqn) and /api/config, so setting
 * MIGRATION_SCHEMA to anything but 'migration' would move writes off the schema the reads
 * are pinned to — a read/write divergence. Do not override it unless the read queries are
 * parameterized on schema too.
 */
export function resolveMigrationSchema(): string {
  const raw = process.env.MIGRATION_SCHEMA;
  if (raw === undefined || raw === '') return DEFAULT_SCHEMA;
  if (!IDENT_RE.test(raw)) {
    throw configError('MIGRATION_SCHEMA', "a bare identifier (defaults to 'migration' when unset)");
  }
  return raw;
}

/** Fully-qualified name for a table in the migration schema, e.g. migration_actions. */
export function migrationTableFqn(table: string): string {
  return `${resolveMigrationCatalog()}.${resolveMigrationSchema()}.${table}`;
}
