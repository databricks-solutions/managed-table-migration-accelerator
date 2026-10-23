import { describe, expect, it } from 'vitest';
import { migrationJobs } from './job-params.js';

/**
 * Contract test for the /api/jobs/:alias/trigger request shape. The trigger bug was
 * structural: the client spread params as SIBLINGS of request_id, so the route's
 * `body.params` was undefined and every job validated the empty object `{}`. Jobs with
 * all-optional params silently "passed" `{}`; convert — the only job with a required
 * scope — surfaced it as a 400. These tests pin the two schemas the bug touched and the
 * route's boolean→string param mapping so a regression fails here, not in production.
 */

/** Mirrors the param mapping in routes/job-trigger.ts (validated primitives → strings). */
function toJobParameters(data: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(data)) {
    if (typeof v === 'string') out[k] = v;
    else if (typeof v === 'number' || typeof v === 'boolean') out[k] = String(v);
  }
  return out;
}

function schema(alias: string) {
  const s = migrationJobs.jobs?.[alias]?.params;
  if (!s) throw new Error(`no schema registered for alias: ${alias}`);
  return s;
}

describe('job trigger param contract', () => {
  it('regression: convert rejects the empty body the old sibling-spread produced', () => {
    // safeParse({}) is exactly what `body.params ?? {}` saw when params were dropped.
    const r = schema('convert').safeParse({});
    expect(r.success).toBe(false);
    if (!r.success) {
      expect(r.error.issues.map((i) => i.message)).toContain(
        'Provide a scope: at least one of wave_ids, target_schema, or bucket.',
      );
    }
  });

  it('fix: convert accepts a wave-scoped body (params nested under `params`)', () => {
    const r = schema('convert').safeParse({ wave_ids: 'wave_01', confirm_compaction_paused: true });
    expect(r.success).toBe(true);
    if (r.success) {
      expect(toJobParameters(r.data)).toEqual({
        wave_ids: 'wave_01',
        confirm_compaction_paused: 'true',
      });
    }
  });

  it('convert enforces wave_ids/target_schema exclusivity', () => {
    const r = schema('convert').safeParse({ wave_ids: 'wave_01', target_schema: 'cat.sch' });
    expect(r.success).toBe(false);
  });

  it('convert rejects a non-streaming bucket alone (bucket is a filter)', () => {
    const r = schema('convert').safeParse({ bucket: 'normal' });
    expect(r.success).toBe(false);
    if (!r.success) {
      expect(r.error.issues.map((i) => i.message)).toContain(
        "bucket is a filter (except 'streaming') — add target_schema, or use wave_ids.",
      );
    }
  });

  it('convert accepts bucket=streaming alone (the one standalone bucket)', () => {
    const r = schema('convert').safeParse({ bucket: 'streaming' });
    expect(r.success).toBe(true);
    if (r.success) expect(toJobParameters(r.data)).toEqual({ bucket: 'streaming' });
  });

  it('convert accepts target_schema + bucket together', () => {
    const r = schema('convert').safeParse({ target_schema: 'cat.sch', bucket: 'normal' });
    expect(r.success).toBe(true);
    if (r.success) expect(toJobParameters(r.data)).toEqual({ target_schema: 'cat.sch', bucket: 'normal' });
  });

  it('discovery chunk: profiler defaults to a valid empty body (profiler-only)', () => {
    // A bare trigger (Overview/Settings/Blocked) sends {} -> defaults run_profiler=true.
    const r = schema('profiler').safeParse({});
    expect(r.success).toBe(true);
  });

  it('discovery chunk: profiler accepts source-scan-only (run_profiler=false + run_source_scan=true)', () => {
    const r = schema('profiler').safeParse({ run_profiler: false, run_source_scan: true, scan_mode: 'broad' });
    expect(r.success).toBe(true);
    if (r.success) {
      expect(toJobParameters(r.data)).toEqual({
        run_profiler: 'false',
        run_source_scan: 'true',
        scan_mode: 'broad',
      });
    }
  });

  it('discovery chunk: profiler rejects the no-op combo (run_profiler=false without run_source_scan=true)', () => {
    const r = schema('profiler').safeParse({ run_profiler: false });
    expect(r.success).toBe(false);
    if (!r.success) {
      expect(r.error.issues.map((i) => i.message)).toContain(
        'With run_profiler=false you must set run_source_scan=true or a path_redirect_action (nothing would run otherwise).',
      );
    }
  });

  it('path redirect: profiler accepts an opt-in-only body and maps it to job params', () => {
    const r = schema('profiler').safeParse({
      run_profiler: false,
      path_redirect_action: 'accept',
      path_redirect_tables: 'c.s.t',
      path_redirect_reason: 'vendor code',
      confirm_execute: false,
    });
    expect(r.success).toBe(true);
    if (r.success) {
      expect(toJobParameters(r.data)).toEqual({
        run_profiler: 'false',
        path_redirect_action: 'accept',
        path_redirect_tables: 'c.s.t',
        path_redirect_reason: 'vendor code',
        confirm_execute: 'false',
      });
    }
  });

  it('path redirect: accept needs a reason, any action needs tables, revoke needs no reason', () => {
    const msgs = (body: Record<string, unknown>) => {
      const r = schema('profiler').safeParse(body);
      return r.success ? [] : r.error.issues.map((i) => i.message);
    };
    expect(msgs({ run_profiler: false, path_redirect_action: 'accept', path_redirect_tables: 'c.s.t' })).toContain(
      'path_redirect_action=accept requires a non-blank path_redirect_reason.',
    );
    expect(msgs({ run_profiler: false, path_redirect_action: 'revoke', path_redirect_tables: ' , ' })).toContain(
      'path_redirect_action needs path_redirect_tables (there is no implicit opt-in-all).',
    );
    expect(msgs({ run_profiler: false, path_redirect_action: 'revoke', path_redirect_tables: 'c.s.t' })).toEqual([]);
  });

  it('path redirect: opt-in fields without an action are rejected (no silent no-op)', () => {
    const r = schema('profiler').safeParse({ path_redirect_tables: 'c.s.t', confirm_execute: true });
    expect(r.success).toBe(false);
    expect(schema('profiler').safeParse({ path_redirect_action: 'nope' }).success).toBe(false);
  });

  it('discovery chunk: profiler rejects run_source_scan sent as a string (strict boolean)', () => {
    const r = schema('profiler').safeParse({ run_source_scan: 'true' });
    expect(r.success).toBe(false);
  });

  it('the former standalone source_scan alias is gone (folded into profiler)', () => {
    expect(migrationJobs.jobs?.source_scan).toBeUndefined();
  });

  it('strict schema rejects request_id leaking into params', () => {
    // Proves the prime-suspect failure mode: had request_id been nested under params,
    // .strict() would reject it. The fix keeps request_id a sibling of params.
    const r = schema('profiler').safeParse({ force_rescan: false, request_id: 'abc' });
    expect(r.success).toBe(false);
  });

  it('lease_ownership accepts its six run-now params and rejects extras', () => {
    const s = migrationJobs.jobs?.lease_ownership?.params;
    expect(s).toBeDefined();
    expect(s!.safeParse({ plan_run_id: 'p1', wave_id: 'w1', confirm_execute: true }).success).toBe(true);
    expect(s!.safeParse({ target_schema: 'c.s' }).success).toBe(true);
    // .strict() rejects an undeclared param (operator is server-injected, never client-sent).
    expect(s!.safeParse({ operator: 'evil@corp.com' }).success).toBe(false);
  });
});

