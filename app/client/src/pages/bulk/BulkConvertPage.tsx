import { useState } from 'react';
import { Link } from 'react-router';
import {
  Badge,
  Card,
  CardContent,
} from '@databricks/appkit-ui/react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, fillCopy } from '../../lib/sectionCopy';
import { batchReadiness, orderBatchesForRecommendation, recommendedBatch } from './bulkBatches.pure';
import { openFreezeByBatch } from './openFreeze.pure';
import { BatchCard, type BatchMemberRow } from './BatchCard';

// The planner clamps parallel_degree to this cap (backend DEFAULT_BULK_MAX_PARALLEL). Shown in
// each card's "degree d (cap N)" description so the operator sees the ceiling, not just the fill.
const BULK_MAX_PARALLEL = 4;

/**
 * Bulk convert — run non-conflicting waves in parallel as a bounded campaign. It READS
 * `wave_parallel_groups ⋈ wave_status` (+ `freeze_ledger_open`), rolls them up client-side with
 * the same pure logic the notebook uses, and TRIGGERS each batch step (the four bulk jobs, Authorize, and the finish step). It computes
 * no batches, sequences nothing, and writes no state — the operator confirms each step and the
 * engine enforces every gate.
 */
export function BulkConvertPage() {
  const planQ = useMigrationQuery('current_plan', {});
  const planRunId = planQ.data?.[0]?.plan_run_id ?? null;
  const groupsQ = useMigrationQuery('wave_parallel_groups', {});
  const freezeQ = useMigrationQuery('freeze_ledger_open', {});

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <SectionHeader
          copy={SECTION_COPY.bulk.batches}
          subtitle={fillCopy(SECTION_COPY.bulk.batches.subtitle, { max: BULK_MAX_PARALLEL })}
        >
          {planRunId ? (
            <span className="font-mono text-[11px] text-muted-foreground">plan {planRunId}</span>
          ) : null}
        </SectionHeader>
        <CardContent>
          <DataSurface
            result={groupsQ}
            emptyTitle="No parallel batches yet"
            emptyDescription="Plan the waves. The planner groups waves that can convert together."
          >
            {(allRows) => {
              // open_lease is wave_status's convert-permission count (stored convert_authority
              // verdicts), so the Authorize step, the recommendation and every card read one number.
              const rows = planRunId
                ? allRows.filter((r) => r.plan_run_id === planRunId)
                : allRows;
              // The joined rows carry both group and wave_status columns, so the same rows serve
              // as both args to batchReadiness (exactly like the notebook).
              const openFreeze = openFreezeByBatch(freezeQ.data ?? [], rows);
              const rollups = batchReadiness(rows, rows, openFreeze);
              const ordered = orderBatchesForRecommendation(rollups);
              const recommendedBatchId = recommendedBatch(rollups)?.batch_id ?? null;
              const membersByBatch = new Map<string, BatchMemberRow[]>();
              for (const r of rows) {
                if (r.blocked_from_parallel) continue;
                const list = membersByBatch.get(r.batch_id);
                if (list) list.push(r);
                else membersByBatch.set(r.batch_id, [r]);
              }
              const serial = rows.filter((r) => r.blocked_from_parallel);

              return (
                <div className="flex flex-col gap-4">
                  {ordered.length === 0 ? (
                    <div className="rounded-lg border border-border bg-muted/40 p-4 text-sm text-muted-foreground">
                      No parallel-safe batches in this plan — every wave runs serially. See the
                      serial waves below, or convert them one at a time from{' '}
                      <Link to="/waves" className="font-medium underline underline-offset-2">
                        Waves
                      </Link>
                      .
                    </div>
                  ) : (
                    ordered.map((rollup) => (
                      <BatchCard
                        key={rollup.batch_id}
                        rollup={rollup}
                        recommendedBatch={rollup.batch_id === recommendedBatchId}
                        members={membersByBatch.get(rollup.batch_id) ?? []}
                        planRunId={planRunId}
                        bulkMaxParallel={BULK_MAX_PARALLEL}
                      />
                    ))
                  )}
                  <SerialWaves rows={serial} />
                </div>
              );
            }}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}

/** The collapsed "Serial waves" section — waves the planner kept out of any parallel batch,
 *  each with its `conflict_reason`. Streaming waves surface here (never in a parallel batch). */
function SerialWaves({ rows }: { rows: BatchMemberRow[] }) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;

  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        {open ? (
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        )}
        <span className="text-sm font-medium text-foreground">Serial waves</span>
        <span className="text-xs text-muted-foreground">(run one at a time)</span>
        <span className="ml-auto text-sm tabular-nums text-muted-foreground">
          {rows.length} wave{rows.length === 1 ? '' : 's'}
        </span>
      </button>
      {open ? (
        <div className="flex flex-col gap-2 border-t border-border p-4">
          {rows.map((r) => (
            <div key={r.wave_id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-mono text-[12px] text-foreground">{r.group_key || r.wave_id}</span>
              {r.conflict_reason ? (
                <Badge variant="outline" className="text-[11px]">
                  {r.conflict_reason}
                </Badge>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
