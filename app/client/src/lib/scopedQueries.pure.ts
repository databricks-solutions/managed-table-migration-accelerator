/**
 * The control-plane queries whose `.sql` declares the read-scope params `scope_catalog` /
 * `scope_schema` (added for the MCP read-scope filter). The app never scopes its reads, but the
 * params still appear in the shared SQL, and AppKit does NOT apply `@param` defaults at runtime —
 * so `useMigrationQuery` must bind the empty-string sentinel for exactly these queries (an
 * unbound `:scope_*` marker fails the statement → "Query execution failed").
 *
 * Kept in lockstep with the `.sql` files by scopedQueries.pure.test.ts (drift guard): adding a
 * scope param to any query, or a new scoped query, fails that test until this list is updated.
 */
export const SCOPED_CONTROL_QUERIES: readonly string[] = [
  'action_queue_deferred_for_wave',
  'action_queue_open',
  'action_queue_open_for_wave',
  'blocked_and_review',
  'freeze_plan_for_scope',
  'migration_progress',
  'migration_runbook',
  'optimize_layout_for_scope',
  'path_access_status',
  'streaming_waves',
  'wave_gate_latest',
  'wave_plan_status',
];

const SCOPED = new Set<string>(SCOPED_CONTROL_QUERIES);

export function isScopedControlQuery(queryKey: string): boolean {
  return SCOPED.has(queryKey);
}
