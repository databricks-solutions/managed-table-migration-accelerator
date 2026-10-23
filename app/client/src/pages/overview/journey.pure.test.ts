import { describe, expect, it } from 'vitest';
import { STEP_COPY } from '../../lib/sectionCopy';
import { cardLayout, cardSpine, detailsSummary, isConvertStep, journeyNow, journeySteps, mutationBadge, stepPresentation, type CardBlock } from './journey.pure';
import { hasReadinessVerdict } from './readinessVerdict.pure';

const BASE = { discovered: 20, planned: 18, frozenOwed: 0, converted: 0, validated: 0 };

function statuses(kind: string | null, extras: Partial<Omit<Parameters<typeof journeySteps>[0], 'kind'>> = {}) {
  return journeySteps({ kind, ...BASE, ...extras }).map(
    (s) => `${s.id}:${s.status}${s.sublabel ? `:${s.sublabel}` : ''}`,
  );
}

describe('journeySteps', () => {
  it('highlights converting — not freezing — when the conductor says convert', () => {
    expect(statuses('convert')).toEqual([
      'profiled:done:20 discovered',
      'planned:done:18 planned',
      'freezing:pending:optional',
      'converting:active:0 converted',
      'validated:pending:0 validated',
      'finishing:pending',
    ]);
  });

  it('highlights freezing only when the conductor says freeze, showing the owed count', () => {
    expect(statuses('freeze', { frozenOwed: 3 })).toEqual([
      'profiled:done:20 discovered',
      'planned:done:18 planned',
      'freezing:active:3 items to pause',
      'converting:pending:0 converted',
      'validated:pending:0 validated',
      'finishing:pending',
    ]);
  });

  // The owed count is freeze/stop ACTIONS (estate_open_pre_convert), not tables, so it never
  // reads like a table count beside "132 discovered" / "121 planned".
  it('names the owed freeze work as items to pause, singular for one', () => {
    const sub = (owed: number | string) =>
      journeySteps({ ...BASE, kind: 'freeze', frozenOwed: owed }).find((s) => s.id === 'freezing')?.sublabel;
    expect(sub(1)).toBe('1 item to pause');
    expect(sub('337')).toBe('337 items to pause');
    expect(sub('0')).toBe('optional');
  });

  it('maps lease into the prep (freezing) bracket — same rail slot as freeze', () => {
    expect(statuses('lease')).toEqual([
      'profiled:done:20 discovered',
      'planned:done:18 planned',
      'freezing:active:optional',
      'converting:pending:0 converted',
      'validated:pending:0 validated',
      'finishing:pending',
    ]);
  });

  it('is a monotone funnel — Converting (cumulative) is never less than Validated', () => {
    // estate_migrated_tables (cumulative) as `converted`, estate_validated_tables as `validated`.
    // The old transient-bucket wiring produced Converting < Validated; the funnel forbids it.
    const steps = journeySteps({ kind: 'validate', ...BASE, converted: 31, validated: 24 });
    const conv = Number(steps.find((s) => s.id === 'converting')?.sublabel?.split(' ')[0]);
    const val = Number(steps.find((s) => s.id === 'validated')?.sublabel?.split(' ')[0]);
    expect(conv).toBeGreaterThanOrEqual(val);
    expect(steps.find((s) => s.id === 'converting')?.sublabel).toBe('31 converted');
    expect(steps.find((s) => s.id === 'validated')?.sublabel).toBe('24 validated');
  });

  it('treats resume as converting in flight', () => {
    const steps = journeySteps({ kind: 'resume', ...BASE, converted: 3 });
    expect(steps.find((s) => s.id === 'freezing')?.status).toBe('pending');
    expect(steps.find((s) => s.id === 'converting')?.status).toBe('active');
    expect(steps.find((s) => s.id === 'converting')?.sublabel).toBe('3 converted');
  });

  it('does not use converting==0 to pick freeze when kind is absent', () => {
    const steps = journeySteps({ kind: null, ...BASE });
    expect(steps.find((s) => s.id === 'freezing')?.status).toBe('pending');
    expect(steps.find((s) => s.id === 'planned')?.status).toBe('active');
  });

  it('resolves a fully-migrated estate to validated (not converting) when kind is absent', () => {
    // converted is CUMULATIVE now, so "everything migrated + some validated" must land on
    // validated even though converted > 0 (which would otherwise short-circuit to converting).
    const steps = journeySteps({ kind: null, ...BASE, converted: 18, validated: 18 });
    expect(steps.find((s) => s.id === 'validated')?.status).toBe('active');
    expect(steps.find((s) => s.id === 'converting')?.status).toBe('done');
  });

  it('falls back to freeze only when wave_state is AWAITING_ACTIONS and kind is absent', () => {
    const steps = journeySteps({ kind: null, waveState: 'AWAITING_ACTIONS', ...BASE });
    expect(steps.find((s) => s.id === 'freezing')?.status).toBe('active');
    expect(steps.find((s) => s.id === 'converting')?.status).toBe('pending');
  });

  it('lets next_step_kind win when it disagrees with wave_state', () => {
    const steps = journeySteps({ kind: 'convert', waveState: 'AWAITING_ACTIONS', ...BASE });
    expect(steps.find((s) => s.id === 'converting')?.status).toBe('active');
    expect(steps.find((s) => s.id === 'freezing')?.status).toBe('pending');
  });

  it('marks validate/done as the last phase', () => {
    const done = journeySteps({ kind: 'done', ...BASE, converted: 18, validated: 18 });
    expect(done.find((s) => s.id === 'converting')?.status).toBe('done');
    expect(done.find((s) => s.id === 'validated')?.status).toBe('done');
  });

  it('puts every post-convert finish step on the sixth Finishing stage, naming the step', () => {
    const cases: Array<[string, string]> = [
      ['unfreeze', 'restart streams'],
      ['reshare', 're-share'],
      ['compat', 'external readers'],
      ['optimize', 'optimize'],
    ];
    for (const [kind, sub] of cases) {
      const steps = journeySteps({ kind, ...BASE, converted: 18, validated: 18 });
      expect(steps.map((s) => s.id)).toEqual([
        'profiled', 'planned', 'freezing', 'converting', 'validated', 'finishing',
      ]);
      expect(steps.find((s) => s.id === 'finishing')).toMatchObject({ status: 'active', sublabel: sub });
      expect(steps.find((s) => s.id === 'validated')?.status).toBe('done');
      expect(steps.find((s) => s.id === 'converting')?.status).toBe('done');
    }
  });

  it('marks Finishing done only when the conductor says done', () => {
    expect(journeySteps({ kind: 'done', ...BASE }).find((s) => s.id === 'finishing')?.status).toBe('done');
    const v = journeySteps({ kind: 'validate', ...BASE }).find((s) => s.id === 'finishing');
    expect(v?.status).toBe('pending');
    expect(v?.sublabel).toBeUndefined();
  });
});

