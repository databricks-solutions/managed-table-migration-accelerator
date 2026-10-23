// app/server/mcp/preflight.ts
import { z } from 'zod';
import { runControlQuery } from './read-query.js';
import {
  buildPreflightOutput,
  conversionPreflight,
  MIGRATED_STATES,
  type PreflightBlocker,
  type PreflightInput,
  type PreflightOutput,
  type PreflightVerdict,
  type PreflightWriter,
} from './conversionPreflight.pure.js';
import { buildAuthorityImpact, countUnverifiedLeases } from './convertImpacts.pure.js';
import { pickWaveRow, waveFinishStep, type FinishKind, type WaveFinishRow } from './finishSteps.pure.js';

export const PREFLIGHT_TOOL_NAME = 'mtm_conversion_preflight';
export const PREFLIGHT_DESCRIPTION =
  'THE FIRST MOVE for any convert request, and the tool to call for "what next / how do I proceed" ' +
  'about a specific schema or table. Pass the scope as `target` (a catalog.schema, a ' +
  'catalog.schema.table, or a `wave:<id>` token for an exact wave); `scope`, `schema`, and `table` ' +
  'are accepted aliases. Returns the ordered, ' +
  'gated path to convert it safely — current state, wave blast radius, what is missing, a one-line ' +
  '`recommendation`, the exact `next_step` (tool + args), the remaining `then` pipeline, and `do_not` ' +
  'caveats — or explains why the request is unsupported. Relay `recommendation`, `next_step`, `then`, ' +
  'and `do_not` verbatim; never invent or reorder stages. Read-only; triggers nothing. ' +
  'The result also includes an `impacts` block: `impacts.authority` names the tables the convert ' +
  'identity is not yet authorized to convert, each grant request with its `ask_text` (who can grant ' +
  'it \u2014 relay verbatim), whether Authorize can close the gap itself, and a grant script; exactly which compaction actors, streams, ' +
  'and writer jobs a freeze will pause/stop, and the post-convert follow-ups (stream restart / D2D ' +
  're-share / Compatibility Mode). It is advisory — the coverage gates remain the authority.';
export const PREFLIGHT_ARGS = z
  .object({
    target: z.string().optional().describe('PRIMARY arg — a table full_name (catalog.schema.table), a catalog.schema, or a `wave:<id>` token (exact wave scope).'),
    scope: z.string().optional().describe('Alias for `target`.'),
    schema: z.string().optional().describe('Alias for `target` (a catalog.schema).'),
    table: z.string().optional().describe('Alias for `target` (a catalog.schema.table).'),
  })
  .strict()
  .refine((d) => Boolean(d.target ?? d.scope ?? d.schema ?? d.table), {
    message: 'Provide `target` (a catalog.schema or catalog.schema.table). Aliases accepted: scope / schema / table.',
  });

/** The job the preview is for: the one-wave gate (`convert`, job_04) or Bulk Convert
 *  (`bulk_convert`, job_11). Both deploy with the same run_as, so both read the same stored
 *  convert_authority verdicts; the value is validated and kept for the route's contract. */
export type PreflightConvertAlias = 'convert' | 'bulk_convert';

/** The `for` query value of GET /api/convert/preflight. Absent/blank/`convert` → 'convert';
 *  `bulk_convert` → 'bulk_convert'; anything else (incl. a repeated param) → null, a 400. */
export function preflightConvertAlias(v: unknown): PreflightConvertAlias | null {
  if (v === undefined) return 'convert';
  if (typeof v !== 'string') return null;
  const t = v.trim();
  if (t === '' || t === 'convert') return 'convert';
  if (t === 'bulk_convert') return 'bulk_convert';
  return null;
}

/** Pick the target from the canonical field or any accepted alias (pure). */
export function resolvePreflightTarget(d: { target?: string; scope?: string; schema?: string; table?: string }): string {
  return (d.target ?? d.scope ?? d.schema ?? d.table ?? '').trim();
}

