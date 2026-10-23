/**
 * Pure scope + step helpers for a converted wave's FINISH steps (unfreeze → re-share → Compat →
 * optimize), shared by the Overview guided card and the Waves fully-migrated branch so the two
 * surfaces can never disagree.
 *
 * Scope: a batch (`plan_run_id` + `batch_id`) wins, then a complete wave pin, then a full
 * `catalog.schema` selector as target_schema; 08 / 08b / 08c resolve the batch first (M6.1).
 * Neither → not ok, and callers fall back (never fire unscoped). 05b has no batch selector, so a
 * batch's unfreeze is `bulk_unfreeze` (finishCtaSupported).
 */
import type { TriggerParams } from './triggerParams.pure';
import { OPEN_APIS_REASON } from './compatOpenApis.pure';
import { parseSchemaScope } from './schemaScope.pure';

export type FinishKind = 'unfreeze' | 'reshare' | 'compat' | 'optimize';

const FINISH_KINDS: ReadonlySet<string> = new Set<FinishKind>(['unfreeze', 'reshare', 'compat', 'optimize']);

export function isFinishKind(kind: string | null | undefined): kind is FinishKind {
  return typeof kind === 'string' && FINISH_KINDS.has(kind);
}

export const FINISH_STEP_LABEL: Record<FinishKind, string> = {
  unfreeze: 'restart streams and compaction',
  reshare: 're-share Delta shares',
  compat: 'decide how external readers connect',
  optimize: 'enable optimization',
};

/** action_queue_{open,deferred}_for_wave args (all strings; '' = no filter). */
export interface FinishQuery {
  scope_catalog: string;
  scope_schema: string;
  plan_run_id: string;
  wave_id: string;
  /** Batch membership narrowing (wins over wave_id when set). */
  batch_id: string;
}

export type FinishGrain = 'batch' | 'wave' | 'schema' | 'none';

export interface FinishScope {
  /** True only when there is a scope the job can target. */
  ok: boolean;
  /** Which selector the params carry. */
  grain: FinishGrain;
  /** Selector params to spread into the job trigger. */
  params: Record<string, string>;
  /** Human label for the confirm copy. */
  label: string;
  query: FinishQuery;
}

const NO_QUERY: FinishQuery = { scope_catalog: '', scope_schema: '', plan_run_id: '', wave_id: '', batch_id: '' };

export function finishScope(input: {
  planRunId?: string | null;
  waveId?: string | null;
  selector?: string | null;
  batchId?: string | null;
}): FinishScope {
  const planRunId = (input.planRunId ?? '').trim();
  const waveId = (input.waveId ?? '').trim();
  const batchId = (input.batchId ?? '').trim();
  if (batchId) {
    // A batch belongs to one plan generation: without plan_run_id the job raises, so never fire.
    if (!planRunId) return { ok: false, grain: 'none', params: {}, label: `batch ${batchId}`, query: NO_QUERY };
    return {
      ok: true,
      grain: 'batch',
      params: { plan_run_id: planRunId, batch_id: batchId },
      label: `batch ${batchId}`,
      query: { ...NO_QUERY, plan_run_id: planRunId, batch_id: batchId },
    };
  }
  const schema = parseSchemaScope(input.selector);
  const [catalog, schemaName] = schema.ok ? schema.targetSchema.split('.', 2) : ['', ''];
  if (planRunId && waveId) {
    return {
      ok: true,
      grain: 'wave',
      params: { plan_run_id: planRunId, wave_id: waveId },
      label: schema.ok ? `${schema.label} · wave ${waveId}` : `wave ${waveId}`,
      query: { scope_catalog: catalog, scope_schema: schemaName, plan_run_id: planRunId, wave_id: waveId, batch_id: '' },
    };
  }
  if (schema.ok) {
    return {
      ok: true,
      grain: 'schema',
      params: { target_schema: schema.targetSchema },
      label: schema.label,
      query: { ...NO_QUERY, scope_catalog: catalog, scope_schema: schemaName },
    };
  }
  return { ok: false, grain: 'none', params: {}, label: schema.label, query: NO_QUERY };
}

/** The shared CTA can run this kind for this scope. A batch's unfreeze is job_11d (BatchCard's
 *  own bulk_unfreeze buttons): 05b has no batch selector, and one kind never maps to two aliases. */
export function finishCtaSupported(kind: FinishKind, scope: FinishScope): boolean {
  return scope.ok && !(kind === 'unfreeze' && scope.grain === 'batch');
}

/** The unit a finish step closes, for confirm copy. */
export function finishUnit(scope: FinishScope): 'batch' | 'wave' {
  return scope.grain === 'batch' ? 'batch' : 'wave';
}

/** The wave_plan_status columns the finish-step mirror reads (numbers/booleans may arrive as strings). */
export interface WaveFinishRow {
  wave_state?: string | null;
  fully_migrated?: boolean | string | null;
  open_post_convert_holding?: number | string | null;
  open_post_convert?: number | string | null;
  open_reshare?: number | string | null;
  open_compat?: number | string | null;
  optimize_pending?: boolean | string | null;
}