describe('lease_ownership params', () => {
  const lease = migrationJobs.jobs!.lease_ownership.params!;

  it('rejects the deprecated table_owner_group (Authorize never changes ownership)', () => {
    const bad = lease.safeParse({
      plan_run_id: 'p1', wave_id: 'w1', target_schema: 'cat.sch',
      confirm_execute: true, table_owner_group: 'migration-owners',
    });
    expect(bad.success).toBe(false);
  });
  it('parses the selector alone', () => {
    const ok = lease.safeParse({ target_schema: 'cat.sch', confirm_execute: true });
    expect(ok.success).toBe(true);
  });
  it('still rejects an unknown key (.strict preserved)', () => {
    const bad = lease.safeParse({ target_schema: 'cat.sch', bogus: 'x' });
    expect(bad.success).toBe(false);
  });

  it('accepts an optional batch_id (bulk selector)', () => {
    const parsed = lease.parse({
      plan_run_id: 'plan1',
      batch_id: 'batch_00',
      confirm_execute: true,
    });
    expect(parsed.batch_id).toBe('batch_00');
  });

  it('still parses without a batch_id (omitted → undefined)', () => {
    const parsed = lease.parse({ target_schema: 'c.s', confirm_execute: false });
    expect(parsed.batch_id).toBeUndefined();
  });
});

