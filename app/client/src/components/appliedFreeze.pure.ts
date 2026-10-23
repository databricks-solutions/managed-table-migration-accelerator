/**
 * Pure summary of an APPLIED freeze bracket from freeze_ledger_open rows — the convert-gate
 * continuity signal ("a freeze already ran; show what IS paused and recommend Convert").
 * Mirrors the 72h staleness threshold of backend executor.freeze_staleness. No React/network.
 */
export interface FreezeLedgerRow {
  op_kind: string | null;
  full_name: string | null;
  target_kind: string | null;
  target_id: string | null;
  status: string | null;
  frozen_at: string | null;
  wave_id: string | null;
  /** Backend lockstep (executor): a FROZEN row is only an APPLIED freeze when dry_run = false.
   *  Missing/null is treated as NOT dry-run (backward-safe) so older views still count. */
  dry_run?: boolean | null;
}

const _OP_LABELS: Record<string, string> = {
  PAUSE_JOB: 'Maintenance job paused (OPTIMIZE/VACUUM)',
  DISABLE_PO: 'Predictive Optimization disabled',
  STOP_PIPELINE: 'DLT pipeline stopped',
  STOP_STREAM_JOB: 'Streaming job stopped',
  PAUSE_WRITER_JOB: 'Writer job paused',
};

/**
 * Parse a freeze_ledger `frozen_at` value into epoch millis, timezone-safe.
 *
 * AppKit serializes a Databricks TIMESTAMP to a string with NO zone designator
 * (e.g. "2026-09-21 11:30:00"). `new Date(zonelessString)` parses that as LOCAL time,
 * so the 72h stale boundary would drift by the app host's UTC offset. Mirror the backend
 * (executor._aware treats a naive datetime as UTC): a zoneless string is read as UTC; a
 * string already carrying `Z` or a `±HH:MM` / `±HHMM` offset is parsed as-is. Returns null
 * for null/empty/unparseable input (age then unknowable → never counted toward staleness).
 */
export function parseLedgerTs(s: string | null | undefined): number | null {
  if (!s) return null;
  const trimmed = s.trim();
  if (!trimmed) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(trimmed);
  // Zone-less → normalize the date/time separator to `T` and pin to UTC.
  const iso = hasZone ? trimmed : `${trimmed.replace(' ', 'T')}Z`;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

export function appliedFreezeForScope(
  rows: FreezeLedgerRow[],
  opts: { waveId?: string | null; staleHours?: number; now?: Date },
): { applied: boolean; frozenCount: number; stale: boolean; ops: { op_kind: string; label: string; count: number }[] } {
  const staleHours = opts.staleHours ?? 72;
  const now = opts.now ?? new Date();
  const scoped = rows.filter(
    (r) =>
      (r.status ?? '').toUpperCase() === 'FROZEN' &&
      // Lockstep with backend executor (FROZEN ∧ ¬dry_run): a dry-run FROZEN row is a planned
      // capture, not real coverage. Missing/null → NOT dry-run (backward-safe), still counts.
      !r.dry_run &&
      (!opts.waveId || r.wave_id === opts.waveId),
  );
  const counts = new Map<string, number>();
  let stale = false;
  for (const r of scoped) {
    const op = (r.op_kind ?? 'UNKNOWN').toUpperCase();
    counts.set(op, (counts.get(op) ?? 0) + 1);
    const frozenMs = parseLedgerTs(r.frozen_at);
    if (frozenMs !== null) {
      const ageH = (now.getTime() - frozenMs) / 3_600_000;
      if (ageH >= staleHours) stale = true;
    }
  }
  const ops = [...counts.entries()].map(([op_kind, count]) => ({
    op_kind,
    label: _OP_LABELS[op_kind] ?? op_kind,
    count,
  }));
  return { applied: scoped.length > 0, frozenCount: scoped.length, stale, ops };
}

/** The applied-freeze summary shape consumed by the Convert gate (continuity prop). */
export type AppliedFreezeSummary = ReturnType<typeof appliedFreezeForScope>;

/**
 * Gap-aware continuity: an applied+fresh freeze SUPERSEDES the gap UI (suppress CoveragePanel /
 * WritersPanel / the Freeze-first shortcut and recommend Convert) ONLY when the live gate is not
 * a coverage gap. A `wave_gate_latest` `gap` (e.g. after a partial/failed convert) always wins —
 * a stale ledger continuity signal must never assert "safe" over a real uncovered unit
 * (SR7/SR13: no evidence of coverage is not "safe"). Mirrors backend fail-closed gating.
 */
export function freezeSupersedesGap(
  applied: AppliedFreezeSummary | undefined,
  gateState: string | null | undefined,
): boolean {
  return applied?.applied === true && !applied.stale && gateState !== 'gap';
}
