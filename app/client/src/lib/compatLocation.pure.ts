/**
 * Client-side guard for the Compatibility Mode location the operator types on the Overview
 * compat CTA. Compatibility Mode needs a real EMPTY external-location path — a human storage
 * decision the tool must never invent (jobs.md §14: "Blank or placeholder paths do not produce
 * executable operations; affected tables stay manual"). This only prevents an obviously-bad
 * submit; `job_08b`'s `resolve_location` remains the authoritative gate.
 *
 * Accepts a recognized cloud-storage scheme or an absolute path, and rejects anything with
 * angle-bracket placeholders (e.g. "abfss://<container>@…").
 */
const _SCHEMES = ['abfss://', 's3://', 's3a://', 'gs://', 'wasbs://', 'dbfs:/'];

/** The path as job_08b compares it (compat._location_key): trimmed, trailing slashes dropped, so
 *  `s3://b/t` and `s3://b/t/` are one location. Applied before the duplicate check and the send. */
export function normalizeCompatLocation(raw: string | null | undefined): string {
  return (raw ?? '').trim().replace(/\/+$/, '');
}

export function isValidCompatLocation(raw: string | null | undefined): boolean {
  const s = (raw ?? '').trim();
  if (!s) return false;
  if (s.includes('<') || s.includes('>')) return false; // placeholder like <container>
  const lower = s.toLowerCase();
  if (_SCHEMES.some((p) => lower.startsWith(p))) return true;
  if (s.startsWith('/')) return true; // absolute path / UC Volume (e.g. /Volumes/…)
  return false;
}
