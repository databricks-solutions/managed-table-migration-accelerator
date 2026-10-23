import { describe, expect, it } from 'vitest';
import { resolveRunPageUrl } from './job-trigger.js';
import { applyOperator, resolveOperator } from '../jobs/operator.js';
import { migrationJobs } from '../jobs/job-params.js';

/**
 * The trigger route returns run_page_url on success so the client can seed its shared
 * active-run state without waiting for the next poll. resolveRunPageUrl is the pure,
 * dependency-injected core of that: it must return the SDK's url on success and degrade to
 * null on any failure (a missing link is no worse than the poll fallback).
 */
describe('resolveRunPageUrl', () => {
  it('returns the run_page_url the SDK reports', async () => {
    const url = await resolveRunPageUrl(() => Promise.resolve({ run_page_url: 'https://ex/runs/42' }), 42);
    expect(url).toBe('https://ex/runs/42');
  });

  it('returns null when the run has no page url', async () => {
    expect(await resolveRunPageUrl(() => Promise.resolve({}), 42)).toBeNull();
  });

  it('degrades to null when getRun throws (grants / transient / not-yet-visible)', async () => {
    const url = await resolveRunPageUrl(
      () => Promise.reject(new Error('RESOURCE_DOES_NOT_EXIST')),
      42,
    );
    expect(url).toBeNull();
  });

  it('returns null (and never calls getRun) when the run id is missing', async () => {
    let called = false;
    const url = await resolveRunPageUrl(() => {
      called = true;
      return Promise.resolve({ run_page_url: 'x' });
    }, undefined);
    expect(url).toBeNull();
    expect(called).toBe(false);
  });
});

/**
 * The trigger route resolves the operator from the forwarded-identity header and injects it
 * into job_parameters AFTER the per-alias Zod parse — for mutating aliases only, never
 * client-spoofable. These assertions pin the exact parse→inject sequence the route performs
 * (offline: the route handler itself needs a live SDK + getExecutionContext, so we test its
 * pure core the same way resolveRunPageUrl is tested above).
 */
describe('operator injection (trigger route core)', () => {
  it('injects the header operator into a mutating alias job_parameters map after parse', () => {
    const parsed = migrationJobs.jobs!.convert.params!.safeParse({ target_schema: 'cat.sch' });
    expect(parsed.success).toBe(true);
    if (!parsed.success) return;

    const jobParameters: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed.data)) {
      if (typeof v === 'string') jobParameters[k] = v;
      else if (typeof v === 'number' || typeof v === 'boolean') jobParameters[k] = String(v);
    }
    const operator = resolveOperator({ 'x-forwarded-preferred-username': 'alice@corp.com' });
    const finalParams = applyOperator('convert', jobParameters, operator);

    expect(finalParams).toEqual({ target_schema: 'cat.sch', operator: 'alice@corp.com' });
  });

  it('does not inject operator for a non-mutating alias (wave_planner)', () => {
    const operator = resolveOperator({ 'x-forwarded-preferred-username': 'alice@corp.com' });
    expect(applyOperator('wave_planner', { wave_max_tables: '300' }, operator)).toEqual({
      wave_max_tables: '300',
    });
  });

  it('a client-supplied operator in body.params is rejected by .strict() (not spoofable)', () => {
    const parsed = migrationJobs.jobs!.convert.params!.safeParse({
      target_schema: 'cat.sch',
      operator: 'spoofed@evil.com',
    });
    expect(parsed.success).toBe(false);
  });
});
