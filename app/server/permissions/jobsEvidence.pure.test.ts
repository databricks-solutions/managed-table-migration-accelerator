import { describe, expect, it } from 'vitest';
import {
  EVIDENCE_DEPLOYER,
  EVIDENCE_LIVE,
  EVIDENCE_RECORDED,
  deployerHeld,
  jobsAuditSql,
  jobsLineHeld,
  recordedJobsHeld,
} from './jobsEvidence.pure.js';

const SP = 'sp-jobs';
const detail = (o: Record<string, unknown> = {}) =>
  JSON.stringify({
    front_door: 'cli', grantee: SP, privilege: 'CAN_MANAGE_RUN', securable_type: 'JOBS', job_ids: ['11', '12'],
    prior_held: false, ...o,
  });
const row = (action: string, at: string, o: Record<string, unknown> = {}, phase = 'SUCCESS') =>
  ({ action, phase, created_at: at, detail: detail(o) });

describe('recordedJobsHeld', () => {
  it('accepts a notebook or CLI grant, or a skip that read the lists, on the current jobs', () => {
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01')], ['11', '12'], [SP]))
      .toEqual({ door: 'cli', at: '2026-10-01' });
    expect(recordedJobsHeld([row('INSTALL_GRANT_SKIPPED', '2026-10-02', { front_door: 'notebook', prior_held: true })],
      ['12'], [SP])).toEqual({ door: 'notebook', at: '2026-10-02' });
  });

  it('ignores app-door rows: the app cannot read the lists', () => {
    expect(recordedJobsHeld([row('INSTALL_GRANT_SKIPPED', '2026-10-02', { front_door: 'app', prior_held: true })],
      ['11', '12'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-02', { front_door: 'app' })], ['11'], [SP])).toBeNull();
  });

  it('drops the record after a later revoke on any of the jobs', () => {
    const rows = [row('INSTALL_GRANT', '2026-10-01'), row('INSTALL_REVOKE', '2026-10-03', { job_ids: ['12'] })];
    expect(recordedJobsHeld(rows, ['11', '12'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01'), row('INSTALL_REVOKE', '2026-10-03', { job_ids: undefined })],
      ['11', '12'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_REVOKE', '2026-09-01'), row('INSTALL_GRANT', '2026-10-01')], ['11', '12'], [SP]))
      .toEqual({ door: 'cli', at: '2026-10-01' });
  });

  it('needs every current job id, the same grantee and a success', () => {
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01')], ['11', '13'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01', { job_ids: undefined })], ['11'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01')], ['11'], ['someone-else'])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01', {}, 'FAILED')], ['11'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT_SKIPPED', '2026-10-01', { prior_held: false })], ['11'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01', { securable_type: 'SCHEMA' })], ['11'], [SP])).toBeNull();
    expect(recordedJobsHeld([row('INSTALL_GRANT', '2026-10-01')], [], [SP])).toBeNull();
    expect(recordedJobsHeld([{ action: 'INSTALL_GRANT', phase: 'SUCCESS', created_at: 'x', detail: 'not json' }], ['11'], [SP]))
      .toBeNull();
  });
});

describe('deployerHeld', () => {
  it('holds only when the jobs identity created every job', () => {
    expect(deployerHeld({ 11: SP, 12: SP }, ['11', '12'], [SP])).toBe(true);
    expect(deployerHeld({ 11: SP, 12: 'ann@x.com' }, ['11', '12'], [SP])).toBe(false);
    expect(deployerHeld({ 11: SP }, ['11', '12'], [SP])).toBe(false);
    expect(deployerHeld({}, [], [SP])).toBe(false);
  });

  it('a service principal whose jobs someone else deployed stays not checked', () => {
    const out = jobsLineHeld(null, null, deployerHeld({ 11: 'ann@x.com' }, ['11'], [SP]));
    expect(out).toEqual({ held: null, evidence: null });
  });
});

describe('jobsLineHeld', () => {
  it('prefers the live read, then the record, then the deployer', () => {
    const live = new Set(['CAN_VIEW']);
    expect(jobsLineHeld(live, { door: 'cli', at: 'x' }, true)).toEqual({ held: live, evidence: { kind: EVIDENCE_LIVE } });
    const rec = jobsLineHeld(null, { door: 'cli', at: '2026-10-01' }, true);
    expect([...(rec.held ?? [])]).toEqual(['CAN_MANAGE_RUN']);
    expect(rec.evidence).toEqual({ kind: EVIDENCE_RECORDED, door: 'cli', at: '2026-10-01' });
    expect(jobsLineHeld(null, null, true).evidence).toEqual({ kind: EVIDENCE_DEPLOYER });
  });
});

describe('jobsAuditSql', () => {
  it('reads only successful jobs-line install rows, newest first', () => {
    const sql = jobsAuditSql('main', 'migration');
    expect(sql).toContain('FROM main.migration.audit_log');
    expect(sql).toContain("phase = 'SUCCESS'");
    expect(sql).toContain("'$.securable_type') = 'JOBS'");
    expect(sql).toContain('ORDER BY created_at DESC');
  });
});
