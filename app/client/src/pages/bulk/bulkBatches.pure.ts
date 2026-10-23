/**
 * Pure bulk-campaign readiness rollup + next-step recommendation (WS5.B B0).
 *
 * A faithful TypeScript port of the backend WS5.0 module `migration/parallelism.py`
 * (`batch_readiness`, `batch_finish_step`, `next_bulk_step`, `recommended_batch`), kept
 * behaviorally identical so the client-side Bulk Convert page and the `11_bulk_cockpit`
 * notebook agree table-for-table. This is the
 * READ side only: it reduces `wave_parallel_groups` rows joined to `wave_status` into one
 * rollup per parallel batch and derives the operator's next step. It never sequences or
 * mutates — the operator confirms and launches the returned step.
 *
 * No React/AppKit/network/clock — plain data in, plain data out.
 */

import { preconvertReaderWarning } from '../../lib/compatOpenApis.pure';
import { waveFinishStep, type FinishKind } from '../../lib/finishScope.pure';
import { unverifiedLeaseText } from '../../lib/leaseNote.pure';

/** A raw cell from a SQL query row: values arrive loosely typed and must be coerced. */
type Cell = string | number | boolean | null | undefined;

/**
 * One `wave_parallel_groups` row (one wave). Numeric fields may arrive as strings from the
 * query layer, so they are typed as `Cell` and coerced with the `_i` int helper on read.
 */
export interface BulkGroupRow {
  plan_run_id: string | null;
  wave_id: string;
  wave_order: Cell;
  bucket: string | null;
  group_key?: string | null;
  batch_id: string;
  batch_order: Cell;
  parallel_degree: Cell;
  blocked_from_parallel: Cell;
  table_count: Cell;
  size_bytes: Cell;
  est_copy_min: Cell;
  max_downtime_min: Cell;
}

/** One `wave_status` row, joined to a group row on `(plan_run_id, wave_id)`. */
export interface WaveStatusRow {
  plan_run_id: string | null;
  wave_id: string;
  wave_order?: Cell;
  bucket?: string | null;
  wave_state: string | null;
  /** Every active table validated / cleaned / converted_external — counts as validated for bulk. */
  fully_migrated?: Cell;
  open_pre_convert: Cell;
  /** Freeze/stop holds the freeze bracket clears. `open_pre_convert` minus auto and manual is the lease. */
  open_pre_convert_auto?: Cell;
  /** Manual holds (the count already excludes LEASE_OWNERSHIP). */
  open_pre_convert_manual?: Cell;
  /** LEASE_OWNERSHIP actions 03d applied but could not verify (IN_PROGRESS); a re-run closes them. */
  open_pre_convert_lease_unverified?: Cell;
  open_post_convert: Cell;
  open_blocking: Cell;
  open_lease: Cell;
  /** wave_status table-state counts (resume + started) and the one-wave finish columns. */
  total_tables?: Cell;
  dry_run_ok?: Cell;
  converting?: Cell;
  converted?: Cell;
  validated?: Cell;
  cleaned?: Cell;
  converted_external?: Cell;
  /** Tables rolled back through 06 (EXTERNAL again); they need a fresh 03 dry run. */
  rolled_back?: Cell;
  open_post_convert_holding?: Cell;
  open_reshare?: Cell;
  open_compat?: Cell;
  optimize_pending?: Cell;
  /** Tables still to convert whose external readers have no decision / need cross-engine ABAC. */
  readers_undecided?: Cell;
  readers_fgac_required?: Cell;
  /** F1 schedule-safety: the wave's rolled-up writer verdict + earliest scheduled write. */
  wave_writer_verdict?: string | null;
  wave_next_scheduled_write_at?: string | null;
}

