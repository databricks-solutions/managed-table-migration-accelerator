/**
 * estateMap.pure — the view-state behind the landing's bird's-eye "estate map". Pure and
 * deterministic (no React) so the graphic stays dumb: it renders whatever `viewForStep(i)`
 * returns for the active scroll step. Encodes the control-plane truths as visual state:
 *   - which actor lane(s) are responsible at each step,
 *   - where the table-dot sits on the flow spine,
 *   - how far the lifecycle state ribbon has filled (monotonic),
 *   - whether the freeze bracket is clamped around convert (and released after),
 *   - whether the Authorize bracket is clamped (a WIDER hold — its grants kept through the rollback
 *     window and released only at cleanup, unlike freeze which releases at validate),
 *   - whether the fails-closed gate is still blocked or has opened,
 *   - whether the 14-day rollback branch is visible.
 */

export type Actor = 'operator' | 'app' | 'job' | 'control';
export type Stage = 'discover' | 'plan' | 'prepare' | 'convert' | 'validate' | 'cleanup';
export type Lifecycle =
  | 'DISCOVERED'
  | 'DRY_RUN_OK'
  | 'CONVERTING'
  | 'CONVERTED'
  | 'VALIDATED'
  | 'CLEANED';

/** The six spine nodes, in execution order (the dot travels along these). */
export const SPINE: readonly Stage[] = [
  'discover',
  'plan',
  'prepare',
  'convert',
  'validate',
  'cleanup',
] as const;

/** Lifecycle order — used to prove the ribbon only ever advances. */
export const LIFECYCLE_ORDER: readonly Lifecycle[] = [
  'DISCOVERED',
  'DRY_RUN_OK',
  'CONVERTING',
  'CONVERTED',
  'VALIDATED',
  'CLEANED',
] as const;

export interface StepView {
  activeStage: Stage;
  activeLanes: Actor[];
  /** Monotonic across steps. */
  ribbonReached: Lifecycle;
  bracket: 'none' | 'clamped' | 'released';
  /**
   * The Authorize bracket. WIDER than `bracket`: its grants are held from prepare through validate
   * (the whole rollback window, since UNSET MANAGED needs the same permission) and revoked only at cleanup.
   */
  leaseBracket: 'none' | 'clamped' | 'released';
  gate: 'idle' | 'blocked' | 'open';
  /** Only within the 14-day window (after convert, before cleanup). */
  showRollbackBranch: boolean;
}

/**
 * The seven narrative steps of the landing scrolly, as view-state. Narrative COPY lives in the
 * page; this is only what the graphic should show. Order:
 * Discover -> Plan -> Prepare+Freeze+Authorize -> Convert -> Validate -> Rollback net -> Clean up.
 * Note the two brackets diverge on release: `bracket` (freeze) releases at validate; `leaseBracket`
 * stays clamped through validate and releases only at cleanup (the grants are held for rollback).
 */
const STEP_VIEWS: readonly StepView[] = [
  { activeStage: 'discover', activeLanes: ['operator', 'app', 'job'], ribbonReached: 'DISCOVERED', bracket: 'none', leaseBracket: 'none', gate: 'idle', showRollbackBranch: false },
  { activeStage: 'plan', activeLanes: ['operator', 'job', 'control'], ribbonReached: 'DISCOVERED', bracket: 'none', leaseBracket: 'none', gate: 'idle', showRollbackBranch: false },
  { activeStage: 'prepare', activeLanes: ['operator', 'app', 'job'], ribbonReached: 'DRY_RUN_OK', bracket: 'clamped', leaseBracket: 'clamped', gate: 'blocked', showRollbackBranch: false },
  { activeStage: 'convert', activeLanes: ['operator', 'app', 'job'], ribbonReached: 'CONVERTED', bracket: 'clamped', leaseBracket: 'clamped', gate: 'open', showRollbackBranch: true },
  { activeStage: 'validate', activeLanes: ['app', 'job'], ribbonReached: 'VALIDATED', bracket: 'released', leaseBracket: 'clamped', gate: 'open', showRollbackBranch: true },
  { activeStage: 'validate', activeLanes: ['operator', 'app', 'job'], ribbonReached: 'VALIDATED', bracket: 'released', leaseBracket: 'clamped', gate: 'open', showRollbackBranch: true },
  { activeStage: 'cleanup', activeLanes: ['app', 'job'], ribbonReached: 'CLEANED', bracket: 'released', leaseBracket: 'released', gate: 'open', showRollbackBranch: false },
];

export const STEP_COUNT = STEP_VIEWS.length;

const clampIndex = (i: number): number => Math.max(0, Math.min(STEP_VIEWS.length - 1, Math.trunc(i)));

/** View-state for a narrative step (index is clamped into range). */
export function viewForStep(i: number): StepView {
  return STEP_VIEWS[clampIndex(i)];
}

/** Left offset (%) of the table-dot for a stage — centred over its spine node. */
export function dotLeftPercent(stage: Stage): number {
  const idx = SPINE.indexOf(stage);
  const safe = idx < 0 ? 0 : idx;
  return ((safe + 0.5) / SPINE.length) * 100;
}

/** Ribbon fill fraction (0..1) for a reached lifecycle state. */
export function ribbonFill(state: Lifecycle): number {
  const idx = LIFECYCLE_ORDER.indexOf(state);
  const safe = idx < 0 ? 0 : idx;
  return (safe + 1) / LIFECYCLE_ORDER.length;
}
