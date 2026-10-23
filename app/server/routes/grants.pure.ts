/**
 * Pure builders + verifier for the App service-principal grant SQL the Settings panel shows.
 *
 * This is a deliberate TypeScript PORT of backend/src/migration/grants.py (Tier 1 specs +
 * grant_statement / show_grants_statement / sp_has_grant) — NOT an import: the app must never
 * depend on backend/ (the permanent app/backend split). Keep the two in behavioural lockstep:
 * same securables, same statement shape, same principal-matching rule.
 *
 * The app only GENERATES and VERIFIES these strings; it never executes a GRANT (SR12 / §0.2).
 * No I/O — unit-tested in grants.pure.test.ts.
 */

export type SecurableType = 'CATALOG' | 'SCHEMA' | 'TABLE';

export interface GrantSpec {
  privilege: string;
  securableType: SecurableType;
  securableFqn: string;
}

/** SHOW GRANTS row — dict-like; column spelling varies by runtime. */
export type ShowGrantsRow = Record<string, unknown>;

const ACTIONS_TABLE = 'migration_actions';

/**
 * Tier 1 — the minimal control-plane grant set (port of grants.py:app_sp_grant_specs):
 * USE CATALOG on the catalog, USE SCHEMA + SELECT on the control schema (SELECT inherits to all
 * current + future tables AND views), MODIFY on migration_actions ONLY (never ALL PRIVILEGES —
 * the app must not rewrite audit_log / wave_plan / freeze_ledger).
 */
export function tier1Specs(catalog: string, schema: string): GrantSpec[] {
  const sch = `${catalog}.${schema}`;
  return [
    { privilege: 'USE CATALOG', securableType: 'CATALOG', securableFqn: catalog },
    { privilege: 'USE SCHEMA', securableType: 'SCHEMA', securableFqn: sch },
    { privilege: 'SELECT', securableType: 'SCHEMA', securableFqn: sch },
    { privilege: 'MODIFY', securableType: 'TABLE', securableFqn: `${sch}.${ACTIONS_TABLE}` },
  ];
}

/**
 * Tier 2 — data-plane namespace grants for ONE target schema being migrated:
 * USE CATALOG (catalog) + USE SCHEMA / SELECT / MANAGE (schema). Schema-grain by design: these
 * apply to every table in the schema, including ones OBO never enumerated (app-sp-grants.md).
 * NOTE: table OWNERSHIP for SET MANAGED is NOT a grant — it is the separate ownership .sql
 * download (Task 5), never emitted here.
 */
export function tier2Specs(catalog: string, schema: string): GrantSpec[] {
  const sch = `${catalog}.${schema}`;
  return [
    { privilege: 'USE CATALOG', securableType: 'CATALOG', securableFqn: catalog },
    { privilege: 'USE SCHEMA', securableType: 'SCHEMA', securableFqn: sch },
    { privilege: 'SELECT', securableType: 'SCHEMA', securableFqn: sch },
    { privilege: 'MANAGE', securableType: 'SCHEMA', securableFqn: sch },
  ];
}

/** A single additive GRANT keyed by the SP client-id UUID, backtick-escaped (dashes require it). */
export function grantStatement(spec: GrantSpec, spClientId: string): string {
  return `GRANT ${spec.privilege} ON ${spec.securableType} ${spec.securableFqn} TO \`${spClientId}\``;
}

/** SHOW GRANTS scoped to the SP on one securable — the read-back for verification. */
export function showGrantsStatement(spec: GrantSpec, spClientId: string): string {
  return `SHOW GRANTS \`${spClientId}\` ON ${spec.securableType} ${spec.securableFqn}`;
}

/** Coerce a dict-like cell (typed `unknown`) to a string without base-stringifying an object. */
function toStr(v: unknown): string {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint') return String(v);
  return '';
}

/** Uppercase, collapse whitespace, treat `_` as a space so USE_CATALOG == USE CATALOG. */
function normPriv(privilege: unknown): string {
  return toStr(privilege)
    .toUpperCase()
    .replace(/_/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .join(' ');
}

function rowGet(row: ShowGrantsRow, ...keys: string[]): unknown {
  for (const key of keys) {
    if (key in row) return row[key];
  }
  return undefined;
}

/**
 * True iff the read-back shows the SP holding `privilege` (or ALL PRIVILEGES). Port of
 * grants.py:sp_has_grant: tolerant of Principal/principal + ActionType/action_type spellings;
 * a row carrying only a display name (not the UUID) is not a match. Honest: empty rows / empty
 * SP → false (never fabricate present).
 */
export function spHasGrant(rows: ShowGrantsRow[], spClientId: string, privilege: string): boolean {
  if (!spClientId) return false;
  const want = normPriv(privilege);
  for (const row of rows ?? []) {
    const principal = rowGet(row, 'Principal', 'principal');
    if (principal != null && toStr(principal).toLowerCase() !== spClientId.toLowerCase()) continue;
    const action = normPriv(rowGet(row, 'ActionType', 'action_type', 'actionType'));
    if (action === want || action === 'ALL PRIVILEGES') return true;
  }
  return false;
}