const NOTE_NO_MANAGED_LOCATION = 'no_managed_location';
const NOTE_MANAGED_LOCATION_UNRESOLVED = 'managed_location_unresolved';
const DONE_STATES = new Set(['VALIDATED', 'CLEANED', 'CONVERTED_EXTERNAL']);
const READY_STATES = new Set(['DRY_RUN_OK', 'CONVERTING']);
const HARD_ELIGIBILITY = new Set(['BLOCKED', 'NEEDS_REVIEW']);
type Row = Record<string, unknown>;
const str = (v: unknown): string => {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return ''; // null/undefined/object — control-table reads are strings, so this is the empty case
};
const truthy = (v: unknown): boolean => str(v).toLowerCase() === 'true';

/**
 * The wave a scope's pre-convert reads pin to (pure): the lowest wave_order wave with a runnable
 * member (DRY_RUN_OK / CONVERTING — what a convert would act on), else the lowest with an
 * unmigrated member, else the lowest-ordered wave. A missing wave_order sorts last; ties break on
 * wave_id. A multi-wave schema therefore reads the wave still to convert, not the first by name,
 * and never a rolled-back wave that needs a dry run while a later sibling is ready.
 */
export function activeScopeWaveId(scopeRows: Row[]): string | null {
  const order = (r: Row): number => {
    const raw = str(r.wave_order);
    const n = Number(raw);
    return raw !== '' && Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
  };
  const planned = scopeRows.filter((r) => str(r.wave_id) !== '');
  if (planned.length === 0) return null;
  const runnable = planned.filter((r) => READY_STATES.has(str(r.state)));
  const pending = planned.filter((r) => !MIGRATED_STATES.has(str(r.state)));
  const pool = runnable.length > 0 ? runnable : pending.length > 0 ? pending : planned;
  const best = pool.reduce((a, b) =>
    order(b) < order(a) || (order(b) === order(a) && str(b.wave_id) < str(a.wave_id)) ? b : a);
  return str(best.wave_id);
}

/**
 * The tables whose unverified grants count toward the verdict (pure): the scope's members in the
 * pinned wave, the same wave the permission gap and the Authorize next_step use. Without a wave, the scope.
 */
export function leaseCountScope(scopeRows: Row[], waveId: string | null): Set<string> {
  const rows = waveId ? scopeRows.filter((r) => str(r.wave_id) === waveId) : scopeRows;
  return new Set(rows.map((r) => str(r.full_name)).filter(Boolean));
}

/**
 * Split the target into a table, schema, or wave scope (pure). A `wave:<id>` token pins the
 * preflight to EXACTLY that wave (filtered on wave_plan.wave_id) — no schema→wave first-match, so
 * a wave-scoped caller in a multi-wave schema never mis-resolves.
 */
export function resolveTarget(target: string): { resolvedAs: 'table' | 'schema' | 'wave' | 'unsupported'; fullName: string; catalog: string; schema: string; waveId: string } {
  const t = target.trim();
  if (!t || t === '*' || t.toLowerCase() === 'everything' || t.toLowerCase() === 'all') {
    return { resolvedAs: 'unsupported', fullName: '', catalog: '', schema: '', waveId: '' };
  }
  const waveMatch = /^wave:(.+)$/i.exec(t);
  if (waveMatch) {
    const waveId = waveMatch[1].trim();
    return waveId
      ? { resolvedAs: 'wave', fullName: '', catalog: '', schema: '', waveId }
      : { resolvedAs: 'unsupported', fullName: '', catalog: '', schema: '', waveId: '' };
  }
  const parts = t.split('.');
  if (parts.length === 3 && parts.every(Boolean)) return { resolvedAs: 'table', fullName: t, catalog: parts[0], schema: parts[1], waveId: '' };
  if (parts.length === 2 && parts.every(Boolean)) return { resolvedAs: 'schema', fullName: '', catalog: parts[0], schema: parts[1], waveId: '' };
  return { resolvedAs: 'unsupported', fullName: '', catalog: '', schema: '', waveId: '' };
}

function coverageOf(gateRows: Row[], waveId: string | null, kind: string): PreflightInput['compactionCoverage'] {
  if (!waveId) return null;
  const row = gateRows.find((r) => str(r.gate_kind) === kind && str(r.wave_id) === waveId);
  if (!row) return null;
  return truthy(row.passed) ? 'VERIFIED' : 'UNVERIFIED';
}

