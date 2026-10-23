/**
 * Pure reducers for the guided freeze-step card (northstar Screen 4). Consume the
 * `freeze_plan_for_scope` rows (Phase 0 view) and derive the downtime read, the ordered
 * "what pauses" groups, the auto-pausable affected list (for the confirm dialog), and the
 * consumers to warn. No React, no network — unit-tested in freezeCard.pure.test.ts.
 */
export interface FreezePlanRow {
  full_name: string;
  entity_kind: string;
  entity_id: string | null;
  entity_name: string | null;
  op_kind: string | null;
  next_scheduled_run_at: string | null;
  safety_class: string;
  operation_classes?: string | null;
}

const SAFE_WINDOW = 'SAFE_WINDOW';
const DOWNTIME = 'DOWNTIME_UNTIL_UNFREEZE';
const WONT_RUN = 'WONT_RUN';
const MANUAL = 'MANUAL';
const UNKNOWN = 'UNKNOWN';

export function parseScopeSelector(
  selector: string | null,
): { ok: boolean; scopeCatalog: string; scopeSchema: string; label: string } {
  const parts = (selector ?? '').split('.');
  if (parts.length === 2 && parts[0] && parts[1]) {
    return { ok: true, scopeCatalog: parts[0], scopeSchema: parts[1], label: selector as string };
  }
  return { ok: false, scopeCatalog: '', scopeSchema: '', label: selector ?? '' };
}

export function freezeDowntimeSummary(
  rows: FreezePlanRow[],
  windowMin: number | null,
): { windowMin: number | null; nSafe: number; nHeadsUp: number; nWontRun: number; nManual: number; nUnknown: number; headsUpNames: string[] } {
  const by = (cls: string) => rows.filter((r) => r.safety_class === cls);
  return {
    windowMin,
    nSafe: by(SAFE_WINDOW).length,
    nHeadsUp: by(DOWNTIME).length,
    nWontRun: by(WONT_RUN).length,
    nManual: by(MANUAL).length,
    nUnknown: by(UNKNOWN).length,
    headsUpNames: by(DOWNTIME).map((r) => r.entity_name ?? r.full_name),
  };
}

const _GROUP_ORDER: { kind: string; match: (r: FreezePlanRow) => boolean }[] = [
  { kind: 'Maintenance jobs (OPTIMIZE / VACUUM)', match: (r) => r.entity_kind === 'COMPACTION_JOB' },
  { kind: 'Predictive Optimization', match: (r) => r.entity_kind === 'PO' },
  { kind: 'DLT pipelines', match: (r) => r.entity_kind === 'PIPELINE' },
  { kind: 'Structured Streaming', match: (r) => r.entity_kind === 'STREAM' },
  { kind: 'Writer jobs (scheduled / event-triggered)', match: (r) => r.entity_kind === 'WRITER_JOB' },
  { kind: "Manual — stop these yourself (freeze can't pause a person)", match: (r) => r.safety_class === MANUAL },
  { kind: 'Unclassified (review manually)', match: (r) => r.safety_class === UNKNOWN },
];

const _ADHOC_KINDS = new Set(['MANUAL_COMPACTION', 'UNKNOWN_COMPACTION', 'ADHOC_WRITER']);

function maintenanceClasses(operationClasses: string | null | undefined): { optimize: boolean; vacuum: boolean } {
  const set = new Set((operationClasses ?? '').split(',').map((c) => c.trim().toUpperCase()).filter(Boolean));
  return { optimize: set.has('COMPACTION'), vacuum: set.has('VACUUM') };
}

/** Human label for a freeze-plan `entity_kind` — the OPERATION family. `op_kind` is NULL for
 *  manual/unknown actors (there is no auto freeze op), so the operation MUST come from
 *  `entity_kind`, never from `op_kind`. Unrecognized kinds fall back to the raw value.
 *  `operationClasses` (the view's comma-joined `COMPACTION`/`VACUUM`) names which maintenance a
 *  compaction-arm actor runs; when absent, a COMPACTION_JOB reads as a generic maintenance job. */
export function whatPausesKindLabel(entityKind: string | null, operationClasses?: string | null): string {
  const { optimize, vacuum } = maintenanceClasses(operationClasses);
  switch ((entityKind ?? '').toUpperCase()) {
    case 'PO':
      return 'Predictive Optimization';
    case 'COMPACTION_JOB':
      if (optimize && vacuum) return 'OPTIMIZE + VACUUM job';
      if (vacuum) return 'VACUUM job';
      if (optimize) return 'OPTIMIZE job';
      return 'Maintenance job (OPTIMIZE/VACUUM)';
    case 'MANUAL_COMPACTION':
      if (optimize && vacuum) return 'Ad-hoc OPTIMIZE + VACUUM';
      if (vacuum) return 'Ad-hoc VACUUM';
      return 'Ad-hoc OPTIMIZE/compaction';
    case 'UNKNOWN_COMPACTION':
      if (optimize && vacuum) return 'OPTIMIZE + VACUUM (unclassified)';
      if (vacuum) return 'VACUUM (unclassified)';
      return 'Compaction (unclassified)';
    case 'PIPELINE':
      return 'DLT pipeline';
    case 'STREAM':
      return 'Streaming job';
    case 'WRITER_JOB':
      return 'Writer job';
    case 'ADHOC_WRITER':
      return 'Ad-hoc writer';
    default:
      return (entityKind ?? '').trim() || 'Attributed writer';
  }
}

/** One legible "what pauses" line. Leads with the OPERATION and the TABLE (`full_name` is NEVER
 *  hidden) and attributes the actor (job id / person) SECONDARILY. This fixes the manual-compaction
 *  line that used to render as a bare actor identity — e.g. "You must stop yourself → your email"
 *  with no table and no operation. For ad-hoc/manual actors the person reads as "last run by …". */
