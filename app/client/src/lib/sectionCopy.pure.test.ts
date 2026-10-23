import { describe, expect, it } from 'vitest';
import { copyViolations, lengthViolations } from './copyRules.pure';
import {
  ACTION_COPY,
  copyEntries,
  DIALOG_COPY,
  dialogProps,
  fillCopy,
  nextStepSubtitle,
  PAGE_COPY,
  pageIntroForPath,
  SECTION_COPY,
  STEP_COPY,
  stepBadge,
  stepTitle,
  WORKSTREAM_COPY,
  type WorkstreamKey,
} from './sectionCopy';
import type { Workstream } from '../hooks/useMigrationQuery';

// Compile-time parity: WorkstreamKey and the hook's Workstream union are the same set.
const _forward: Record<Workstream, unknown> = WORKSTREAM_COPY;
const _backward: Record<WorkstreamKey, unknown> = {} as Record<Workstream, unknown>;
void _forward;
void _backward;

const visible = copyEntries().filter((e) => e.kind !== 'technical');
const label = (e: { group: string; key: string; field: string }) => `${e.group}.${e.key}.${e.field}`;

describe('sectionCopy entries', () => {
  it('has a non-empty title and subtitle for every page and section', () => {
    const sections = [
      ...Object.values(PAGE_COPY),
      ...Object.values(SECTION_COPY).flatMap((s) => Object.values(s)),
    ];
    for (const c of sections) {
      expect(c.title.trim()).not.toBe('');
      expect(c.subtitle.trim()).not.toBe('');
    }
  });

  it.each(visible.map((e) => [label(e), e] as const))('%s uses plain words', (_name, e) => {
    expect(copyViolations(e.text)).toEqual([]);
  });

  it.each(visible.map((e) => [label(e), e] as const))('%s stays within its length cap', (_name, e) => {
    expect(lengthViolations(e.text, e.kind as 'title' | 'subtitle' | 'helper' | 'dialog' | 'primer')).toEqual([]);
  });

  it('covers all 14 next-step kinds', () => {
    expect(Object.keys(STEP_COPY).sort()).toEqual(
      [
        'actions',
        'blocked',
        'compat',
        'convert',
        'done',
        'freeze',
        'lease',
        'optimize',
        'prep',
        'reshare',
        'replan',
        'resume',
        'unfreeze',
        'validate',
      ].sort(),
    );
  });

  it('covers all eight workstreams', () => {
    expect(Object.keys(WORKSTREAM_COPY)).toHaveLength(8);
  });

  it('keeps the two refresh buttons identical apart from the label', () => {
    const { label: a, ...restA } = ACTION_COPY.refreshRecommendation;
    const { label: b, ...restB } = ACTION_COPY.refreshNextStep;
    expect(a).toBe('Refresh recommendation');
    expect(b).toBe('Refresh the next step');
    expect(restA).toEqual(restB);
  });

  it('keeps job names in `technical` only', () => {
    for (const d of Object.values(DIALOG_COPY)) {
      expect(`${d.title} ${d.description}`).not.toMatch(/job_/);
    }
  });
});

describe('fillCopy', () => {
  it('fills placeholders', () => {
    expect(fillCopy('{n} tables', { n: 3 })).toBe('3 tables');
    expect(fillCopy('Converts {scope}.', { scope: 'main.sales' })).toBe('Converts main.sales.');
  });

  it('throws on a missing value', () => {
    expect(() => fillCopy('{n} tables in {scope}', { n: 3 })).toThrow(/scope/);
  });
});

describe('nextStepSubtitle', () => {
  it('returns the done variant', () => {
    expect(nextStepSubtitle('done')).toBe(SECTION_COPY.overview.nextStepDone.subtitle);
  });

  it('returns the blocked variant', () => {
    expect(nextStepSubtitle('BLOCKED')).toBe(SECTION_COPY.overview.nextStepBlocked.subtitle);
  });

  it('asks the user to press the button for any other step', () => {
    expect(nextStepSubtitle('validate')).toMatch(/Press the button to continue\.$/);
    expect(nextStepSubtitle(null)).toMatch(/Press the button to continue\.$/);
  });
});

describe('stepTitle and stepBadge', () => {
  it('uses the app title for a known kind', () => {
    expect(stepTitle('prep', 'Prep (dry-run + gate)')).toBe('Check the wave is ready');
    expect(stepTitle('reshare', 'Re-share Delta shares (D2D)')).toBe('Share the converted tables again');
  });

  it('falls back for an unknown kind', () => {
    expect(stepTitle('unknown_kind', 'X')).toBe('X');
    expect(stepBadge('unknown_kind')).toBe('unknown_kind');
  });
});

describe('pageIntroForPath', () => {
  it('returns nothing for How it works', () => {
    expect(pageIntroForPath('/how-it-works')).toBeNull();
    expect(pageIntroForPath('/how-it-works/waves')).toBeNull();
  });

  it('returns the detail entry for a workstream page', () => {
    expect(pageIntroForPath('/workstreams/path_access')).toBe(PAGE_COPY['/workstreams/:workstream']);
  });

  it('returns the page entry for a top-level route', () => {
    expect(pageIntroForPath('/overview')?.title).toBe('Overview');
  });

  it('returns null for an unknown route', () => {
    expect(pageIntroForPath('/nope')).toBeNull();
  });
});

describe('dialogProps', () => {
  it('fills placeholders in every field and defaults the trigger to the title', () => {
    const p = dialogProps(
      { title: 'Check {scope}', description: 'Checks {scope}.', confirm: 'Check', technical: 'Runs x on {scope}' },
      { scope: 'main.sales' },
    );
    expect(p).toEqual({
      triggerLabel: 'Check main.sales',
      title: 'Check main.sales',
      description: 'Checks main.sales.',
      confirmLabel: 'Check',
      technical: 'Runs x on main.sales',
    });
  });
});