/** One rollup per parallel batch. Field names mirror the backend dict (snake_case). */
export interface BatchRollup {
  plan_run_id: string | null;
  batch_id: string;
  batch_order: number;
  parallel_degree: number;
  bucket: string | null;
  member_wave_ids: string[];
  table_count: number;
  size_bytes: number;
  est_copy_min: number;
  max_downtime_min: number;
  ready_count: number;
  total_count: number;
  open_pre_convert: number;
  /** Pre-convert holds the freeze owes (auto + manual; the raw count for a row without the split). */
  open_pre_convert_freeze?: number;
  /** Open LEASE_OWNERSHIP actions over members still to convert — closed by 03d, not the freeze. */
  open_pre_convert_lease?: number;
  /** Of those, the ones 03d applied but could not verify, over members still to convert. */
  open_pre_convert_lease_unverified?: number;
  open_post_convert: number;
  open_blocking: number;
  open_lease: number;
  /** open_lease summed over members that still have tables to convert (drives the lease step). */
  open_lease_to_convert: number;
  /** Tables currently in state CONVERTED (converted, not yet validated), summed over members. */
  converted_unvalidated: number;
  waves_need_freeze: number;
  convert_ready: boolean;
  all_validated: boolean;
  open_freeze: number;
  /** Any member has begun converting (or finished). */
  started: boolean;
  /** Started, no BLOCKED member, and a member still has DRY_RUN_OK/CONVERTING tables. */
  resume_convert: boolean;
  /** Earliest finish step any member owes (only once every member is validated). */
  finish_step: BulkStepValue | null;
  /** Members with rolled-back tables, in member order. Non-empty routes to the dry run. */
  rolled_back_wave_ids?: string[];
  rolled_back_tables?: number;
  /** One `job_03_dry_run` scope per rolled-back schema (deduped; never estate-wide). */
  dry_run_scopes?: DryRunScope[];
  /** External-reader advisory counts summed over members (never a hold). */
  readers_undecided?: number;
  readers_fgac_required?: number;
  readers_attention?: number;
  /** F1: worst (most-restrictive) member writer verdict + the batch's earliest scheduled write. */
  batch_writer_verdict: string;
  batch_next_scheduled_write_at: string | null;
  rollup: BatchRollupState;
}

export type BatchRollupState =
  | 'BLOCKED'
  | 'ROLLED_BACK'
  | 'AWAITING_ACTIONS'
  | 'DONE'
  | 'READY'
  | 'IN_PROGRESS';

/** The `job_03_dry_run` params for one rolled-back wave's schema (backend `dry_run_scopes`). */
export interface DryRunScope {
  wave_id: string;
  catalog_filter: string;
  schema_filter: string;
}

/** The ten bulk steps — string VALUES match the backend `STEP_BULK_*` constants exactly. */
export const BulkStep = {
  DRY_RUN: 'bulk_dry_run',
  FREEZE: 'bulk_freeze',
  LEASE: 'bulk_lease',
  CONVERT: 'bulk_convert',
  VALIDATE: 'bulk_validate',
  UNFREEZE: 'bulk_unfreeze',
  RESHARE: 'bulk_reshare',
  COMPAT: 'bulk_compat',
  OPTIMIZE: 'bulk_optimize',
  DONE: 'bulk_done',
} as const;

export type BulkStepValue = (typeof BulkStep)[keyof typeof BulkStep];

const READY = 'READY';
const DONE = 'DONE';
const BLOCKED = 'BLOCKED';

/** The one-wave finish order, applied to the whole batch (backend `BULK_FINISH_ORDER`). */
export const BULK_FINISH_ORDER: readonly BulkStepValue[] = [
  BulkStep.UNFREEZE,
  BulkStep.RESHARE,
  BulkStep.COMPAT,
  BulkStep.OPTIMIZE,
];

const FINISH_TO_BULK: Record<FinishKind, BulkStepValue> = {
  unfreeze: BulkStep.UNFREEZE,
  reshare: BulkStep.RESHARE,
  compat: BulkStep.COMPAT,
  optimize: BulkStep.OPTIMIZE,
};

const VERDICT_UNKNOWN = 'UNKNOWN';

