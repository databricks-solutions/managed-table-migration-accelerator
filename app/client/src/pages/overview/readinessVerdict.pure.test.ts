import { describe, expect, it } from 'vitest';
import { STEP_COPY } from '../../lib/sectionCopy';
import { computeConvertImpact, type WaveCounts } from '../../lib/impact';
import { hasReadinessVerdict, readinessVerdict } from './readinessVerdict.pure';

// One wave_plan_status row; the convert scope (useConvertScope) feeds rows like these through
// computeConvertImpact, so the verdict and the Tables fact read the same numbers.
const wave = (over: Partial<WaveCounts> = {}): WaveCounts => ({
  bucket: 'normal',
  total_tables: 15,
  dry_run_ok: 15,
  converting: 0,
  converted: 0,
  converted_external: 0,
  validated: 0,
  cleaned: 0,
  blocked: 0,
  rolled_back: 0,
  ...over,
});
const loaded = (rows: WaveCounts[], streaming = false) => ({
  loading: false,
  error: null,
  impact: computeConvertImpact(rows, streaming),
});

describe('hasReadinessVerdict', () => {
  it('is on for the convert, resume, freeze and authorize steps only', () => {
    const on = Object.keys(STEP_COPY).filter(hasReadinessVerdict);
    expect(on.sort()).toEqual(['convert', 'freeze', 'lease', 'resume']);
    expect(hasReadinessVerdict(null)).toBe(false);
    expect(hasReadinessVerdict('RESUME')).toBe(true);
  });
});

describe('readinessVerdict', () => {
  it('says Ready when every table still to convert passed the readiness check', () => {
    expect(readinessVerdict('convert', loaded([wave()]))).toEqual({
      tone: 'success',
      lead: 'Ready.',
      text: '15 of 15 tables passed the readiness check.',
    });
    expect(readinessVerdict('freeze', loaded([wave({ total_tables: 1, dry_run_ok: 1 })]))).toEqual({
      tone: 'success',
      lead: 'Ready.',
      text: '1 of 1 table passed the readiness check.',
    });
  });

  it('counts a chunked multi-wave convert exactly as the Tables fact does (willAct)', () => {
    const rows = [wave(), wave({ total_tables: 19, dry_run_ok: 17, converting: 2 })];
    const input = loaded(rows);
    const v = readinessVerdict('convert', input)!;
    expect(input.impact.willAct).toBe(34);
    expect(v.text).toBe('34 of 34 tables passed the readiness check.');
  });

  it('leaves out tables already migrated, as the convert does', () => {
    const v = readinessVerdict('resume', loaded([wave({ total_tables: 15, dry_run_ok: 5, converted: 10 })]))!;
    expect(v).toMatchObject({ tone: 'success', text: '5 of 5 tables passed the readiness check.' });
  });

  it('never reads Ready when a table is blocked, and names the blocked count', () => {
    const one = readinessVerdict('convert', loaded([wave({ dry_run_ok: 14, blocked: 1 })]))!;
    expect(one).toEqual({ tone: 'warning', lead: null, text: '14 of 15 tables passed the readiness check. 1 is blocked.' });
    const many = readinessVerdict('lease', loaded([wave({ dry_run_ok: 12, blocked: 3 })]))!;
    expect(many.text).toBe('12 of 15 tables passed the readiness check. 3 are blocked.');
    expect(many.lead).toBeNull();
  });

  it('never reads Ready while some tables have not been checked yet', () => {
    const v = readinessVerdict('convert', loaded([wave({ dry_run_ok: 13 })]))!;
    expect(v).toEqual({ tone: 'neutral', lead: null, text: '13 of 15 tables passed the readiness check. 2 are not checked yet.' });
    const one = readinessVerdict('convert', loaded([wave({ dry_run_ok: 14 })]))!;
    expect(one.text).toBe('14 of 15 tables passed the readiness check. 1 is not checked yet.');
  });

  it('names the blocked count first when tables are both blocked and unchecked', () => {
    const v = readinessVerdict('convert', loaded([wave({ dry_run_ok: 10, blocked: 2 })]))!;
    expect(v.tone).toBe('warning');
    expect(v.text).toBe('10 of 15 tables passed the readiness check. 2 are blocked.');
  });

  it('reads counts sent as strings, as AppKit sends every scalar', () => {
    const impact = { willAct: '14', skipped: { blocked: '1', notReady: '0' } };
    expect(readinessVerdict('convert', { loading: false, error: null, impact })?.text).toBe(
      '14 of 15 tables passed the readiness check. 1 is blocked.',
    );
  });

  it('says nothing while the plan is loading or after it failed to load', () => {
    const impact = computeConvertImpact([wave()], false);
    expect(readinessVerdict('convert', { loading: true, error: null, impact })).toBeNull();
    expect(readinessVerdict('convert', { loading: false, error: 'boom', impact })).toBeNull();
  });

  it('says nothing when no table passed, or the scope has no rows', () => {
    expect(readinessVerdict('convert', loaded([wave({ dry_run_ok: 0, blocked: 15 })]))).toBeNull();
    expect(readinessVerdict('convert', loaded([]))).toBeNull();
  });

  it('says nothing for a step kind with no verdict', () => {
    for (const kind of ['prep', 'validate', 'unfreeze', 'done', 'blocked', null]) {
      expect(readinessVerdict(kind, loaded([wave()])), String(kind)).toBeNull();
    }
  });

  it('skips a streaming wave exactly as the convert scope does', () => {
    expect(readinessVerdict('convert', loaded([wave({ bucket: 'streaming' })], false))).toBeNull();
    expect(readinessVerdict('convert', loaded([wave({ bucket: 'streaming' })], true))?.tone).toBe('success');
  });
});
