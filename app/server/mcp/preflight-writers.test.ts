import { describe, expect, it } from 'vitest';
import { buildWriterJobs } from './preflight.js';

const scope = [
  { full_name: 'c.s.t1', writer_schedule_verdict: 'WARN_WINDOW', writer_schedule_reason: 'fires 22:00', next_scheduled_write_at: '2026-09-19T22:00:00Z' },
  { full_name: 'c.s.t2', writer_schedule_verdict: 'SAFE_NOW', writer_schedule_reason: null, next_scheduled_write_at: null },
];
const writers = [
  { full_name: 'c.s.t1', entity_id: '111', writer_class: 'SCHEDULED_JOB' },
  { full_name: 'c.s.t2', entity_id: '222', writer_class: 'EVENT_JOB' },
  { full_name: 'c.s.t1', entity_id: null, writer_class: 'NOTEBOOK' }, // ad-hoc — excluded from jobs
  { full_name: 'c.s.tX', entity_id: '999', writer_class: 'SCHEDULED_JOB' }, // out of scope — excluded
];
const inScope = new Set(['c.s.t1', 'c.s.t2']);

describe('buildWriterJobs', () => {
  it('threads the per-table next_scheduled_write_at onto each schedulable job', () => {
    const jobs = buildWriterJobs(scope, writers, inScope, 'c.s');
    expect(jobs.map((j) => j.full_name)).toEqual(['c.s.t1', 'c.s.t2']);
    expect(jobs[0]).toMatchObject({
      job_id: '111',
      writer_class: 'SCHEDULED_JOB',
      writer_schedule_verdict: 'WARN_WINDOW',
      reason: 'fires 22:00',
      next_scheduled_write_at: '2026-09-19T22:00:00Z',
    });
    expect(jobs[0].remediation).toContain('mtm_freeze(');
  });
  it('leaves next_scheduled_write_at null when the manifest has none', () => {
    const jobs = buildWriterJobs(scope, writers, inScope, 'c.s');
    expect(jobs[1]).toMatchObject({ job_id: '222', next_scheduled_write_at: null });
  });
  it('excludes ad-hoc classes and out-of-scope rows', () => {
    const jobs = buildWriterJobs(scope, writers, inScope, 'c.s');
    expect(jobs).toHaveLength(2);
  });
});
