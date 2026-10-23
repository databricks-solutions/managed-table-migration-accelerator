import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The next-step card shows its decision facts and the step's button without opening anything;
// the rest waits in closed sections. No app test renders React, so this pins the order in the
// source: a step button that ended up inside a closed section would hide the one thing to do
// (and break the browser walks that click it by name).
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8');
const between = (src: string, start: string, end: string) => {
  const i = src.indexOf(start);
  expect(i, `${start} not found`).toBeGreaterThanOrEqual(0);
  return src.slice(i, src.indexOf(end, i + start.length));
};

const card = () => between(read('pages/overview/OverviewPage.tsx'), 'function ResolutionCard', '\n}\n');

// Every branch of the card's step-button dispatch.
const STEP_BUTTONS = [
  '<GuidedConvertCta',
  '<GuidedValidateCta',
  '<GuidedPrepCta',
  '<GuidedFreezeCta',
  '<FinishStepCta',
  '<GuidedLeaseCta',
  '<GuidedActionsCta',
  '<ReviewInWavesButton',
];

describe('next-step card layers', () => {
  it('has collapsed sections', () => {
    expect(card().match(/<Disclosure\b/g)?.length ?? 0).toBeGreaterThanOrEqual(2);
  });

  it('renders every step button before the first collapsed section', () => {
    const body = card();
    const firstFold = body.indexOf('<Disclosure');
    for (const btn of STEP_BUTTONS) {
      const at = body.indexOf(btn);
      expect(at, `${btn} missing from the card`).toBeGreaterThanOrEqual(0);
      expect(at, `${btn} must render before the first <Disclosure`).toBeLessThan(firstFold);
    }
  });

  it('keeps "You approve the step" and the max downtime fact on the face', () => {
    const body = card();
    const firstFold = body.indexOf('<Disclosure');
    for (const marker of ['You approve the step', 'label="Max downtime"']) {
      const at = body.indexOf(marker);
      expect(at, `${marker} missing`).toBeGreaterThanOrEqual(0);
      expect(at, `${marker} must render before the first <Disclosure`).toBeLessThan(firstFold);
    }
  });

  // Milestone 6: the progress bar and the Ready line sit between the description and the facts,
  // so they read first and never push the step's button into a closed section.
  it('renders the progress bar and the Ready line after the description, before the facts and the step', () => {
    const body = card();
    const description = body.indexOf('present.description');
    const firstFact = body.indexOf('<DarkStat');
    const firstButton = Math.min(...STEP_BUTTONS.map((b) => body.indexOf(b)));
    for (const marker of ['<Stepper variant="bracket"', '<ReadinessLine']) {
      const at = body.indexOf(marker);
      expect(at, `${marker} missing from the card`).toBeGreaterThan(description);
      expect(at, `${marker} must render before the facts`).toBeLessThan(firstFact);
      expect(at, `${marker} must render before the step's button`).toBeLessThan(firstButton);
    }
  });

  it('draws the freeze card\'s bar from the same function, never a second hand-built one', () => {
    const freezeCard = between(read('pages/overview/OverviewPage.tsx'), 'function GuidedFreezeCard', '\n}\n');
    expect(freezeCard).toContain("cardSpine('freeze')");
    expect(freezeCard).not.toMatch(/label: 'Prep'/);
  });

  it('never opens a section by default', () => {
    expect(card()).not.toMatch(/<Disclosure\b[^>]*\b(open|defaultOpen)\b/);
  });

  // A native <details> keeps closed content mounted: its queries fire as before and its text stays
  // in the page for the browser checks. A Radix Collapsible unmounts it.
  it('the shared Disclosure is a native details element that starts closed unless asked', () => {
    const src = read('components/Disclosure.tsx');
    expect(src).toContain('<details');
    expect(src.match(/\bopen=\{[^}]*\}/g)).toEqual(['open={defaultOpen || undefined}']);
    expect(src).toContain('defaultOpen = false');
    expect(src).not.toMatch(/import[^;]*Collapsible|<Collapsible/);
  });
});
