/**
 * Convert-permission coverage for one wave — the pre-convert analogue of coverage.ts's
 * freeze-bracket coverage, read from the stored `convert_authority` verdicts
 * (`convert_authority_for_wave`: every member still to convert with its latest verdict, or none).
 *
 * The verdict is decided only by the Python rule (migration/authority.py); this module counts it
 * through the parity-pinned roll-up (convertAuthority.pure.ts, authority_rollup_cases.json) and
 * decides nothing. Fail-closed: a member with no stored verdict is a gap, never a pass.
 *
 * `unevaluated` only when the query has not loaded or soft-failed (rows undefined); `none` when no
 * member still to convert is blocked; otherwise `gap`, listing the blocked tables.
 */

import { applyAuthorityToWaves, authorityByFullName, isToConvert, STATUS_AUTHORIZED } from './convertAuthority.pure';

export type OwnershipSignal = 'covered' | 'gap' | 'none' | 'unevaluated';

export interface OwnershipCoverage {
  signal: OwnershipSignal;
  /** Members still to convert the convert identity is not yet authorized for (incl. not checked yet). */
  needsLease: number;
  /** Members still to convert. */
  leaseCount: number;
  /** The blocked tables, with the stored reason (authority.authority_text) when there is a verdict. */
  tables: { fullName: string; status: string | null; reason: string | null }[];
}

/** Coverage from `convert_authority_for_wave` rows for one wave. */
export function ownershipForWave(rows: readonly Record<string, unknown>[] | null | undefined): OwnershipCoverage {
  if (rows == null) {
    return { signal: 'unevaluated', needsLease: 0, leaseCount: 0, tables: [] };
  }
  const byFn = authorityByFullName(rows);
  const w = applyAuthorityToWaves(['w'], rows.map((r) => ({ wave_id: 'w', full_name: r.full_name, state: r.state })), byFn).get('w')!;
  const seen = new Set<string>();
  const tables: OwnershipCoverage['tables'] = [];
  for (const r of rows) {
    const fn = typeof r.full_name === 'string' ? r.full_name : '';
    if (!fn || seen.has(fn) || !isToConvert(r.state)) continue;
    seen.add(fn);
    const v = byFn.get(fn);
    if (v?.status === STATUS_AUTHORIZED) continue;
    tables.push({ fullName: fn, status: v?.status ?? null, reason: v?.reason ?? null });
  }
  return { signal: w.open_lease > 0 ? 'gap' : 'none', needsLease: w.open_lease, leaseCount: w.lease_total, tables };
}