export function whatPausesItemLabel(row: FreezePlanRow): string {
  const kind = whatPausesKindLabel(row.entity_kind, row.operation_classes);
  const table = row.full_name;
  let line = `${kind} · ${table}`;
  const actor = row.entity_name;
  if (actor && actor !== table && actor !== kind) {
    const isAdhoc = _ADHOC_KINDS.has((row.entity_kind ?? '').toUpperCase());
    line += isAdhoc ? ` — last run by ${actor}` : ` — ${actor}`;
  }
  if (row.op_kind) line += ` · ${row.op_kind}`;
  if (row.next_scheduled_run_at) line += ` · next run ${row.next_scheduled_run_at}`;
  return line;
}

export function whatPausesGroups(rows: FreezePlanRow[]): { kind: string; items: FreezePlanRow[] }[] {
  const groups = _GROUP_ORDER.map((g) => ({ kind: g.kind, match: g.match, items: [] as FreezePlanRow[] }));
  for (const row of rows) {
    const group = groups.find((g) => g.match(row));
    if (group) {
      group.items.push(row);
    }
  }
  return groups
    .map(({ kind, items }) => ({ kind, items }))
    .filter((g) => g.items.length > 0);
}

export function willPauseAffected(rows: FreezePlanRow[]): { count: number; names: string[] } {
  const auto = rows.filter((r) => r.safety_class === SAFE_WINDOW || r.safety_class === DOWNTIME || r.safety_class === WONT_RUN);
  return { count: auto.length, names: auto.map((r) => r.entity_name ?? r.full_name) };
}

export function headsUpNotes(rows: FreezePlanRow[]): string[] {
  return rows.filter((r) => r.safety_class === DOWNTIME).map((r) => r.entity_name ?? r.full_name);
}

/** Card posture derived from the plan rows: does freezing actually DO anything here?
 *  - FREEZABLE   — ≥1 auto-pausable entity (willPauseAffected > 0): freeze is the real step.
 *  - MANUAL_ONLY — 0 auto-pausable but rows exist (all MANUAL/UNKNOWN): freeze is a no-op; the
 *                  hold is a manual action the operator must resolve, not something freeze pauses.
 *  - EMPTY       — nothing attributed to this scope at all: freezing changes nothing.
 *  Never assume "safe": an UNKNOWN-only scope is MANUAL_ONLY (review), never FREEZABLE (SR7). */
export type FreezePosture = 'FREEZABLE' | 'MANUAL_ONLY' | 'EMPTY';

export function freezeCardPosture(rows: FreezePlanRow[]): FreezePosture {
  if (willPauseAffected(rows).count > 0) return 'FREEZABLE';
  if (rows.length > 0) return 'MANUAL_ONLY';
  return 'EMPTY';
}

/**
 * What the guided Freeze card's PRIMARY CTA should be, by posture. Freeze can only pause
 * auto-pausable actors (jobs / Predictive Optimization / pipelines / streams). When a scope has
 * none, leading with "Freeze anyway" is misleading:
 *   - FREEZABLE   -> lead with the real freeze; not demoted.
 *   - MANUAL_ONLY -> lead with "Resolve open actions" (runbook); freeze stays only as a demoted
 *                    escape hatch (it changes nothing, but the operator may still want it).
 *   - EMPTY       -> lead with "Continue to the convert gate"; do NOT offer freeze at all.
 * Pure so the CTA choice is unit-tested, not buried in JSX.
 */
export type FreezeCtaPrimary = 'FREEZE' | 'RESOLVE_ACTIONS' | 'CONTINUE';

export interface FreezeCtaModel {
  primary: FreezeCtaPrimary;
  showFreeze: boolean; // is the freeze trigger rendered at all?
  freezeDemoted: boolean; // if shown, is it de-emphasized (ghost/outline) rather than primary?
}

export function freezeCtaModel(posture: FreezePosture): FreezeCtaModel {
  if (posture === 'FREEZABLE') {
    return { primary: 'FREEZE', showFreeze: true, freezeDemoted: false };
  }
  if (posture === 'MANUAL_ONLY') {
    return { primary: 'RESOLVE_ACTIONS', showFreeze: true, freezeDemoted: true };
  }
  return { primary: 'CONTINUE', showFreeze: false, freezeDemoted: true };
}

/** Link from the guided freeze card to the Freeze page's PLANNED view, scoped to one
 *  `catalog.schema`. FreezePage reads `?scope` and previews `freeze_plan_for_scope` for it. */
export function freezePlanHref(label: string): string {
  return `/freeze?scope=${encodeURIComponent(label)}`;
}

/** Tone for a freeze-plan review pill (maps to a tint class at the render edge). */
export type FreezePillTone = 'safe' | 'downtime' | 'wontRun' | 'manual' | 'unknown';

/** Map a `freeze_plan` `safety_class` to a short review pill (label + tone). Mirrors the
 *  Phase-0 contract values; any unrecognized value is `unknown` — never silently "safe" (SR7). */
export function freezePlanRowPill(safetyClass: string): { label: string; tone: FreezePillTone } {
  switch (safetyClass) {
    case SAFE_WINDOW:
      return { label: 'Safe', tone: 'safe' };
    case DOWNTIME:
      return { label: 'Downtime until unfreeze', tone: 'downtime' };
    case WONT_RUN:
      return { label: "Won't run", tone: 'wontRun' };
    case MANUAL:
      return { label: 'Manual — you stop it', tone: 'manual' };
    case UNKNOWN:
    default:
      return { label: 'Unclassified — review', tone: 'unknown' };
  }
}
