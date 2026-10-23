import { describe, expect, it } from 'vitest';
import { adhocAdvisoryCopy, adhocState, writerMixDescriptor } from './writerMix.pure';

describe('adhocState — three-valued, never a false green', () => {
  it('maps null/undefined to unknown, not no', () => {
    expect(adhocState(null)).toBe('unknown');
    expect(adhocState(undefined)).toBe('unknown');
  });
  it('maps booleans directly', () => {
    expect(adhocState(true)).toBe('yes');
    expect(adhocState(false)).toBe('no');
  });
});

describe('writerMixDescriptor', () => {
  it('summarizes the class mix', () => {
    expect(writerMixDescriptor('SCHEDULED_JOB,SCHEDULED_JOB,EVENT_JOB,USER')).toBe('2 scheduled · 1 event · 1 ad-hoc');
  });
  it('is honest about unknown', () => {
    expect(writerMixDescriptor(null)).toBe('schedule/writers unknown');
    expect(writerMixDescriptor('')).toBe('schedule/writers unknown');
  });
});

describe('adhocAdvisoryCopy — unknown is not "no"', () => {
  it('gives distinct copy for each state and never claims auto-handling', () => {
    expect(adhocAdvisoryCopy('unknown')).toMatch(/attribution unavailable/i);
    expect(adhocAdvisoryCopy('yes')).toMatch(/can.?t be paused|off-peak/i);
    expect(adhocAdvisoryCopy('no')).not.toMatch(/auto/i);
  });
});