describe('stepPresentation', () => {
  it('presents convert/resume with every convert fact LIVE', () => {
    for (const kind of ['convert', 'resume']) {
      const p = stepPresentation(kind);
      expect(p.recommendedCompute).toBe('live');
      expect(p.reversible).toBe('live');
      expect(p.downtime).toBe('live');
      expect(p.blastRadius).toBe('live');
      expect(p.followUps).toBe('live');
      expect(p.description).toMatch(/14 days/);
    }
  });

  it('presents prep convert facts as PREVIEW and reversible as N/A (read-only dry-run)', () => {
    const p = stepPresentation('prep');
    expect(p.recommendedCompute).toBe('preview');
    expect(p.blastRadius).toBe('preview');
    expect(p.downtime).toBe('preview');
    expect(p.followUps).toBe('preview');
    // A dry-run changes nothing — reversibility does not apply (never "Reversible: No").
    expect(p.reversible).toBe('na');
    expect(p.description).toMatch(/checks every table/i);
    expect(p.description).toMatch(/changes no tables/i);
  });

  it('presents validate as fully read-only (no convert facts)', () => {
    const p = stepPresentation('validate');
    expect(p.recommendedCompute).toBe('na');
    expect(p.reversible).toBe('na');
    expect(p.downtime).toBe('na');
    expect(p.blastRadius).toBe('na');
    expect(p.followUps).toBe('na');
    expect(p.description).toMatch(/converted table/i);
  });

  it('treats freeze/unfreeze as restore-only: reversible metadata, no compute/downtime/blast', () => {
    for (const kind of ['freeze', 'unfreeze']) {
      const p = stepPresentation(kind);
      expect(p.recommendedCompute).toBe('na');
      expect(p.downtime).toBe('na');
      expect(p.blastRadius).toBe('na');
      expect(p.reversible).toBe('preview'); // reversible metadata, not a 14-day convert window
    }
    // unfreeze restores + closes paired actions now; freeze's are still upcoming.
    expect(stepPresentation('unfreeze').followUps).toBe('live');
    expect(stepPresentation('freeze').followUps).toBe('preview');
  });

  it('treats Authorize as reversible-metadata prep: no compute/downtime/blast, no SET MANAGED', () => {
    const p = stepPresentation('lease');
    expect(p.recommendedCompute).toBe('na');
    expect(p.downtime).toBe('na');
    expect(p.blastRadius).toBe('na');
    expect(p.reversible).toBe('preview'); // recorded grants, revoked at cleanup
    expect(p.description).toMatch(/Ownership never changes/);
  });

  it('presents reshare/compat as post-convert follow-ups with no convert sizing', () => {
    for (const kind of ['reshare', 'compat']) {
      const p = stepPresentation(kind);
      expect(p.recommendedCompute).toBe('na');
      expect(p.reversible).toBe('na');
      expect(p.followUps).toBe('live');
    }
  });

  it('presents optimize as a reversible-metadata follow-up with no convert sizing', () => {
    const p = stepPresentation('optimize');
    expect(p.recommendedCompute).toBe('na');
    expect(p.downtime).toBe('na');
    expect(p.blastRadius).toBe('na');
    expect(p.reversible).toBe('preview');
    expect(p.followUps).toBe('live');
    expect(p.description).toMatch(/Predictive Optimization/);
  });

  it('defaults unknown/absent kinds to a read-only profile (never live convert facts)', () => {
    for (const kind of [null, undefined, '', 'some_future_kind']) {
      const p = stepPresentation(kind);
      expect(p.recommendedCompute).toBe('na');
      expect(p.reversible).toBe('na');
      expect(p.downtime).toBe('na');
      expect(p.blastRadius).toBe('na');
      expect(p.followUps).toBe('na');
    }
  });

  it('gives every known kind a non-empty description', () => {
    for (const kind of [
      'convert', 'resume', 'prep', 'validate', 'freeze', 'lease', 'unfreeze', 'reshare', 'compat', 'optimize', 'actions', 'replan',
    ]) {
      expect(stepPresentation(kind).description.trim().length).toBeGreaterThan(0);
    }
  });

  it('is case-insensitive on kind', () => {
    expect(stepPresentation('CONVERT').recommendedCompute).toBe('live');
    expect(stepPresentation('Prep').recommendedCompute).toBe('preview');
  });
});