/**
 * Writer schedule-safety verdict rank — a byte-for-byte mirror of the backend
 * `job_schedule._VERDICT_RANK` (the single source of truth): most-restrictive first, so the
 * MIN-rank member verdict governs a batch and the SAFEST (max-rank) sorts first for the
 * recommendation. Any unknown/absent verdict coalesces to UNKNOWN, never a fabricated SAFE_NOW.
 */
const VERDICT_RANK: Record<string, number> = {
  MUST_STOP: 0,
  WARN_WINDOW: 1,
  UNKNOWN: 2,
  SAFE_NOW: 3,
};

/**
 * Recommendation tier rank — a mirror of the backend `parallelism._DEFAULT_TIER_RANK`. Batch
 * order already encodes this tier sequence (pilot → normal → streaming → compat → share →
 * final_large); it is re-derived here so the schedule-safety tiebreak stays WITHIN a tier and a
 * schedule-safe batch can never jump a risk tier.
 */
const TIER_RANK: Record<string, number> = {
  pilot: 0,
  normal: 1,
  streaming: 2,
  compat: 3,
  share: 4,
  final_large: 5,
};

/**
 * The batch's ascending recommendation sort key for schedule safety, mirroring the backend
 * `job_schedule.schedule_safety_sort_key`. Primary: `-VERDICT_RANK` so SAFE_NOW (3 → -3) sorts
 * before UNKNOWN (-2), WARN_WINDOW (-1), MUST_STOP (0, last). Secondary: a LATER or absent next
 * fire is safer, so `null` → -Infinity (first), else `-fireEpochSeconds` (later fire → smaller →
 * first). Always applied AFTER the tier key in the caller so risk tiers/streaming isolation win.
 */
function scheduleSafetySortKey(
  verdict: string | null | undefined,
  nextScheduledWriteAt: string | null | undefined,
): [number, number] {
  const rank = VERDICT_RANK[(verdict ?? VERDICT_UNKNOWN).trim().toUpperCase()] ?? VERDICT_RANK[VERDICT_UNKNOWN];
  let secondary: number;
  if (nextScheduledWriteAt == null) {
    secondary = Number.NEGATIVE_INFINITY;
  } else {
    const ms = Date.parse(nextScheduledWriteAt);
    // An unparseable timestamp is treated as "no known fire" (safest), matching the Python
    // None branch rather than fabricating an ordering from NaN.
    secondary = Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : -(ms / 1000);
  }
  return [-rank, secondary];
}

/** Roll member verdicts to the worst (MIN-rank) and the earliest scheduled fire (MIN). */
function waveScheduleSafety(
  members: Array<{ verdict: string | null | undefined; nextAt: string | null | undefined }>,
): { verdict: string; nextAt: string | null } {
  let worstRank: number | null = null;
  let worstVerdict = VERDICT_UNKNOWN;
  let earliest: string | null = null;
  let earliestMs = Number.POSITIVE_INFINITY;
  for (const m of members) {
    const v = (m.verdict ?? VERDICT_UNKNOWN).trim().toUpperCase();
    const rank = VERDICT_RANK[v] ?? VERDICT_RANK[VERDICT_UNKNOWN];
    if (worstRank === null || rank < worstRank) {
      worstRank = rank;
      worstVerdict = v;
    }
    if (m.nextAt != null) {
      const ms = Date.parse(m.nextAt);
      if (!Number.isNaN(ms) && ms < earliestMs) {
        earliestMs = ms;
        earliest = m.nextAt;
      }
    }
  }
  return { verdict: worstVerdict, nextAt: earliest };
}

/**
 * Coerce a query cell to an int, mirroring the backend `_i` (Python `int()`): null/empty →
 * 0, numeric integer-strings → their int, real numbers → truncated toward zero. A float-
 * string ("3.7") or non-numeric value coerces to 0, matching Python's ValueError → 0 path.
 */
function _i(v: Cell): number {
  if (v === null || v === undefined) return 0;
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : 0;
  const t = v.trim();
  return /^[+-]?\d+$/.test(t) ? parseInt(t, 10) : 0;
}

