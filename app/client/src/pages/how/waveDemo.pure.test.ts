import { describe, expect, it } from 'vitest';
import {
  BUCKET_ORDER,
  CAP_BOUNDS,
  DEFAULT_CAPS,
  chunkIntoWaves,
  clampCaps,
  type DemoTable,
  type WaveCaps,
} from './waveDemo.pure';

const CAPS: WaveCaps = { maxTables: 3, maxTb: 10, pilotMax: 2 };

function t(id: string, bucket: DemoTable['bucket'], tb = 1): DemoTable {
  return { id, tb, bucket };
}

describe('chunkIntoWaves', () => {
  it('orders waves by bucket execution order', () => {
    const waves = chunkIntoWaves(
      [t('a', 'final_large'), t('b', 'normal'), t('c', 'pilot'), t('d', 'streaming')],
      CAPS,
    );
    expect(waves.map((w) => w.bucket)).toEqual(['pilot', 'normal', 'streaming', 'final_large']);
    // wave ids are assigned in that same order.
    expect(waves.map((w) => w.waveId)).toEqual(['wave_00', 'wave_01', 'wave_02', 'wave_03']);
  });

  it('chunks a bucket by the table-count cap', () => {
    const waves = chunkIntoWaves(
      [t('a', 'normal'), t('b', 'normal'), t('c', 'normal'), t('d', 'normal')],
      CAPS,
    );
    const normal = waves.filter((w) => w.bucket === 'normal');
    expect(normal).toHaveLength(2);
    expect(normal[0].tables.map((x) => x.id)).toEqual(['a', 'b', 'c']);
    expect(normal[1].tables.map((x) => x.id)).toEqual(['d']);
  });

  it('chunks a bucket by the TB cap before the count cap', () => {
    const waves = chunkIntoWaves([t('a', 'normal', 6), t('b', 'normal', 6)], CAPS);
    const normal = waves.filter((w) => w.bucket === 'normal');
    expect(normal).toHaveLength(2);
    expect(normal[0].totalTb).toBe(6);
    expect(normal[1].totalTb).toBe(6);
  });

  it('isolates streaming into its own wave(s)', () => {
    const waves = chunkIntoWaves([t('a', 'normal'), t('s', 'streaming')], CAPS);
    const streaming = waves.filter((w) => w.bucket === 'streaming');
    expect(streaming).toHaveLength(1);
    expect(streaming[0].tables.every((x) => x.bucket === 'streaming')).toBe(true);
  });

  it('caps the pilot and demotes overflow to normal (never dropped)', () => {
    const waves = chunkIntoWaves(
      [t('p1', 'pilot'), t('p2', 'pilot'), t('p3', 'pilot')],
      CAPS,
    );
    const pilot = waves.filter((w) => w.bucket === 'pilot');
    const normal = waves.filter((w) => w.bucket === 'normal');
    const pilotCount = pilot.reduce((n, w) => n + w.tables.length, 0);
    const normalCount = normal.reduce((n, w) => n + w.tables.length, 0);
    expect(pilotCount).toBe(2);
    expect(normalCount).toBe(1); // the demoted overflow
  });

  it('flags an oversized singleton wave when one table exceeds the TB cap', () => {
    const waves = chunkIntoWaves([t('huge', 'final_large', 40)], CAPS);
    expect(waves).toHaveLength(1);
    expect(waves[0].exceedsByteCap).toBe(true);
  });

  it('is a no-op on empty input', () => {
    expect(chunkIntoWaves([], CAPS)).toEqual([]);
  });

  it('exposes the six buckets in execution order', () => {
    expect(BUCKET_ORDER).toEqual(['pilot', 'normal', 'streaming', 'compat', 'share', 'final_large']);
  });

  it('produces more, smaller waves as the caps tighten', () => {
    const normals = Array.from({ length: 6 }, (_, i) => t(`n${i}`, 'normal', 1));
    const loose = chunkIntoWaves(normals, { maxTables: 6, maxTb: 20, pilotMax: 3 });
    const tight = chunkIntoWaves(normals, { maxTables: 2, maxTb: 20, pilotMax: 3 });
    expect(tight.length).toBeGreaterThan(loose.length);
    const maxWaveSize = (ws: ReturnType<typeof chunkIntoWaves>) =>
      Math.max(...ws.map((w) => w.tables.length));
    expect(maxWaveSize(tight)).toBeLessThan(maxWaveSize(loose));
  });

  it('demotes more pilot overflow to normal as the pilot cap drops', () => {
    const pilots = [t('p1', 'pilot'), t('p2', 'pilot'), t('p3', 'pilot'), t('p4', 'pilot')];
    const countIn = (ws: ReturnType<typeof chunkIntoWaves>, b: DemoTable['bucket']) =>
      ws.filter((w) => w.bucket === b).reduce((n, w) => n + w.tables.length, 0);
    const cap3 = chunkIntoWaves(pilots, { maxTables: 5, maxTb: 20, pilotMax: 3 });
    const cap1 = chunkIntoWaves(pilots, { maxTables: 5, maxTb: 20, pilotMax: 1 });
    expect(countIn(cap3, 'pilot')).toBe(3);
    expect(countIn(cap1, 'pilot')).toBe(1);
    expect(countIn(cap1, 'normal')).toBe(3); // the demoted overflow, never dropped
  });

  it('disables the byte cap when maxTb <= 0', () => {
    const waves = chunkIntoWaves([t('a', 'normal', 6), t('b', 'normal', 6)], {
      maxTables: 3,
      maxTb: 0,
      pilotMax: 2,
    });
    const normal = waves.filter((w) => w.bucket === 'normal');
    expect(normal).toHaveLength(1); // no TB split
    expect(normal[0].exceedsByteCap).toBe(false);
  });
});

describe('clampCaps', () => {
  it('keeps the default caps inside the bounds', () => {
    expect(clampCaps(DEFAULT_CAPS)).toEqual(DEFAULT_CAPS);
  });

  it('clamps each axis to its bounds and rounds to integers', () => {
    const clamped = clampCaps({ maxTables: 99, maxTb: -4, pilotMax: 2.6 });
    expect(clamped.maxTables).toBe(CAP_BOUNDS.maxTables.max);
    expect(clamped.maxTb).toBe(CAP_BOUNDS.maxTb.min);
    expect(clamped.pilotMax).toBe(3);
  });
});
