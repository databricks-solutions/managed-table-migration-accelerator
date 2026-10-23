import { describe, expect, it } from 'vitest';
import { BUCKETS, bucketOptions, type BucketPlanRowLike } from './bucketOptions.pure';

function wave(bucket: string, total_tables = 1): BucketPlanRowLike {
  return { bucket, total_tables };
}

describe('bucketOptions', () => {
  it('returns the fixed vocabulary in order, regardless of plan', () => {
    expect(bucketOptions([]).map((o) => o.value)).toEqual(BUCKETS.map((b) => b.value));
  });

  it('disables nothing while the plan is empty/loading', () => {
    expect(bucketOptions([]).every((o) => o.disabled === false)).toBe(true);
  });

  it('counts waves and tables per bucket from the plan', () => {
    const rows = [wave('pilot', 15), wave('streaming', 1), wave('streaming', 3)];
    const opts = bucketOptions(rows);
    const pilot = opts.find((o) => o.value === 'pilot');
    const streaming = opts.find((o) => o.value === 'streaming');
    expect(pilot).toMatchObject({ waveCount: 1, tableCount: 15, disabled: false });
    expect(streaming).toMatchObject({ waveCount: 2, tableCount: 4, disabled: false });
  });

  it('disables buckets absent from a loaded plan', () => {
    const opts = bucketOptions([wave('pilot')]);
    expect(opts.find((o) => o.value === 'pilot')?.disabled).toBe(false);
    expect(opts.find((o) => o.value === 'normal')?.disabled).toBe(true);
    expect(opts.find((o) => o.value === 'streaming')?.disabled).toBe(true);
  });

  it('tolerates null total_tables as zero', () => {
    const opts = bucketOptions([{ bucket: 'pilot', total_tables: null }]);
    expect(opts.find((o) => o.value === 'pilot')).toMatchObject({ waveCount: 1, tableCount: 0 });
  });
});