/** A view boolean that may arrive as a real boolean or as a 'true'/'false' string (backend `_truthy`). */
function _b(v: Cell): boolean {
  if (typeof v === 'string') return ['true', '1'].includes(v.trim().toLowerCase());
  return Boolean(v);
}

/**
 * Coerce a query cell to a float, mirroring the backend `float(v or 0.0)` (falsy → 0).
 * Intentionally lenient: malformed input coerces to 0 rather than throwing. Python's
 * `float()` would raise, but the UI must degrade — one bad row must never crash the page.
 */
function _f(v: Cell): number {
  if (!v) return 0;
  const n = typeof v === 'number' ? v : parseFloat(String(v));
  return Number.isFinite(n) ? n : 0;
}

/**
 * Round to 2 decimals with bit-exact parity to Python's `round(x, 2)`.
 *
 * WHY: Python `round()` uses round-half-to-even (banker's rounding) on the value's exact
 * binary magnitude, whereas JS `Math.round`/`toFixed` round half-up/half-away. `toFixed(2)`
 * is correctly rounded on the true value for EVERY case except a value that is exactly
 * halfway at 2 decimals — which in IEEE-754 only happens for odd multiples of 1/8
 * (0.125, 0.375, …), where `toFixed` rounds up but Python rounds to even. We defer to
 * `toFixed` and override just those exact-half doubles to their even neighbor, matching
 * `parallelism.round(est_copy_min, 2)` so the app and the notebook agree to the last cent.
 */
function round2(x: number): number {
  const eighths = x * 8;
  if (Number.isInteger(eighths) && Math.abs(eighths) % 2 === 1) {
    // Exact binary half (x is an odd k/8): scaled-by-100 is an exact `.5`; pick the even side.
    const scaled = x * 100;
    const floor = Math.floor(scaled);
    return (floor % 2 === 0 ? floor : floor + 1) / 100;
  }
  return Number(x.toFixed(2));
}

/**
 * Code-point string comparison matching Python's `str` ordering (used as a sort tiebreak).
 * JS `localeCompare` is locale-sensitive and diverges from Python; plain `<`/`>` compares by
 * code unit, which equals code-point order for the ASCII identifiers used here.
 */
function cmpStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Stable join key over `(plan_run_id, wave_id)`. JSON-encodes the raw tuple so `null`/
 * `undefined` (Python `None`) stays DISTINCT from the empty string `''` — Python tuple keys
 * `(None, x)` and `('', x)` are distinct, and this must map them to different buckets too.
 */
function joinKey(planRunId: string | null | undefined, waveId: string | null | undefined): string {
  return JSON.stringify([planRunId ?? null, waveId ?? null]);
}

/** Backend `conductor.wave_started`. */
function waveStarted(s: WaveStatusRow): boolean {
  return _i(s.converting) + _i(s.converted) + _i(s.validated) + _i(s.cleaned) + _i(s.converted_external) > 0;
}

/** Backend `parallelism._member_to_convert`: READY, or DRY_RUN_OK/CONVERTING rows left. */
function memberToConvert(s: WaveStatusRow): boolean {
  return String(s.wave_state ?? '') === READY || _i(s.dry_run_ok) + _i(s.converting) > 0;
}

/**
 * Backend `parallelism._pre_convert_split`: `[freezeOwed, leaseActions]` for one member. A row
 * without the auto/manual split keeps the legacy reading (everything owes the freeze).
 */
function preConvertSplit(s: WaveStatusRow | undefined): [number, number] {
  const pre = _i(s?.open_pre_convert);
  const auto = s?.open_pre_convert_auto;
  const manual = s?.open_pre_convert_manual;
  if (auto == null && manual == null) return [pre, 0];
  return [_i(auto) + _i(manual), Math.max(0, pre - _i(auto) - _i(manual))];
}

/**
 * The earliest finish step any member owes, or null — backend `parallelism.batch_finish_step`.
 * Each member goes through the one-wave `waveFinishStep` (AWAITING_ACTIONS + fully migrated
 * only); an open freeze always owes the unfreeze.
 */
