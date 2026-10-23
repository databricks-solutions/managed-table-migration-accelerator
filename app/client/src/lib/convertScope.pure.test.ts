import { describe, it, expect } from 'vitest';
import { convertScopeState } from './convertScope.pure';

const scope = (ts = '', bk = '', wv = '') => convertScopeState({ ts, bk, wv });

describe('convertScopeState', () => {
  it('no selector at all is not a scope', () => {
    const s = scope();
    expect(s.hasScope).toBe(false);
    expect(s.bucketOnly).toBe(false);
    expect(s.scopeConflict).toBe(false);
    expect(s.disabledReason).toBe('Provide a scope first');
  });

  it('target_schema alone is a valid scope', () => {
    const s = scope('cat.sch');
    expect(s.hasScope).toBe(true);
    expect(s.disabledReason).toBeNull();
  });

  it('wave_ids alone is a valid scope', () => {
    const s = scope('', '', 'wave_01');
    expect(s.hasScope).toBe(true);
    expect(s.disabledReason).toBeNull();
  });

  it('a non-streaming bucket alone is bucket-only and rejected', () => {
    const s = scope('', 'normal');
    expect(s.bucketOnly).toBe(true);
    expect(s.hasScope).toBe(false);
    expect(s.disabledReason).toBe(
      'bucket only narrows a schema — pick a schema or wave (streaming is the exception)',
    );
  });

  it('bucket=streaming alone is a valid standalone scope', () => {
    const s = scope('', 'streaming');
    expect(s.bucketOnly).toBe(false);
    expect(s.hasScope).toBe(true);
    expect(s.disabledReason).toBeNull();
  });

  it('target_schema + bucket is valid (bucket narrows the schema)', () => {
    const s = scope('cat.sch', 'normal');
    expect(s.bucketOnly).toBe(false);
    expect(s.hasScope).toBe(true);
    expect(s.disabledReason).toBeNull();
  });

  it('wave_ids with a schema is a mutually-exclusive conflict', () => {
    const s = scope('cat.sch', '', 'wave_01');
    expect(s.scopeConflict).toBe(true);
    expect(s.disabledReason).toBe('wave_ids is exclusive with target_schema/bucket');
  });

  it('wave_ids with a bucket is a mutually-exclusive conflict', () => {
    const s = scope('', 'normal', 'wave_01');
    expect(s.scopeConflict).toBe(true);
    expect(s.disabledReason).toBe('wave_ids is exclusive with target_schema/bucket');
  });

  it('conflict outranks bucket-only in the reason', () => {
    // wave + non-streaming bucket: both flags could fire; conflict is the reported reason.
    const s = scope('', 'normal', 'wave_01');
    expect(s.scopeConflict).toBe(true);
    expect(s.bucketOnly).toBe(false);
  });

  describe('widget XOR disable flags', () => {
    it('nothing selected disables nothing', () => {
      const s = scope();
      expect(s.schemaDisabled).toBe(false);
      expect(s.bucketDisabled).toBe(false);
      expect(s.waveDisabled).toBe(false);
    });

    it('a chosen schema greys only the wave picker', () => {
      const s = scope('cat.sch');
      expect(s.waveDisabled).toBe(true);
      expect(s.schemaDisabled).toBe(false);
      expect(s.bucketDisabled).toBe(false);
    });

    it('a chosen bucket greys only the wave picker', () => {
      const s = scope('', 'normal');
      expect(s.waveDisabled).toBe(true);
      expect(s.schemaDisabled).toBe(false);
      expect(s.bucketDisabled).toBe(false);
    });

    it('bucket=streaming also greys the wave picker', () => {
      const s = scope('', 'streaming');
      expect(s.waveDisabled).toBe(true);
      expect(s.schemaDisabled).toBe(false);
      expect(s.bucketDisabled).toBe(false);
    });

    it('a chosen wave greys both the schema and bucket pickers', () => {
      const s = scope('', '', 'wave_01');
      expect(s.schemaDisabled).toBe(true);
      expect(s.bucketDisabled).toBe(true);
      expect(s.waveDisabled).toBe(false);
    });
  });
});
