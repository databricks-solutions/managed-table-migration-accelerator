import { describe, expect, it } from 'vitest';
import {
  convertBlockedByWriters,
  showStopWritersAction,
  willBePaused,
  writerCoverageState,
  writersPanelVisible,
  type WriterPauseRow,
} from './writersPanel.pure';

const cov = (writer: 'covered' | 'gap' | 'unevaluated') => ({
  compaction: 'covered' as const, stream: 'covered' as const, writer,
});

describe('writer gate — coverage decides, advisory verdict never does', () => {
  it('blocks convert while writer coverage is a gap', () => {
    expect(convertBlockedByWriters(cov('gap'))).toBe(true);
    expect(showStopWritersAction(cov('gap'))).toBe(true);
  });
  it('blocks convert while writer coverage is unevaluated (SR7: absence is not held)', () => {
    expect(convertBlockedByWriters(cov('unevaluated'))).toBe(true);
  });
  it('a SAFE_NOW advisory does NOT enable convert when coverage is not held', () => {
    // verdict is intentionally not an input — coverage alone decides.
    expect(convertBlockedByWriters(cov('gap'))).toBe(true);
  });
  it('enables convert only when writer coverage is held', () => {
    expect(convertBlockedByWriters(cov('covered'))).toBe(false);
    expect(showStopWritersAction(cov('covered'))).toBe(false);
    expect(writerCoverageState(cov('covered'))).toBe('covered');
  });
  it('treats missing coverage as unevaluated → blocked', () => {
    expect(writerCoverageState(undefined)).toBe('unevaluated');
    expect(convertBlockedByWriters(undefined)).toBe(true);
  });
});

describe('willBePaused — presentation-only pause split (mirrors format_pause_warning)', () => {
  const row = (r: Partial<WriterPauseRow>): WriterPauseRow => ({
    entity_id: r.entity_id ?? null,
    writer_class: r.writer_class ?? null,
    full_name: r.full_name ?? null,
    next_scheduled_write_at: r.next_scheduled_write_at,
  });

  it('puts SCHEDULED_JOB/EVENT_JOB with a job id (and next fire) in schedulable', () => {
    const rows: WriterPauseRow[] = [
      row({
        entity_id: '111',
        writer_class: 'SCHEDULED_JOB',
        full_name: 'c.s.orders',
        next_scheduled_write_at: '2026-09-19 02:00 UTC',
      }),
      row({ entity_id: '222', writer_class: 'EVENT_JOB', full_name: 'c.s.events' }),
    ];
    const { schedulable, adhoc } = willBePaused(rows);
    expect(schedulable).toHaveLength(2);
    expect(schedulable[0]).toMatchObject({
      entity_id: '111',
      writer_class: 'SCHEDULED_JOB',
      next_scheduled_write_at: '2026-09-19 02:00 UTC',
    });
    expect(schedulable[1].writer_class).toBe('EVENT_JOB');
    expect(adhoc).toHaveLength(0);
  });

  it('separates NOTEBOOK/USER/EXTERNAL into adhoc', () => {
    const rows: WriterPauseRow[] = [
      row({ entity_id: null, writer_class: 'NOTEBOOK', full_name: 'c.s.nb' }),
      row({ entity_id: 'u1', writer_class: 'USER', full_name: 'c.s.usr' }),
      row({ entity_id: null, writer_class: 'EXTERNAL', full_name: 'c.s.ext' }),
    ];
    const { schedulable, adhoc } = willBePaused(rows);
    expect(schedulable).toHaveLength(0);
    expect(adhoc.map((r) => r.writer_class)).toEqual(['NOTEBOOK', 'USER', 'EXTERNAL']);
  });

  it('excludes a schedulable-class row whose entity_id is null (not schedulable)', () => {
    const rows: WriterPauseRow[] = [
      row({ entity_id: null, writer_class: 'SCHEDULED_JOB', full_name: 'c.s.nojob' }),
    ];
    const { schedulable, adhoc } = willBePaused(rows);
    expect(schedulable).toHaveLength(0);
    expect(adhoc).toHaveLength(0);
  });

  it('yields an empty schedulable list for empty input (→ no banner)', () => {
    expect(willBePaused([]).schedulable).toHaveLength(0);
    expect(willBePaused([]).adhoc).toHaveLength(0);
  });

  it('dedupes identical (class·job·table) rows — writer_sources holds one row per lineage sample', () => {
    // The profiler stores one writer_sources row PER lineage sample, so a single job that ran once
    // can appear N times for the same table (e2e A1 saw the same SCHEDULED_JOB nine times). The
    // banner must count/list DISTINCT jobs, not samples ("1 production job", not "9").
    const dup = (): WriterPauseRow =>
      row({
        entity_id: '85987032771623',
        writer_class: 'SCHEDULED_JOB',
        full_name: 'c.s.writer_wave_000',
        next_scheduled_write_at: '2026-09-20T16:10:00.000Z',
      });
    const { schedulable, adhoc } = willBePaused([dup(), dup(), dup(), dup(), dup()]);
    expect(schedulable).toHaveLength(1);
    expect(schedulable[0]).toMatchObject({ entity_id: '85987032771623', writer_class: 'SCHEDULED_JOB' });
    expect(adhoc).toHaveLength(0);
  });

  it('dedupes adhoc rows too, and keeps genuinely distinct jobs on the same table', () => {
    const rows: WriterPauseRow[] = [
      row({ entity_id: '111', writer_class: 'SCHEDULED_JOB', full_name: 'c.s.t' }),
      row({ entity_id: '111', writer_class: 'SCHEDULED_JOB', full_name: 'c.s.t' }), // dup
      row({ entity_id: '222', writer_class: 'EVENT_JOB', full_name: 'c.s.t' }), // distinct job
      row({ entity_id: 'u1', writer_class: 'USER', full_name: 'c.s.t' }),
      row({ entity_id: 'u1', writer_class: 'USER', full_name: 'c.s.t' }), // dup adhoc
    ];
    const { schedulable, adhoc } = willBePaused(rows);
    expect(schedulable.map((r) => r.entity_id)).toEqual(['111', '222']);
    expect(adhoc).toHaveLength(1);
    expect(adhoc[0].entity_id).toBe('u1');
  });
});

describe('writersPanelVisible', () => {
  it('is true when an advisory verdict is passed (even null)', () => {
    expect(writersPanelVisible({ writerScheduleVerdict: null })).toBe(true);
    expect(writersPanelVisible({ writerScheduleVerdict: 'SAFE_NOW' })).toBe(true);
  });
  it('is true when writer coverage is covered or gap', () => {
    expect(writersPanelVisible({ writerCoverage: 'covered' })).toBe(true);
    expect(writersPanelVisible({ writerCoverage: 'gap' })).toBe(true);
  });
  it('is true when there are writer rows to name in the pause banner', () => {
    expect(
      writersPanelVisible({ writerJobs: [{ entity_id: '1', writer_class: 'SCHEDULED_JOB', full_name: 'c.s.t' }] }),
    ).toBe(true);
  });
  it('is false when nothing is known (no verdict, unevaluated coverage, no rows)', () => {
    expect(writersPanelVisible({ writerCoverage: 'unevaluated' })).toBe(false);
    expect(writersPanelVisible({})).toBe(false);
    expect(writersPanelVisible({ writerJobs: [] })).toBe(false);
  });
});
