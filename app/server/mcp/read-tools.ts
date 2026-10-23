import { z } from 'zod';
import { SCOPE_ARGS } from './readScope.pure.js';

export type ReadTool = { name: string; query: string; description: string; args?: z.ZodObject<z.ZodRawShape> };

export const READ_TOOLS: ReadTool[] = [
  // NOTE: mtm_get_next_step is registered directly in register-mcp.ts (allToolDefs) because it
  // takes an optional `target` and delegates to preflight for a scoped recommendation. Do not add
  // it here or it will be registered twice. Convert permission (mtm_get_convert_permissions) is
  // shaped in convert-permissions.ts for the same reason.
  { name: 'mtm_get_estate_status', query: 'migration_progress',
    description: "Per catalog.schema migration progress (totals, dry-run-ok, converted, % migrated). Read-only. Call this for 'how is the migration going / status' questions; for 'what next', call mtm_get_next_step. Optionally scope with schema/catalog.",
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_list_schema_steps', query: 'schema_steps',
    description: "Every schema in the plan with ITS OWN next step (focus mode): status (ACTIVE / DONE / HELD_ELSEWHERE / ROLLED_BACK / NEEDS_REPLAN / BLOCKED), the wave it works on next (wave i of n), work held elsewhere, drift (tables found after the plan), and the team pin rank. Read-only. Use it for 'which schemas can I work on / where does each stand'; then pass one as `target` to mtm_get_next_step.",
    args: z.object({}).strict() },
  { name: 'mtm_list_waves', query: 'wave_plan_status',
    description: 'Every wave in the current plan with state counts, bucket, planned bytes, staleness. Read-only. Optionally scope with schema/catalog — returns waves that CONTAIN that schema/catalog (a wave may span schemas).',
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_get_wave_members', query: 'wave_members', description: 'Tables in one wave with live flag badges and drift vs. the plan snapshot. Read-only.',
    args: z.object({ wave_id: z.string().describe('Wave id from mtm_list_waves.') }).strict() },
  { name: 'mtm_list_blockers', query: 'blocked_and_review',
    description: 'Tables in BLOCKED / NEEDS_REVIEW with the blocker token and remediation. Read-only. Optionally scope with schema/catalog.',
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_list_open_actions', query: 'action_queue_open',
    description: 'Open migration_actions work queue with remediation SQL/API. Read-only. Optionally scope with schema/catalog.',
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_get_runbook', query: 'migration_runbook',
    description: 'Phase-ordered manual statements (PRE_CONVERT -> CONVERT -> POST_CONVERT). Read-only. Optionally scope with schema/catalog.',
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_get_readiness', query: 'wave_gate_latest',
    description: 'Latest gate verdicts (GO/NO-GO, compaction/stream coverage) with provenance + staleness. Read-only. Optionally scope with schema/catalog.',
    args: z.object({ ...SCOPE_ARGS }).strict() },
  { name: 'mtm_get_streaming_waves', query: 'streaming_waves',
    description: 'Streaming waves and their five-step bracket state. Read-only. Optionally scope with schema/catalog.',
    args: z.object({ ...SCOPE_ARGS }).strict() },
];
