import type { StepperStep } from '../../components/Stepper';
import { num } from '../../lib/focus.pure';
import { STEP_COPY } from '../../lib/sectionCopy';
import { hasReadinessVerdict } from './readinessVerdict.pure';
import { isFinishKind } from '../../lib/finishScope.pure';

export type JourneyPhase = 'profiled' | 'planned' | 'freezing' | 'converting' | 'validated' | 'finishing';

const PHASE_ORDER: JourneyPhase[] = ['profiled', 'planned', 'freezing', 'converting', 'validated', 'finishing'];

const CONVERTING_KINDS = new Set(['convert', 'resume']);
const VALIDATED_KINDS = new Set(['validate', 'done']);
// A converted wave's finish steps (conductor post_convert_step order). Sixth rail stage — a
// deliberate delta from the v4 mockup's five-stage rail (design-deltas T44).
const FINISHING_KINDS = new Set(['unfreeze', 'reshare', 'compat', 'optimize']);
const FINISH_SUBLABEL: Record<string, string> = {
  unfreeze: 'restart streams',
  reshare: 're-share',
  compat: 'external readers',
  optimize: 'optimize',
};
const PLANNED_KINDS = new Set(['prep', 'replan', 'blocked', 'actions']);

/** True when the recommendation is a convert-gate step (convert or resume). */
export function isConvertStep(kind: string | null | undefined): boolean {
  return CONVERTING_KINDS.has((kind ?? '').toLowerCase());
}

/**
 * How a wave-level convert fact should be presented for a given `next_step_kind`:
 *   - `live`    — describes THIS step now (render normally).
 *   - `preview` — describes the UPCOMING convert, not this step (render under a preview group,
 *                 labelled so it never reads as "needed now").
 *   - `na`      — does not apply to this step (omit, or render an explicit N/A).
 */
export type FactMode = 'live' | 'preview' | 'na';

export interface StepPresentation {
  /** One-line, step-specific description for the card's CardDescription. */
  description: string;
  /** Recommended-compute sizing (advisory convert sizing of the wave's largest table). */
  recommendedCompute: FactMode;
  /** Reversibility. `live` for convert/resume (14d); `meta` handled by callers via the mode. */
  reversible: FactMode;
  /** ETA / max-downtime (both are convert-copy figures). */
  downtime: FactMode;
  /** Downstream blast radius of the (eventual) convert. */
  blastRadius: FactMode;
  /** POST_CONVERT follow-ups (streaming restart / re-share / compat / below-floor). */
  followUps: FactMode;
}

// Read-only default: any step that is not an in-place convert presents convert facts as a
// preview at most, never as live — so a NEW next_step_kind can never regress to claiming a
// read-only step needs 32 cores / is irreversible.
type StepModes = Omit<StepPresentation, 'description'>;

const _READONLY_DEFAULT: StepModes = {
  recommendedCompute: 'na',
  reversible: 'na',
  downtime: 'na',
  blastRadius: 'na',
  followUps: 'na',
};

const _READONLY_DESCRIPTION = 'A step from the current plan. Changes no data.';

// Per-kind presentation profiles (control_plane §3.1). Convert/resume are the only steps where
// the wave's convert facts are LIVE; prep is the convert's immediate look-ahead (preview);
// everything else is read-only / restore-only and those facts are N/A. The visible description
// comes from STEP_COPY (lib/sectionCopy.ts).
const _LIVE: StepModes = { recommendedCompute: 'live', reversible: 'live', downtime: 'live', blastRadius: 'live', followUps: 'live' };
const _METADATA_PREP: StepModes = { ..._READONLY_DEFAULT, reversible: 'preview', followUps: 'preview' };
const _FINISH: StepModes = { ..._READONLY_DEFAULT, followUps: 'live' };

const _STEP_MODES: Record<string, StepModes> = {
  convert: _LIVE,
  resume: _LIVE,
  prep: { recommendedCompute: 'preview', reversible: 'na', downtime: 'preview', blastRadius: 'preview', followUps: 'preview' },
  validate: _READONLY_DEFAULT,
  freeze: _METADATA_PREP,
  lease: _METADATA_PREP,
  unfreeze: { ..._FINISH, reversible: 'preview' },
  reshare: _FINISH,
  compat: _FINISH,
  optimize: { ..._FINISH, reversible: 'preview' },
  actions: _READONLY_DEFAULT,
  replan: _READONLY_DEFAULT,
};

