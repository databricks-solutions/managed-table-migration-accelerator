// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.
//
// Assembles the Convert-Gate CUJ `impacts` block from already-read control-table rows: which
// tables the convert identity is not yet authorized to convert (the stored convert_authority
// verdicts, decided only by the Python rule), exactly what a freeze will pause/stop (compaction
// actors + streams + writer jobs), and the post-convert follow-ups. Presentation ONLY — the
// gates re-checked by 04_convert remain the convert authority.

import {
  applyAuthorityToWaves,
  authorityByFullName,
  grantScript,
  isToConvert,
  STATUS_AUTHORIZED,
  STATUS_NEEDS_GRANT,
  STATUS_SELF_AUTHORIZABLE,
  STATUS_UNKNOWN,
  waveAuthorityText,
  type AuthorityRow,
  type AuthorityStatus,
  type GrantRequest,
} from '../../client/src/lib/convertAuthority.pure.js';
import {
  asReaderReadiness,
  preconvertReaderWarning,
  READER_COMPAT_ON,
  READER_FGAC_REQUIRED,
  READER_OPEN_APIS,
  READER_UNDECIDED,
} from '../../client/src/lib/compatOpenApis.pure.js';

export type CoverageState = 'VERIFIED' | 'UNVERIFIED' | 'NOT_REQUIRED' | null;

/** One schedulable writer job a freeze will pause (mirrors PreflightWriters.will_be_paused). */
export interface WriterPauseRow {
  full_name: string;
  job_id: string | null;
  writer_class: string;
  next_scheduled_write_at?: string | null;
}

/** Deprecated `impacts.lease` row (one release): a table the convert identity is not authorized for. */
export interface LeaseNeed {
  full_name: string;
  owner?: string;
  ownership_change: string;
}

/** One table still to convert whose stored verdict is not AUTHORIZED (or missing: status null). */
export interface AuthorityTable {
  full_name: string;
  /** null = not checked yet (no stored verdict). */
  status: AuthorityStatus | null;
  /** authority.authority_text — relay verbatim. */
  reason: string | null;
  missing: string[];
  self_grantable: boolean;
  basis: string | null;
  basis_via: string | null;
  /** Each request's ask_text names who can grant it — relay verbatim. */
  grant_requests: GrantRequest[];
}

/** Convert permission for the pinned wave (convert_authority, read only). */
export interface AuthorityImpact {
  /** The identity the stored verdicts are for (the convert job's run_as); null before any verdict. */
  actor: string | null;
  /** Members still to convert. */
  total: number;
  /** Members the convert gate blocks: no verdict yet, or not AUTHORIZED. */
  not_authorized: number;
  /** Of those, the ones with no stored verdict yet. */
  unevaluated: number;
  /** Of those, the ones Authorize (job_03d_lease_ownership) can grant itself. */
  self_grantable: number;
  /** Grants Authorize applied but could not verify (IN_PROGRESS LEASE_OWNERSHIP). */
  unverified: number;
  /** True only when Authorize can close the whole gap itself: every blocked table is checked and self-grantable. */
  authorize_can_close: boolean;
  counts: Record<AuthorityStatus, number>;
  /** authority.wave_authority_text — relay verbatim. */
  text: string;
  tables: AuthorityTable[];
  /** authority.grant_script over the blocked tables: one section per grantor. */
  grant_script: string;
}

/** One attributed unit a freeze will pause/stop (CoveragePanel), from action_queue_open. */
export interface FreezeUnit {
  full_name: string;
  target_kind: string | null;
  target_id: string | null;
  title: string;
  remediation: string | null;
}

export interface ConvertImpacts {
  authority: AuthorityImpact;
  /** DEPRECATED alias of `authority` (one release): gap = not_authorized, run_as_owns = nothing
   *  blocked, unknown = not checked yet + UNKNOWN, run_as = actor. Read `authority` instead. */
  lease: { gap: number; total: number; run_as_owns: boolean; tables: LeaseNeed[]; unknown: number; unverified: number; verified: 'stored'; run_as: string | null; owner_read_by?: string | null };
  freeze: {
    coverage: { compaction: CoverageState; stream: CoverageState; writer: CoverageState };
    will_freeze: { compaction_actors: FreezeUnit[]; streams: FreezeUnit[]; writer_jobs: WriterPauseRow[] };
  };
  post_convert: { needs_streaming_restart: boolean; needs_reshare: boolean; needs_compat: boolean };
  /** Pre-convert external-reader state of the in-scope tables (preflight_scope.reader_readiness,
   *  compat.reader_readiness). Advisory: tell the reader teams before the window; never a gate. */
  external_readers: ExternalReaders;
}

