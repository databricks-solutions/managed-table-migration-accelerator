/**
 * waveBreakdown.pure — turn `wave_plan_status` rows into the honest "which wave(s) does this
 * step cover, and how many tables each" view rendered by the Overview breakdown blocks
 * (`PrepWavesBreakdown` for the dry-run, `ConvertWavesBreakdown` for convert/resume).
 *
 * The two steps differ in scope, so the scope is an explicit input, never assumed:
 *   - PREP (job_10_campaign) is SCHEMA-scoped: the waves it covers are the `wave_plan_status`
 *     rows whose `group_key === next_group_key` (`{ mode: 'schema' }`).
 *   - CONVERT (job_04_convert) is scoped by `guidedConvertScope`: a SINGLE wave for the pilot /
 *     single-wave schemas (`{ mode: 'wave' }`), or the whole schema for a genuinely multi-wave
 *     non-pilot schema (`{ mode: 'schema' }`). The breakdown must mirror exactly what the job
 *     will act on — never the schema for a pilot that converts one wave.
 *
 * Count semantics are explicit (control_plane.md §0):
 *   - `plannedCount` = `total_tables` — the tables planned in that wave (what a dry-run re-checks).
 *   - `willActCount` = `dry_run_ok + converting` — tables convert will `SET MANAGED` (0 for a
 *     streaming wave excluded from a non-streaming convert), mirroring `impact.computeConvertImpact`
 *     so the sum equals the anchor/gate `willAct`.
 *   - `tableCount` = the headline for the active `countMode` (prep → planned; convert → willAct).
 * Pure: no React, no I/O — rows in, a view out.
 */

/** The subset of the `wave_plan_status` query result this module reads. */
export interface WaveStatusRow {
  wave_id?: string | null;
  wave_label?: string | null;
  wave_order?: number | null;
  total_tables?: number | null;
  remaining_tables?: number | null;
  group_key?: string | null;
  wave_schema?: string | null;
  schema_count?: number | null;
  wave_state?: string | null;
  // Lifecycle counts (present on wave_plan_status) — needed for willAct, mirroring WaveCounts.
  bucket?: string | null;
  dry_run_ok?: number | null;
  converting?: number | null;
  converted?: number | null;
  validated?: number | null;
  cleaned?: number | null;
  blocked?: number | null;
  rolled_back?: number | null;
}

export interface WaveLine {
  waveId: string;
  /** wave_label, falling back to wave_id when the label is null. */
  waveLabel: string;
  waveOrder: number;
  /** Headline count for the active countMode (planned total, or willAct). */
  tableCount: number;
  /** total_tables — tables planned in this wave. */
  plannedCount: number;
  /** DRY_RUN_OK + CONVERTING — tables convert will act on (0 for an excluded streaming wave). */
  willActCount: number;
  /** remaining_tables — remaining to migrate (for a tooltip; not the headline). */
  remainingCount: number;
  /** wave_state (e.g. PLANNED / DRY_RUN_OK), same vocabulary as the Waves board. */
  state: string;
  /** The wave's schema (display-only; relevant when spansSchemas). */
  schema: string | null;
  /** True for the wave equal to the trigger (next_wave_id) — where the step starts. */
  isTrigger: boolean;
  /** The identifier the row's copy button copies — see {@link waveCopyValue}. */
  copyValue: string;
}

/**
 * What a wave row's copy button copies: the wave's `catalog.schema` (its group_key at the default
 * schema grain), else the wave id. A pilot ("pilot") or catalog-grain ("main") group_key has no
 * dot and is not a pasteable schema, so those rows copy the wave id instead.
 */
export function waveCopyValue(r: Pick<WaveStatusRow, 'wave_id' | 'group_key'>): string {
  const gk = (r.group_key ?? '').trim();
  return gk.includes('.') ? gk : String(r.wave_id ?? '');
}

export interface WavesView {
  waves: WaveLine[];
  waveCount: number;
  /** Sum of tableCount across the covered waves (in the active countMode). */
  totalTables: number;
  multiWave: boolean;
  /** Any covered wave spans >1 schema (coarse catalog grain) — group_key is a catalog. */
  spansSchemas: boolean;
}

/** The resolved set of waves this step will act on. `wave` = exactly one wave (pilot / single);
 *  `schema` = every wave whose group_key matches (the schema-scoped set). */
export type BreakdownScope =
  | { mode: 'wave'; waveId: string }
  | { mode: 'schema'; groupKey: string };