/**
 * Presentation profile for a `next_step_kind` (pure). Unknown/absent kinds fall to the
 * read-only default so a card never presents convert facts as live for a step that isn't one.
 */
export function stepPresentation(kind: string | null | undefined): StepPresentation {
  const k = (kind ?? '').toLowerCase();
  return {
    ...(_STEP_MODES[k] ?? _READONLY_DEFAULT),
    description: STEP_COPY[k]?.description ?? _READONLY_DESCRIPTION,
  };
}

/**
 * One block of the next-step card. Face blocks show without opening anything; the rest sit in its
 * collapsed Details. `technicalSummary` (the conductor's directive) and `convertWindow`
 * (`min_slack_min`) are also gated on their data in the component.
 */
export type CardBlock =
  | 'spine'
  | 'verdict'
  | 'scope'
  | 'tables'
  | 'reversible'
  | 'maxDowntime'
  | 'prepWaves'
  | 'job'
  | 'owners'
  | 'time'
  | 'recommendedCompute'
  | 'blastRadius'
  | 'convertWaves'
  | 'upcomingConvert'
  | 'followUps'
  | 'technicalSummary'
  | 'convertWindow';

export interface CardLayout {
  /** Shown without opening anything, in this order, above the step's button. */
  face: CardBlock[];
  /** Inside the collapsed Details, in this order. */
  details: CardBlock[];
}

/**
 * Which of the next-step card's blocks show on its face and which wait in Details. Derived from
 * `stepPresentation` so the two cannot drift: the same blocks render as before, only the layer
 * changes. The face answers "what is it, how much, can I undo it, and the downtime"; everything
 * else is one click away.
 */
export function cardLayout(kind: string | null | undefined): CardLayout {
  const p = stepPresentation(kind);
  const prep = kind === 'prep';
  const face: CardBlock[] = [];
  // The freeze card draws this same bar itself (cardSpine('freeze')): never two on one card.
  if (cardSpine(kind) !== null && (kind ?? '').toLowerCase() !== 'freeze') face.push('spine');
  if (hasReadinessVerdict(kind)) face.push('verdict');
  face.push('scope');
  // A finish step acts on the wave's CONVERTED tables; the tables-to-convert count is 0 there.
  if (!prep && !isFinishKind((kind ?? '').toLowerCase())) face.push('tables');
  if (p.reversible !== 'na') face.push('reversible');
  if (p.downtime === 'live') face.push('maxDowntime');
  if (prep) face.push('prepWaves');

  const details: CardBlock[] = ['job', 'owners'];
  if (p.downtime === 'live') details.push('time');
  if (p.recommendedCompute === 'live') details.push('recommendedCompute');
  if (p.blastRadius === 'live') details.push('blastRadius');
  if (isConvertStep(kind)) details.push('convertWaves');
  if ([p.recommendedCompute, p.downtime, p.blastRadius].includes('preview')) details.push('upcomingConvert');
  if (p.followUps !== 'na') details.push('followUps');
  details.push('technicalSummary', 'convertWindow');
  return { face, details };
}

type SpineStage = 'prep' | 'freeze' | 'convert' | 'validate' | 'cleanup';

const SPINE: { id: SpineStage; label: string }[] = [
  { id: 'prep', label: 'Prep' },
  { id: 'freeze', label: 'Freeze' },
  { id: 'convert', label: 'Convert' },
  { id: 'validate', label: 'Validate' },
  { id: 'cleanup', label: 'Cleanup' },
];

