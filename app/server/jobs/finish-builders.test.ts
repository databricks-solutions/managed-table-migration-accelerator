import { describe, expect, it } from 'vitest';
import { migrationJobs } from './job-params.js';
import {
  compatParams,
  deferParams,
  finishScope,
  optimizeParams,
  reopenParams,
  type OptimizeMode,
} from '../../client/src/lib/finishScope.pure.js';

/**
 * Drift guard between the client finish-step param builders and the strict server schemas they
 * are sent to: a builder edit the schema does not accept would otherwise surface only at click
 * time, as a 400.
 */

function schema(alias: string) {
  const s = migrationJobs.jobs?.[alias]?.params;
  if (!s) throw new Error(`no schema registered for alias: ${alias}`);
  return s;
}

const SCOPES = {
  'wave-pinned': finishScope({ planRunId: 'p1', waveId: 'w1', selector: 'cat.sch' }),
  target_schema: finishScope({ selector: 'cat.sch' }),
  batch: finishScope({ planRunId: 'p1', batchId: 'batch_01' }),
};

describe('finish-step builders parse under their alias schemas', () => {
  it('every fixture scope is ok and carries the expected selector', () => {
    expect(SCOPES['wave-pinned'].params).toEqual({ plan_run_id: 'p1', wave_id: 'w1' });
    expect(SCOPES.target_schema.params).toEqual({ target_schema: 'cat.sch' });
    expect(SCOPES.batch.params).toEqual({ plan_run_id: 'p1', batch_id: 'batch_01' });
  });

  for (const [name, scope] of Object.entries(SCOPES)) {
    for (const mode of ['enable', 'review', 'decline'] as OptimizeMode[]) {
      it(`enable_optimization accepts optimizeParams(${name}, ${mode})`, () => {
        const r = schema('enable_optimization').safeParse(optimizeParams(scope, mode));
        expect(r.success, JSON.stringify(r.error?.issues)).toBe(true);
      });
    }

    it(`enable_compat accepts compatParams(${name})`, () => {
      const r = schema('enable_compat').safeParse(
        compatParams(scope, '{"cat.sch.t":"abfss://c@a.dfs.core.windows.net/t"}'),
      );
      expect(r.success, JSON.stringify(r.error?.issues)).toBe(true);
    });

    for (const alias of ['reshare', 'enable_compat']) {
      it(`${alias} accepts deferParams(${name})`, () => {
        const r = schema(alias).safeParse(deferParams(scope, 'RESHARE_D2D|cat.sch.t|', '  recipient not ready '));
        expect(r.success, JSON.stringify(r.error?.issues)).toBe(true);
      });

      it(`${alias} accepts reopenParams(${name})`, () => {
        const r = schema(alias).safeParse(reopenParams(scope, 'ENABLE_COMPAT|cat.sch.t|'));
        expect(r.success, JSON.stringify(r.error?.issues)).toBe(true);
      });

      it(`${alias} rejects deferParams(${name}) with a blank reason`, () => {
        const r = schema(alias).safeParse(deferParams(scope, 'RESHARE_D2D|cat.sch.t|', '   '));
        expect(r.success).toBe(false);
      });
    }
  }

  it('unfreeze_verify rejects a batch scope (a batch unfreeze is bulk_unfreeze, never 05b)', () => {
    const r = schema('unfreeze_verify').safeParse({ ...SCOPES.batch.params, confirm_execute: true });
    expect(r.success).toBe(false);
  });
});
