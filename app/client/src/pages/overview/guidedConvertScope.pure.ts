/**
 * Guided-convert scope resolution for the Overview next-step card. The conductor recommends a
 * convert scope as a `target_schema` (= the wave's `group_key`) plus the `next_wave_id`. When
 * that group_key spans exactly one wave (e.g. the pilot), the schema scope and the single wave
 * are the same set — so we confirm and send the WAVE ID, matching the Waves board exactly
 * (`resolve_wave_targets(wave_ids=[w])` and `resolve_wave_targets(target_schema=group_key)`
 * select the identical wave). A genuinely multi-wave schema stays schema-scoped.
 *
 * `kind` drives the type-to-confirm label ("wave id" vs "schema"); `useWaveScope` drives which
 * selector param is sent. Keeping the two in lockstep preserves the dialog's "type what you
 * send" contract.
 */
export type ConfirmKind = 'wave' | 'schema' | 'bucket';

export interface GuidedScopeInput {
  /** migration_next_actions.next_wave_id. */
  nextWaveId: string;
  /** migration_next_actions.next_selector (= group_key); '' when the scope is wave-only. */
  nextSelector: string;
  /** Count of wave_plan_status rows whose group_key === nextSelector. */
  wavesInSelector: number;
  /** migration_next_actions.next_bucket. The pilot is deliberately one isolated wave. */
  nextBucket?: string | null;
  /** Focus mode: every convert is pinned to the scope's selected wave (never the whole schema). */
  forceWave?: boolean;
}

export interface GuidedScope {
  /** 'wave' when wave-only or a single-wave schema; otherwise 'schema'. */
  kind: ConfirmKind;
  /** The token the operator types: the wave id (wave kind) or the selector (schema kind). */
  confirmTarget: string;
  /** true -> send wave_ids (single wave); false -> send target_schema (multi-wave schema). */
  useWaveScope: boolean;
}

export function guidedConvertScope(i: GuidedScopeInput): GuidedScope {
  // The pilot's group_key is now its real catalog.schema, which also hosts the demoted
  // `normal` waves — so wavesInSelector can be >1. But "prove the tooling" must convert
  // ONLY the pilot wave, so pin the pilot to wave scope regardless of sibling waves.
  const pilotSingleWave = i.nextBucket === 'pilot' && !!i.nextWaveId;
  // Focus mode walks a schema one wave at a time, so it always confirms and sends the wave id
  // (when known — never a wave id we don't have).
  if (i.forceWave && i.nextWaveId) return { kind: 'wave', confirmTarget: i.nextWaveId, useWaveScope: true };
  // Wave-only (no selector), or a schema whose group_key spans exactly one wave — and only
  // when we actually know that wave id (never confirm a wave id we don't have).
  const singleWave =
    pilotSingleWave || (!!i.nextWaveId && (i.nextSelector === '' || i.wavesInSelector <= 1));
  if (singleWave) return { kind: 'wave', confirmTarget: i.nextWaveId, useWaveScope: true };
  return { kind: 'schema', confirmTarget: i.nextSelector, useWaveScope: false };
}
