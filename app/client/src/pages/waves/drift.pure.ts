/** Human copy for a per-table drift kind emitted by wave_members.sql. */
export function driftLabel(kind: string | null | undefined): string | null {
  switch ((kind ?? '').toUpperCase()) {
    case 'STALE':
      return 'Detected after this plan was generated — re-plan to move it to the right wave.';
    case 'PLAN_INTEGRITY':
      return 'This plan is inconsistent with the table\u2019s current signals — re-plan to reconcile.';
    default:
      return null;
  }
}

export function waveIsStale(w: { plan_stale?: boolean | null }): boolean {
  return Boolean(w.plan_stale);
}
