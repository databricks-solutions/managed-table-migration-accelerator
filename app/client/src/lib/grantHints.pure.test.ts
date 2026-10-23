import { describe, expect, it } from 'vitest';
import { grantHint } from './grantHints.pure';

describe('grantHint', () => {
  it('gives distinct control-plane vs data-plane copy for the same privilege', () => {
    expect(grantHint(1, 'SELECT')).toMatch(/control tables/i);
    expect(grantHint(2, 'SELECT')).toMatch(/dry-run|validate/i);
    expect(grantHint(1, 'SELECT')).not.toBe(grantHint(2, 'SELECT'));
  });

  it('explains the scoped single write and the PO/freeze grant', () => {
    expect(grantHint(1, 'MODIFY')).toMatch(/single write|migration_actions/i);
    expect(grantHint(2, 'MANAGE')).toMatch(/predictive optimization|freeze/i);
  });

  it('is non-empty for every privilege each tier actually emits', () => {
    for (const p of ['USE CATALOG', 'USE SCHEMA', 'SELECT', 'MODIFY']) expect(grantHint(1, p)).not.toBe('');
    for (const p of ['USE CATALOG', 'USE SCHEMA', 'SELECT', 'MANAGE']) expect(grantHint(2, p)).not.toBe('');
  });

  it('returns empty string for an unknown pair (caller renders no hint)', () => {
    expect(grantHint(2, 'MODIFY')).toBe('');
  });
});
