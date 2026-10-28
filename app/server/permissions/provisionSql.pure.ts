/**
 * The install-record SQL the app writes on the signed-in user's behalf: the same audit_log,
 * migration_scope and ownership_ledger rows the notebook (spark.sql) and the CLI (Statement
 * Execution) write, built exactly as backend/src/migration/provision.py builds them. Pinned to the
 * `sql` section of backend/src/tests/fixtures/provision_cases.json by provisionSql.pure.test.ts.
 */
import {
  AUDIT_INSTALL_GRANT,
  AUDIT_INSTALL_REVOKE,
  AUDIT_TABLE,
  type PlanLine,
  SCOPE_TABLE,
  LEDGER_PREFIX,
  LEDGER_STATUS_GRANTED,
  LEDGER_STATUS_HANDBACK_READY,
  LEDGER_STATUS_REVOKED,
  pyJson,
  scopeKind,
} from '../../client/src/lib/provision.pure.js';

export type Ts = string | Date | null | undefined;

/** `provision.sql_lit`: a single-quoted literal with backslash and quote escaped. */
export function sqlLit(v: unknown): string {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return String(v);
  const s = typeof v === 'string' ? v : typeof v === 'bigint' ? String(v) : JSON.stringify(v);
  return `'${s.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

/** UTC `YYYY-MM-DD HH:MM:SS`, the form `provision_io._now()` writes. */
export function tsText(v: Date): string {
  return v.toISOString().slice(0, 19).replace('T', ' ');
}

export function tsLit(v: Ts): string {
  if (v === null || v === undefined) return 'NULL';
  return `TIMESTAMP ${sqlLit(v instanceof Date ? tsText(v) : String(v))}`;
}

export const AUDIT_COLUMNS: [string, string][] = [
  ['audit_id', 'STRING'], ['full_name', 'STRING'], ['action', 'STRING'], ['phase', 'STRING'],
  ['from_state', 'STRING'], ['to_state', 'STRING'], ['operator', 'STRING'], ['runtime_version', 'STRING'],
  ['sql_command', 'STRING'], ['pre_convert_version', 'BIGINT'], ['pre_convert_num_files', 'BIGINT'],
  ['pre_convert_size_bytes', 'BIGINT'], ['error_class', 'STRING'], ['error_message', 'STRING'],
  ['detail', 'STRING'], ['start_time', 'TIMESTAMP'], ['end_time', 'TIMESTAMP'], ['created_at', 'TIMESTAMP'],
  ['plan_run_id', 'STRING'], ['wave_id', 'STRING'], ['actual_cores', 'INT'], ['recommended_cores', 'INT'],
  ['cluster_size_status', 'STRING'], ['job_id', 'STRING'], ['job_run_id', 'STRING'],
];

export type AuditRow = Record<string, unknown>;

export function auditInsertSql(catalog: string, schema: string, rows: AuditRow[]): string {
  const cols = AUDIT_COLUMNS.map(([c]) => c);
  const vals = rows.map((r) => `(${AUDIT_COLUMNS.map(([c, t]) => {
    const v = r[c];
    if (t === 'TIMESTAMP') return tsLit(v as Ts);
    if (t === 'BIGINT' || t === 'INT') return v === null || v === undefined ? 'NULL' : `CAST(${sqlLit(v)} AS ${t})`;
    return sqlLit(v);
  }).join(', ')})`);
  return `INSERT INTO ${catalog}.${schema}.${AUDIT_TABLE} (${cols.join(', ')}) VALUES ${vals.join(', ')}`;
}

type AuditLine = Pick<PlanLine, 'set' | 'grantee' | 'privilege' | 'securable_type' | 'securable' | 'statement'> &
  Partial<Pick<PlanLine, 'grantee_kind' | 'scope' | 'wave_id' | 'job_ids'>>;

export function auditDetail(
  ln: AuditLine,
  o: { door: string; pid: string; added?: boolean | null; prior_held?: boolean | null; grantor?: string | null },
): string {
  const d: Record<string, unknown> = {
    front_door: o.door, plan_id: o.pid, set: ln.set ?? null, grantee: ln.grantee ?? null,
    grantee_kind: ln.grantee_kind ?? null, privilege: ln.privilege ?? null, securable_type: ln.securable_type ?? null,
    securable: ln.securable ?? null, scope: ln.scope ?? null, added: o.added ?? null, prior_held: o.prior_held ?? null,
  };
  if (o.grantor) d.grantor = o.grantor;
  if (ln.job_ids?.length) d.job_ids = ln.job_ids.map(String);
  return pyJson(d, { sortKeys: true });
}

/** `provision.install_audit_row` (every audit column present; `audit_id` from the caller). */
export function installAuditRow(
  ln: AuditLine,
  o: {
    audit_id: string; action: string; phase: string; door: string; pid: string; operator: string | null; now: Ts;
    added?: boolean | null; prior_held?: boolean | null; error?: string | null; grantor?: string | null; statement?: string | null;
  },
): AuditRow {
  const row: AuditRow = Object.fromEntries(AUDIT_COLUMNS.map(([c]) => [c, null]));
  return {
    ...row,
    audit_id: o.audit_id,
    full_name: ln.securable ?? null,
    action: o.action,
    phase: o.phase,
    operator: o.operator ?? null,
    sql_command: o.statement || ln.statement || null,
    error_class: o.error ? 'INSTALL_GRANT_FAILED' : null,
    error_message: (o.error ?? '').slice(0, 4000) || null,
    detail: auditDetail(ln, { door: o.door, pid: o.pid, added: o.added, prior_held: o.prior_held, grantor: o.grantor }),
    start_time: o.now ?? null,
    end_time: o.now ?? null,
    created_at: o.now ?? null,
    wave_id: ln.wave_id ?? null,
  };
}

export const SCOPE_COLUMNS: [string, string][] = [
  ['scope', 'STRING'], ['scope_kind', 'STRING'], ['added_by', 'STRING'], ['added_at', 'TIMESTAMP'],
  ['front_door', 'STRING'], ['sets_granted', 'STRING'], ['convert_rights', 'STRING'],
  ['removed_at', 'TIMESTAMP'], ['removed_by', 'STRING'],
];

const scopeTable = (catalog: string, schema: string) => `${catalog}.${schema}.${SCOPE_TABLE}`;

export function scopeRows(
  scopes: string[],
  o: { added_by: string | null; door: string; sets: string[]; convert_rights: string; now: Ts },
): Record<string, unknown>[] {
  return scopes.map((s) => ({
    scope: s, scope_kind: scopeKind(s), added_by: o.added_by, added_at: o.now, front_door: o.door,
    sets_granted: o.sets.join(','), convert_rights: o.convert_rights, removed_at: null, removed_by: null,
  }));
}

export function scopeMergeSql(catalog: string, schema: string, rows: Record<string, unknown>[]): string {
  const sel = rows.map((r) => 'SELECT ' + SCOPE_COLUMNS.map(([c, t]) =>
    t === 'TIMESTAMP' && (r[c] === null || r[c] === undefined)
      ? `CAST(NULL AS TIMESTAMP) AS ${c}`
      : `${t === 'TIMESTAMP' ? tsLit(r[c] as Ts) : sqlLit(r[c])} AS ${c}`).join(', ')).join(' UNION ALL ');
  const names = SCOPE_COLUMNS.map(([c]) => c);
  return `MERGE INTO ${scopeTable(catalog, schema)} t USING (${sel}) s ON t.scope = s.scope ` +
    'WHEN MATCHED THEN UPDATE SET t.sets_granted = s.sets_granted, ' +
    't.convert_rights = s.convert_rights, t.removed_at = NULL, t.removed_by = NULL ' +
    `WHEN NOT MATCHED THEN INSERT (${names.join(', ')}) VALUES (${names.map((c) => `s.${c}`).join(', ')})`;
}

export const storedScopesSql = (catalog: string, schema: string): string =>
  `SELECT ${SCOPE_COLUMNS.map(([c]) => c).join(', ')} FROM ${scopeTable(catalog, schema)} WHERE removed_at IS NULL ORDER BY scope`;

export const LEDGER_COLUMNS: [string, string][] = [
  ['ledger_key', 'STRING'], ['action_key', 'STRING'], ['action_id', 'STRING'], ['op_kind', 'STRING'],
  ['scope', 'STRING'], ['catalog', 'STRING'], ['schema', 'STRING'], ['table_name', 'STRING'],
  ['full_name', 'STRING'], ['prior_owner', 'STRING'], ['new_owner', 'STRING'], ['dry_run', 'BOOLEAN'],
  ['status', 'STRING'], ['leased_at', 'TIMESTAMP'], ['leased_by', 'STRING'], ['restored_at', 'TIMESTAMP'],
  ['restored_by', 'STRING'], ['plan_run_id', 'STRING'], ['wave_id', 'STRING'], ['evidence', 'STRING'],
  ['notes', 'STRING'], ['updated_at', 'TIMESTAMP'], ['securable_type', 'STRING'], ['securable_name', 'STRING'],
  ['privilege', 'STRING'], ['grantee', 'STRING'], ['prior_held', 'BOOLEAN'], ['added', 'BOOLEAN'],
  ['granted_at', 'TIMESTAMP'], ['revoked_at', 'TIMESTAMP'],
];
export const LEDGER_MUTABLE_COLUMNS = new Set([
  'dry_run', 'status', 'leased_at', 'leased_by', 'restored_at', 'restored_by', 'plan_run_id', 'wave_id',
  'evidence', 'notes', 'updated_at', 'prior_held', 'added', 'granted_at', 'revoked_at',
]);
export const LEDGER_BOOLEAN_COLUMNS = LEDGER_COLUMNS.filter(([, t]) => t === 'BOOLEAN').map(([c]) => c);

/** `provision.ledger_row`: the ownership_ledger GRANT row for one installer Change line. */
export function ledgerRow(
  ln: Pick<PlanLine, 'securable_type' | 'securable' | 'privilege' | 'grantee'> & { wave_id?: string | null },
  o: { installer: string | null; door: string; pid: string; now: Ts; added: boolean; prior_held: boolean | null },
): Record<string, unknown> {
  const stype = String(ln.securable_type).toUpperCase();
  const fqn = String(ln.securable);
  const at = fqn.indexOf('.');
  return {
    ledger_key: `${LEDGER_PREFIX}|${stype} ${fqn}|${ln.privilege}|${ln.grantee}`,
    action_key: null, action_id: null, op_kind: 'GRANT', scope: stype,
    catalog: at < 0 ? fqn : fqn.slice(0, at), schema: at < 0 ? null : fqn.slice(at + 1) || null,
    table_name: null, full_name: null, prior_owner: null, new_owner: null, dry_run: false,
    status: LEDGER_STATUS_GRANTED, leased_at: o.now, leased_by: o.installer, restored_at: null, restored_by: null,
    plan_run_id: null, wave_id: ln.wave_id ?? null,
    evidence: pyJson({ front_door: o.door, plan_id: o.pid }, { sortKeys: true }),
    notes: 'installer convert rights (handed back with revoke_convert)', updated_at: o.now,
    securable_type: stype, securable_name: fqn, privilege: ln.privilege, grantee: ln.grantee,
    prior_held: o.prior_held, added: o.added, granted_at: o.now, revoked_at: null,
  };
}

/** `provision._KEEP_ADDED`: a re-apply never overwrites a live row that recorded the installer adding the grant. */
const KEEP_ADDED = '(coalesce(s.prior_held, false) AND coalesce(t.added, false) AND NOT coalesce(t.prior_held, false) ' +
  `AND t.status IN (${sqlLit(LEDGER_STATUS_GRANTED)}, ${sqlLit(LEDGER_STATUS_HANDBACK_READY)}))`;

export function ledgerMergeSql(catalog: string, schema: string, rows: Record<string, unknown>[]): string {
  const lit = (v: unknown, t: string) =>
    v === null || v === undefined ? `CAST(NULL AS ${t})` : t === 'TIMESTAMP' ? tsLit(v as Ts) : sqlLit(v);
  const sel = rows.map((r) => 'SELECT ' + LEDGER_COLUMNS.map(([c, t]) => `${lit(r[c], t)} AS ${c}`).join(', ')).join(' UNION ALL ');
  const names = LEDGER_COLUMNS.map(([c]) => c);
  const upd = names.filter((c) => LEDGER_MUTABLE_COLUMNS.has(c)).map((c) => `t.${c} = s.${c}`).join(', ');
  return `MERGE INTO ${catalog}.${schema}.ownership_ledger t USING (${sel}) s ` +
    `ON t.ledger_key = s.ledger_key WHEN MATCHED AND NOT ${KEEP_ADDED} THEN UPDATE SET ${upd} ` +
    `WHEN NOT MATCHED THEN INSERT (${names.join(', ')}) VALUES (${names.map((c) => `s.${c}`).join(', ')})`;
}

export function ledgerStatusSql(
  catalog: string, schema: string, keys: string[],
  o: { status: string; now: Ts; by?: string | null; from_status?: string[] },
): string {
  const quoted = keys.map(sqlLit).join(', ');
  const froms = (o.from_status ?? [LEDGER_STATUS_GRANTED]).map(sqlLit).join(', ');
  const extra = o.status === LEDGER_STATUS_REVOKED ? `, revoked_at = ${tsLit(o.now)}, restored_by = ${sqlLit(o.by ?? null)}` : '';
  return `UPDATE ${catalog}.${schema}.ownership_ledger SET status = ${sqlLit(o.status)}, ` +
    `updated_at = ${tsLit(o.now)}${extra} WHERE ledger_key IN (${quoted}) AND status IN (${froms})`;
}

/** Close hand-back actions once their REVOKE ran (operator-owned columns only). */
export function handbackDoneSql(catalog: string, schema: string, keys: string[], o: { now: Ts; by?: string | null }): string {
  return `UPDATE ${catalog}.${schema}.migration_actions SET status = 'DONE', ` +
    `resolution = 'installer convert rights handed back', completed_by = ${sqlLit(o.by ?? null)}, ` +
    `completed_at = ${tsLit(o.now)}, updated_at = ${tsLit(o.now)} ` +
    `WHERE action_key IN (${keys.map(sqlLit).join(', ')}) AND status NOT IN ('DONE', 'OBSOLETE')`;
}

export const installerLedgerSql = (catalog: string, schema: string): string =>
  `SELECT * FROM ${catalog}.${schema}.ownership_ledger WHERE ledger_key LIKE '${LEDGER_PREFIX}|%' AND dry_run = false`;

export const manifestStatesSql = (catalog: string, schema: string): string =>
  `SELECT catalog, schema, state FROM ${catalog}.${schema}.table_manifest`;

/** `provision.install_audit_sql`: every install grant and revoke the tool recorded. */
export const installAuditSql = (catalog: string, schema: string): string =>
  `SELECT action, phase, detail, created_at FROM ${catalog}.${schema}.${AUDIT_TABLE} ` +
  `WHERE action IN ('${AUDIT_INSTALL_GRANT}', '${AUDIT_INSTALL_REVOKE}')`;

export const windowEndsSql = (catalog: string, schema: string): string =>
  "SELECT lower(concat(split(full_name, '[.]')[0], '.', split(full_name, '[.]')[1])) AS scope, " +
  "date_format(max(end_time) + INTERVAL 14 DAYS, 'yyyy-MM-dd') AS ends " +
  `FROM ${catalog}.${schema}.${AUDIT_TABLE} WHERE action = 'CONVERT' ` +
  "AND phase = 'SUCCESS' GROUP BY 1";

/** `provision_io.wave_schemas`: one wave's schemas in the current (or pinned) plan. */
export function waveSchemasSql(catalog: string, schema: string, waveId: string, planRunId?: string | null): string {
  const pin = planRunId
    ? `AND plan_run_id = ${sqlLit(planRunId)}`
    : `AND plan_run_id = (SELECT max_by(plan_run_id, planned_at) FROM ${catalog}.${schema}.wave_plan)`;
  return 'SELECT DISTINCT lower(catalog) AS c, lower(schema) AS s, plan_run_id ' +
    `FROM ${catalog}.${schema}.wave_plan WHERE wave_id = ${sqlLit(waveId)} ${pin}`;
}