describe('reshare / enable_compat finish-action params (08/08b finish_action)', () => {
  for (const alias of ['reshare', 'enable_compat']) {
    it(`${alias}: accepts a one-action defer with a reason`, () => {
      const r = schema(alias).safeParse({
        plan_run_id: 'p1',
        wave_id: 'w1',
        finish_action: 'defer',
        defer_reason: 'recipient not ready',
        action_keys: 'RESHARE_D2D|c.s.t|',
        confirm_execute: true,
      });
      expect(r.success).toBe(true);
      if (r.success) {
        expect(toJobParameters(r.data)).toMatchObject({
          finish_action: 'defer',
          defer_reason: 'recipient not ready',
          action_keys: 'RESHARE_D2D|c.s.t|',
          confirm_execute: 'true',
        });
      }
    });

    it(`${alias}: rejects a defer without a non-blank reason`, () => {
      for (const defer_reason of [undefined, '', '   ']) {
        const r = schema(alias).safeParse({ target_schema: 'c.s', finish_action: 'defer', defer_reason });
        expect(r.success).toBe(false);
        if (!r.success) {
          expect(r.error.issues.map((i) => i.message)).toContain(
            'finish_action=defer requires a non-blank defer_reason.',
          );
        }
      }
    });

    it(`${alias}: rejects action_keys on a run`, () => {
      for (const finish_action of [undefined, 'run'] as const) {
        const r = schema(alias).safeParse({ target_schema: 'c.s', finish_action, action_keys: 'k1' });
        expect(r.success).toBe(false);
        if (!r.success) {
          expect(r.error.issues.map((i) => i.message)).toContain(
            'action_keys narrows finish_action=defer|reopen only.',
          );
        }
      }
    });

    it(`${alias}: rejects an action_keys that names no key (never widens to the whole scope)`, () => {
      for (const action_keys of [',', '  ', ' , ', ',,']) {
        for (const finish_action of ['defer', 'reopen', undefined] as const) {
          const r = schema(alias).safeParse({
            target_schema: 'c.s', finish_action, defer_reason: 'recipient not ready', action_keys,
          });
          expect(r.success).toBe(false);
          if (!r.success) {
            expect(r.error.issues.map((i) => i.message)).toEqual([
              'action_keys names no action key (separators/whitespace only) — blank it or name the keys.',
            ]);
          }
        }
      }
    });

    it(`${alias}: a blank action_keys is no narrowing and is accepted on a run`, () => {
      expect(schema(alias).safeParse({ target_schema: 'c.s', action_keys: '' }).success).toBe(true);
    });

    it(`${alias}: accepts a reopen with action_keys and no reason`, () => {
      const r = schema(alias).safeParse({
        target_schema: 'c.s', finish_action: 'reopen', action_keys: 'k1', confirm_execute: true,
      });
      expect(r.success).toBe(true);
    });

    it(`${alias}: rejects an unknown finish_action`, () => {
      expect(schema(alias).safeParse({ target_schema: 'c.s', finish_action: 'skip' }).success).toBe(false);
    });

    it(`${alias}: a plain run still validates with no finish fields`, () => {
      expect(schema(alias).safeParse({ target_schema: 'c.s', confirm_execute: true }).success).toBe(true);
    });
  }
});

describe('finish jobs accept the M6.1 batch selector (M6.2)', () => {
  for (const alias of ['reshare', 'enable_compat', 'enable_optimization']) {
    it(`${alias} accepts plan_run_id + batch_id`, () => {
      const s = migrationJobs.jobs![alias].params!;
      const parsed = s.parse({ plan_run_id: 'p1', batch_id: 'batch_01', confirm_execute: false });
      expect(parsed.batch_id).toBe('batch_01');
    });
  }
});
