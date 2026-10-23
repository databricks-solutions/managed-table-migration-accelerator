import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  MAX_REQUESTED_SCOPES,
  grantStatement,
  parseScopeParam,
  showGrantsStatement,
  spHasGrant,
  tier1Specs,
  tier2Specs,
} from './grants.pure.js';

const SP = 'a1b2c3d4-0000-1111-2222-333344445555';

const CASES = JSON.parse(
  readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'fixtures/scope_cases.json'), 'utf8'),
) as { valid: { input: string; catalog: string; schema: string }[]; invalid: string[] };

describe('parseScopeParam', () => {
  it('accepts every golden valid scope', () => {
    for (const c of CASES.valid) {
      expect(parseScopeParam(c.input)).toEqual([{ catalog: c.catalog, schema: c.schema }]);
    }
  });

  it('drops every golden invalid scope, including injection-shaped input', () => {
    for (const bad of CASES.invalid) {
      expect(parseScopeParam(bad)).toEqual([]);
    }
  });

  it('splits commas, accepts a repeated parameter, collapses duplicates and keeps the valid ones', () => {
    expect(parseScopeParam(['main.sales,bad name,main.sales', 'hr.people'])).toEqual([
      { catalog: 'main', schema: 'sales' },
      { catalog: 'hr', schema: 'people' },
    ]);
  });

  it(`caps at ${MAX_REQUESTED_SCOPES} scopes`, () => {
    const raw = Array.from({ length: MAX_REQUESTED_SCOPES + 10 }, (_, i) => `c.s${i}`).join(',');
    expect(parseScopeParam(raw)).toHaveLength(MAX_REQUESTED_SCOPES);
  });

  it('is empty for an absent or non-string parameter', () => {
    expect(parseScopeParam(undefined)).toEqual([]);
    expect(parseScopeParam({ a: 'main.sales' })).toEqual([]);
  });
});

describe('tier1Specs', () => {
  it('is the 4 least-privilege control-plane grants, in order', () => {
    expect(tier1Specs('cat', 'migration')).toEqual([
      { privilege: 'USE CATALOG', securableType: 'CATALOG', securableFqn: 'cat' },
      { privilege: 'USE SCHEMA', securableType: 'SCHEMA', securableFqn: 'cat.migration' },
      { privilege: 'SELECT', securableType: 'SCHEMA', securableFqn: 'cat.migration' },
      { privilege: 'MODIFY', securableType: 'TABLE', securableFqn: 'cat.migration.migration_actions' },
    ]);
  });
});

describe('tier2Specs', () => {
  it('is the namespace grants for one target schema (USE CATALOG/USE SCHEMA/SELECT/MANAGE)', () => {
    expect(tier2Specs('sales', 'orders')).toEqual([
      { privilege: 'USE CATALOG', securableType: 'CATALOG', securableFqn: 'sales' },
      { privilege: 'USE SCHEMA', securableType: 'SCHEMA', securableFqn: 'sales.orders' },
      { privilege: 'SELECT', securableType: 'SCHEMA', securableFqn: 'sales.orders' },
      { privilege: 'MANAGE', securableType: 'SCHEMA', securableFqn: 'sales.orders' },
    ]);
  });
});

describe('grantStatement / showGrantsStatement', () => {
  it('emits GRANT … TO `<uuid>` with the SP backtick-escaped', () => {
    expect(grantStatement(tier1Specs('c', 's')[0], SP)).toBe(
      `GRANT USE CATALOG ON CATALOG c TO \`${SP}\``,
    );
    expect(grantStatement(tier1Specs('c', 's')[3], SP)).toBe(
      `GRANT MODIFY ON TABLE c.s.migration_actions TO \`${SP}\``,
    );
  });

  it('emits the scoped SHOW GRANTS read-back', () => {
    expect(showGrantsStatement(tier1Specs('c', 's')[1], SP)).toBe(
      `SHOW GRANTS \`${SP}\` ON SCHEMA c.s`,
    );
  });
});

describe('spHasGrant (port of grants.py:sp_has_grant)', () => {
  it('matches the privilege for the SP (Principal + ActionType spellings)', () => {
    const rows = [{ Principal: SP, ActionType: 'USE CATALOG' }];
    expect(spHasGrant(rows, SP, 'USE CATALOG')).toBe(true);
  });

  it('normalizes USE_CATALOG vs USE CATALOG and lowercases the principal compare', () => {
    const rows = [{ principal: SP.toUpperCase(), action_type: 'USE_CATALOG' }];
    expect(spHasGrant(rows, SP, 'USE CATALOG')).toBe(true);
  });

  it('treats ALL PRIVILEGES as covering any privilege', () => {
    expect(spHasGrant([{ Principal: SP, ActionType: 'ALL PRIVILEGES' }], SP, 'SELECT')).toBe(true);
  });

  it('does NOT match another principal or a missing privilege', () => {
    expect(spHasGrant([{ Principal: 'someone-else', ActionType: 'SELECT' }], SP, 'SELECT')).toBe(false);
    expect(spHasGrant([{ Principal: SP, ActionType: 'MODIFY' }], SP, 'SELECT')).toBe(false);
  });

  it('is false on empty rows or empty SP (honest — never fabricate present)', () => {
    expect(spHasGrant([], SP, 'SELECT')).toBe(false);
    expect(spHasGrant([{ Principal: SP, ActionType: 'SELECT' }], '', 'SELECT')).toBe(false);
  });
});
