/**
 * Convert-scope validity for the expert Convert-by-scope trigger. Mirrors the authoritative
 * engine rule in `migration.lineage.resolve_wave_targets` and the app-server refine in
 * `server/jobs/job-params.ts`:
 *
 *   - A scope needs at least one of: target_schema, wave_ids, or bucket=streaming.
 *   - wave_ids is EXCLUSIVE with target_schema/bucket.
 *   - bucket is a FILTER, not a standalone selector — a non-streaming bucket alone would
 *     convert that bucket across every schema in the plan, so it is rejected. `streaming`
 *     is the one exception (a target_schema scope excludes streaming waves, so bucket=streaming
 *     is the documented standalone selector for them).
 *
 * This is the client mirror of that rule; the server + engine remain the backstops.
 */
export const STREAMING_BUCKET = 'streaming';

export interface ConvertScopeInput {
  /** target_schema, already trimmed. */
  ts: string;
  /** bucket, already trimmed. */
  bk: string;
  /** wave_ids, already trimmed. */
  wv: string;
}

export interface ConvertScopeState {
  /** True when the scope is complete enough to enable the convert trigger. */
  hasScope: boolean;
  /** wave_ids used together with schema/bucket — a mutually-exclusive selector conflict. */
  scopeConflict: boolean;
  /** A non-streaming bucket with no schema and no wave_ids — bucket-only, which is rejected. */
  bucketOnly: boolean;
  /** Human reason the trigger is disabled, or null when it is enabled. */
  disabledReason: string | null;
  /** Widget-level XOR: once a wave is chosen, the schema picker greys out. */
  schemaDisabled: boolean;
  /** Widget-level XOR: once a wave is chosen, the bucket picker greys out. */
  bucketDisabled: boolean;
  /** Widget-level XOR: once a schema or bucket is chosen, the wave picker greys out. */
  waveDisabled: boolean;
}

const BUCKET_ONLY_REASON =
  'bucket only narrows a schema — pick a schema or wave (streaming is the exception)';
const CONFLICT_REASON = 'wave_ids is exclusive with target_schema/bucket';
const NO_SCOPE_REASON = 'Provide a scope first';

export function convertScopeState({ ts, bk, wv }: ConvertScopeInput): ConvertScopeState {
  const hasSchemaOrWave = ts !== '' || wv !== '';
  const streamingBucket = bk === STREAMING_BUCKET;
  // streaming is a valid standalone selector; any other bucket only narrows a schema.
  const hasScope = hasSchemaOrWave || streamingBucket;
  const scopeConflict = wv !== '' && (ts !== '' || bk !== '');
  const bucketOnly = bk !== '' && !streamingBucket && ts === '' && wv === '';

  let disabledReason: string | null = null;
  if (scopeConflict) disabledReason = CONFLICT_REASON;
  else if (bucketOnly) disabledReason = BUCKET_ONLY_REASON;
  else if (!hasScope) disabledReason = NO_SCOPE_REASON;

  // Widget-level mutual exclusion: wave_ids is exclusive with target_schema/bucket, so a
  // chosen wave greys the schema+bucket pickers and a chosen schema/bucket greys the wave
  // picker. This turns the post-hoc scopeConflict into a can't-select-both mode switch.
  const schemaDisabled = wv !== '';
  const bucketDisabled = wv !== '';
  const waveDisabled = ts !== '' || bk !== '';

  return {
    hasScope,
    scopeConflict,
    bucketOnly,
    disabledReason,
    schemaDisabled,
    bucketDisabled,
    waveDisabled,
  };
}
