// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.
//
// mtm_get_convert_permissions: shapes convert_authority_for_plan rows (blocked_only='true': the
// tables still to convert whose stored verdict is missing or not AUTHORIZED) into who must grant
// what. Every sentence comes from the Python rule (reason, ask_text) or its parity-pinned mirror
// (wave sentence, grant script); nothing here decides a verdict or names a grantor.

import {
  applyAuthorityToWaves,
  authorityByFullName,
  STATUS_UNKNOWN,
  waveAuthorityText,
} from '../../client/src/lib/convertAuthority.pure.js';
import { buildAuthorityImpact, type AuthorityTable } from './convertImpacts.pure.js';

export interface ConvertPermissionsWave {
  /** null = tables not in the current plan yet. */
  wave_id: string | null;
  not_authorized: number;
  /** authority.wave_authority_text — relay verbatim. */
  text: string;
}

export interface ConvertPermissions {
  scope: { plan_run_id: string | null; batch_id: string | null; wave_id: string | null };
  /** The identity the stored verdicts are for (the convert job's run_as). */
  actor: string | null;
  not_authorized: number;
  unevaluated: number;
  self_grantable: number;
  /** True only when Authorize (mtm_authorize_convert) can close every listed gap itself. */
  authorize_can_close: boolean;
  waves: ConvertPermissionsWave[];
  tables: AuthorityTable[];
  /** authority.grant_script: one section per grantor; the metastore-admin / MANAGE fallback last. */
  grant_script: string;
  /** The read reached the row cap, so later tables are missing. */
  truncated: boolean;
  recommendation: string;
  do_not: string[];
}

const str = (v: unknown): string =>
  typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : '';

export const CONVERT_PERMISSIONS_DO_NOT = [
  'Relay each grant request\u2019s ask_text VERBATIM \u2014 it names who can grant it. Do NOT invent a grantor or suggest transferring ownership; ownership never changes.',
  'Do NOT call mtm_authorize_convert unless authorize_can_close is true: Authorize only grants what the convert identity can grant itself, and leaves the rest MANUAL.',
];

export function shapeConvertPermissions(
  rows: readonly Record<string, unknown>[],
  scope: ConvertPermissions['scope'],
  rowCap: number,
): ConvertPermissions {
  const all = [...rows];
  const impact = buildAuthorityImpact(all, 0);
  const byFn = authorityByFullName(all);
  const waveIds = [...new Set(all.map((r) => str(r.wave_id)))].sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a < b ? -1 : a > b ? 1 : 0));
  const rollup = applyAuthorityToWaves(waveIds, all, byFn);
  const waves = waveIds.map((w) => {
    const r = rollup.get(w)!;
    // blocked_only rows: lease_total would equal open_lease, so the "N of M" form is not claimed.
    const { lease_total: _total, ...counts } = r;
    return { wave_id: w || null, not_authorized: r.open_lease, text: waveAuthorityText({ ...counts, convert_run_as: impact.actor ?? '' }) };
  });
  const truncated = all.length >= rowCap;
  let recommendation: string;
  if (impact.not_authorized === 0) {
    recommendation = truncated
      ? 'The read hit the row cap; narrow the scope (wave_id) to see every table.'
      : 'Every table still to convert in this scope is authorized for the convert identity.';
  } else if (impact.authorize_can_close) {
    recommendation = 'Authorize can grant every missing privilege itself: call mtm_authorize_convert for the wave (confirm=true), then re-check.';
  } else if (impact.unevaluated > 0 && impact.unevaluated === impact.not_authorized) {
    recommendation = 'These tables have not been checked yet: run the profiler or the dry run for their schema, then re-check.';
  } else {
    recommendation = 'Someone else must grant some of these: send each owner the grant_script section addressed to them (or relay each ask_text), then re-run Authorize to re-check.';
  }
  return {
    scope,
    actor: impact.actor,
    not_authorized: impact.not_authorized,
    unevaluated: impact.unevaluated,
    self_grantable: impact.self_grantable,
    authorize_can_close: impact.authorize_can_close,
    waves,
    tables: impact.tables,
    grant_script: impact.grant_script,
    truncated,
    recommendation: impact.counts[STATUS_UNKNOWN] > 0
      ? `${recommendation} Some verdicts are UNKNOWN (the facts could not be read): the convert gate blocks them until a re-check reads them.`
      : recommendation,
    do_not: [...CONVERT_PERMISSIONS_DO_NOT],
  };
}
