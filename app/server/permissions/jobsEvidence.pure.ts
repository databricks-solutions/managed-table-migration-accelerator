/**
 * How the app decides the jobs-permission line (`CAN_MANAGE_RUN` for the jobs' identity on the
 * deployment's jobs) when it cannot read the jobs' access lists. The app's service principal is
 * bound to the jobs at `CAN_MANAGE_RUN`, and the Permissions API answers only `CAN_MANAGE` holders,
 * so the live read normally fails. The ladder, strongest first:
 *
 * 1. live: the access lists were read.
 * 2. recorded: a notebook or CLI install read them as the installer and wrote `INSTALL_GRANT`
 *    (SUCCESS) or `INSTALL_GRANT_SKIPPED` for this grantee on (at least) these job ids, with no
 *    later revoke. App-door rows never count: the app cannot read the lists, so its rows prove nothing.
 * 3. deployer: every job was created by the jobs' identity, which deployed it and so owns it.
 *
 * Otherwise the line stays not checked. Pure, no I/O.
 */
import {
  AUDIT_INSTALL_GRANT,
  AUDIT_INSTALL_GRANT_SKIPPED,
  AUDIT_INSTALL_REVOKE,
  AUDIT_TABLE,
  DOOR_CLI,
  DOOR_NOTEBOOK,
  EVIDENCE_DEPLOYER,
  EVIDENCE_LIVE,
  EVIDENCE_RECORDED,
  JOBS_SECURABLE,
  JOB_RUN_PRIVILEGE,
  type JobsEvidence,
  samePrincipal,
} from '../../client/src/lib/provision.pure.js';

export { EVIDENCE_DEPLOYER, EVIDENCE_LIVE, EVIDENCE_RECORDED, type JobsEvidence };

/** The install audit rows for jobs-permission lines, newest first. */
export const jobsAuditSql = (catalog: string, schema: string): string =>
  `SELECT action, phase, detail, created_at FROM ${catalog}.${schema}.${AUDIT_TABLE} ` +
  `WHERE action IN ('${AUDIT_INSTALL_GRANT}', '${AUDIT_INSTALL_GRANT_SKIPPED}', '${AUDIT_INSTALL_REVOKE}') ` +
  `AND phase = 'SUCCESS' AND get_json_object(detail, '$.securable_type') = '${JOBS_SECURABLE}' ` +
  'ORDER BY created_at DESC LIMIT 200';

const RECORDING_DOORS = new Set([DOOR_NOTEBOOK, DOOR_CLI]);

const text = (v: unknown): string => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v as number));

function parseDetail(raw: unknown): Record<string, unknown> | null {
  try {
    const d: unknown = JSON.parse(text(raw));
    return d && typeof d === 'object' ? (d as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * The newest notebook or CLI install record that the grantee holds run permission on every one of
 * `jobIds`, or null: none, or a newer revoke touched one of the jobs (a revoke with no job ids,
 * written before they were recorded, counts as touching them all).
 */
export function recordedJobsHeld(
  rows: readonly Record<string, unknown>[],
  jobIds: readonly string[],
  principals: readonly string[],
): { door: string; at: string } | null {
  if (!jobIds.length) return null;
  const newest = [...rows].sort((a, b) => text(b.created_at).localeCompare(text(a.created_at)));
  for (const r of newest) {
    if (text(r.phase) !== 'SUCCESS') continue;
    const d = parseDetail(r.detail);
    if (!d || text(d.securable_type).toUpperCase() !== JOBS_SECURABLE) continue;
    if (text(d.privilege).toUpperCase() !== JOB_RUN_PRIVILEGE) continue;
    if (!principals.some((p) => samePrincipal(text(d.grantee), p))) continue;
    const recorded = new Set((Array.isArray(d.job_ids) ? d.job_ids : []).map(text));
    const action = text(r.action);
    if (action === AUDIT_INSTALL_REVOKE) {
      if (!recorded.size || jobIds.some((j) => recorded.has(String(j)))) return null;
      continue;
    }
    if (!jobIds.every((j) => recorded.has(String(j)))) continue;
    const door = text(d.front_door);
    if (!RECORDING_DOORS.has(door)) continue;
    if (action === AUDIT_INSTALL_GRANT || (action === AUDIT_INSTALL_GRANT_SKIPPED && d.prior_held === true)) {
      return { door, at: text(r.created_at) };
    }
  }
  return null;
}

/** True when every job was created by one of `principals` (the deployer owns what it deployed). */
export function deployerHeld(
  creators: Readonly<Record<string, string | null>>,
  jobIds: readonly string[],
  principals: readonly string[],
): boolean {
  return jobIds.length > 0 && jobIds.every((j) => principals.some((p) => samePrincipal(creators[String(j)], p)));
}

/** The jobs line's held privileges and the evidence for them; `held` null = not checked. */
export function jobsLineHeld(
  live: Set<string> | null,
  recorded: { door: string; at: string } | null,
  deployer: boolean,
): { held: Set<string> | null; evidence: JobsEvidence | null } {
  if (live !== null) return { held: live, evidence: { kind: EVIDENCE_LIVE } };
  if (recorded) return { held: new Set([JOB_RUN_PRIVILEGE]), evidence: { kind: EVIDENCE_RECORDED, door: recorded.door, at: recorded.at } };
  if (deployer) return { held: new Set([JOB_RUN_PRIVILEGE]), evidence: { kind: EVIDENCE_DEPLOYER } };
  return { held: null, evidence: null };
}
