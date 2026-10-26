/**
 * Operator-facing labels for migration action kinds — the "labels map" that keeps the raw
 * ACTION_KIND enum (FREEZE_COMPACTION, STOP_WRITER_JOB, …) out of the UI. Pure so the copy is
 * unit-tested and owned in one place. Keys mirror backend actions.ALL_KINDS; an unknown kind is
 * humanized (Title Case of the words) rather than shown as a raw enum, and empty/null -> 'Action'.
 */
const _LABELS: Record<string, string> = {
  FREEZE_COMPACTION: 'Pause OPTIMIZE / compaction',
  UNFREEZE_COMPACTION: 'Resume OPTIMIZE / compaction',
  STOP_STREAM: 'Stop the streaming job',
  RESTART_STREAM: 'Restart the streaming job',
  STOP_WRITER_JOB: 'Pause the writer job',
  RESTART_WRITER_JOB: 'Restart the writer job',
  RESHARE_D2D: 'Re-share the table (Delta Sharing)',
  ENABLE_COMPAT: 'Decide how external readers connect',
  FIX_PATH_ACCESS: 'Switch consumers to name-based access',
  UPGRADE_CONSUMER_DBR: 'Upgrade the consumer runtime',
  LEASE_OWNERSHIP: 'Lease table ownership',
  RESTORE_OWNERSHIP: 'Hand back convert permission',
  RETIRE_MAINTENANCE_JOB: 'Retire the maintenance job',
  ENABLE_PREDICTIVE_OPTIMIZATION: 'Enable Predictive Optimization',
  ENABLE_AUTO_LIQUID: 'Enable automatic liquid clustering',
};

/**
 * The action kinds this map labels — mirrors backend actions.ALL_KINDS (15 kinds).
 * Exported so a parity test can pin the app-side vocabulary. NOTE: TS cannot import the
 * Python source, so this guards accidental drift WITHIN the app; when backend adds a kind to
 * ALL_KINDS, add it to _LABELS (and it appears here automatically) so the UI never shows a raw enum.
 */
export const KNOWN_ACTION_KINDS: readonly string[] = Object.keys(_LABELS);

export function actionKindLabel(kind: string | null | undefined): string {
  const k = (kind ?? '').trim().toUpperCase();
  if (!k) return 'Action';
  if (k in _LABELS) return _LABELS[k];
  return k
    .toLowerCase()
    .split('_')
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}