/** Map the five reads into the pure classifier input (pure). `authorityRows` are the pinned
 *  wave's convert_authority_for_wave rows (every member still to convert, verdict or none). */
export function rowsToPreflightInput(
  resolvedAs: 'table' | 'schema' | 'wave' | 'unsupported',
  scopeRows: Row[],
  gateRows: Row[],
  authorityRows: Row[],
  blockerRows: Row[],
): PreflightInput {
  const discovered = scopeRows.length > 0;
  const states = [...new Set(scopeRows.map((r) => str(r.state)).filter(Boolean))];
  const eligibilities = [...new Set(scopeRows.map((r) => str(r.eligibility)).filter(Boolean))];
  // A row is "assessed" once the profiler stamped an eligibility verdict; state stays DISCOVERED.
  const unassessedCount = scopeRows.filter((r) => !str(r.eligibility)).length;
  const planned = discovered && scopeRows.every((r) => str(r.wave_id) !== '');
  const waveId = activeScopeWaveId(scopeRows);

  const authority = authorityRows.length ? buildAuthorityImpact(authorityRows, 0) : null;

  const codes = new Set(blockerRows.map((r) => str(r.blocker_code)));
  const managedLocation: PreflightInput['managedLocation'] = codes.has(NOTE_NO_MANAGED_LOCATION)
    ? 'MISSING'
    : codes.has(NOTE_MANAGED_LOCATION_UNRESOLVED)
      ? 'UNRESOLVED'
      : null;

  return {
    resolvedAs,
    discovered,
    states,
    eligibilities,
    planned,
    waveId,
    managedLocation,
    compactionCoverage: coverageOf(gateRows, waveId, 'COMPACTION_COVERAGE'),
    streamStopCoverage: coverageOf(gateRows, waveId, 'STREAM_STOP_COVERAGE'),
    writerStopCoverage: coverageOf(gateRows, waveId, 'WRITER_STOP_COVERAGE'),
    leaseGap: authority?.not_authorized ?? 0,
    ...(authority ? { authorityText: authority.text } : {}),
    hasHardBlocker: eligibilities.some((e) => HARD_ELIGIBILITY.has(e)),
    dryRunOk: scopeRows.some((r) => READY_STATES.has(str(r.state))),
    allDone: discovered && scopeRows.every((r) => DONE_STATES.has(str(r.state))),
    unassessedCount,
    planRunId: scopeRows.map((r) => str(r.plan_run_id)).find(Boolean) ?? null,
  };
}

/**
 * The wave's finish step, only when it can be confirmed (pure): waveFinishStep also returns null for
 * a wave that is not fully migrated, which must not read as "no finish step left". So a missing row
 * or a not-fully-migrated wave is undefined (unconfirmed).
 */
export function finishStepFromWaveRow(row: WaveFinishRow | null | undefined): FinishKind | null | undefined {
  if (!row || !truthy(row.fully_migrated)) return undefined;
  return waveFinishStep(row);
}

/** The single wave the scope rows sit in, or null when they span no wave or more than one (pure). */
export function singleScopeWaveId(scopeRows: Row[]): string | null {
  const waveIds = new Set(scopeRows.map((r) => str(r.wave_id)).filter(Boolean));
  return waveIds.size === 1 ? [...waveIds][0] : null;
}

/**
 * The scope's finish step (pure). A scope spanning more than one wave (a schema whose streaming /
 * share / compat tables sit in their own waves) is undefined (unconfirmed): one wave's finish state
 * says nothing about its siblings, which may still owe mtm_unfreeze.
 */
export function finishStepForScope(scopeRows: Row[], waveRows: Row[] | null, planRunId: unknown): FinishKind | null | undefined {
  const waveId = singleScopeWaveId(scopeRows);
  if (!waveId || !waveRows) return undefined;
  return finishStepFromWaveRow(pickWaveRow(waveRows, planRunId, waveId));
}

/**
 * The catalog.schema the next_step args scope to (pure). NEEDS_VALIDATE scopes to the first row not
 * yet done, so a wave spanning schemas validates each in turn instead of re-validating the first.
 */
