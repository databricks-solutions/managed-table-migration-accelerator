/**
 * Pure guard for the GUARDED re-plan control. Node-testable (no React).
 */

/**
 * True when any wave is mid-conversion — a state in which re-planning would overwrite the
 * wave_plan and invalidate the (plan_run_id, wave_id) the running job_04 is pinned to. The
 * UI disables re-plan on this; job_02's own fail-closed CONVERTING guard is the backstop.
 * A wave counts as converting if its state is CONVERTING or it has any converting members.
 */
export function anyConverting(
  rows: ReadonlyArray<{ wave_state?: string | null; converting?: number | null }>,
): boolean {
  return rows.some(
    (r) => (r.wave_state ?? '').toUpperCase() === 'CONVERTING' || Number(r.converting ?? 0) > 0,
  );
}
