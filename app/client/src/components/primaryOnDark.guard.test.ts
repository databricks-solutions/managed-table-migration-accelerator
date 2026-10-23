import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The main button of every guided step on the dark card is the white PRIMARY_ON_DARK (the stock
// `default` variant is navy on the navy card and reads as disabled); lava red stays reserved for
// the convert, which changes tables. No app test renders React, so this pins it in the source.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8');
const fn = (src: string, name: string) => {
  const i = src.indexOf(`function ${name}`);
  expect(i, `${name} not found`).toBeGreaterThanOrEqual(0);
  return src.slice(i, src.indexOf('\n}\n', i));
};

describe('guided main buttons on the dark card', () => {
  const overview = read('pages/overview/OverviewPage.tsx');

  it.each([
    'FirstRunResolution', 'PlanTheWavesResolution', 'GuidedValidateCta', 'ReviewInWavesButton',
    'GuidedPrepCta', 'GuidedLeaseCta', 'GuidedActionsCard', 'GuidedFreezeCard', 'GuidedConvertGate',
  ])('%s uses the white main button', (name) => {
    expect(fn(overview, name)).toContain('PRIMARY_ON_DARK');
  });

  it('the convert keeps its red, and no brand-lava main button is left', () => {
    const gate = fn(overview, 'GuidedConvertGate');
    const dialog = gate.slice(gate.indexOf('<ConvertGateDialog'));
    expect(dialog.slice(0, dialog.indexOf('/>'))).not.toContain('PRIMARY_ON_DARK');
    expect(overview).not.toContain('bg-[var(--brand)] text-[var(--fg-on-dark)]');
  });

  it('the finish steps use it on the dark surface', () => {
    const finish = read('components/FinishStepCta.tsx');
    for (const name of ['UnfreezeCta', 'ReshareCta', 'CompatCta', 'OptimizeCta']) {
      expect(fn(finish, name), name).toContain('triggerClassName={primaryOn(surface)}');
    }
  });
});

describe('the clustering step', () => {
  const cta = fn(read('components/FinishStepCta.tsx'), 'OptimizeCta');

  it('leads with turning clustering on, then review, then a quiet skip', () => {
    const iOn = cta.indexOf('DIALOG_COPY.finishOptimize,');
    const iReview = cta.indexOf('DIALOG_COPY.finishOptimizePlan');
    const iSkip = cta.indexOf('DIALOG_COPY.skipClustering');
    expect(iOn).toBeGreaterThanOrEqual(0);
    expect(iOn).toBeLessThan(iReview);
    expect(iReview).toBeLessThan(iSkip);
    expect(cta).toContain("optimizeParams(scope, 'skip')");
    expect(cta).toContain('triggerClassName={link}');
  });

  it('shows the breakdown line first, and only after a successful read (display only)', () => {
    expect(cta).toContain("useMigrationQuery('optimize_layout_for_scope', finishQueryArgs(scope))");
    expect(cta).toContain('(layoutQ.error ?? null) === null && !layoutQ.loading');
    expect(cta.indexOf('data-clustering-breakdown')).toBeLessThan(cta.indexOf('DIALOG_COPY.finishOptimize,'));
    expect(cta.slice(0, cta.indexOf('DIALOG_COPY.finishOptimize,'))).not.toContain('params=');
  });

  it('never offers the old finish-without-Predictive-Optimization decline', () => {
    expect(cta).not.toContain('finishWithoutPo');
    expect(cta).not.toContain("'decline'");
  });
});
