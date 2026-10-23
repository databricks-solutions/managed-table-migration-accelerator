/**
 * Schema-target routing for mtm_get_next_step (focus mode): the CONDUCTOR picks the wave, the
 * preflight explains it. For a schema (or catalog-grain) target with a row in the latest
 * per-scope batch (`schema_steps` ⇐ migration_next_actions_by_scope), the answer is the preflight
 * of that row's wave (`wave:<id>`) — the same live gates, verdicts and `next_step` shape — so MCP
 * and the app's focus card always name the same wave, including the finish-first case (a
 * converted wave finishes before a sibling starts). Without a scoped row the target goes to the
 * schema preflight exactly as before (NOT_DISCOVERED / NEEDS_PROFILE / NEEDS_PLAN …). Table and
 * `wave:` targets are never rerouted. Read-only.
 */
import type { PreflightOutput, PreflightVerdict } from './conversionPreflight.pure.js';

type Row = Record<string, unknown>;

const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '');
const int = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

/** A scope target: `catalog.schema`, or a bare catalog (catalog-grain plans). Not a table / wave. */
export function isScopeTarget(target: string): boolean {
  const t = target.trim();
  if (!t || t === '*' || /^wave:/i.test(t) || /\s/.test(t)) return false;
  const parts = t.split('.');
  return (parts.length === 1 || parts.length === 2) && parts.every(Boolean);
}

/** The latest batch's row for `scope` (rows are already the latest batch), or `null`. */
export function scopedRowFor(rows: readonly Row[], scope: string): Row | null {
  const s = scope.trim();
  return rows.find((r) => str(r.scope_key) === s) ?? null;
}

export interface FocusBlock {
  scope: string;
  /** focus.SCOPE_*: ACTIVE / DONE / HELD_ELSEWHERE / ROLLED_BACK / NEEDS_REPLAN / BLOCKED. */
  scope_status: string;
  /** The scope's own next step kind from the conductor (same vocabulary as next_step_kind). */
  next_step_kind: string;
  wave_id: string | null;
  position: { wave_index: number | null; wave_count: number };
  held: { streaming_waves: number; rolled_back_waves: number; review_tables: number; blocked_tables: number };
  drift: { unplanned_tables: number; plan_stale_waves: number };
  /** The scope's rank in the team priorities (1 = first), or null when not pinned. */
  pin_rank: number | null;
  as_of: string | null;
}

export function focusBlock(row: Row): FocusBlock {
  const idx = int(row.wave_index);
  const rank = int(row.pin_rank);
  return {
    scope: str(row.scope_key),
    scope_status: str(row.scope_status),
    next_step_kind: str(row.next_step_kind),
    wave_id: str(row.next_wave_id) || null,
    position: { wave_index: idx > 0 ? idx : null, wave_count: int(row.wave_count) },
    held: {
      streaming_waves: int(row.held_streaming_waves),
      rolled_back_waves: int(row.rolled_back_waves),
      review_tables: int(row.held_review_tables),
      blocked_tables: int(row.held_blocked_tables),
    },
    drift: { unplanned_tables: int(row.unplanned_tables), plan_stale_waves: int(row.plan_stale_waves) },
    pin_rank: rank > 0 ? rank : null,
    as_of: str(row.as_of) || null,
  };
}

/** The preflight verdicts each conductor step kind normally reads as (for the agreement hint). */
export const KIND_VERDICTS: Readonly<Record<string, readonly PreflightVerdict[]>> = {
  replan: ['NEEDS_PLAN'],
  prep: ['NEEDS_DRY_RUN', 'NEEDS_PROFILE'],
  freeze: ['NEEDS_FREEZE', 'NEEDS_STREAM_STOP', 'NEEDS_WRITER_STOP'],
  lease: ['NEEDS_PERMISSION'],
  convert: ['READY_TO_CONVERT'],
  resume: ['READY_TO_CONVERT'],
  validate: ['NEEDS_VALIDATE'],
  unfreeze: ['NEEDS_FINISH'],
  reshare: ['NEEDS_FINISH'],
  compat: ['NEEDS_FINISH'],
  optimize: ['NEEDS_FINISH'],
  done: ['ALREADY_DONE'],
  blocked: ['BLOCKED', 'MANAGED_LOCATION_MISSING'],
};

/** A hint when the conductor's planned step and the live preflight disagree (never silent). */
export function agreementHint(kind: string, verdict: PreflightVerdict, waveId: string | null): string | null {
  const expected = KIND_VERDICTS[kind];
  if (!expected || expected.includes(verdict)) return null;
  const where = waveId ? ` on ${waveId}` : '';
  return (
    `The schema's planned next step is ${kind}${where}, but the live check reads ${verdict}. ` +
    'Follow next_step (it reflects the live gates); call mtm_recompute_next_step if they keep disagreeing.'
  );
}

export type FocusedPreflight = PreflightOutput & { scope: string; focus: FocusBlock };

/** Annotate a preflight answer with the scope the user asked about and its focus block. */
export function withFocus(out: PreflightOutput, scope: string, row: Row): FocusedPreflight {
  const focus = focusBlock(row);
  const hint = agreementHint(focus.next_step_kind, out.verdict, focus.wave_id);
  return {
    ...out,
    target: scope,
    scope,
    focus,
    ...(hint ? { suggestions: [...(out.suggestions ?? []), hint] } : {}),
  };
}

export interface FocusDeps {
  /** The latest `schema_steps` rows; may throw (an unreadable table is "no scoped row"). */
  readScopedRows: () => Promise<Row[]>;
  runPreflight: (target: string) => Promise<PreflightOutput>;
}

/** The schema-target answer: the scoped wave's preflight when a scoped row exists, else as before. */
export async function scopedNextStep(target: string, deps: FocusDeps): Promise<PreflightOutput | FocusedPreflight> {
  if (!isScopeTarget(target)) return deps.runPreflight(target);
  let row: Row | null = null;
  try {
    row = scopedRowFor(await deps.readScopedRows(), target);
  } catch {
    row = null; // best-effort: a missing per-scope table never breaks the answer
  }
  if (!row) return deps.runPreflight(target);
  const waveId = str(row.next_wave_id);
  const out = await deps.runPreflight(waveId ? `wave:${waveId}` : target);
  return withFocus(out, target.trim(), row);
}