describe('mutationBadge', () => {
  it('labels SET MANAGED steps as mutating (mutates wins regardless of kind)', () => {
    for (const kind of ['convert', 'resume', 'some_future_mutator']) {
      expect(mutationBadge(kind, true)).toEqual({ label: 'Mutates tables', tone: 'mutates' });
    }
  });

  it('labels lease/freeze/unfreeze as reversible metadata — not "changes no tables"', () => {
    for (const kind of ['lease', 'freeze', 'unfreeze', 'optimize', 'LEASE']) {
      const b = mutationBadge(kind, false);
      expect(b.tone).toBe('metadata');
      expect(b.label).toBe('Reversible metadata change');
    }
  });

  it('labels genuinely read-only kinds (and bootstrap cards) as "Changes no data"', () => {
    for (const kind of ['prep', 'validate', 'actions', 'replan', null, undefined, '']) {
      expect(mutationBadge(kind, false)).toEqual({ label: 'Changes no data', tone: 'readonly' });
    }
  });
});

describe('cardLayout', () => {
  const KINDS: (string | null)[] = [...Object.keys(STEP_COPY), null, 'something_new'];

  // Today's render rules for the next-step card (OverviewPage ResolutionCard before the layers),
  // restated from stepPresentation: the oracle that no block is dropped or shown twice.
  function todaysBlocks(kind: string | null): Set<CardBlock> {
    const p = stepPresentation(kind);
    const on: [CardBlock, boolean][] = [
      ['scope', true],
      ['job', true],
      ['owners', true],
      ['reversible', p.reversible !== 'na'],
      ['time', p.downtime === 'live'],
      ['maxDowntime', p.downtime === 'live'],
      ['recommendedCompute', p.recommendedCompute === 'live'],
      ['blastRadius', p.blastRadius === 'live'],
      // Liquid-clustering step M4 (deliberate): a finish step acts on CONVERTED tables, so the
      // "tables still to convert" count (0 there) is hidden instead of reading "Tables 0".
      ['tables', kind !== 'prep' && !['unfreeze', 'reshare', 'compat', 'optimize'].includes(kind ?? '')],
      ['prepWaves', kind === 'prep'],
      ['convertWaves', isConvertStep(kind)],
      ['upcomingConvert', [p.recommendedCompute, p.downtime, p.blastRadius].includes('preview')],
      ['followUps', p.followUps !== 'na'],
      ['technicalSummary', true],
      ['convertWindow', true],
      // Milestone 6, new on the face: the progress bar (never twice on a freeze step, whose card
      // draws its own) and the Ready line.
      ['spine', cardSpine(kind) !== null && kind !== 'freeze'],
      ['verdict', hasReadinessVerdict(kind)],
    ];
    return new Set(on.filter(([, shown]) => shown).map(([b]) => b));
  }

  it.each(KINDS)('puts every block today\'s card renders for %s in exactly one layer', (kind) => {
    const { face, details } = cardLayout(kind);
    expect(face.filter((b) => details.includes(b))).toEqual([]);
    expect(new Set([...face, ...details])).toEqual(todaysBlocks(kind));
    expect(new Set(face).size + new Set(details).size).toBe(face.length + details.length);
  });

  it('shows max downtime without opening anything exactly on the convert steps', () => {
    for (const kind of KINDS) {
      expect(cardLayout(kind).face.includes('maxDowntime'), String(kind)).toBe(isConvertStep(kind));
    }
  });

  it('keeps the decision facts on the face and the job, owners and sizing in Details', () => {
    expect(cardLayout('convert')).toEqual({
      face: ['spine', 'verdict', 'scope', 'tables', 'reversible', 'maxDowntime'],
      details: ['job', 'owners', 'time', 'recommendedCompute', 'blastRadius', 'convertWaves', 'followUps', 'technicalSummary', 'convertWindow'],
    });
    expect(cardLayout('prep').face).toEqual(['spine', 'scope', 'prepWaves']);
    expect(cardLayout('freeze').face).toEqual(['verdict', 'scope', 'tables', 'reversible']);
    expect(cardLayout('done').face).toEqual(['scope', 'tables']);
    expect(cardLayout('prep').details).toContain('upcomingConvert');
  });

  it('always has Details, because the job and owners always render', () => {
    for (const kind of KINDS) {
      expect(cardLayout(kind).details.slice(0, 2), String(kind)).toEqual(['job', 'owners']);
    }
  });

  it('names what Details holds, so the closed row says what opening it shows', () => {
    expect(detailsSummary(cardLayout('convert'))).toBe('Job, owners, sizing, waves, follow-ups');
    expect(detailsSummary(cardLayout('prep'))).toBe('Job, owners, sizing, follow-ups');
    expect(detailsSummary(cardLayout('validate'))).toBe('Job, owners');
  });
});