export function preflightScopeSchema(verdict: PreflightVerdict, scopeRows: Row[], defaultSchema: string | null): string | null {
  if (verdict !== 'NEEDS_VALIDATE') return defaultSchema;
  const pending = scopeRows.find((r) => !DONE_STATES.has(str(r.state)));
  const schema = pending ? str(pending.full_name).split('.').slice(0, 2).join('.') : '';
  return schema || defaultSchema;
}

/**
 * Build the schedulable writer-job list for the writers block (pure). Mirrors backend
 * executor._WRITER_STOPPABLE_CLASSES ({SCHEDULED_JOB, EVENT_JOB}). Threads the per-TABLE
 * manifest next_scheduled_write_at (one governing fire per table — W-C) onto each job so the
 * `will_be_paused` projection can name WHEN each job next fires. Advisory only — never gates.
 */
export function buildWriterJobs(
  scopeRows: Row[],
  writerRows: Row[],
  inScope: Set<string>,
  scopeSchema: string | null,
): PreflightWriter[] {
  const WRITER_JOB_CLASSES = new Set(['SCHEDULED_JOB', 'EVENT_JOB']);
  const byTable = new Map(
    scopeRows.map((r) => [
      str(r.full_name),
      {
        verdict: str(r.writer_schedule_verdict) || null,
        reason: str(r.writer_schedule_reason) || null,
        nextAt: str(r.next_scheduled_write_at) || null,
      },
    ]),
  );
  return writerRows
    .filter((r) => inScope.has(str(r.full_name)) && WRITER_JOB_CLASSES.has(str(r.writer_class)))
    .map((r) => {
      const v = byTable.get(str(r.full_name));
      return {
        full_name: str(r.full_name),
        job_id: str(r.entity_id) || null,
        writer_class: str(r.writer_class),
        writer_schedule_verdict: v?.verdict ?? null,
        reason: v?.reason ?? null,
        remediation: scopeSchema
          ? `mtm_freeze(${JSON.stringify({ target_schema: scopeSchema, confirm_execute: true })})`
          : 'mtm_freeze({ confirm_execute: true })',
        next_scheduled_write_at: v?.nextAt ?? null,
      };
    });
}

