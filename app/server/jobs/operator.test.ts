import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { OPERATOR_ALIASES, applyOperator, resolveDisplayName, resolveOperator } from './operator.js';

const HERE = dirname(fileURLToPath(import.meta.url));

describe('resolveOperator', () => {
  it('prefers x-forwarded-email over the display name Apps sends as preferred-username', () => {
    expect(
      resolveOperator({
        'x-forwarded-preferred-username': 'Alice Smith',
        'x-forwarded-email': 'alice@corp.com',
      }),
    ).toBe('alice@corp.com');
  });

  it('falls back to x-forwarded-preferred-username when the email header is absent', () => {
    expect(resolveOperator({ 'x-forwarded-preferred-username': 'bob@corp.com' })).toBe('bob@corp.com');
  });

  it('falls back when the email header is blank', () => {
    expect(
      resolveOperator({ 'x-forwarded-email': '  ', 'x-forwarded-preferred-username': 'Bob' }),
    ).toBe('Bob');
  });

  it('trims whitespace and treats a blank header as absent', () => {
    expect(
      resolveOperator({ 'x-forwarded-preferred-username': '  carol@corp.com  ' }),
    ).toBe('carol@corp.com');
    expect(resolveOperator({ 'x-forwarded-preferred-username': '   ' })).toBeNull();
  });

  it('reads the first value when a header arrives as an array', () => {
    expect(
      resolveOperator({ 'x-forwarded-preferred-username': ['dave@corp.com', 'x'] }),
    ).toBe('dave@corp.com');
  });

  it('returns null when neither header is present', () => {
    expect(resolveOperator({})).toBeNull();
  });
});

describe('resolveDisplayName', () => {
  it('prefers the display name for UI surfaces', () => {
    expect(
      resolveDisplayName({
        'x-forwarded-preferred-username': 'Alice Smith',
        'x-forwarded-email': 'alice@corp.com',
      }),
    ).toBe('Alice Smith');
  });

  it('falls back to the email, and null when neither header is present', () => {
    expect(resolveDisplayName({ 'x-forwarded-email': 'alice@corp.com' })).toBe('alice@corp.com');
    expect(resolveDisplayName({})).toBeNull();
  });
});

describe('applyOperator', () => {
  it('injects operator for a mutating alias and returns a NEW object', () => {
    const base = { target_schema: 'cat.sch' };
    const out = applyOperator('convert', base, 'alice@corp.com');
    expect(out).toEqual({ target_schema: 'cat.sch', operator: 'alice@corp.com' });
    expect(out).not.toBe(base);
    expect(base).toEqual({ target_schema: 'cat.sch' }); // input untouched
  });

  it('does NOT inject for a non-mutating alias (wave_planner)', () => {
    expect(applyOperator('wave_planner', { wave_max_tables: '300' }, 'alice@corp.com')).toEqual({
      wave_max_tables: '300',
    });
  });

  it('injects for profiler, whose gated redirect opt-in is audited', () => {
    expect(applyOperator('profiler', { path_redirect_action: 'accept' }, 'alice@corp.com')).toEqual({
      path_redirect_action: 'accept',
      operator: 'alice@corp.com',
    });
  });

  it('does NOT inject when the operator is null (header absent)', () => {
    expect(applyOperator('convert', { target_schema: 'cat.sch' }, null)).toEqual({
      target_schema: 'cat.sch',
    });
  });

  it('overwrites any pre-existing operator key with the server value (defensive)', () => {
    expect(applyOperator('rollback', { operator: 'spoofed' }, 'real@corp.com')).toEqual({
      operator: 'real@corp.com',
    });
  });

  it('covers exactly the 16 attributed aliases', () => {
    expect([...OPERATOR_ALIASES].sort()).toEqual(
      [
        'bulk_convert',
        'bulk_freeze',
        'bulk_unfreeze',
        'bulk_validate',
        'cleanup',
        'conductor',
        'convert',
        'enable_compat',
        'enable_optimization',
        'freeze',
        'lease_ownership',
        'profiler',
        'reshare',
        'rollback',
        'unfreeze_verify',
        'validate',
      ].sort(),
    );
  });

  it('matches exactly the app-bound jobs that declare an `operator` parameter', () => {
    // Each app.yml binding `- name: <alias>` → `job: id: ${resources.jobs.<key>.id}`; the job's
    // backend/resources/<key>.yml declares `- name: operator` iff the app must inject it.
    const appYml = readFileSync(resolve(HERE, '../../resources/app.yml'), 'utf8');
    const bindings = [...appYml.matchAll(/- name: (\w+)\s+job:\s+id: \$\{resources\.jobs\.(\w+)\.id\}/g)];
    expect(bindings.length).toBeGreaterThan(10);
    const declaring = bindings
      .filter(([, , key]) =>
        /^\s*- name: operator\s*$/m.test(readFileSync(resolve(HERE, `../../../backend/resources/${key}.yml`), 'utf8')),
      )
      .map(([, alias]) => alias);
    expect([...OPERATOR_ALIASES].sort()).toEqual(declaring.sort());
  });
});