describe('journeyNow', () => {
  it('names the highlighted stage and its place, for the closed journey row', () => {
    expect(journeyNow(journeySteps({ ...BASE, kind: 'convert' }))).toBe('Now at: Converting · 4 of 6');
    expect(journeyNow(journeySteps({ ...BASE, kind: 'prep' }))).toBe('Now at: Planned · 2 of 6');
  });

  it('says nothing rather than guess when no stage is highlighted', () => {
    expect(journeyNow([{ id: 'a', label: 'A', status: 'done' }])).toBeNull();
    expect(journeyNow([])).toBeNull();
  });
});

describe('cardSpine', () => {
  const KINDS: (string | null)[] = [...Object.keys(STEP_COPY), null, 'something_new'];
  const ACTIVE: Record<string, string> = {
    prep: 'prep',
    freeze: 'freeze',
    lease: 'freeze',
    convert: 'convert',
    resume: 'convert',
    validate: 'validate',
    unfreeze: 'cleanup',
    reshare: 'cleanup',
    compat: 'cleanup',
    optimize: 'cleanup',
  };
  const NO_BAR = ['done', 'replan', 'blocked', 'actions'];

  const show = (kind: string) => cardSpine(kind)?.map((s) => `${s.id}:${s.status}${s.sublabel ? `:${s.sublabel}` : ''}`);

  it('maps every step kind to a bar or explicitly to none', () => {
    for (const kind of Object.keys(STEP_COPY)) {
      expect(kind in ACTIVE || NO_BAR.includes(kind), kind).toBe(true);
    }
  });

  it.each(Object.entries(ACTIVE))('marks exactly one step active for %s: %s', (kind, active) => {
    const steps = cardSpine(kind)!;
    expect(steps.map((s) => s.id)).toEqual(['prep', 'freeze', 'convert', 'validate', 'cleanup']);
    expect(steps.filter((s) => s.status === 'active').map((s) => s.id)).toEqual([active]);
  });

  it.each(NO_BAR)('draws no bar for %s, so it can never mislead', (kind) => {
    expect(cardSpine(kind)).toBeNull();
  });

  it('draws no bar for a missing or unknown kind', () => {
    expect(cardSpine(null)).toBeNull();
    expect(cardSpine('something_new')).toBeNull();
    for (const kind of KINDS) expect(cardSpine(kind) === null, String(kind)).toBe(!(String(kind) in ACTIVE));
  });

  it('produces exactly the freeze card\'s bar on a freeze step', () => {
    expect(cardSpine('freeze')).toEqual([
      { id: 'prep', label: 'Prep', status: 'done' },
      { id: 'freeze', label: 'Freeze', sublabel: 'you are here', status: 'active' },
      { id: 'convert', label: 'Convert', status: 'pending' },
      { id: 'validate', label: 'Validate', status: 'pending' },
      { id: 'cleanup', label: 'Cleanup', status: 'pending' },
    ]);
  });

  it('marks the freeze optional, never done, when the step is not the freeze', () => {
    expect(show('convert')).toEqual([
      'prep:done',
      'freeze:pending:optional',
      'convert:active:you are here',
      'validate:pending',
      'cleanup:pending',
    ]);
    expect(show('prep')).toEqual([
      'prep:active:you are here',
      'freeze:pending:optional',
      'convert:pending',
      'validate:pending',
      'cleanup:pending',
    ]);
    expect(show('optimize')).toEqual([
      'prep:done',
      'freeze:pending:optional',
      'convert:done',
      'validate:done',
      'cleanup:active:you are here',
    ]);
  });

  it('reads the kind case-insensitively, like the rest of the card', () => {
    expect(cardSpine('CONVERT')).toEqual(cardSpine('convert'));
  });
});


describe('cardLayout on finish steps', () => {
  it('hides the tables-to-convert count, which is 0 once a wave is converted', () => {
    for (const kind of ['unfreeze', 'reshare', 'compat', 'optimize']) {
      expect(cardLayout(kind).face, kind).not.toContain('tables');
    }
    expect(cardLayout('convert').face).toContain('tables');
    expect(cardLayout('validate').face).toContain('tables');
  });
});
