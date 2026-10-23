import { Spinner } from '@databricks/appkit-ui/react';
import { CircleCheck, Clock, TriangleAlert } from 'lucide-react';
import { useBulkConvertPreflight } from '../../hooks/useBulkConvertPreflight';
import {
  bulkPrecheckCopy,
  bulkPreflightTargets,
  rollupBulkPrechecks,
  type BulkPreflightMember,
} from './bulkPrechecks.pure';

// Labels must be unique because the rollup keys by label. Equal group_keys are a shared_schema
// conflict (parallelism.py wave_conflict_edges), so two never share a batch. Same names as BatchCard.
const memberLabel = (m: BulkPreflightMember) => m.group_key || m.wave_id;

/**
 * Per-member pre-convert checks for a bulk convert (design §8 Q2): the one-wave gate's four facts,
 * one `wave:<id>` preflight per member it converts. Render it only inside the open confirm (its
 * reads fire on mount). A preview — the bulk convert re-checks every gate per member and refuses.
 */
export function BulkPrechecksPanel({
  members,
  resume,
  cap,
}: {
  members: readonly BulkPreflightMember[];
  resume: boolean;
  cap: number;
}) {
  const { checked, unchecked } = bulkPreflightTargets(members, resume, cap);
  const results = useBulkConvertPreflight(checked.map((m) => ({ wave_id: m.wave_id, label: memberLabel(m) })));
  const summary = rollupBulkPrechecks(results);

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        Pre-convert checks · {checked.length} wave{checked.length === 1 ? '' : 's'}
        {summary.loading ? <Spinner className="size-3.5" aria-hidden /> : null}
      </div>
      <ul className="mt-2 flex flex-col gap-1.5 text-xs">
        {summary.rows.map((r) => (
          <li key={r.id} className="flex items-start gap-2">
            {r.status === 'pass' ? (
              <CircleCheck className="mt-0.5 size-4 shrink-0 text-[var(--success-foreground)]" aria-hidden />
            ) : r.status === 'blocked' ? (
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-[var(--warning-foreground)]" aria-hidden />
            ) : (
              <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
            )}
            <span className="flex flex-col">
              <span className="font-medium text-foreground">{r.label}</span>
              <span className="text-muted-foreground">{bulkPrecheckCopy(r)}</span>
            </span>
          </li>
        ))}
      </ul>
      {summary.readerAdvisory ? (
        <div className="mt-2 flex items-start gap-2 rounded-md border border-[var(--warning-border)] bg-[var(--warning-bg)] p-2 text-xs">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-[var(--warning-foreground)]" aria-hidden />
          <span className="flex flex-col">
            <span className="font-medium text-foreground">External readers · advisory</span>
            <span className="text-muted-foreground">{summary.readerAdvisory}</span>
          </span>
        </div>
      ) : null}
      {summary.failed.length > 0 ? (
        <p className="mt-2 text-xs text-[var(--warning-foreground)]">
          Couldn’t load the preflight for {summary.failed.join(', ')}, so its checks read unknown.
        </p>
      ) : null}
      {unchecked.length > 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Not checked here (over the {cap}-wave preview cap): {unchecked.map(memberLabel).join(', ')}.
        </p>
      ) : null}
      <p className="mt-2 text-xs text-muted-foreground">
        Checked against the account bulk convert runs as. This is a preview. Bulk convert checks every
        gate again on each wave and stops on a gap.
      </p>
    </div>
  );
}