export interface ExternalReaders {
  /** External readers with no recorded decision: storage-path readers stop at convert. */
  undecided: string[];
  /** Row filters or column masks: those readers need cross-engine ABAC after convert. */
  fgac_required: string[];
  open_apis: number;
  compat_on: number;
}

// Mirrors wave_status.readers_*: only active members still to convert count (progress._MIGRATED + DROPPED).
const NOT_TO_CONVERT = new Set(['CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED', 'DROPPED']);

/** Group in-scope rows still to convert by their `reader_readiness` (missing reads NONE). Pure. */
export function externalReaders(rows: Record<string, unknown>[]): ExternalReaders {
  const out: ExternalReaders = { undecided: [], fgac_required: [], open_apis: 0, compat_on: 0 };
  for (const r of rows) {
    if (NOT_TO_CONVERT.has(str(r.state).toUpperCase())) continue;
    const state = asReaderReadiness(r.reader_readiness);
    if (state === READER_UNDECIDED) out.undecided.push(str(r.full_name));
    else if (state === READER_FGAC_REQUIRED) out.fgac_required.push(str(r.full_name));
    else if (state === READER_OPEN_APIS) out.open_apis += 1;
    else if (state === READER_COMPAT_ON) out.compat_on += 1;
  }
  out.undecided.sort();
  out.fgac_required.sort();
  return out;
}

/** The canonical pre-convert reader warning (compat.preconvert_reader_warning) for these counts,
 *  as a `do_not` caveat the model relays before a convert, or [] when there is nothing to warn. */
export function readerDoNot(undecided: number, fgacRequired: number): string[] {
  const warning = preconvertReaderWarning(undecided, fgacRequired);
  if (!warning) return [];
  return [
    `Do NOT launch the convert before you tell the user about external readers: ${warning} ` +
      'Ask them to confirm the reader teams were told. This is advisory: the job has no reader gate.',
  ];
}

const str = (v: unknown): string => {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return '';
};
const truthy = (v: unknown): boolean => str(v).toLowerCase() === 'true';
const orNull = (v: unknown): string | null => (str(v) === '' ? null : str(v));

function toFreezeUnit(r: Record<string, unknown>): FreezeUnit {
  return {
    full_name: str(r.full_name),
    target_kind: orNull(r.target_kind),
    target_id: orNull(r.target_id),
    title: str(r.title),
    remediation: orNull(r.remediation_sql) ?? orNull(r.remediation_api),
  };
}

/** In-scope grants Authorize (03d) applied but could not verify: IN_PROGRESS PRE_CONVERT LEASE_OWNERSHIP rows
 *  (wave_action_status.open_pre_convert_lease_unverified, per table). Pure. */
export function countUnverifiedLeases(actionRows: Record<string, unknown>[], inScope: Set<string>): number {
  return actionRows.filter((r) =>
    str(r.action_kind) === 'LEASE_OWNERSHIP' && str(r.status) === 'IN_PROGRESS'
    && str(r.phase) === 'PRE_CONVERT' && inScope.has(str(r.full_name))).length;
}

/**
 * The `authority` block from convert_authority_for_wave rows (every member still to convert, with
 * its latest stored verdict or none). Counts mirror authority.apply_authority_to_waves (pinned by
 * authority_rollup_cases.json); nothing here re-decides a verdict. Pure.
 */
