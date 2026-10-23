// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.
//
// The FINISH steps of a converted wave (unfreeze -> re-share -> Compat -> optimize), shared by the
// estate next step (estateNextStep.pure.ts) and the scoped preflight (conversionPreflight.pure.ts)
// so both MCP answers pick the same step, send the same args, and list the same remaining path.

import { OPEN_APIS_DOC_URL, OPEN_APIS_REASON } from '../../client/src/lib/compatOpenApis.pure.js';

export type FinishKind = 'unfreeze' | 'reshare' | 'compat' | 'optimize';

/** A wave_plan_status row (numbers/booleans may arrive as strings). */
export type WaveFinishRow = Readonly<Record<string, unknown>>;

/** Conductor kind -> job alias for the finish kinds (guarded against conductor.STEP_JOB_ALIAS). */
export const FINISH_KIND_TO_ALIAS: Record<FinishKind, string> = {
  unfreeze: 'unfreeze_verify',
  reshare: 'reshare',
  compat: 'enable_compat',
  optimize: 'enable_optimization',
};

export const FINISH_KIND_OF_ALIAS: Record<string, FinishKind> = Object.fromEntries(
  Object.entries(FINISH_KIND_TO_ALIAS).map(([kind, alias]) => [alias, kind]),
) as Record<string, FinishKind>;

export const FINISH_ALIASES: ReadonlySet<string> = new Set(Object.values(FINISH_KIND_TO_ALIAS));

/** The finish tools in conductor.post_convert_step order (guarded against job-tools TOOL_NAME). */
export const FINISH_TOOLS = ['mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization'] as const;

export const FINISH_STEP_WHY: Record<FinishKind, string> = {
  unfreeze: 'restart the streams, writer jobs and compaction that mtm_freeze stopped',
  reshare: 're-share its Delta Sharing (D2D) tables',
  compat: 'decide how its external readers connect (UC open APIs first; Compatibility Mode only if a reader cannot)',
  optimize: 'turn on automatic liquid clustering (Predictive Optimization is already on after the convert)',
};

export const FINISH_DO_NOT: Record<FinishKind, string[]> = {
  unfreeze: [
    'Do NOT skip or defer this step \u2014 streaming jobs that read or write the converted tables MUST be restarted. mtm_unfreeze restarts what mtm_freeze stopped, from the freeze ledger (pipelines restart without a full refresh; checkpoints are untouched). Do NOT restart jobs by hand.',
  ],
  reshare: [
    'Do NOT edit shares by hand \u2014 mtm_reshare issues the exact ALTER SHARE REMOVE + ADD WITH HISTORY per share and verifies. A table with no attributable share stays MANUAL.',
    'Only if the user says a re-share cannot be finished now: call mtm_reshare with finish_action="defer", a non-blank defer_reason, and confirm_execute=true (action_keys may name specific actions). finish_action="reopen" undoes it.',
  ],
  compat: [
    `Ask FIRST whether the external readers can use the UC open APIs (Delta clients via the Unity REST API, Iceberg clients via the Iceberg REST catalog; ${OPEN_APIS_DOC_URL}). They read the one managed copy, so no second copy is needed. If yes: call mtm_enable_compat with finish_action="defer", defer_reason="${OPEN_APIS_REASON}" (exactly), and confirm_execute=true \u2014 action_keys may name the tables whose readers use them. A metastore admin must turn on External data access and grant the reader EXTERNAL USE SCHEMA; do NOT invent the reader principal and do NOT run a GRANT yourself.`,
    'Only for a reader that cannot use the open APIs, Compatibility Mode: Do NOT invent a compat_location \u2014 every table needs its OWN EMPTY external-location path that the user supplies. As given, mtm_enable_compat is a dry run that records the exact statements; to execute, ask the user for one path per table and call it again with compat_location_map (JSON {full_name: path}) and confirm_execute=true. compat_location (one path) is only for a scope where exactly one table needs Compat: job_08b refuses a location shared by more than one table (they stay MANUAL and the run fails).',
    'Only if the user says the step cannot be finished now (for another reason): call mtm_enable_compat with finish_action="defer", their own non-blank defer_reason, and confirm_execute=true. finish_action="reopen" undoes either defer.',
  ],
  optimize: [
    'Do NOT run OPTIMIZE or VACUUM yourself \u2014 mtm_enable_optimization turns on automatic liquid clustering (a ZORDERed table keeps its ZORDER columns as the starting keys; tables already on AUTO are skipped) and never VACUUMs. Predictive Optimization is already on after the convert; the job turns it on only where it reads off.',
    'Do NOT claim a maintenance job was retired \u2014 a human retires it; an executed mtm_enable_optimization then verifies and closes the RETIRE_MAINTENANCE_JOB action.',
    'Only if the user says to skip clustering for now: call mtm_enable_optimization with auto_liquid_policy="recommend", enable_po_on_convert=false and confirm_execute=true \u2014 it changes no table, keeps each table\u2019s clustering statements on the runbook, and finishes the step. Do NOT send auto_liquid_policy="off".',
  ],
};