export function batchFinishStep(memberRows: WaveStatusRow[], openFreeze: Cell = 0): BulkStepValue | null {
  const owed = new Set<BulkStepValue>();
  if (_i(openFreeze) > 0) owed.add(BulkStep.UNFREEZE);
  for (const s of memberRows) {
    const kind = waveFinishStep({
      wave_state: s.wave_state,
      fully_migrated: _b(s.fully_migrated),
      open_post_convert_holding: s.open_post_convert_holding == null ? null : _i(s.open_post_convert_holding),
      open_post_convert: _i(s.open_post_convert),
      open_reshare: _i(s.open_reshare),
      open_compat: _i(s.open_compat),
      optimize_pending: _b(s.optimize_pending),
    });
    if (kind) owed.add(FINISH_TO_BULK[kind]);
  }
  return BULK_FINISH_ORDER.find((st) => owed.has(st)) ?? null;
}

/**
 * One rollup row per `batch_id` (parallel batches only; `blocked_from_parallel` excluded).
 *
 * Joins each group row to its `wave_status` row on `(plan_run_id, wave_id)` and aggregates
 * per batch. A missing wave_status row coalesces to an empty PLANNED-style state (no queue ==
 * not ready); a missing `open_freeze` entry coalesces to 0. Deterministic; sorted by
 * `(batch_order, batch_id)`.
 */
/**
 * Backend `parallelism.dry_run_filters`: `[catalog_filter, schema_filter]` for one wave's group
 * key — a schema for `catalog.schema`, the catalog (`*`) for a catalog-grain key, and null for a
 * blank key (never an estate-wide dry run).
 */
export function dryRunFilters(groupKey: string | null | undefined): [string, string] | null {
  const gk = String(groupKey ?? '').trim();
  if (!gk) return null;
  const dot = gk.indexOf('.');
  const cat = dot < 0 ? gk : gk.slice(0, dot);
  const sch = dot < 0 ? '' : gk.slice(dot + 1);
  return cat ? [cat, sch || '*'] : null;
}

