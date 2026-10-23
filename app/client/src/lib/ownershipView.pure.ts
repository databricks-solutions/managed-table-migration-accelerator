/**
 * OwnershipView — the SINGLE convert-permission model the convert gate renders from.
 *
 * Both sources read the stored `convert_authority` verdicts (decided only by
 * migration/authority.py), so they can never disagree:
 *   - `viewFromAuthority` (PRIMARY) — the preflight's `impacts.authority`: counts, the stored wave
 *     sentence, each blocked table's reason / missing privileges / grant requests, and the grant
 *     script. Returns null when there is no authority block (preflight unloaded).
 *   - `viewFromCoverage` (FALLBACK) — `ownershipForWave(convert_authority_for_wave)`: the same
 *     counts, reasons and wave sentence, without grant requests (`source:'plan'`).
 *
 * Pure: no I/O, no verdict logic. job_04_convert re-checks live and stops before any change.
 */
import type { OwnershipCoverage } from './ownershipCoverage.pure';
import { unverifiedLeaseText } from './leaseNote.pure';
import { waveAuthorityText, type AuthorityStatus, type GrantRequest } from './convertAuthority.pure';

export type OwnershipViewSignal = 'covered' | 'gap' | 'none' | 'unevaluated';

export interface PermissionTable {
  fullName: string;
  /** null = not checked yet (no stored verdict). */
  status: AuthorityStatus | null;
  /** authority.authority_text — render verbatim. */
  reason: string | null;
  missing: string[];
  basis: string | null;
  basisVia: string | null;
  /** Each request's ask_text names who can grant it — render verbatim. */
  requests: GrantRequest[];
}

export interface OwnershipView {
  /** covered = every table still to convert is authorized; gap = something is not; none = nothing to
   *  convert; unevaluated = the wave's verdicts could not be read (the plan fallback only). */
  signal: OwnershipViewSignal;
  /** stored = the preflight's impacts.authority (with grant requests); plan = the wave counts only. */
  source: 'stored' | 'plan';
  /** Tables still to convert. */
  total: number;
  /** Tables the convert identity is not yet authorized for (incl. not checked yet). */
  needsAction: number;
  /** Of those, the ones with no stored verdict yet. */
  unevaluated: number;
  /** Of those, the ones Authorize can grant itself. */
  selfGrantable: number;
  /** Grants Authorize applied but could not verify (IN_PROGRESS LEASE_OWNERSHIP): they hold convert. */
  unverified: number;
  /** The shared re-run sentence when `unverified > 0`, else null. */
  unverifiedNote: string | null;
  /** Authorize can close the whole gap itself (every blocked table checked and self-grantable). */
  authorizeCanClose: boolean;
  /** The identity the verdicts are for (the convert job's run_as), when known. */
  actor: string | null;
  /** authority.wave_authority_text — render verbatim; null when nothing is blocked. */
  text: string | null;
  /** authority.grant_script over the blocked tables; null on the plan fallback. */
  grantScript: string | null;
  tables: PermissionTable[];
}

/** The `impacts.authority` shape (mirror of ConvertImpacts.authority, all fields optional for safety). */
export interface AuthorityLike {
  actor?: string | null;
  total?: number;
  not_authorized?: number;
  unevaluated?: number;
  self_grantable?: number;
  unverified?: number;
  authorize_can_close?: boolean;
  text?: string;
  grant_script?: string;
  tables?: {
    full_name: string;
    status: AuthorityStatus | null;
    reason?: string | null;
    missing?: string[];
    basis?: string | null;
    basis_via?: string | null;
    grant_requests?: GrantRequest[];
  }[];
}

function signalFor(needsAction: number, unverified: number, total: number): OwnershipViewSignal {
  return needsAction > 0 || unverified > 0 ? 'gap' : total > 0 ? 'covered' : 'none';
}

/** PRIMARY: project the preflight's `impacts.authority`. Null when there is no authority block. */
export function viewFromAuthority(au: AuthorityLike | null | undefined): OwnershipView | null {
  if (!au) return null;
  const needsAction = au.not_authorized ?? 0;
  const unverified = au.unverified ?? 0;
  const total = au.total ?? needsAction;
  return {
    signal: signalFor(needsAction, unverified, total),
    source: 'stored',
    total,
    needsAction,
    unevaluated: au.unevaluated ?? 0,
    selfGrantable: au.self_grantable ?? 0,
    unverified,
    unverifiedNote: unverified > 0 ? unverifiedLeaseText(unverified) : null,
    authorizeCanClose: au.authorize_can_close === true,
    actor: au.actor ?? null,
    text: au.text || null,
    grantScript: au.grant_script || null,
    tables: (au.tables ?? []).map((t) => ({
      fullName: t.full_name,
      status: t.status ?? null,
      reason: t.reason ?? null,
      missing: t.missing ?? [],
      basis: t.basis ?? null,
      basisVia: t.basis_via ?? null,
      requests: t.grant_requests ?? [],
    })),
  };
}

/** FALLBACK: the wave's stored verdict counts (no grant requests, so Authorize is never offered from it). */
export function viewFromCoverage(cov: OwnershipCoverage): OwnershipView {
  const unevaluated = cov.tables.filter((t) => t.status === null).length;
  const selfGrantable = cov.tables.filter((t) => t.status === 'SELF_AUTHORIZABLE').length;
  const gap = cov.signal === 'gap';
  return {
    signal: cov.signal === 'unevaluated' ? 'unevaluated' : gap ? 'gap' : cov.leaseCount > 0 ? 'covered' : 'none',
    source: 'plan',
    total: cov.leaseCount,
    needsAction: cov.needsLease,
    unevaluated,
    selfGrantable,
    unverified: 0,
    unverifiedNote: null,
    authorizeCanClose: false,
    actor: null,
    text: gap
      ? waveAuthorityText({
          open_lease: cov.needsLease,
          lease_total: cov.leaseCount,
          authority_unevaluated: unevaluated,
          authority_self_grantable: selfGrantable,
        })
      : null,
    grantScript: null,
    tables: cov.tables.map((t) => ({ fullName: t.fullName, status: t.status as AuthorityStatus | null, reason: t.reason, missing: [], basis: null, basisVia: null, requests: [] })),
  };
}

/** The gate's permission view: the preflight's authority block when loaded, else the wave counts. */
export function gateOwnership(args: {
  authority: AuthorityLike | null | undefined;
  ownership: OwnershipCoverage | null | undefined;
}): OwnershipView | null {
  return viewFromAuthority(args.authority) ?? (args.ownership ? viewFromCoverage(args.ownership) : null);
}
