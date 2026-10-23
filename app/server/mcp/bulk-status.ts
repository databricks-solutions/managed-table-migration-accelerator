import { z } from 'zod';
import { buildBulkStatus, type BulkStatus } from './bulkStatus.pure.js';
import { MAX_ROWS, runControlQuery } from './read-query.js';

export const BULK_STATUS_TOOL_NAME = 'mtm_get_bulk_status';

export const BULK_STATUS_ARGS = z
  .object({
    batch_id: z.string().optional().describe('Optional: one parallel batch (e.g. batch_01). Omit for the recommended batch.'),
  })
  .strict();

export const BULK_STATUS_DESCRIPTION =
  'Bulk Convert status for the current plan: one rollup per parallel-safe batch (readiness and the ' +
  'freeze / authorize / convert / validate / finish work it still owes), the recommended batch (the started ' +
  'one first, else the first READY one), and that batch\u2019s ONE next step with exact args ' +
  '(`next_step.tool` + `next_step.args`). Optional `batch_id` returns that batch\u2019s next step instead. ' +
  '`next_step.exposed_over_mcp=false` means the tool is not on this endpoint \u2014 relay the ' +
  'recommendation (launch it from the app\u2019s Bulk Convert page or job_11e_bulk_cockpit). Streaming ' +
  'waves are never batched (`serial_waves`). Relay `recommendation`, `next_step`, and `do_not` VERBATIM. ' +
  'Read-only; triggers nothing.';

export interface BulkStatusDeps {
  query(name: string, extra?: Record<string, string>): Promise<Record<string, unknown>[]>;
}

const why = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** The thin edge: three reads, each optional one degraded to a note; the rollup is pure. */
export async function getBulkStatus(
  deps: BulkStatusDeps,
  args: { batch_id?: string },
  exposed: ReadonlySet<string>,
): Promise<BulkStatus> {
  const notes: string[] = [];
  let planRunId: string | null = null;
  try {
    const v = (await deps.query('current_plan'))[0]?.plan_run_id;
    planRunId = typeof v === 'string' && v.trim() ? v.trim() : null;
    if (!planRunId) notes.push('current_plan returned no plan_run_id.');
  } catch (e) {
    notes.push(`The current plan could not be read (${why(e)}).`);
  }
  const groupRows = await deps.query('wave_parallel_groups');
  if (groupRows.length >= MAX_ROWS) {
    notes.push(`wave_parallel_groups hit the ${MAX_ROWS}-row read cap, so batches beyond it are missing here.`);
  }
  let freezeRows: Record<string, unknown>[] = [];
  try {
    freezeRows = await deps.query('freeze_ledger_open');
  } catch (e) {
    notes.push(`The freeze ledger could not be read (${why(e)}); open_freeze reads 0, so an owed bulk unfreeze may not be recommended.`);
  }
  return buildBulkStatus({ planRunId, groupRows, freezeRows, batchId: args.batch_id ?? null, exposed, notes });
}

export function liveBulkStatusDeps(): BulkStatusDeps {
  return {
    query: (name, extra) => runControlQuery(name, extra),
  };
}
