/**
 * Human-readable byte size — the ONE formatter shared across surfaces (Waves next-convert
 * meta line, Bulk convert batch stats) so a size never renders two different ways. TB/GB
 * drop the decimal at >= 10 (a whole number reads cleaner at scale); anything under 1 GB
 * shows whole MB, floored at 1 MB so a non-zero size never renders as "0".
 */
export function formatBytes(n: number): string {
  if (!n || n <= 0) return '0 B';
  const tb = n / 1e12;
  if (tb >= 1) return `${tb.toFixed(tb >= 10 ? 0 : 1)} TB`;
  const gb = n / 1e9;
  if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  const mb = n / 1e6;
  return `${Math.max(1, Math.round(mb))} MB`;
}