// Which stage each step kind is. Lease (Authorize) shares the freeze slot, as on the journey rail.
// The finish steps only follow a fully validated wave (conductor.pick_finishing_wave), so on them
// Validate is done. done / replan / blocked / actions and unknown kinds draw no bar.
const SPINE_STAGE: Record<string, SpineStage> = {
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

/**
 * The next-step card's progress bar, Prep → Freeze → Convert → Validate → Cleanup, with this step's
 * stage active, or null when the step has no place on it. The freeze is optional: past it, it reads
 * "optional" and is never marked done, because nothing says it ran.
 */
export function cardSpine(kind: string | null | undefined): StepperStep[] | null {
  const active = SPINE_STAGE[(kind ?? '').toLowerCase()];
  if (!active) return null;
  const at = SPINE.findIndex((s) => s.id === active);
  return SPINE.map((s, i): StepperStep => {
    if (i === at) return { id: s.id, label: s.label, sublabel: 'you are here', status: 'active' };
    if (s.id === 'freeze') return { id: s.id, label: s.label, sublabel: 'optional', status: 'pending' };
    return { id: s.id, label: s.label, status: i < at ? 'done' : 'pending' };
  });
}

const SIZING_BLOCKS: readonly CardBlock[] = ['time', 'recommendedCompute', 'blastRadius', 'upcomingConvert'];

/** The closed Details row's hint: what opening it shows, e.g. "Job, owners, sizing, waves, follow-ups". */
export function detailsSummary({ details }: CardLayout): string {
  const parts = ['Job', 'owners'];
  if (details.some((b) => SIZING_BLOCKS.includes(b))) parts.push('sizing');
  if (details.includes('convertWaves')) parts.push('waves');
  if (details.includes('followUps')) parts.push('follow-ups');
  return parts.join(', ');
}

export type MutationTone = 'mutates' | 'metadata' | 'readonly';

export interface MutationDescriptor {
  /** Short uppercase badge label. */
  label: string;
  /** Colour tone the badge renders in (warning / info / success). */
  tone: MutationTone;
}

// Kinds that change only REVERSIBLE METADATA (Authorize GRANT / REVOKE, PO enable/disable, stream
// stop/restart, PO / auto-liquid enable) — never table DATA and never SET MANAGED. Badging these
// "Changes no tables" reads as "does nothing", which for the Authorize/freeze/unfreeze cards is the
// opposite of true.
const _METADATA_KINDS = new Set(['lease', 'freeze', 'unfreeze', 'optimize']);

/**
 * How a step's mutation character should be badged (pure). `mutates`
 * (migration_next_actions.mutates) is authoritative for the SET MANAGED case, so a new mutating
 * kind can never be mislabelled as safe. Otherwise the reversible-metadata kinds are called out
 * honestly (they DO change metadata), and everything else is genuinely read-only.
 */
export function mutationBadge(
  kind: string | null | undefined,
  mutates: boolean,
): MutationDescriptor {
  if (mutates) return { label: 'Mutates tables', tone: 'mutates' };
  if (_METADATA_KINDS.has((kind ?? '').toLowerCase())) {
    return { label: 'Reversible metadata change', tone: 'metadata' };
  }
  return { label: 'Changes no data', tone: 'readonly' };
}

export interface JourneyInput {
  /** migration_next_actions.next_step_kind. Null when no recommendation yet. */
  kind: string | null | undefined;
  /** wave_status.wave_state for the selected wave. Used only when kind is absent. */
  waveState?: string | null;
  /**
   * The rail is a monotone funnel — each stage carries a REAL, cumulative estate count so the
   * numbers only ever move forward (discovered ≥ planned ≥ converted ≥ validated) and never
   * regress after an action. All five counts come precomputed off `migration_next_actions`:
   *   - `discovered`  = estate_discovered_tables — the whole discovered inventory (Profiled).
   *   - `planned`     = estate_total_tables — the planned estate, wave_plan ⋈ manifest (Planned).
   *   - `frozenOwed`  = estate_open_pre_convert — freeze/stop actions still OWED before convert
   *                     (Freezing). This is the optional bracket's remaining work, so it ticks
   *                     DOWN as actions close (0 ⇒ the stage reads "optional").
   *   - `converted`   = estate_migrated_tables — CUMULATIVE converted (CONVERTED+VALIDATED+CLEANED),
   *                     NOT the transient CONVERTED-only bucket, so Converting ≥ Validated always.
   *   - `validated`   = estate_validated_tables — VALIDATED+CLEANED (Validated).
   */
  discovered: number;
  planned: number;
  frozenOwed: number | string;
  converted: number;
  validated: number;
}

/**
 * Overview journey rail. Driven by next_step_kind (primary) and wave_state (fallback
 * only). Freeze is skippable: it is never "current" just because nothing has converted.
 */
/** The freezing stage's sublabel: the owed freeze/stop ACTIONS (not tables), or "optional". */
function owedLabel(owed: number): string {
  if (owed <= 0) return 'optional';
  return owed === 1 ? '1 item to pause' : `${owed} items to pause`;
}

export function journeySteps(input: JourneyInput): StepperStep[] {
  const current = resolvePhase(input);
  return [
    {
      id: 'profiled',
      label: 'Profiled',
      status: statusOf('profiled', current, input.kind),
      sublabel: input.discovered > 0 ? `${input.discovered} discovered` : undefined,
    },
    {
      id: 'planned',
      label: 'Planned',
      status: statusOf('planned', current, input.kind),
      sublabel: input.planned > 0 ? `${input.planned} planned` : undefined,
    },
    {
      id: 'freezing',
      label: 'Freezing',
      status: freezeStatusFor(current),
      // Optional bracket: show the freeze/stop work still OWED (ticks down as actions close);
      // "optional" when nothing is owed. Never asserts "skipped" — a freeze may well have run,
      // and there is no estate-level "freeze ran" signal to prove otherwise.
      sublabel: owedLabel(num(input.frozenOwed)),
    },
    {
      id: 'converting',
      label: 'Converting',
      status: statusOf('converting', current, input.kind),
      sublabel: `${input.converted} converted`,
    },
    {
      id: 'validated',
      label: 'Validated',
      status: statusOf('validated', current, input.kind),
      sublabel: `${input.validated} validated`,
    },
    {
      id: 'finishing',
      label: 'Finishing',
      status: statusOf('finishing', current, input.kind),
      sublabel: FINISH_SUBLABEL[(input.kind ?? '').toLowerCase()],
    },
  ];
}

/** The closed journey row's hint, e.g. "Now at: Converting · 4 of 6"; null when no stage is active. */
export function journeyNow(steps: readonly StepperStep[]): string | null {
  const i = steps.findIndex((s) => s.status === 'active');
  return i < 0 ? null : `Now at: ${steps[i].label} · ${i + 1} of ${steps.length}`;
}

function resolvePhase(input: JourneyInput): JourneyPhase {
  const kind = (input.kind ?? '').toLowerCase();
  // Lease shares the prep bracket with freeze — both transfer only reversible metadata before
  // the convert. The rail label stays "Freezing"; the chip title/directive carry the exact
  // ownership-lease wording (from the conductor).
  if (kind === 'freeze' || kind === 'lease') return 'freezing';
  if (CONVERTING_KINDS.has(kind)) return 'converting';
  if (FINISHING_KINDS.has(kind)) return 'finishing';
  if (VALIDATED_KINDS.has(kind)) return 'validated';
  if (PLANNED_KINDS.has(kind)) return 'planned';
  if (kind) return 'planned';

  // No recommendation: wave_state is the only engine signal. Never infer freeze from
  // converting == 0.
  const ws = (input.waveState ?? '').toUpperCase();
  if (ws === 'AWAITING_ACTIONS') return 'freezing';
  // Validated wins BEFORE converting: `converted` is now the CUMULATIVE migrated count, so a
  // fully-migrated-and-validated estate (converted >= planned, validated > 0) must resolve to
  // 'validated' rather than being short-circuited to 'converting' by the `converted > 0` check.
  if (ws === 'DONE' || (input.validated > 0 && input.converted >= input.planned && input.planned > 0)) {
    return 'validated';
  }
  if (ws === 'IN_PROGRESS' || input.converted > 0) return 'converting';
  if (input.planned > 0) return 'planned';
  return 'profiled';
}

function freezeStatusFor(current: JourneyPhase): StepperStep['status'] {
  if (current === 'freezing') return 'active';
  // Optional slot: never mark done just because convert is next (that claimed freeze ran).
  return 'pending';
}

function statusOf(
  phase: JourneyPhase,
  current: JourneyPhase,
  kind: string | null | undefined,
): StepperStep['status'] {
  const k = (kind ?? '').toLowerCase();
  if (k === 'done') return 'done';
  if (phase === current) return k === 'blocked' ? 'blocked' : 'active';
  return phaseIndex(phase) < phaseIndex(current) ? 'done' : 'pending';
}

function phaseIndex(p: JourneyPhase): number {
  return PHASE_ORDER.indexOf(p);
}