export function batchReadiness(
  groupRows: BulkGroupRow[],
  waveStatusRows: WaveStatusRow[],
  openFreezeByBatch?: Record<string, number>,
): BatchRollup[] {
  const freezeMap = openFreezeByBatch ?? {};
  const statusByKey = new Map<string, WaveStatusRow>();
  for (const s of waveStatusRows) {
    statusByKey.set(joinKey(s.plan_run_id, s.wave_id), s);
  }

  const batches = new Map<string, BulkGroupRow[]>();
  for (const g of groupRows) {
    if (g.blocked_from_parallel) continue;
    const list = batches.get(g.batch_id);
    if (list) list.push(g);
    else batches.set(g.batch_id, [g]);
  }

  const out: BatchRollup[] = [];
  for (const [batchId, unsorted] of batches) {
    const members = [...unsorted].sort(
      (a, b) => _i(a.wave_order) - _i(b.wave_order) || cmpStr(String(a.wave_id), String(b.wave_id)),
    );
    const head = members[0];

    const memberRows: WaveStatusRow[] = [];
    const memberStates: string[] = [];
    const memberValidated: boolean[] = [];
    let openPre = 0;
    let openPost = 0;
    let openBlocking = 0;
    let openLease = 0;
    let openLeaseToConvert = 0;
    let openPreFreeze = 0;
    let openPreLease = 0;
    let openLeaseUnverified = 0;
    let convertedUnvalidated = 0;
    let readersUndecided = 0;
    let readersFgac = 0;
    const rolledBackWaveIds: string[] = [];
    let rolledBackTables = 0;
    const dryRunScopes: DryRunScope[] = [];
    let wavesNeedFreeze = 0;
    let tableCount = 0;
    let sizeBytes = 0;
    let estCopyMin = 0;
    let maxDowntimeMin = 0;
    const schedMembers: Array<{ verdict: string | null | undefined; nextAt: string | null | undefined }> = [];
    for (const g of members) {
      tableCount += _i(g.table_count);
      sizeBytes += _i(g.size_bytes);
      estCopyMin += _f(g.est_copy_min);
      maxDowntimeMin = Math.max(maxDowntimeMin, _f(g.max_downtime_min));
      const s = statusByKey.get(joinKey(g.plan_run_id, g.wave_id));
      if (s) memberRows.push(s);
      memberStates.push(String(s?.wave_state ?? ''));
      memberValidated.push(String(s?.wave_state ?? '') === DONE || _b(s?.fully_migrated));
      const pre = _i(s?.open_pre_convert);
      openPre += pre;
      openPost += _i(s?.open_post_convert);
      openBlocking += _i(s?.open_blocking);
      openLease += _i(s?.open_lease);
      // wave_status already counts only members still to convert (advisory, never a hold).
      readersUndecided += _i(s?.readers_undecided);
      readersFgac += _i(s?.readers_fgac_required);
      const [freezeOwed, leaseActions] = preConvertSplit(s);
      openPreFreeze += freezeOwed;
      if (s && memberToConvert(s)) {
        openLeaseToConvert += _i(s.open_lease);
        openPreLease += leaseActions;
        openLeaseUnverified += _i(s.open_pre_convert_lease_unverified);
      }
      // A rolled-back table is EXTERNAL again and needs a fresh dry run (03 re-selects any
      // ELIGIBLE table) before the batch can convert it.
      const rb = _i(s?.rolled_back);
      if (rb > 0) {
        rolledBackWaveIds.push(g.wave_id);
        rolledBackTables += rb;
        const f = dryRunFilters(g.group_key);
        if (f && !dryRunScopes.some((d) => d.catalog_filter === f[0] && d.schema_filter === f[1])) {
          dryRunScopes.push({ wave_id: g.wave_id, catalog_filter: f[0], schema_filter: f[1] });
        }
      }
      // wave_status.converted counts tables CURRENTLY in state CONVERTED (not yet validated).
      convertedUnvalidated += _i(s?.converted);
      if (freezeOwed > 0) wavesNeedFreeze += 1;
      // F1 schedule-safety rollup: worst member verdict governs, earliest fire is next write.
      schedMembers.push({ verdict: s?.wave_writer_verdict, nextAt: s?.wave_next_scheduled_write_at });
    }
    const sched = waveScheduleSafety(schedMembers);

    const readyCount = memberStates.filter((st) => st === READY).length;
    const totalCount = members.length;
    const convertReady = totalCount > 0 && memberStates.every((st) => st === READY);
    const allValidated = totalCount > 0 && memberValidated.every(Boolean);
    const openFreeze = _i(freezeMap[batchId] ?? 0);
    const anyBlocked = memberStates.some((st) => st === BLOCKED);
    const started = memberRows.some(waveStarted);
    const resumeConvert =
      started && !anyBlocked && memberRows.some((s) => _i(s.dry_run_ok) + _i(s.converting) > 0);
    const finishStep = allValidated ? batchFinishStep(memberRows, openFreeze) : null;

    let rollupState: BatchRollupState;
    if (anyBlocked) rollupState = 'BLOCKED';
    else if (rolledBackWaveIds.length > 0) rollupState = 'ROLLED_BACK';
    else if (openPre > 0) rollupState = 'AWAITING_ACTIONS';
    // A finish step is still owed: never show DONE beside paused compaction or an un-re-shared table.
    else if (allValidated && finishStep) rollupState = 'AWAITING_ACTIONS';
    else if (allValidated) rollupState = 'DONE';
    else if (convertReady) rollupState = 'READY';
    else rollupState = 'IN_PROGRESS';

    out.push({
      plan_run_id: head.plan_run_id,
      batch_id: batchId,
      batch_order: _i(head.batch_order),
      parallel_degree: _i(head.parallel_degree),
      bucket: head.bucket,
      member_wave_ids: members.map((g) => g.wave_id),
      table_count: tableCount,
      size_bytes: sizeBytes,
      est_copy_min: round2(estCopyMin),
      max_downtime_min: maxDowntimeMin,
      ready_count: readyCount,
      total_count: totalCount,
      open_pre_convert: openPre,
      open_pre_convert_freeze: openPreFreeze,
      open_pre_convert_lease: openPreLease,
      open_pre_convert_lease_unverified: openLeaseUnverified,
      open_post_convert: openPost,
      open_blocking: openBlocking,
      open_lease: openLease,
      open_lease_to_convert: openLeaseToConvert,
      converted_unvalidated: convertedUnvalidated,
      waves_need_freeze: wavesNeedFreeze,
      convert_ready: convertReady,
      all_validated: allValidated,
      open_freeze: openFreeze,
      started,
      resume_convert: resumeConvert,
      finish_step: finishStep,
      rolled_back_wave_ids: rolledBackWaveIds,
      rolled_back_tables: rolledBackTables,
      dry_run_scopes: dryRunScopes,
      readers_undecided: readersUndecided,
      readers_fgac_required: readersFgac,
      readers_attention: readersUndecided + readersFgac,
      batch_writer_verdict: sched.verdict,
      batch_next_scheduled_write_at: sched.nextAt,
      rollup: rollupState,
    });
  }
  out.sort((a, b) => a.batch_order - b.batch_order || cmpStr(a.batch_id, b.batch_id));
  return out;
}