/** Which per-wave metric is the headline: planned totals (prep) or willAct (convert). */
export type CountMode = 'planned' | 'willAct';

// Back-compat aliases for the prep call site + its existing tests.
export type PrepWaveLine = WaveLine;
export type PrepWavesView = WavesView;

const EMPTY: WavesView = {
  waves: [],
  waveCount: 0,
  totalTables: 0,
  multiWave: false,
  spansSchemas: false,
};

function num(v: number | null | undefined): number {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/** Runnable tables in a wave — mirrors impact.computeConvertImpact so the breakdown total
 *  equals the anchor/gate `willAct` (streaming waves contribute 0 unless included). */
function waveWillAct(r: WaveStatusRow, includeStreaming: boolean): number {
  if (!includeStreaming && (r.bucket ?? '') === 'streaming') return 0;
  return num(r.dry_run_ok) + num(r.converting);
}

/**
 * Build the wave breakdown for the resolved scope.
 *
 * @param rows  the `wave_plan_status` result (one row per wave)
 * @param opts.scope           the resolved set of waves (wave-scoped or schema-scoped)
 * @param opts.triggerWaveId   next_wave_id — the wave the step starts from (tagged)
 * @param opts.countMode       'planned' (prep) | 'willAct' (convert); default 'planned'
 * @param opts.includeStreaming whether streaming waves count toward willAct; default false
 */
export function waveBreakdownView(
  rows: ReadonlyArray<WaveStatusRow>,
  opts: {
    scope: BreakdownScope;
    triggerWaveId?: string | null;
    countMode?: CountMode;
    includeStreaming?: boolean;
  },
): WavesView {
  if (!rows || rows.length === 0) return EMPTY;
  const countMode = opts.countMode ?? 'planned';
  const includeStreaming = opts.includeStreaming ?? false;

  let matched: WaveStatusRow[];
  if (opts.scope.mode === 'wave') {
    const w = (opts.scope.waveId ?? '').trim();
    if (!w) return EMPTY;
    matched = rows.filter((r) => (r.wave_id ?? '') === w);
  } else {
    const gk = (opts.scope.groupKey ?? '').trim();
    if (!gk) return EMPTY;
    matched = rows.filter((r) => (r.group_key ?? '') === gk && (r.wave_id ?? '') !== '');
  }
  if (matched.length === 0) return EMPTY;

  const trigger = (opts.triggerWaveId ?? '').trim();
  const waves: WaveLine[] = matched
    .map((r) => {
      const waveId = String(r.wave_id);
      const plannedCount = num(r.total_tables);
      const willActCount = waveWillAct(r, includeStreaming);
      return {
        waveId,
        waveLabel: (r.wave_label ?? '').trim() || waveId,
        waveOrder: num(r.wave_order),
        tableCount: countMode === 'willAct' ? willActCount : plannedCount,
        plannedCount,
        willActCount,
        remainingCount: num(r.remaining_tables),
        state: (r.wave_state ?? '').trim().toUpperCase(),
        schema: (r.wave_schema ?? null) || null,
        isTrigger: trigger !== '' && waveId === trigger,
        copyValue: waveCopyValue(r),
      };
    })
    .sort((a, b) => a.waveOrder - b.waveOrder || a.waveId.localeCompare(b.waveId));

  return {
    waves,
    waveCount: waves.length,
    totalTables: waves.reduce((sum, w) => sum + w.tableCount, 0),
    multiWave: waves.length > 1,
    spansSchemas: matched.some((r) => num(r.schema_count) > 1),
  };
}

/**
 * Prep breakdown — schema-scoped, planned counts. A thin wrapper over {@link waveBreakdownView}
 * so the prep card and its existing tests stay unchanged.
 *
 * @param opts.groupKey       migration_next_actions.next_group_key (the dry-run's schema/group)
 * @param opts.triggerWaveId  migration_next_actions.next_wave_id (the wave prep starts from)
 */
export function prepWavesView(
  rows: ReadonlyArray<WaveStatusRow>,
  opts: { groupKey?: string | null; triggerWaveId?: string | null },
): WavesView {
  const gk = (opts.groupKey ?? '').trim();
  if (!gk) return EMPTY;
  return waveBreakdownView(rows, {
    scope: { mode: 'schema', groupKey: gk },
    triggerWaveId: opts.triggerWaveId,
    countMode: 'planned',
  });
}
