import { describe, expect, it } from 'vitest';
import {
  cleanupOption,
  rollbackOption,
  chooseTable,
  chooseWave,
  hasTable,
  hasWave,
  scopeLabel,
  EMPTY_ROLLBACK_SCOPE,
  type CleanupRow,
  type RollbackRow,
} from './maintenance.pure';

// Fixed "now" so converted-ago strings are deterministic. 2026-02-01T00:00:00Z.
const NOW = Date.parse('2026-02-01T00:00:00Z');

const cleanupRow = (over: Partial<CleanupRow> = {}): CleanupRow => ({
  full_name: 'main.sales.orders',
  catalog: 'main',
  schema: 'sales',
  table_name: 'orders',
  size_band: '~1TB',
  po_enabled: false,
  converted_at: '2026-01-01 00:00:00',
  days_since_convert: 31,
  ...over,
});

const rollbackRow = (over: Partial<RollbackRow> = {}): RollbackRow => ({
  full_name: 'main.sales.orders',
  catalog: 'main',
  schema: 'sales',
  table_name: 'orders',
  state: 'CONVERTED',
  converted_at: '2026-01-25 00:00:00',
  days_since_convert: 7,
  within_window: true,
  days_left: 7,
  ...over,
});

describe('cleanupOption', () => {
  it('marks a non-PO table ready and carries search keywords', () => {
    const o = cleanupOption(cleanupRow(), NOW);
    expect(o.full_name).toBe('main.sales.orders');
    expect(o.badge).toEqual({ label: 'ready', tone: 'ready' });
    expect(o.keywords).toEqual(['main', 'sales', 'orders']);
    expect(o.secondary).toContain('~1TB');
    expect(o.secondary).toContain('converted');
  });

  it('mutes a PO-managed table (VACUUM skipped)', () => {
    const o = cleanupOption(cleanupRow({ po_enabled: true }), NOW);
    expect(o.badge).toEqual({ label: 'VACUUM skipped', tone: 'muted' });
    expect(o.secondary).toContain('PO auto-cleaned');
  });

  it('renders an unknown convert date honestly, never as ready-with-date', () => {
    const o = cleanupOption(cleanupRow({ converted_at: null }), NOW);
    expect(o.secondary).toContain('convert date unknown');
  });
});

describe('rollbackOption', () => {
  it('shows days-left while within the window', () => {
    const o = rollbackOption(rollbackRow({ within_window: true, days_left: 5 }), NOW);
    expect(o.badge).toEqual({ label: '5d left', tone: 'expiring' });
    expect(o.secondary).toContain('CONVERTED');
  });

  it('mutes an expired row as window closed', () => {
    const o = rollbackOption(
      rollbackRow({ within_window: false, days_left: -3, state: 'VALIDATED' }),
      NOW,
    );
    expect(o.badge).toEqual({ label: 'window closed', tone: 'muted' });
  });
});

describe('rollback scope XOR', () => {
  it('starts empty', () => {
    expect(hasTable(EMPTY_ROLLBACK_SCOPE)).toBe(false);
    expect(hasWave(EMPTY_ROLLBACK_SCOPE)).toBe(false);
    expect(scopeLabel(EMPTY_ROLLBACK_SCOPE)).toBe('');
  });

  it('choosing a table clears any wave and labels by full_name', () => {
    const s = chooseTable('main.sales.orders');
    expect(hasTable(s)).toBe(true);
    expect(hasWave(s)).toBe(false);
    expect(s.waveId).toBe('');
    expect(s.planRunId).toBe('');
    expect(scopeLabel(s)).toBe('main.sales.orders');
  });

  it('choosing a wave clears any table and carries the correlated plan_run_id', () => {
    const s = chooseWave({ wave_id: 'wave-003', plan_run_id: 'plan-abc' });
    expect(hasWave(s)).toBe(true);
    expect(hasTable(s)).toBe(false);
    expect(s.fullName).toBe('');
    expect(s.planRunId).toBe('plan-abc');
    expect(scopeLabel(s)).toBe('wave wave-003');
  });

  it('clearing (null) returns an empty scope', () => {
    expect(chooseTable(null)).toEqual(EMPTY_ROLLBACK_SCOPE);
    expect(chooseWave(null)).toEqual(EMPTY_ROLLBACK_SCOPE);
  });
});