/**
 * Order batch rollups for the single recommended next launch — a faithful mirror of the backend
 * `parallelism.order_batches_for_recommendation`. Risk tiers win first (the same sequence
 * `batch_order` encodes), so a schedule-safe normal batch never jumps ahead of a riskier pilot
 * batch. WITHIN a tier the schedule-SAFEST batch is recommended first via `scheduleSafetySortKey`
 * (applied AFTER the tier key); `batch_order` then `batch_id` are the final deterministic
 * tiebreak. Recommendation ORDER only — it never re-maps wave→batch membership.
 */
export function orderBatchesForRecommendation(rollups: BatchRollup[]): BatchRollup[] {
  return [...rollups].sort((a, b) => {
    const tierA = TIER_RANK[a.bucket ?? ''] ?? 99;
    const tierB = TIER_RANK[b.bucket ?? ''] ?? 99;
    if (tierA !== tierB) return tierA - tierB;
    const [ra, sa] = scheduleSafetySortKey(a.batch_writer_verdict, a.batch_next_scheduled_write_at);
    const [rb, sb] = scheduleSafetySortKey(b.batch_writer_verdict, b.batch_next_scheduled_write_at);
    if (ra !== rb) return ra - rb;
    if (sa !== sb) return sa - sb;
    return a.batch_order - b.batch_order || cmpStr(a.batch_id, b.batch_id);
  });
}

/**
 * The next-step recommendation for ONE `batchReadiness` rollup — backend `next_bulk_step`.
 * The one-wave journey applied to the batch: freeze → lease → convert (or resume) → validate →
 * unfreeze → re-share → Compat → optimize. Only `open_pre_convert_freeze` routes to the freeze: an
 * open LEASE_OWNERSHIP action also holds a wave in AWAITING_ACTIONS, but `03d` closes it, not the
 * freeze, so it routes to the lease. Manual-only holds still route to the freeze (no bulk actions
 * step; the operator attests them). A rollup without the split falls back to `open_pre_convert`.
 * A BLOCKED batch never leases or converts. A rolled-back member comes first: its tables are
 * EXTERNAL again and need the existing per-schema `03` dry run (`dry_run_scopes`). Never
 * auto-sequences.
 */
