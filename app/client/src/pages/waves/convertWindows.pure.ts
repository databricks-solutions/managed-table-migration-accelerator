/** A wave's minimum slack before its next expected write; no computed window reads as a dash. */
export function slackText(minSlackMin: number | null | undefined): string {
  return minSlackMin == null ? '—' : `${minSlackMin} min`;
}
