/**
 * Pure helpers for OBO estate discovery + the convert-permission summary (Settings grants panel).
 * No I/O — unit-tested in estate.pure.test.ts.
 *
 * OBO discovery reflects the ADMIN'S visibility, never "the estate" (SR7): aggregateVisibility
 * just tallies what the forwarded token could list. The convert-permission summary only counts
 * the stored convert_authority verdicts; it decides nothing.
 */

export interface DiscoveredTable {
  catalog: string;
  schema: string;
  tableType: string;
}

export interface SchemaVisibility {
  catalog: string;
  schema: string;
  externalTableCount: number;
}

/**
 * One UC table-summary (`full_name` = `catalog.schema.table`, `table_type`) as a DiscoveredTable,
 * or null when the name is not exactly three non-empty parts (UC names cannot contain dots).
 */
export function tableFromSummary(fullName: unknown, tableType: unknown): DiscoveredTable | null {
  if (typeof fullName !== 'string') return null;
  const parts = fullName.split('.');
  if (parts.length !== 3 || parts.some((p) => p.trim() === '')) return null;
  return { catalog: parts[0], schema: parts[1], tableType: typeof tableType === 'string' ? tableType : '' };
}

/** Count EXTERNAL tables per catalog.schema, sorted; non-EXTERNAL ignored (case-insensitive). */
export function aggregateVisibility(rows: DiscoveredTable[]): SchemaVisibility[] {
  const counts = new Map<string, number>();
  for (const r of rows ?? []) {
    if (String(r.tableType ?? '').toUpperCase() !== 'EXTERNAL') continue;
    const key = `${r.catalog}\u0000${r.schema}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, externalTableCount]) => {
      const [catalog, schema] = key.split('\u0000');
      return { catalog, schema, externalTableCount };
    })
    .sort((a, b) => a.catalog.localeCompare(b.catalog) || a.schema.localeCompare(b.schema));
}

/** Convert-permission counts over the tables still to convert (Settings grants panel). */
export interface AuthoritySummary {
  /** The identity the stored verdicts are for (the convert job's run_as); null before any verdict. */
  actor: string | null;
  total: number;
  authorized: number;
  self_authorizable: number;
  needs_grant: number;
  unknown: number;
  /** No stored verdict yet. */
  not_checked: number;
}

const toCount = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
};

/**
 * Tally `(status, n, actor)` rows (status NULL or 'NOT_CHECKED' = no verdict yet) into an
 * AuthoritySummary. An unexpected status counts toward `unknown`: the convert gate blocks it too.
 */
export function tallyAuthority(rows: { status?: unknown; n?: unknown; actor?: unknown }[]): AuthoritySummary {
  const out: AuthoritySummary = { actor: null, total: 0, authorized: 0, self_authorizable: 0, needs_grant: 0, unknown: 0, not_checked: 0 };
  for (const r of rows ?? []) {
    const n = toCount(r.n);
    const status = typeof r.status === 'string' ? r.status.trim().toUpperCase() : '';
    out.total += n;
    if (status === '' || status === 'NOT_CHECKED') out.not_checked += n;
    else if (status === 'AUTHORIZED') out.authorized += n;
    else if (status === 'SELF_AUTHORIZABLE') out.self_authorizable += n;
    else if (status === 'NEEDS_GRANT') out.needs_grant += n;
    else out.unknown += n;
    if (!out.actor && typeof r.actor === 'string' && r.actor.trim()) out.actor = r.actor.trim();
  }
  return out;
}

/** The aggregate tallyAuthority reads: tables still to convert by their latest verdict's status. */
export function authoritySummarySql(controlFqn: string): string {
  return (
    'SELECT COALESCE(au.status, \'NOT_CHECKED\') AS status, COUNT(*) AS n, MAX(au.actor) AS actor ' +
    `FROM ${controlFqn}.table_manifest m ` +
    'LEFT JOIN (SELECT full_name, status, actor FROM (' +
    'SELECT full_name, status, actor, ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn ' +
    `FROM ${controlFqn}.convert_authority) WHERE rn = 1) au ON au.full_name = m.full_name ` +
    "WHERE UPPER(COALESCE(m.state, '')) NOT IN ('DROPPED','CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED') " +
    'GROUP BY 1'
  );
}