export function buildAuthorityImpact(authorityRows: Record<string, unknown>[], unverified: number): AuthorityImpact {
  const byFn = authorityByFullName(authorityRows);
  const members = authorityRows.map((r) => ({ wave_id: 'w', full_name: r.full_name, state: r.state }));
  const w = applyAuthorityToWaves(['w'], members, byFn).get('w')!;
  const actor = [...byFn.values()].map((r) => r.actor).find((a): a is string => Boolean(a)) ?? null;
  const counts: Record<AuthorityStatus, number> = {
    [STATUS_AUTHORIZED]: 0, [STATUS_SELF_AUTHORIZABLE]: 0, [STATUS_NEEDS_GRANT]: 0, [STATUS_UNKNOWN]: 0,
  };
  const tables: AuthorityTable[] = [];
  const seen = new Set<string>();
  for (const raw of authorityRows) {
    const fn = str(raw.full_name);
    if (!fn || seen.has(fn) || !isToConvert(raw.state)) continue;
    seen.add(fn);
    const v = byFn.get(fn);
    if (v) counts[v.status] += 1;
    if (v?.status === STATUS_AUTHORIZED) continue;
    tables.push({
      full_name: fn,
      status: v?.status ?? null,
      reason: v?.reason ?? null,
      missing: v?.missing ?? [],
      self_grantable: v?.self_grantable ?? false,
      basis: v?.basis ?? null,
      basis_via: v?.basis_via ?? null,
      grant_requests: v?.grant_requests ?? [],
    });
  }
  const blocked = tables.map((t) => byFn.get(t.full_name)).filter((r): r is AuthorityRow => r !== undefined);
  return {
    actor,
    total: w.lease_total,
    not_authorized: w.open_lease,
    unevaluated: w.authority_unevaluated,
    self_grantable: w.authority_self_grantable,
    unverified,
    authorize_can_close: w.open_lease > 0 && w.authority_unevaluated === 0 && w.authority_self_grantable === w.open_lease,
    counts,
    text: waveAuthorityText({ ...w, convert_run_as: actor ?? '' }),
    tables,
    grant_script: grantScript(blocked),
  };
}

/** The deprecated `impacts.lease` alias, derived from `authority` (pure). */
export function leaseAlias(au: AuthorityImpact): ConvertImpacts['lease'] {
  return {
    gap: au.not_authorized,
    total: au.total,
    run_as_owns: au.not_authorized === 0,
    tables: au.tables.map((t) => ({ full_name: t.full_name, ownership_change: t.status ?? 'NOT_CHECKED' })),
    unknown: au.unevaluated + au.counts[STATUS_UNKNOWN],
    unverified: au.unverified,
    verified: 'stored',
    run_as: au.actor,
    owner_read_by: null,
  };
}

export function buildConvertImpacts(a: {
  /** convert_authority_for_wave rows for the pinned wave ([] when the scope has no wave). */
  authorityRows: Record<string, unknown>[];
  freezeActionRows: Record<string, unknown>[];
  scopeRows: Record<string, unknown>[];
  inScope: Set<string>;
  /** The pinned wave's members, for the unverified-grant count (defaults to inScope). */
  leaseScope?: Set<string>;
  coverage: { compaction: CoverageState; stream: CoverageState; writer: CoverageState };
  willBePaused: WriterPauseRow[];
}): ConvertImpacts {
  const authority = buildAuthorityImpact(a.authorityRows, countUnverifiedLeases(a.freezeActionRows, a.leaseScope ?? a.inScope));

  const pre = a.freezeActionRows.filter(
    (r) => str(r.phase) === 'PRE_CONVERT' && a.inScope.has(str(r.full_name)),
  );
  const compaction_actors = pre
    .filter((r) => str(r.action_kind) === 'FREEZE_COMPACTION')
    .map(toFreezeUnit);
  const streams = pre.filter((r) => str(r.action_kind) === 'STOP_STREAM').map(toFreezeUnit);

  const inScopeRows = a.scopeRows.filter((r) => a.inScope.has(str(r.full_name)));
  const anyTrue = (col: string): boolean => inScopeRows.some((r) => truthy(r[col]));

  return {
    authority,
    lease: leaseAlias(authority),
    freeze: {
      coverage: a.coverage,
      will_freeze: { compaction_actors, streams, writer_jobs: a.willBePaused },
    },
    post_convert: {
      needs_streaming_restart: anyTrue('has_streaming_writer'),
      needs_reshare: anyTrue('is_delta_shared'),
      needs_compat: anyTrue('needs_compatibility_mode'),
    },
    external_readers: externalReaders(inScopeRows),
  };
}
