// External readers: UC open APIs first, Compatibility Mode only as the fallback.
// Lockstep with backend/src/migration/compat.py (guarded by compatOpenApis.pure.test.ts).

export const OPEN_APIS_DOC_URL = 'https://learn.microsoft.com/en-us/azure/databricks/external-access/';
export const UNITY_REST_DOC_URL = 'https://learn.microsoft.com/en-us/azure/databricks/external-access/unity-rest';
export const ICEBERG_REST_DOC_URL = 'https://learn.microsoft.com/en-us/azure/databricks/external-access/iceberg';
export const EXTERNAL_ACCESS_ADMIN_DOC_URL = 'https://learn.microsoft.com/en-us/azure/databricks/external-access/admin';
export const CREDENTIAL_VENDING_DOC_URL =
  'https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending';
export const COMPAT_MODE_DOC_URL =
  'https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode';

/** The fixed defer_reason that records "readers use the UC open APIs" on an ENABLE_COMPAT action. */
export const OPEN_APIS_REASON = 'Readers use UC open APIs; Compatibility Mode not needed';

/** How a recorded open-APIs decision reads on the action lists and the runbook. */
export const OPEN_APIS_NOT_NEEDED_LABEL = 'Not needed: readers use UC open APIs';

const DEFERRED_PREFIX = 'deferred:';

const norm = (text: string | null | undefined): string => (text ?? '').split(/\s+/).filter(Boolean).join(' ').toLowerCase();

/** True when an action's `resolution` records the open-APIs decision (case/whitespace tolerant). */
export function isOpenApisResolution(resolution: string | null | undefined): boolean {
  const n = norm(resolution);
  if (!n.startsWith(DEFERRED_PREFIX)) return false;
  return n.slice(DEFERRED_PREFIX.length).trim() === norm(OPEN_APIS_REASON);
}

// ---- Pre-convert reader readiness (advisory: never a gate, never a hold) ----

export const CROSS_ENGINE_ABAC_DOC_URL =
  'https://learn.microsoft.com/en-us/azure/databricks/external-access/cross-engine-abac';
export const CATALOG_COMMITS_DOC_URL =
  'https://learn.microsoft.com/en-us/azure/databricks/tables/features/catalog-commits';
export const CATALOG_COMMITS_MIN_DBR = '18.0';

/** external_writes.NOTE_COMPAT_UNAVAILABLE_RLS_CLS: external readers on a row-filter/column-mask table. */
export const NOTE_COMPAT_UNAVAILABLE_RLS_CLS = 'compat_mode_unavailable_rls_cls';

export const READER_NONE = 'NONE';
export const READER_OPEN_APIS = 'OPEN_APIS';
export const READER_COMPAT_ON = 'COMPAT_ON';
export const READER_UNDECIDED = 'UNDECIDED';
export const READER_FGAC_REQUIRED = 'FGAC_REQUIRED';
export type ReaderReadiness =
  | typeof READER_NONE
  | typeof READER_OPEN_APIS
  | typeof READER_COMPAT_ON
  | typeof READER_UNDECIDED
  | typeof READER_FGAC_REQUIRED;
export const READER_STATES: readonly ReaderReadiness[] = [
  READER_NONE,
  READER_OPEN_APIS,
  READER_COMPAT_ON,
  READER_UNDECIDED,
  READER_FGAC_REQUIRED,
];
/** The states the pre-convert warning and acknowledgement fire on. */
export const READER_ATTENTION: readonly ReaderReadiness[] = [READER_UNDECIDED, READER_FGAC_REQUIRED];

export const READERS_UNDECIDED_TEMPLATE =
  '{n} table(s) have external readers with no decision recorded. ' +
  'Readers that use a storage path stop at convert. ' +
  'Move them to the UC open APIs before the window, or plan Compatibility Mode.';
export const READERS_FGAC_TEMPLATE =
  '{n} table(s) have external readers and row filters or column masks. ' +
  'Compatibility Mode cannot serve them. Those readers stop at convert. ' +
  'After convert, set up cross-engine ABAC with catalog commits (DBR 18.0+).';

const truthy = (v: unknown): boolean => (typeof v === 'string' ? v.trim().toLowerCase() === 'true' : Boolean(v));
const upper = (v: string | null | undefined): string => (v ?? '').toUpperCase();

/**
 * One table's pre-convert external-reader state. Mirrors compat.reader_readiness rule for rule
 * (first match wins; a missing or unreadable ENABLE_COMPAT action reads UNDECIDED, never clear).
 */
export function readerReadiness(
  needsCompat: unknown,
  accessNotes: string | null | undefined,
  compatStatus: string | null | undefined,
  compatResolution: string | null | undefined,
): ReaderReadiness {
  if ((accessNotes ?? '').includes(NOTE_COMPAT_UNAVAILABLE_RLS_CLS)) return READER_FGAC_REQUIRED;
  if (!truthy(needsCompat)) return READER_NONE;
  if (upper(compatStatus) === 'DONE') return READER_COMPAT_ON;
  if (upper(compatStatus) === 'DEFERRED' && isOpenApisResolution(compatResolution)) return READER_OPEN_APIS;
  return READER_UNDECIDED;
}

/** A `reader_readiness` value read from SQL, narrowed (anything unknown or NULL reads NONE). */
export function asReaderReadiness(value: unknown): ReaderReadiness {
  const v = typeof value === 'string' ? value.toUpperCase() : '';
  return (READER_STATES as readonly string[]).includes(v) ? (v as ReaderReadiness) : READER_NONE;
}

const toCount = (n: unknown): number => {
  const v = Math.trunc(Number(n ?? 0));
  return Number.isFinite(v) && v > 0 ? v : 0;
};

/** The one canonical pre-convert warning ('' when nothing needs attention); compat.preconvert_reader_warning. */
export function preconvertReaderWarning(undecided: unknown, fgacRequired: unknown): string {
  const parts: string[] = [];
  const u = toCount(undecided);
  const f = toCount(fgacRequired);
  if (u > 0) parts.push(READERS_UNDECIDED_TEMPLATE.replace('{n}', String(u)));
  if (f > 0) parts.push(READERS_FGAC_TEMPLATE.replace('{n}', String(f)));
  return parts.join(' ');
}

/** Advisory: turn on catalog commits for a converted table (the cross-engine ABAC prerequisite). */
export function catalogCommitsSql(fullName: string): string {
  return `ALTER TABLE ${fullName} SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');`;
}

/** The grant a reader's principal needs; the principal is a human decision, never invented. */
export function externalUseGrantSql(catalog?: string | null, schema?: string | null): string {
  const scope = catalog && schema ? `${catalog}.${schema}` : '<catalog>.<schema>';
  return `GRANT EXTERNAL USE SCHEMA ON SCHEMA ${scope} TO \`<reader_principal>\`;`;
}
