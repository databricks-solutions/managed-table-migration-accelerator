import { describe, expect, it } from 'vitest';
import { isAdhocWriter, writerClassBadge, verdictBadge } from './writerJobs.pure';

describe('isAdhocWriter', () => {
  it('is true only for NOTEBOOK/USER/EXTERNAL', () => {
    expect(['NOTEBOOK', 'USER', 'EXTERNAL'].every(isAdhocWriter)).toBe(true);
    expect(['SCHEDULED_JOB', 'EVENT_JOB', 'STREAMING', 'COMPACTION_PO'].some(isAdhocWriter)).toBe(false);
  });
});

describe('writerClassBadge', () => {
  it('tones scheduled=info, event=warn, ad-hoc=advisory', () => {
    expect(writerClassBadge('SCHEDULED_JOB').tone).toBe('info');
    expect(writerClassBadge('EVENT_JOB').tone).toBe('warn');
    expect(writerClassBadge('USER').tone).toBe('advisory');
  });
});

describe('verdictBadge (always advisory)', () => {
  it('maps each verdict to its tone and treats UNKNOWN/null as honest muted', () => {
    expect(verdictBadge('SAFE_NOW')).toMatchObject({ tone: 'success', advisory: true });
    expect(verdictBadge('WARN_WINDOW').tone).toBe('warn');
    expect(verdictBadge('MUST_STOP').tone).toBe('danger');
    expect(verdictBadge('UNKNOWN').tone).toBe('muted');
    expect(verdictBadge(null)).toMatchObject({ tone: 'muted', advisory: true });
  });
});
