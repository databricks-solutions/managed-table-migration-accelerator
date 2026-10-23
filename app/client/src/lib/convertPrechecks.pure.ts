import { preconvertReaderWarning } from './compatOpenApis.pure';
import { unverifiedLeaseText } from './leaseNote.pure';
import type { AuthorityLike } from './ownershipView.pure';

export interface PreflightLike {
  verdict?: string;
  why?: string;
  blockers?: { full_name: string; blocker_code: string }[];
  impacts?: {
    /** Convert permission from the stored convert_authority verdicts (impacts.lease is its deprecated alias). */
    authority?: AuthorityLike;
    freeze?: { coverage?: { compaction?: string | null; stream?: string | null; writer?: string | null } };
    external_readers?: { undecided?: string[]; fgac_required?: string[] };
  };
}

/** An advisory shown beside the prechecks. Never a precheck: it never changes `anyBlocked`. */
export interface PrecheckAdvisory {
  id: 'external_readers';
  label: string;
  detail: string;
  /** The tables it names, for the acknowledgement count. */
  tables: string[];
}

export type PrecheckStatus = 'pass' | 'blocked' | 'unknown';
export interface Precheck {
  id: 'dry_run' | 'permission' | 'freeze' | 'location';
  label: string;
  status: PrecheckStatus;
  detail: string;
}
export interface ConvertPrechecks {
  checks: Precheck[];
  anyBlocked: boolean;
  /** Tables the convert identity is not yet authorized for (impacts.authority.not_authorized). */
  notAuthorized: number;
  advisories: PrecheckAdvisory[];
}

export const READER_ADVISORY_LABEL = 'External readers';

/** The pre-convert external-reader advisory (compat.preconvert_reader_warning), or none. Pure. */
export function readerAdvisories(p: PreflightLike | null | undefined): PrecheckAdvisory[] {
  const readers = p?.impacts?.external_readers;
  const undecided = readers?.undecided ?? [];
  const fgac = readers?.fgac_required ?? [];
  const detail = preconvertReaderWarning(undecided.length, fgac.length);
  if (!detail) return [];
  return [{ id: 'external_readers', label: READER_ADVISORY_LABEL, detail, tables: [...undecided, ...fgac] }];
}

const LATER_THAN_DRY_RUN = new Set([
  'NEEDS_FREEZE', 'NEEDS_STREAM_STOP', 'NEEDS_WRITER_STOP', 'NEEDS_PERMISSION', 'READY_TO_CONVERT',
]);
const MANAGED_LOC_CODES = new Set(['no_managed_location', 'managed_location_unresolved']);

function freezeStatus(cov?: { compaction?: string | null; stream?: string | null; writer?: string | null }): PrecheckStatus {
  if (!cov) return 'unknown';
  const vals = [cov.compaction, cov.stream, cov.writer];
  if (vals.some((v) => v === 'UNVERIFIED')) return 'blocked';
  if (vals.every((v) => v === 'VERIFIED' || v === 'NOT_REQUIRED')) return 'pass';
  return 'unknown';
}

export function convertPrechecks(p: PreflightLike | null | undefined): ConvertPrechecks {
  const au = p?.impacts?.authority;
  const notAuthorized = au?.not_authorized ?? 0;
  // Grants Authorize applied but could not verify still hold convert (an open blocking action).
  const unverified = au?.unverified ?? 0;
  const permission: PrecheckStatus = !au
    ? 'unknown'
    : notAuthorized > 0 || unverified > 0
      ? 'blocked'
      : 'pass';
  // The blocked sentence is the stored wave sentence (authority.wave_authority_text), verbatim.
  const permissionDetail =
    permission === 'blocked'
      ? [
          ...(notAuthorized > 0 ? [au?.text || `${notAuthorized} table(s) still to convert are not yet authorized for the convert identity.`] : []),
          ...(unverified > 0 ? [unverifiedLeaseText(unverified)] : []),
        ].join(' ')
      : permission === 'pass'
        ? `${au?.actor ? `The convert identity (${au.actor})` : 'The convert identity'} is authorized for every table still to convert. Convert checks again before it changes anything.`
        : 'Convert permission could not be read from the preflight. Convert checks it again and stops before it changes anything.';

  const freeze = freezeStatus(p?.impacts?.freeze?.coverage);
  const location: PrecheckStatus = !p
    ? 'unknown'
    : (p.blockers ?? []).some((b) => MANAGED_LOC_CODES.has(b.blocker_code))
      ? 'blocked'
      : 'pass';
  const dry_run: PrecheckStatus =
    p?.verdict === 'NEEDS_DRY_RUN' ? 'blocked' : p?.verdict && LATER_THAN_DRY_RUN.has(p.verdict) ? 'pass' : 'unknown';

  const dryRunDetail =
    dry_run === 'pass'
      ? 'A dry run has recorded a readiness verdict for this scope.'
      : dry_run === 'blocked'
        ? 'No dry run has recorded readiness for this scope yet — run one first.'
        : 'Dry-run readiness could not be determined from the preflight.';
  const freezeDetail =
    freeze === 'pass'
      ? 'Compaction paused and attributed streams/writers stopped.'
      : freeze === 'blocked'
        ? 'Compaction or an attributed stream/writer is not yet frozen for this scope.'
        : 'Freeze coverage could not be determined from the preflight.';
  const locationDetail =
    location === 'pass'
      ? 'A managed storage location is configured for this scope.'
      : location === 'blocked'
        ? 'No managed storage location is configured for this scope.'
        : 'Managed-location status could not be determined from the preflight.';

  const checks: Precheck[] = [
    { id: 'dry_run', label: 'Dry run', status: dry_run, detail: dryRunDetail },
    { id: 'permission', label: 'Permission', status: permission, detail: permissionDetail },
    { id: 'freeze', label: 'Freeze', status: freeze, detail: freezeDetail },
    { id: 'location', label: 'Managed location', status: location, detail: locationDetail },
  ];
  return {
    checks,
    anyBlocked: checks.some((c) => c.status === 'blocked'),
    notAuthorized,
    advisories: readerAdvisories(p),
  };
}
