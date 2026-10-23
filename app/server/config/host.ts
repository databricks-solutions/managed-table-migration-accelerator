/**
 * The workspace host the client uses to build historical run-page deep links.
 *
 * The value originates in the `DATABRICKS_HOST` env the Apps runtime injects (the same auth
 * env `createWorkspaceClient()` uses). It is exposed to the client via GET /api/config so a
 * stored `job_run_id` (from wave_gate_latest) can become a `<host>/jobs/runs/<id>` link. In
 * local dev the var is usually unset, so this returns `null` and the feature degrades to a
 * live-run link (from active runs) or a plain-text run id — never a broken link.
 *
 * This is display-only provenance, never used for auth or a request target, so a loose host
 * is acceptable; we only normalise shape (scheme + no trailing slash), not identity.
 */
export function normalizeHost(raw?: string | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  return withScheme.replace(/\/+$/, '');
}

/**
 * The numeric workspace/org id (the `?o=` URL param), or `null` when unset/malformed.
 *
 * Originates in `DATABRICKS_WORKSPACE_ID` (bound from `var.workspace_id` in app.yml) and is
 * exposed via GET /api/config so the client can build fully-qualified path-access deep links
 * (`<host>/?o=<id>#workspace/...`). Display-only provenance, never an auth/request target, so
 * we only accept a clean all-digits id and reject anything else (returns `null` → the client
 * omits `?o=` and falls back to host-only links, which still resolve in-session).
 */
export function normalizeWorkspaceId(raw?: string | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  return /^\d+$/.test(trimmed) ? trimmed : null;
}
