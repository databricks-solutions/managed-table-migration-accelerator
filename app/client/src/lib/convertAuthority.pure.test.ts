import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  applyAuthorityToWaves,
  authorityByFullName,
  grantScript,
  isToConvert,
  parseAuthorityRow,
  waveAuthorityText,
  type WaveAuthorityTextInput,
} from './convertAuthority.pure';

const HERE = dirname(fileURLToPath(import.meta.url));
interface Fixture {
  authority_rows: Record<string, unknown>[];
  members: { wave_id: string; full_name: string; state: string }[];
  waves: { wave_id: string; convert_run_as: string | null; expected: Record<string, unknown>; expected_text: string }[];
  text_cases: { wave: WaveAuthorityTextInput; expected: string }[];
  grant_script_cases: { full_names: string[]; expected: string }[];
}
const FX = JSON.parse(
  readFileSync(resolve(HERE, '../../../../backend/src/tests/fixtures/authority_rollup_cases.json'), 'utf8'),
) as Fixture;
const BY_FN = authorityByFullName(FX.authority_rows);

describe('convertAuthority mirror — pinned to authority_rollup_cases.json', () => {
  it('parses every stored row', () => {
    expect(BY_FN.size).toBe(FX.authority_rows.length);
  });

  it.each(FX.waves.map((w) => [w.wave_id, w] as const))('roll-up and sentence for %s', (_id, w) => {
    const got = applyAuthorityToWaves([w.wave_id], FX.members, BY_FN).get(w.wave_id);
    expect(got).toEqual(w.expected);
    expect(waveAuthorityText({ ...got, ...(w.convert_run_as ? { convert_run_as: w.convert_run_as } : {}) })).toBe(w.expected_text);
  });

  it.each(FX.text_cases.map((c) => [JSON.stringify(c.wave), c] as const))('wave sentence %s', (_k, c) => {
    expect(waveAuthorityText(c.wave)).toBe(c.expected);
  });

  it.each(FX.grant_script_cases.map((c, i) => [i, c] as const))('grant script case %i', (_i, c) => {
    expect(grantScript(c.full_names.map((n) => BY_FN.get(n)!))).toBe(c.expected);
  });
});

describe('parseAuthorityRow — the query layer', () => {
  const stored = FX.authority_rows.find((r) => Array.isArray(r.grant_requests) && (r.grant_requests as unknown[]).length > 0)!;

  it('reads the *_json twins the statement API returns', () => {
    const asQuery = {
      ...stored,
      missing: undefined,
      grant_requests: undefined,
      missing_json: JSON.stringify(stored.missing),
      grant_requests_json: JSON.stringify(stored.grant_requests),
      self_grantable: String(stored.self_grantable),
    };
    expect(parseAuthorityRow(asQuery)).toEqual(parseAuthorityRow(stored));
  });

  it('returns null for an unevaluated LEFT JOIN member', () => {
    expect(parseAuthorityRow({ full_name: 'c.s.t', status: null })).toBeNull();
    expect(parseAuthorityRow({ full_name: 'c.s.t', status: 'LEASED' })).toBeNull();
  });

  it('degrades malformed JSON to empty lists, never a throw', () => {
    const r = parseAuthorityRow({ full_name: 'c.s.t', status: 'UNKNOWN', missing_json: '[oops', grant_requests_json: '{}' });
    expect(r?.missing).toEqual([]);
    expect(r?.grant_requests).toEqual([]);
  });

  it('isToConvert mirrors authority.is_to_convert', () => {
    expect(['DRY_RUN_OK', 'CONVERTING', 'ASSESSED', '', 'rolled_back'].map(isToConvert)).toEqual([true, true, true, true, true]);
    expect(['CONVERTED', 'converted_external', 'VALIDATED', 'CLEANED', 'DROPPED'].map(isToConvert)).toEqual([false, false, false, false, false]);
  });
});
