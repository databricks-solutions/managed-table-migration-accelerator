/**
 * Workspace-object deep links (pure).
 *
 * The Path-access locations panel turns each finding into a click-through to the offending
 * notebook (and, for JOB-attributed rows, the job page) so an operator can open the file and
 * verify path-based access. Both builders return `null` when the host or the target is missing
 * (e.g. local dev with `DATABRICKS_HOST` unset), so callers degrade to plain text — never a
 * broken `href` — matching the host-only convention `jobRunUrl` already uses.
 */

/**
 * Ensure a workspace path is rooted at `/Workspace`, the form the `#workspace` deep link
 * expects (Databricks' shareable link is `<host>/#workspace/Workspace/...`). The Workspace SDK
 * returns paths without the prefix (`/Users/...`); fixtures already carry it (`/Workspace/...`).
 * Idempotent: a path already under `/Workspace` is returned unchanged.
 */
export function normalizeWorkspacePath(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`;
  return p === '/Workspace' || p.startsWith('/Workspace/') ? p : `/Workspace${p}`;
}

/** The `?o=<workspaceId>` query segment, or `''` when the id is missing/blank. */
function orgParam(workspaceId?: string | null): string {
  return workspaceId ? `?o=${workspaceId}` : '';
}

/**
 * A path-based notebook/file deep link, or `null` when the host or path is missing. Path-based
 * (not object-id) so the link survives object recreation. When `workspaceId` is known, the
 * fully-qualified, cross-workspace-safe form is emitted (`<host>/?o=<id>#workspace<path>`,
 * the documented shareable shape); the `?o=` goes BEFORE the `#` fragment. Without it, the
 * host-only form (`<host>/#workspace<path>`) still resolves within the current session.
 */
export function workspaceObjectUrl(
  host?: string | null,
  path?: string | null,
  workspaceId?: string | null,
): string | null {
  if (!host || !path) return null;
  return `${host}/${orgParam(workspaceId)}#workspace${normalizeWorkspacePath(path)}`;
}

/**
 * A Databricks job-page deep link (`<host>/jobs/<jobId>[?o=<workspaceId>]`), or `null` when the
 * host or job id is missing.
 */
export function jobUrl(
  host?: string | null,
  jobId?: string | null,
  workspaceId?: string | null,
): string | null {
  if (!host || !jobId) return null;
  return `${host}/jobs/${jobId}${orgParam(workspaceId)}`;
}