export function nextBulkStep(batchRollup: BatchRollup): BulkStepValue {
  if ((batchRollup.rolled_back_wave_ids ?? []).length > 0) return BulkStep.DRY_RUN;
  const freeze = batchRollup.open_pre_convert_freeze ?? batchRollup.open_pre_convert;
  if (_i(freeze) > 0) return BulkStep.FREEZE;
  if (
    batchRollup.rollup !== BLOCKED &&
    (_i(batchRollup.open_lease_to_convert) > 0 || _i(batchRollup.open_pre_convert_lease) > 0)
  ) {
    return BulkStep.LEASE;
  }
  if (batchRollup.convert_ready || batchRollup.resume_convert) return BulkStep.CONVERT;
  // batchFinishStep already owes the unfreeze whenever open_freeze > 0.
  if (batchRollup.all_validated) return batchRollup.finish_step || BulkStep.DONE;
  return BulkStep.VALIDATE;
}

/**
 * Backend `parallelism.lease_step_note`: why a batch still routes to Authorize after one ran, or
 * null. `03d` leaves a grant it applied but could not verify IN_PROGRESS; a re-run re-reads the
 * permissions and closes it. Only said while Authorize is the next step.
 */
export function leaseStepNote(batchRollup: BatchRollup): string | null {
  const n = _i(batchRollup.open_pre_convert_lease_unverified);
  if (n <= 0 || nextBulkStep(batchRollup) !== BulkStep.LEASE) return null;
  return unverifiedLeaseText(n);
}

const BULK_PRE_CONVERT_STEPS: ReadonlySet<BulkStepValue> = new Set([
  BulkStep.DRY_RUN,
  BulkStep.FREEZE,
  BulkStep.LEASE,
  BulkStep.CONVERT,
]);

/** Backend `parallelism.reader_step_note`: the external-reader warning before a convert, or null. */
export function readerStepNote(batchRollup: BatchRollup): string | null {
  if (!BULK_PRE_CONVERT_STEPS.has(nextBulkStep(batchRollup))) return null;
  return preconvertReaderWarning(_i(batchRollup.readers_undecided), _i(batchRollup.readers_fgac_required)) || null;
}

/** Backend `parallelism.rolled_back_step_note`: which member(s) were rolled back, or null. */
export function rolledBackStepNote(batchRollup: BatchRollup): string | null {
  const ids = (batchRollup.rolled_back_wave_ids ?? []).map(String);
  if (ids.length === 0) return null;
  const one = ids.length === 1;
  return (
    `${ids.join(', ')} ${one ? 'was' : 'were'} rolled back; re-run ` +
    `${one ? 'its dry run' : 'their dry runs'} (job_03_dry_run, one per schema) before ` +
    'this batch can convert again.'
  );
}

/** Backend `parallelism._has_work_to_validate`: a member still has tables in state CONVERTED. */
function hasWorkToValidate(batchRollup: BatchRollup): boolean {
  return _i(batchRollup.converted_unvalidated) > 0;
}

/** Backend `parallelism._in_flight`. */
function inFlight(batchRollup: BatchRollup): boolean {
  if (!batchRollup.started || batchRollup.rollup === 'BLOCKED') return false;
  const step = nextBulkStep(batchRollup);
  // A rollback is a deliberate operator choice: it never holds the recommendation.
  if (step === BulkStep.DONE || step === BulkStep.DRY_RUN) return false;
  return step !== BulkStep.VALIDATE || hasWorkToValidate(batchRollup);
}

/**
 * The one batch to launch next — backend `parallelism.recommended_batch`. Finish the started
 * batch first: the lowest-ordered in-flight batch (started, not BLOCKED, and its next step is a
 * real step — freeze, lease, convert/resume, a finish step, or a validate with tables in state
 * CONVERTED to validate). A started batch whose step is a no-op validate, or the dry run a
 * rolled-back member owes, is not held, so it never hides a READY batch. Otherwise the first READY batch in `orderBatchesForRecommendation` order.
 * Recommendation only.
 */
export function recommendedBatch(rollups: BatchRollup[]): BatchRollup | null {
  const inFlightBatches = rollups
    .filter(inFlight)
    .sort((a, b) => a.batch_order - b.batch_order || cmpStr(a.batch_id, b.batch_id));
  if (inFlightBatches.length > 0) return inFlightBatches[0];
  return orderBatchesForRecommendation(rollups).find((r) => r.rollup === READY) ?? null;
}
