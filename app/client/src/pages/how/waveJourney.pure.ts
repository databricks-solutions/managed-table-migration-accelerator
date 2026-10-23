/**
 * waveJourney.pure — the journey one wave takes from dry-run to "next wave", as teaching data.
 * Pure (no React) so the order is unit-tested and the graphic just renders it. Bracketed
 * (`optional`) steps apply only when the wave needs them; the order mirrors the conductor's
 * routing (`conductor.resolve_next_step`).
 */

export type JourneyPhase = 'before' | 'convert' | 'after';

export interface JourneyStep {
  id: string;
  label: string;
  /** Recommended only when it applies to the wave; skipped otherwise. */
  optional: boolean;
  phase: JourneyPhase;
  /** The backend job the step runs. Absent only for the terminal "next wave" marker. */
  job?: string;
  /** When an optional step applies, or what a required step does. */
  when: string;
  /** True for the one step that changes tables (convert). */
  mutates?: boolean;
}

export const WAVE_JOURNEY: readonly JourneyStep[] = [
  {
    id: 'dry_run', label: 'dry-run', optional: false, phase: 'before', job: 'job_03_dry_run',
    when: 'Runs 15 pre-flight checks per table and marks each DRY_RUN_OK or BLOCKED.',
  },
  {
    id: 'freeze', label: 'freeze', optional: true, phase: 'before', job: 'job_03c_freeze',
    when: 'A compaction job, Predictive Optimization, a stream, or a scheduled writer job is live on the wave.',
  },
  {
    id: 'lease', label: 'authorize', optional: true, phase: 'before', job: 'job_03d_lease_ownership',
    when: 'The convert identity is missing a permission on some of the tables.',
  },
  {
    id: 'convert', label: 'convert', optional: false, phase: 'convert', job: 'job_04_convert', mutates: true,
    when: 'Runs ALTER TABLE … SET MANAGED — the only step that changes tables.',
  },
  {
    id: 'validate', label: 'validate', optional: false, phase: 'convert', job: 'job_05_validate',
    when: 'Proves each managed table against its pre-convert snapshot.',
  },
  {
    id: 'unfreeze', label: 'unfreeze', optional: true, phase: 'after', job: 'job_05b_unfreeze_verify',
    when: 'The wave was frozen: restarts streams and writer jobs from the freeze ledger.',
  },
  {
    id: 'reshare', label: 're-share', optional: true, phase: 'after', job: 'job_08_reshare',
    when: 'A table is shared Databricks-to-Databricks; recipients are not updated automatically.',
  },
  {
    id: 'compat', label: 'compat', optional: true, phase: 'after', job: 'job_08b_enable_compat',
    when: 'Non-Databricks clients read the table; needs an empty storage location you choose.',
  },
  {
    id: 'optimize', label: 'optimize', optional: true, phase: 'after', job: 'job_08c_enable_optimization',
    when: 'Turns on automatic liquid clustering (Predictive Optimization is already on after the convert).',
  },
  {
    id: 'next_wave', label: 'next wave', optional: false, phase: 'after',
    when: 'Starts once this wave has no applicable step left.',
  },
];

/** The journey as one line, optional steps in brackets: `dry-run → [freeze] → … → next wave`. */
export function journeyLine(steps: readonly JourneyStep[] = WAVE_JOURNEY): string {
  return steps.map((s) => (s.optional ? `[${s.label}]` : s.label)).join(' → ');
}

/** The optional steps of one phase, in journey order (for the "applies when" legend). */
export function optionalSteps(phase: JourneyPhase, steps: readonly JourneyStep[] = WAVE_JOURNEY): JourneyStep[] {
  return steps.filter((s) => s.optional && s.phase === phase);
}

/**
 * The order the recommendation picks work in (`migration.focus.resolve_with_pins`), as teaching
 * data. The same order runs inside one schema in focus mode, minus the team-priority rung.
 */
export interface OrderRung {
  id: 'replan' | 'started' | 'finishing' | 'pins' | 'default';
  label: string;
  why: string;
}

export const RECOMMENDATION_ORDER: readonly OrderRung[] = [
  { id: 'replan', label: 'Re-plan first', why: 'With no current plan there is nothing safe to pick.' },
  { id: 'started', label: 'Resume a started wave', why: 'A wave with tables converting or converted is finished before anything new starts.' },
  { id: 'finishing', label: 'Finish a converted wave', why: 'Its restarts, re-share, external readers and optimize come before the next wave, so paused writers never stay paused.' },
  { id: 'pins', label: 'Team priorities', why: 'The first pinned schema that has a step to take. A blocked or done schema is skipped, never cleared by hand.' },
  { id: 'default', label: 'The next wave in plan order', why: 'Pilot first, then the rest by risk.' },
];

