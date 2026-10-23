/**
 * One audit_log state cell as text for a badge, never an object.
 *
 * `from_state` / `to_state` usually hold a lifecycle state (`CONVERTED`). On `08c`'s
 * `ENABLE_AUTO_LIQUID` rows they hold the table's clustering layout as JSON
 * (`{"cluster_by_auto", "clustering_columns", "partition_columns", "source"}`), and AppKit's result
 * delivery parses a string cell that starts with `{` or `[` into an object. Rendering that object
 * as a React child crashes the page, so every cell goes through here: a layout becomes a short
 * label with the full JSON as the tooltip; any other object is shown as compact JSON.
 */
const MAX_LABEL = 48;

export interface AuditStateText {
  label: string;
  /** Full JSON for the tooltip, when the cell held an object. */
  title: string | null;
}

function parseJson(value: string): unknown {
  const t = value.trim();
  if (!(t.startsWith('{') || t.startsWith('['))) return value;
  try {
    return JSON.parse(t) as unknown;
  } catch {
    return value;
  }
}

function layoutLabel(o: Record<string, unknown>): string | null {
  if (!('cluster_by_auto' in o) && !('clustering_columns' in o)) return null;
  const keys = Array.isArray(o.clustering_columns) ? o.clustering_columns.map(String) : null;
  if (o.cluster_by_auto === true) return keys && keys.length ? `clustering: AUTO (${keys.join(', ')})` : 'clustering: AUTO';
  if (keys === null || o.cluster_by_auto === null || o.cluster_by_auto === undefined) {
    return keys && keys.length ? `clustering: keys ${keys.join(', ')}` : 'clustering: unknown';
  }
  return keys.length ? `clustering: keys ${keys.join(', ')}` : 'no clustering';
}

const clip = (s: string) => (s.length > MAX_LABEL ? `${s.slice(0, MAX_LABEL - 1)}…` : s);

export function auditStateLabel(value: unknown): AuditStateText | null {
  const v = typeof value === 'string' ? parseJson(value) : value;
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'string') return { label: v, title: null };
  if (typeof v === 'object') {
    const json = JSON.stringify(v);
    const layout = Array.isArray(v) ? null : layoutLabel(v as Record<string, unknown>);
    return { label: layout ?? clip(json), title: json };
  }
  if (typeof v === 'number' || typeof v === 'boolean') return { label: String(v), title: null };
  return null;
}
