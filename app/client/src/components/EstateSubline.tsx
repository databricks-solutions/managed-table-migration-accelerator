import { Link } from 'react-router';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { estateSubline } from '../pages/overview/estateSubline.pure';

/**
 * EstateSubline — the ONE shared "N planned of M discovered … · K held back" line.
 *
 * Every surface that names the estate scope (Overview, Waves, Coverage) renders this exact
 * string from the SAME precomputed `migration_next_actions` row, so the denominator can never
 * diverge between pages. It reads the conductor's `estate_*` fields directly (no client-side
 * aggregation) and reconciles the discovered-vs-planned gap: `discovered = planned + held back`,
 * where held-back tables are the exact Blocked & review rows (`estate_ineligible_tables`).
 *
 * Degrades honestly: renders nothing until a next-actions row exists (pre-conductor / empty
 * control plane) rather than fabricating a zero-estate line.
 */
export function EstateSubline({ className }: { className?: string }) {
  const q = useMigrationQuery('migration_next_actions', {});
  const n = q.data?.[0];
  if (!n) return null;
  const heldBack = n.estate_ineligible_tables;
  return (
    <p className={className ?? 'text-xs text-muted-foreground'}>
      {estateSubline({
        planned: n.estate_total_tables,
        discovered: n.estate_discovered_tables,
        schemas: n.estate_total_schemas,
        migrated: n.estate_migrated_tables,
        remaining: n.estate_remaining_tables,
        heldBack,
      })}
      {heldBack > 0 && (
        <>
          {' — '}
          <Link to="/blocked" className="font-medium text-primary hover:underline">
            see Blocked &amp; review
          </Link>
        </>
      )}
    </p>
  );
}
