/**
 * gateFlow.pure — the five-stage process-flow model for the Convert gate (SPEC Screen 2).
 * Pure: no React, no Spark, no network. Mirrors pages/overview/journey.pure.ts — a step model
 * the rail renders. Deliberately takes primitives (not the dialog's WaveCoverage/GateState
 * types) so it stays decoupled and trivially testable; the two adapters below bridge the
 * dialog's prop shapes to those primitives so that derivation is unit-tested too.
 *
 * The rail reads: ① Readiness → ② Permissions → ③ Freeze → ④ Convert → ⑤ After. Each stage
 * carries an INTRINSIC status (done ✓ / attention ! / current ● / pending); which stage is
 * SELECTED is separate dialog state, not a status.
 */

export type GateStageId = 'readiness' | 'permissions' | 'freeze' | 'convert' | 'after';
export type GateStageStatus = 'done' | 'attention' | 'current' | 'pending';

export interface GateStage {
  id: GateStageId;
  label: string;
  sublabel?: string;
  status: GateStageStatus;
}

/** The gate's convert-permission state: unevaluated = not read yet, so it needs an acknowledgement. */
export type PermissionSignal = 'covered' | 'gap' | 'unevaluated';

export interface GateFlowInput {
  /** true = dry-run passed; false = blockers; null = evaluated-not-passed/unknown;
   *  undefined = not applicable (multi-wave / no readiness line). */
  readinessPassed: boolean | null | undefined;
  /** Convert permission from the stored convert_authority verdicts; unevaluated = not read yet
   *  (loading or failed); undefined = not applicable (nothing to convert / multi-wave). */
  ownershipSignal: PermissionSignal | undefined;
  /** The permission read failed (vs. still loading), for the unevaluated sublabel. */
  permissionReadFailed?: boolean;
  /** Tables the convert identity is not yet authorized for, for the sublabel ("10 not authorized"). */
  permissionNotAuthorized?: number;
  /** Grants Authorize applied but could not verify (the rail reads "grant not verified" when none is owed). */
  permissionUnverified?: number;
  /** Freeze-bracket coverage verdict (compaction + stream + writer folded by gateState). */
  freezeState: 'held' | 'gap' | 'unevaluated';
  /** Whether the scope has attributed streams (changes the After stage sublabel). */
  isStreaming: boolean;
  /** A freeze bracket is already applied for this scope (freeze_ledger FROZEN rows). */
  frozenApplied?: boolean;
  /** How many entities are currently frozen (for the "N paused" sublabel). */
  frozenCount?: number;
  /** How many gate requirements are still outstanding (unmet acks + type-to-confirm), for the
   *  Convert node badge. Undefined/0 → the neutral "runs SET MANAGED" sublabel. */
  outstandingCount?: number;
}

function readinessStatus(passed: boolean | null | undefined): {
  status: GateStageStatus;
  sublabel?: string;
} {
  if (passed === undefined) return { status: 'pending' };
  if (passed === true) return { status: 'done', sublabel: 'passed' };
  if (passed === false) return { status: 'attention', sublabel: 'blockers' };
  return { status: 'attention', sublabel: 'not checked' }; // null
}

function permissionsStatus(
  signal: PermissionSignal | undefined,
  notAuthorized?: number,
  unverified?: number,
  readFailed?: boolean,
): { status: GateStageStatus; sublabel?: string } {
  if (signal === undefined) return { status: 'pending' };
  if (signal === 'covered') return { status: 'done', sublabel: 'authorized' };
  if (signal === 'unevaluated') return { status: 'attention', sublabel: readFailed ? 'could not read' : 'checking…' };
  return {
    status: 'attention',
    sublabel: notAuthorized && notAuthorized > 0
      ? `${notAuthorized} not authorized`
      : unverified && unverified > 0 ? 'grant not verified' : 'not authorized',
  };
}

function freezeStatus(
  state: 'held' | 'gap' | 'unevaluated',
  frozenApplied?: boolean,
  frozenCount?: number,
): {
  status: GateStageStatus;
  sublabel?: string;
} {
  if (frozenApplied) return { status: 'done', sublabel: `${frozenCount ?? 0} paused` };
  if (state === 'held') return { status: 'done', sublabel: 'frozen' };
  if (state === 'gap') return { status: 'attention', sublabel: 'not frozen yet' };
  return { status: 'pending', sublabel: 'not checked' };
}

export function gateStages(input: GateFlowInput): GateStage[] {
  const r = readinessStatus(input.readinessPassed);
  const p = permissionsStatus(
    input.ownershipSignal,
    input.permissionNotAuthorized,
    input.permissionUnverified,
    input.permissionReadFailed,
  );
  const f = freezeStatus(input.freezeState, input.frozenApplied, input.frozenCount);
  return [
    { id: 'readiness', label: 'Readiness', status: r.status, sublabel: r.sublabel },
    { id: 'permissions', label: 'Permissions', status: p.status, sublabel: p.sublabel },
    { id: 'freeze', label: 'Freeze', status: f.status, sublabel: f.sublabel },
    {
      id: 'convert',
      label: 'Convert',
      status: 'current',
      sublabel: (input.outstandingCount ?? 0) > 0 ? `${input.outstandingCount} to confirm` : 'runs SET MANAGED',
    },
    {
      id: 'after',
      label: 'After',
      status: 'pending',
      sublabel: input.isStreaming ? 'streams restart' : 'auto-resumes',
    },
  ];
}

/** Which stage the gate lands on when opened: surface the most common blocker first. */
export function defaultGateStageId(input: GateFlowInput): GateStageId {
  if (input.freezeState === 'gap') return 'freeze';
  if (input.ownershipSignal === 'gap' || input.ownershipSignal === 'unevaluated') return 'permissions';
  if (input.readinessPassed === false || input.readinessPassed === null) return 'readiness';
  // An already-applied freeze (no higher-priority gap above) sends the operator on to convert.
  if (input.frozenApplied) return 'convert';
  return 'convert';
}

/** Tailwind tone classes for a rail node, by intrinsic status. */
export function stageToneClass(status: GateStageStatus): string {
  switch (status) {
    case 'done':
      return 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]';
    case 'attention':
      return 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]';
    case 'current':
      return 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]';
    default:
      return 'border-border bg-muted text-muted-foreground';
  }
}

/** The node marker: ✓ done, ! attention, ● current, else the 1-based index. */
export function stageMarker(status: GateStageStatus, index: number): string {
  if (status === 'done') return '✓';
  if (status === 'attention') return '!';
  if (status === 'current') return '●';
  return String(index + 1);
}

/** Adapter: the dialog's `readiness` prop → the tri-state passed flag gateStages expects.
 *  undefined (line not shown) stays undefined; null (unevaluated) stays null; else passed===true. */
export function readinessPassedFrom(
  readiness: { passed?: boolean | null } | null | undefined,
): boolean | null | undefined {
  if (readiness === undefined) return undefined;
  if (readiness === null) return null;
  return readiness.passed === true;
}

/** Adapter: the permission view's signal → the gate's. `none` (nothing to convert) is not
 *  applicable; no view at all is unevaluated while a preflight applies (it is loading or failed),
 *  so an unread permission is never mistaken for "nothing to check". */
export function ownershipSignalFrom(
  signal: string | null | undefined,
  opts: { preflightApplies?: boolean } = {},
): PermissionSignal | undefined {
  if (signal === 'gap' || signal === 'covered' || signal === 'unevaluated') return signal;
  if (signal == null && opts.preflightApplies) return 'unevaluated';
  return undefined;
}
