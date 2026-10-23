import { describe, expect, it } from 'vitest';
import { actionStatusPresentation } from './actionStatus.pure';

describe('actionStatusPresentation', () => {
  it('renders DEFERRED distinctly with an explanation', () => {
    const p = actionStatusPresentation('DEFERRED');
    expect(p).toMatchObject({ label: 'Deferred', deferred: true });
    expect(p.title).toMatch(/does not hold the wave/i);
    expect(actionStatusPresentation('deferred').deferred).toBe(true);
  });

  it('passes every other status through unchanged', () => {
    for (const s of ['OPEN', 'IN_PROGRESS', 'DONE', 'PLANNED', 'FROZEN']) {
      expect(actionStatusPresentation(s)).toEqual({ label: s, deferred: false });
    }
    expect(actionStatusPresentation(null)).toEqual({ label: '—', deferred: false });
  });
});
