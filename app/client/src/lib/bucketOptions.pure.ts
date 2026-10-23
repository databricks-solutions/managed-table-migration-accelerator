/**
 * The fixed bucket vocabulary + planner window hints. Mirrors backend `migration.lineage`
 * (`BUCKETS` order = first-match precedence: streaming → compat → share → final_large →
 * pilot → normal; `_WINDOW_HINTS` copy). The planner only ever emits one of these, so the
 * operator picks instead of typing. Presentation order is the vocabulary order below.
 */
export const BUCKETS: { value: string; hint: string }[] = [
  { value: 'pilot', hint: 'low blast radius — safe to convert during business hours' },
  { value: 'normal', hint: "routine tables — best converted during the owning team's off-peak hours" },
  { value: 'streaming', hint: 'has streaming — coordinated window; restart every stream reader/writer right after convert' },
  { value: 'compat', hint: 'has external (non-Databricks) readers — coordinated window; after convert move them to UC open APIs (Compatibility Mode only if they cannot)' },
  { value: 'share', hint: 'Delta-shared — coordinated window; re-share to recipients after convert (not auto-updated)' },
  { value: 'final_large', hint: 'largest tables — schedule for a low-traffic window (overnight or weekend)' },
];

/** Minimal shape of a wave_plan_status row this model reads. */
export interface BucketPlanRowLike {
  bucket: string | null;
  total_tables: number | null;
}

export interface BucketOption {
  value: string;
  hint: string;
  /** Number of waves in the current plan that carry this bucket. */
  waveCount: number;
  /** Sum of total_tables across those waves. */
  tableCount: number;
  /** True only when the plan is loaded AND this bucket has no waves — scoping to it would
   *  resolve to nothing, so the option is offered but not selectable. */
  disabled: boolean;
}

/**
 * The bucket dropdown options in vocabulary order, annotated with how many waves/tables each
 * bucket has in the current plan.
 *
 * While the plan is empty/loading (`planRows.length === 0`) nothing is disabled — we never
 * falsely disable a bucket before we know the plan. Once loaded, a bucket with zero waves is
 * disabled so the operator can't scope a convert to an empty bucket.
 */
export function bucketOptions(planRows: BucketPlanRowLike[]): BucketOption[] {
  const planLoaded = planRows.length > 0;
  return BUCKETS.map((b) => {
    const waves = planRows.filter((r) => r.bucket === b.value);
    const tableCount = waves.reduce((sum, r) => sum + (r.total_tables ?? 0), 0);
    return {
      value: b.value,
      hint: b.hint,
      waveCount: waves.length,
      tableCount,
      disabled: planLoaded && waves.length === 0,
    };
  });
}
