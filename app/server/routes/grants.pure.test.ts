import { describe, expect, it } from 'vitest';
import {
  grantStatement,
  showGrantsStatement,
  spHasGrant,
  tier1Specs,
  tier2Specs,
} from './grants.pure.js';

const SP = 'a1b2c3d4-0000-1111-2222-333344445555';

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
