import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { PlanLine } from '../../client/src/lib/provision.pure.js';
import {
  auditInsertSql,
  handbackDoneSql,
  installAuditRow,
  installerLedgerSql,
  ledgerMergeSql,
  ledgerRow,
  ledgerStatusSql,
  manifestStatesSql,
  scopeMergeSql,
  scopeRows,
  storedScopesSql,
  tsText,
  windowEndsSql,
} from './provisionSql.pure.js';
import { handbackActionKey } from '../../client/src/lib/provision.pure.js';

// The app writes the same install records as the notebook and the CLI: pinned to provision.py.
const SQL = (JSON.parse(
  readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/src/tests/fixtures/provision_cases.json'), 'utf8'),
) as { sql: Record<string, unknown> }).sql as {
  line: PlanLine;
  audit_row: Record<string, unknown>;
  audit_insert: string;
  scope_rows: Record<string, unknown>[];
  scope_merge: string;
  stored_scopes: string;
  ledger_rows: Record<string, unknown>[];
  ledger_merge: string;
  ledger_status: string;
  handback_done: string;
  installer_ledger: string;
  manifest_states: string;
  window_ends: string;
};
const TS = '2026-10-26 12:00:00';

describe('install-record SQL equals provision.py (golden fixture)', () => {
  it('audit row and INSERT', () => {
    const row = installAuditRow(SQL.line, {
      audit_id: '00000000-0000-0000-0000-000000000001', action: 'INSTALL_GRANT', phase: 'FAILED', door: 'app',
      pid: 'd04c4bab08e2', operator: "o'brien@x.com", now: TS, added: false, prior_held: null,
      error: 'PERMISSION_DENIED: no MANAGE on `sales`\\x',
    });
    expect(row).toEqual(SQL.audit_row);
    expect(auditInsertSql('main', 'migration', [row])).toBe(SQL.audit_insert);
  });

  it('scope rows and MERGE', () => {
    const rows = scopeRows(['sales.orders', 'finance.*'], {
      added_by: 'ann@x.com', door: 'app', sets: ['Change', 'Read'], convert_rights: 'all_at_once', now: TS,
    });
    expect(rows).toEqual(SQL.scope_rows);
    expect(scopeMergeSql('main', 'migration', rows)).toBe(SQL.scope_merge);
    expect(storedScopesSql('main', 'migration')).toBe(SQL.stored_scopes);
  });

  it('ledger rows, MERGE, status flip and hand-back close', () => {
    const [sch, cat] = SQL.ledger_rows;
    const base = { installer: 'ann@x.com', door: 'app', pid: 'd04c4bab08e2', now: TS, added: true, prior_held: false };
    const mk = (r: Record<string, unknown>) => ledgerRow({
      securable_type: String(r.securable_type), securable: String(r.securable_name), privilege: String(r.privilege),
      grantee: String(r.grantee), wave_id: null,
    }, base);
    expect(mk(sch)).toEqual(sch);
    expect({ ...mk(cat), status: cat.status }).toEqual(cat);
    expect(ledgerMergeSql('main', 'migration', SQL.ledger_rows)).toBe(SQL.ledger_merge);
    const keys = SQL.ledger_rows.map((r) => String(r.ledger_key));
    expect(ledgerStatusSql('main', 'migration', keys, {
      status: 'REVOKED', now: TS, by: 'ann@x.com', from_status: ['GRANTED', 'HANDBACK_READY'],
    })).toBe(SQL.ledger_status);
    expect(handbackDoneSql('main', 'migration', SQL.ledger_rows.map(handbackActionKey), { now: TS, by: 'ann@x.com' }))
      .toBe(SQL.handback_done);
  });

  it('record reads', () => {
    expect(installerLedgerSql('main', 'migration')).toBe(SQL.installer_ledger);
    expect(manifestStatesSql('main', 'migration')).toBe(SQL.manifest_states);
    expect(windowEndsSql('main', 'migration')).toBe(SQL.window_ends);
  });

  it('timestamps are UTC to the second, like provision_io._now()', () => {
    expect(tsText(new Date(Date.UTC(2026, 9, 26, 12, 0, 0, 999)))).toBe(TS);
  });
});
