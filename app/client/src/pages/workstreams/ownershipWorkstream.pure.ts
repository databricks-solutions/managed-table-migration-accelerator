/**
 * Pure view of the convert-permission workstream: `convert_authority_for_plan` rows
 * (blocked_only='true': tables still to convert whose stored verdict is missing or not
 * AUTHORIZED), split by what clears them. The verdict, reason and every grantor sentence come
 * from the Python rule (migration/authority.py); nothing here decides or names a grantor.
 */
import { PERMISSION_STATUS_LABEL, parseAuthorityRow, STATUS_NEEDS_GRANT, STATUS_SELF_AUTHORIZABLE, type AuthorityStatus } from '../../lib/convertAuthority.pure';

export interface PermissionRow {
  full_name: string;
  /** null = not checked yet (no stored verdict). */
  status: AuthorityStatus | null;
  /** authority.authority_text — rendered verbatim. */
  reason: string | null;
  /** Each grant request someone else must run: its ask_text, verbatim. */
  asks: string[];
}

export interface PermissionSplit {
  rows: PermissionRow[];
  /** Authorize (job_03d) can grant everything these need itself. */
  selfGrantable: number;
  /** Someone else must grant (the owner, a MANAGE holder or a metastore admin). */
  needsGrant: number;
  /** No stored verdict yet, or the facts could not be read (UNKNOWN): re-check first. */
  notChecked: number;
}

export { PERMISSION_STATUS_LABEL };

export function splitPermissions(raw: readonly Record<string, unknown>[]): PermissionSplit {
  const out: PermissionSplit = { rows: [], selfGrantable: 0, needsGrant: 0, notChecked: 0 };
  const seen = new Set<string>();
  for (const r of raw) {
    const fn = typeof r.full_name === 'string' ? r.full_name : '';
    if (!fn || seen.has(fn)) continue;
    seen.add(fn);
    const v = parseAuthorityRow(r);
    if (v?.status === 'AUTHORIZED') continue;
    out.rows.push({
      full_name: fn,
      status: v?.status ?? null,
      reason: v?.reason ?? null,
      asks: (v?.grant_requests ?? []).filter((g) => !g.self_grantable).map((g) => g.ask_text),
    });
    if (v?.status === STATUS_SELF_AUTHORIZABLE) out.selfGrantable += 1;
    else if (v?.status === STATUS_NEEDS_GRANT) out.needsGrant += 1;
    else out.notChecked += 1;
  }
  return out;
}
