/**
 * prepWaves.pure — back-compat shim. The prep-wave breakdown was generalized into
 * `waveBreakdown.pure.ts` (scope-aware, shared by prep + convert). This module re-exports the
 * prep-facing surface (`prepWavesView` + the row/view types) so the prep card and its tests keep
 * importing from here unchanged. New call sites should import from `waveBreakdown.pure` directly.
 */
export {
  prepWavesView,
  waveBreakdownView,
  type WaveStatusRow,
  type WaveLine,
  type WavesView,
  type PrepWaveLine,
  type PrepWavesView,
  type BreakdownScope,
  type CountMode,
} from './waveBreakdown.pure';
