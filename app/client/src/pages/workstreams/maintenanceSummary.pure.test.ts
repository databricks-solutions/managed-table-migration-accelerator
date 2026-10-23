import { describe, expect, it } from 'vitest';
import { summarizeMaintenance } from './maintenanceSummary.pure';

describe('summarizeMaintenance', () => {
  it('counts each maintenance kind and totals only those three', () => {
    const rows = [
      { action_kind: 'RETIRE_MAINTENANCE_JOB' },
      { action_kind: 'RETIRE_MAINTENANCE_JOB' },
      { action_kind: 'ENABLE_PREDICTIVE_OPTIMIZATION' },
      { action_kind: 'ENABLE_AUTO_LIQUID' },
      { action_kind: 'STOP_STREAM' }, // ignored — not a maintenance kind
    ];
    expect(summarizeMaintenance(rows)).toEqual({
      retire: 2,
      enablePo: 1,
      autoLiquid: 1,
      total: 4,
    });
  });

  it('returns all zeros for an empty list', () => {
    expect(summarizeMaintenance([])).toEqual({ retire: 0, enablePo: 0, autoLiquid: 0, total: 0 });
  });
});
