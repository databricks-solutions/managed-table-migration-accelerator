/**
 * happyPath.pure — the teaching math behind the Happy Path module's Swimlane spine. Pure and
 * deterministic (no React, no I/O) so the graphic just renders its output and the geometry is
 * unit-tested. The Swimlane pins a lifecycle spine of N stage-nodes and travels a dot to the
 * active node as the reader scrolls; `dotLeftPercent` centres that dot over its node.
 */

/** Clamp an index into `[0, count - 1]` (integers), guarding an empty spine. */
export function clampNodeIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  return Math.max(0, Math.min(count - 1, Math.trunc(index)));
}

/**
 * Left offset (%) of the travelling dot for the node at `index` of a `count`-node spine —
 * centred over the node (nodes are laid out as equal flex cells). Clamps out-of-range indices
 * to the ends; returns 0 for an empty spine.
 */
export function dotLeftPercent(index: number, count: number): number {
  if (count <= 0) return 0;
  const safe = clampNodeIndex(index, count);
  return ((safe + 0.5) / count) * 100;
}