const num = (v: unknown): number => Number(v ?? 0) || 0;
const truthy = (v: unknown): boolean => v === true || String(v).toLowerCase() === 'true';

/**
 * TS mirror of backend `conductor.post_convert_step`, gated like `pick_finishing_wave`
 * (AWAITING_ACTIONS + fully migrated). Keep in step with conductor.py — the order and the
 * holding-count fallback are the contract.
 */
export function waveFinishStep(w: WaveFinishRow): FinishKind | null {
  if ((w.wave_state ?? '').toUpperCase() !== 'AWAITING_ACTIONS' || !truthy(w.fully_migrated)) return null;
  const reshare = num(w.open_reshare);
  const compat = num(w.open_compat);
  const holding = w.open_post_convert_holding != null ? num(w.open_post_convert_holding) : num(w.open_post_convert);
  if (holding - reshare - compat > 0) return 'unfreeze';
  if (reshare > 0) return 'reshare';
  if (compat > 0) return 'compat';
  if (truthy(w.optimize_pending)) return 'optimize';
  return null;
}

export function waveFinish(
  w: WaveFinishRow & { plan_run_id?: string | null; wave_id?: string | null; group_key?: string | null },
): { kind: FinishKind; scope: FinishScope } | null {
  const kind = waveFinishStep(w);
  if (!kind) return null;
  return { kind, scope: finishScope({ planRunId: w.plan_run_id, waveId: w.wave_id, selector: w.group_key }) };
}

export type OptimizeMode = 'enable' | 'review' | 'skip';

/**
 * 08c params. enable = the guided default (design D3); review = the same as a dry run; skip =
 * finish the step without changing any table: `recommend` records each table's clustering
 * statements as open runbook actions and runs nothing, and the executed run still stamps the step
 * done (08c's stamp rule).
 */
export function optimizeParams(scope: FinishScope, mode: OptimizeMode): TriggerParams {
  return {
    ...scope.params,
    enable_po_on_convert: mode !== 'skip',
    auto_liquid_policy: mode === 'skip' ? 'recommend' : 'enable_eligible',
    confirm_execute: mode !== 'review',
  };
}

/** 08b params: one EMPTY path per table in compat_location_map (D7); never a shared compat_location. */
export function compatParams(scope: FinishScope, mapJson: string): TriggerParams {
  return { ...scope.params, confirm_execute: true, compat_location_map: mapJson };
}

export function deferParams(scope: FinishScope, actionKey: string, reason: string): TriggerParams {
  return {
    ...scope.params,
    finish_action: 'defer',
    defer_reason: reason.trim(),
    action_keys: actionKey,
    confirm_execute: true,
  };
}

/** 08b params that mark the named ENABLE_COMPAT actions not needed: their readers use the UC open APIs. */
export function openApisParams(scope: FinishScope, actionKeys: readonly string[]): TriggerParams {
  return {
    ...scope.params,
    finish_action: 'defer',
    defer_reason: OPEN_APIS_REASON,
    action_keys: actionKeys.map((k) => k.trim()).filter((k) => k !== '').join(','),
    confirm_execute: true,
  };
}

export function reopenParams(scope: FinishScope, actionKey: string): TriggerParams {
  return { ...scope.params, finish_action: 'reopen', action_keys: actionKey, confirm_execute: true };
}

export type DeferrableKind = 'RESHARE_D2D' | 'ENABLE_COMPAT';

/** Structural subset of an action row the Defer/Reopen panel reads. */
export interface FinishActionRow {
  action_key: string | null;
  action_kind: string | null;
  title?: string | null;
  full_name?: string | null;
  resolution?: string | null;
  status?: string | null;
}

/** The row's trimmed action_key, or null when blank. A blank key must never reach deferParams /
 *  reopenParams: the job reads action_keys="" as no narrowing — every action in scope. */
export function finishActionKey(r: Pick<FinishActionRow, 'action_key'>): string | null {
  const key = (r.action_key ?? '').trim();
  return key === '' ? null : key;
}

export function finishActionsOfKind<T extends FinishActionRow>(rows: readonly T[], kind: DeferrableKind): T[] {
  return rows.filter((r) => (r.action_kind ?? '').toUpperCase() === kind && finishActionKey(r) !== null);
}

export const DEFER_PANEL_ERROR =
  'Couldn’t load this wave’s actions, so Defer / Reopen are unavailable. Refresh to retry.';

/** Which Defer panel to render (pure): a failed read wins, so it never reads as "nothing to defer". */
export function deferPanelState(
  openErr: unknown,
  deferredErr: unknown,
  openCount: number,
  deferredCount: number,
): 'error' | 'empty' | 'rows' {
  if ((openErr ?? null) !== null || (deferredErr ?? null) !== null) return 'error';
  return openCount === 0 && deferredCount === 0 ? 'empty' : 'rows';
}