/** Composite: run the reads (SP), classify, decorate. Read-only; triggers nothing. */
export async function runPreflight(target: string): Promise<PreflightOutput> {
  const tgt = resolveTarget(target);
  if (tgt.resolvedAs === 'unsupported') {
    const input = rowsToPreflightInput('unsupported', [], [], [], []);
    return buildPreflightOutput({ target, resolvedAs: 'unsupported', scopeSchema: null, input, result: conversionPreflight(input), alsoConverts: [], blockers: [] });
  }

  // Read 1: scope (empty-string wildcard sentinel for the two unused target params).
  const scopeRows = await runControlQuery('preflight_scope', {
    target_full_name: tgt.fullName,
    target_catalog: tgt.resolvedAs === 'schema' ? tgt.catalog : '',
    target_schema: tgt.resolvedAs === 'schema' ? tgt.schema : '',
    target_wave_id: tgt.resolvedAs === 'wave' ? tgt.waveId : '',
  });

  // Writer picture is client-filtered by full_name, so read it UNCONDITIONALLY (not gated on a
  // wave) — a NEEDS_PLAN schema with no wave must still surface its attributed writers.
  // writer_sources is read estate-wide then client-filtered by full_name under the query MAX_ROWS
  // cap, so the `writers` block is ADVISORY and bounded (never a false green — the convert gate is
  // WRITER_STOP_COVERAGE-driven and independent of this list).
  const writerRows = await runControlQuery('writer_sources');

  const waveId = activeScopeWaveId(scopeRows);
  let gateRows: Row[] = [];
  let authorityRows: Row[] = [];
  let blockerRows: Row[] = [];
  let waveRows: Row[] | null = null;
  if (waveId) {
    // Reads 3–6 only when the target is planned. wave_gate_latest is filtered client-side by
    // wave_id (it does not declare a wave_id param); convert_authority_for_wave takes wave_id.
    // wave_plan_status (the finish counts) is best-effort: null = not read.
    [gateRows, authorityRows, blockerRows, waveRows] = await Promise.all([
      runControlQuery('wave_gate_latest'),
      runControlQuery('convert_authority_for_wave', { wave_id: waveId }),
      runControlQuery('blocked_and_review'),
      runControlQuery('wave_plan_status').catch((): Row[] | null => null),
    ]);
  }

  const input = rowsToPreflightInput(tgt.resolvedAs, scopeRows, gateRows, authorityRows, blockerRows);

  // Blast radius (read 2) for a table request whose wave has other members.
  let alsoConverts: string[] = [];
  if (tgt.resolvedAs === 'table' && waveId) {
    const members = await runControlQuery('wave_members', { wave_id: waveId });
    alsoConverts = members.map((r) => str(r.full_name)).filter((f) => f && f !== tgt.fullName);
  }

  const inScope = new Set(scopeRows.map((r) => str(r.full_name)));

  const waveRow = waveRows && singleScopeWaveId(scopeRows) ? pickWaveRow(waveRows, input.planRunId, waveId) : null;
  const finishStep = finishStepForScope(scopeRows, waveRows, input.planRunId);
  const gatedInput: PreflightInput = {
    ...input,
    ...(finishStep !== undefined ? { finishStep } : {}),
  };

  const blockers: PreflightBlocker[] = blockerRows
    .filter((r) => inScope.has(str(r.full_name)))
    .map((r) => ({ full_name: str(r.full_name), blocker_code: str(r.blocker_code), remediation_statement: r.remediation_statement == null ? null : str(r.remediation_statement) }));

  const scopeSchema = tgt.resolvedAs === 'schema' ? `${tgt.catalog}.${tgt.schema}` : (scopeRows.length ? str(scopeRows[0].full_name).split('.').slice(0, 2).join('.') : null);

  // Writer picture (unconditional in the output; empty when nothing is attributed in scope).
  const ADHOC = new Set(['NOTEBOOK', 'USER', 'EXTERNAL']);
  const writerRowsInScope = writerRows.filter((r) => inScope.has(str(r.full_name)));
  const jobs = buildWriterJobs(scopeRows, writerRows, inScope, scopeSchema);
  const adhocAdvisories = writerRowsInScope
    .filter((r) => ADHOC.has(str(r.writer_class)))
    .map((r) => `${str(r.full_name)} has an ad-hoc (${str(r.writer_class)}) writer — cannot be paused; pick an off-peak window (an in-window write fails and must be retried; nothing is lost).`);
  // has_adhoc_writer is three-valued: '' (unread) -> null, 'true'/'false' -> boolean.
  const adhocCol = scopeRows.map((r) => str(r.has_adhoc_writer)).find((v) => v !== '') ?? '';
  const hasAdhoc = adhocCol === '' ? null : adhocCol.toLowerCase() === 'true';
  const writers = { has_adhoc_writer: hasAdhoc, stop_coverage: input.writerStopCoverage, jobs };

  // Freeze-set enumeration (CUJ parity): the owed PRE_CONVERT FREEZE_COMPACTION / STOP_STREAM
  // actions for this scope. Bound the operator queue to the target's schema via its scope params;
  // buildConvertImpacts additionally filters by inScope. Read-only — advisory, never a gate.
  const dot = scopeSchema ? scopeSchema.indexOf('.') : -1;
  const freezeActionRows = await runControlQuery('action_queue_open', {
    scope_catalog: dot > 0 ? scopeSchema!.slice(0, dot) : '',
    scope_schema: dot > 0 ? scopeSchema!.slice(dot + 1) : '',
  });

  const leaseScope = leaseCountScope(scopeRows, waveId);
  const classifiedInput: PreflightInput = { ...gatedInput, leaseUnverified: countUnverifiedLeases(freezeActionRows, leaseScope) };
  const result = conversionPreflight(classifiedInput);
  return buildPreflightOutput({
    target,
    resolvedAs: tgt.resolvedAs,
    scopeSchema: preflightScopeSchema(result.verdict, scopeRows, scopeSchema),
    input: classifiedInput,
    result,
    alsoConverts,
    blockers,
    writers,
    adhocAdvisories,
    waveRow,
    impactsInput: { authorityRows, freezeActionRows, scopeRows, inScope, leaseScope },
  });
}
