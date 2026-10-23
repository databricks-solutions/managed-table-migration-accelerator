import { describe, expect, it } from 'vitest';
import { GUIDES, GUIDE_IDS, getGuide, type GuideId } from './directGuides.pure';

const notebooksOf = (id: GuideId): string[] =>
  (getGuide(id)?.steps ?? []).map((s) => s.notebook).filter((n): n is string => Boolean(n));

const textOf = (id: GuideId): string =>
  (getGuide(id)?.steps ?? []).map((s) => `${s.title} ${s.body} ${s.code ?? ''}`).join(' ');

describe('directGuides — catalog integrity', () => {
  it('exposes exactly the three guides, ids unique and in tab order', () => {
    expect(GUIDE_IDS).toEqual(['cockpit', 'adhoc', 'notebooks']);
    expect(new Set(GUIDE_IDS).size).toBe(GUIDE_IDS.length);
    expect(GUIDES).toHaveLength(3);
  });

  it('getGuide resolves each id and rejects an unknown one', () => {
    for (const id of GUIDE_IDS) expect(getGuide(id)?.id).toBe(id);
    expect(getGuide('nope' as GuideId)).toBeUndefined();
  });

  it('every guide is fully populated with non-empty prose and >= 3 steps', () => {
    for (const g of GUIDES) {
      expect(g.label.length).toBeGreaterThan(0);
      expect(g.tagline.length).toBeGreaterThan(0);
      expect(g.intro.length).toBeGreaterThan(0);
      expect(g.note.length).toBeGreaterThan(0);
      expect(g.steps.length).toBeGreaterThanOrEqual(3);
      for (const s of g.steps) {
        expect(s.title.trim().length).toBeGreaterThan(0);
        expect(s.body.trim().length).toBeGreaterThan(0);
      }
    }
  });
});

describe('directGuides — grounded in the tooling', () => {
  it('cockpit guide teaches 09_cockpit.py and the three actions', () => {
    expect(notebooksOf('cockpit')).toContain('09_cockpit.py');
    const text = textOf('cockpit');
    expect(text).toContain('action=show');
    expect(text).toContain('action=advance');
    expect(text).toContain('action=advanced');
  });

  it('cockpit guide teaches focus (target_schema) and team priorities (pin_action)', () => {
    const text = textOf('cockpit');
    expect(text).toContain('target_schema=<catalog.schema>');
    expect(text).toContain('pin_action=add');
    expect(text).toMatch(/after work already in flight/);
  });

  it('adhoc guide covers profiler, wave planner, and the path-access scanner', () => {
    const nb = notebooksOf('adhoc');
    expect(nb).toContain('00_profiler.py');
    expect(nb).toContain('02_wave_planner.py');
    expect(nb).toContain('01_source_scan.py');
  });

  it('full-run guide covers the core stages in execution order', () => {
    const nb = notebooksOf('notebooks');
    const core = ['00_profiler.py', '02_wave_planner.py', '03_dry_run.py', '04_convert.py', '05_validate.py', '07_post_convert_cleanup.py'];
    for (const stage of core) expect(nb).toContain(stage);
    // Each core stage appears in non-decreasing position (execution order preserved).
    const positions = core.map((stage) => nb.indexOf(stage));
    const sorted = [...positions].sort((a, b) => a - b);
    expect(positions).toEqual(sorted);
  });

  it('full-run guide gates the convert on the compaction flag and keeps streaming isolated', () => {
    const text = textOf('notebooks');
    expect(text).toContain('confirm_compaction_paused=true');
    expect(text).toContain('bucket=streaming');
  });
});
