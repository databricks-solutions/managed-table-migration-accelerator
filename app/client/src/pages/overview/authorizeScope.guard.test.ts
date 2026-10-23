import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// A schema-scoped Authorize grants on every table job_03d's target_schema scope covers, and a grant
// on a table that never converts is never revoked. The guided card pins the routed wave; the
// Convert permission page is the deliberate schema-wide way in (for tables held back because the
// convert identity cannot read them).
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8');
const between = (src: string, start: string, end: string) => {
  const i = src.indexOf(start);
  expect(i, `${start} not found`).toBeGreaterThanOrEqual(0);
  return src.slice(i, src.indexOf(end, i + start.length));
};

describe('Authorize scope', () => {
  it('the guided Overview card pins the routed wave', () => {
    const body = between(read('pages/overview/OverviewPage.tsx'), 'function GuidedLeaseCta', '\n}\n');
    expect(body).toContain('finishScope({ planRunId: n.plan_run_id, waveId: n.next_wave_id');
    expect(body).toContain('{ ...scope.params, confirm_execute: true }');
    expect(body).not.toMatch(/target_schema:/);
  });

  it('the Convert permission page offers the schema-wide Authorize as well as its dry run', () => {
    const body = between(read('pages/workstreams/WorkstreamDetailPage.tsx'), 'function OwnershipEvidenceCard', '\n}\n');
    expect(body).toContain('params={{ target_schema: targetSchema, confirm_execute: false }}');
    expect(body).toContain('params={{ target_schema: targetSchema, confirm_execute: true }}');
    expect(body).not.toContain('Open one schema');
  });

  it('the estate subline counts migrated tables, like the Migrated tile', () => {
    for (const rel of ['pages/overview/OverviewPage.tsx', 'components/EstateSubline.tsx']) {
      const src = read(rel);
      expect(src).toContain('migrated: n.estate_migrated_tables,');
      expect(src).not.toContain('converted: n.estate_converted');
    }
  });
});