const str = (v: unknown): string => {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return '';
};
const num = (v: unknown): number => Number(str(v) || 0) || 0;
const truthy = (v: unknown): boolean => str(v).toLowerCase() === 'true';

/** Restore work that holds the wave, minus the re-share / Compat executors. A row without
 *  open_post_convert_holding uses the lumped open_post_convert (never read as 0). */
function restoreCount(w: WaveFinishRow): number {
  const holding = w.open_post_convert_holding != null ? num(w.open_post_convert_holding) : num(w.open_post_convert);
  return holding - num(w.open_reshare) - num(w.open_compat);
}

/**
 * The wave's next finish step: conductor.post_convert_step, gated like pick_finishing_wave
 * (AWAITING_ACTIONS + fully migrated). Same rule as the app's waveFinishStep (parity-tested).
 */
export function waveFinishStep(w: WaveFinishRow | null | undefined): FinishKind | null {
  if (!w) return null;
  if (str(w.wave_state).toUpperCase() !== 'AWAITING_ACTIONS' || !truthy(w.fully_migrated)) return null;
  if (restoreCount(w) > 0) return 'unfreeze';
  if (num(w.open_reshare) > 0) return 'reshare';
  if (num(w.open_compat) > 0) return 'compat';
  if (truthy(w.optimize_pending)) return 'optimize';
  return null;
}

/**
 * The finish tools the wave still needs, in order. Without a row every tool is listed. Optimize is
 * needed until the wave is fully migrated with optimize_pending false (every table stamped).
 */
export function neededFinishTools(w: WaveFinishRow | null | undefined): string[] {
  if (!w) return [...FINISH_TOOLS];
  const out: string[] = [];
  if (restoreCount(w) > 0) out.push('mtm_unfreeze');
  if (num(w.open_reshare) > 0) out.push('mtm_reshare');
  if (num(w.open_compat) > 0) out.push('mtm_enable_compat');
  if (!truthy(w.fully_migrated) || truthy(w.optimize_pending)) out.push('mtm_enable_optimization');
  return out;
}

/** The path after `tool` in `pipeline`, keeping only the finish tools the wave still needs. */
export function remainingPipeline(pipeline: readonly string[], tool: string, w?: WaveFinishRow | null): string[] {
  const i = pipeline.indexOf(tool);
  if (i < 0) return [];
  const needed = new Set(neededFinishTools(w));
  const finish: readonly string[] = FINISH_TOOLS;
  return pipeline.slice(i + 1).filter((t) => !finish.includes(t) || needed.has(t));
}

/** The wave_plan_status row for (planRunId, waveId); waveId alone matches when planRunId is blank. */
export function pickWaveRow(rows: readonly WaveFinishRow[], planRunId: unknown, waveId: unknown): WaveFinishRow | null {
  const wid = str(waveId).trim();
  if (!wid) return null;
  const prid = str(planRunId).trim();
  return rows.find((r) => str(r.wave_id) === wid && (!prid || str(r.plan_run_id) === prid)) ?? null;
}

/**
 * Finish-step selector (design §8 Q1, same as the app's finishScope): a complete wave pin wins,
 * else a full catalog.schema as target_schema, else {} (the step is not offered).
 */
export function finishSelector(planRunId: unknown, waveId: unknown, targetSchema: unknown): Record<string, string> {
  const prid = str(planRunId).trim();
  const wid = str(waveId).trim();
  if (prid && wid) return { plan_run_id: prid, wave_id: wid };
  const ts = str(targetSchema).trim();
  const parts = ts.split('.');
  if (parts.length === 2 && parts.every(Boolean)) return { target_schema: ts };
  return {};
}

/**
 * Recommended args for a finish alias (design D3 + the M4 decisions), or null when there is no
 * scope. unfreeze / reshare execute; compat stays a dry run because compat_location is a human
 * storage decision the recommendation cannot supply; optimize enables PO + auto-liquid.
 */
export function finishArgs(alias: string, selector: Record<string, string>): Record<string, unknown> | null {
  if (Object.keys(selector).length === 0) return null;
  switch (alias) {
    case 'unfreeze_verify':
    case 'reshare':
      return { ...selector, confirm_execute: true };
    case 'enable_compat':
      return { ...selector };
    case 'enable_optimization':
      return { ...selector, enable_po_on_convert: true, auto_liquid_policy: 'enable_eligible', confirm_execute: true };
    default:
      return null;
  }
}
