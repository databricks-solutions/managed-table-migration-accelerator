import { unverifiedLeaseText } from '../../lib/leaseNote.pure';

/**
 * The Next convert card's one line (pure). Leases 03d applied but could not verify are named with
 * the shared re-run sentence (as the conductor, MCP preflight and convert gate do); any other open
 * blocking actions keep the generic line. Missing counts read as 0.
 */
export function nextConvertNote(openBlocking: number | null | undefined, leaseUnverified: number | null | undefined): string {
  const blocking = Math.max(0, Number(openBlocking ?? 0) || 0);
  const unverified = Math.min(blocking, Math.max(0, Number(leaseUnverified ?? 0) || 0));
  const other = blocking - unverified;
  const otherLine = `${other} blocking action${other === 1 ? '' : 's'} still open — the gate fails closed until they clear.`;
  if (unverified > 0) return other > 0 ? `${unverifiedLeaseText(unverified)} ${otherLine}` : unverifiedLeaseText(unverified);
  if (other > 0) return otherLine;
  return "The lowest-order runnable wave. Blocked lower-order waves don't gate it.";
}
