import { z } from 'zod';
import type { IJobsConfig } from '@databricks/appkit';

/**
 * Run-now parameter schemas for the 19 app-triggerable jobs, from the backend
 * docs/contracts/jobs.md (post-BE-02 promotions). ONLY run-now-settable params appear;
 * deploy-fixed and widget-only params are excluded — the job ignores them silently, so
 * .strict() REJECTS any that are sent (a loud 400 beats a silent no-op).
 *
 * Alias keys are the SHORT names (profiler, wave_planner, …), not the bundle resource
 * keys (job_00_profiler, …) — mirroring those would produce DATABRICKS_JOB_JOB_00_PROFILER.
 * (The former standalone `source_scan` alias was folded into `profiler` — the discovery chunk.)
 * Job IDs come from bundle resolution: each alias maps to DATABRICKS_JOB_<ALIAS_UPPER>
 * (e.g. `profiler` → DATABRICKS_JOB_PROFILER). This module supplies only the params
 * schemas; the plugin merges them with the env-resolved IDs.
 *
 * taskType is intentionally NOT set: the contract documents param names, not task types,
 * and this plugin maps params to task-specific SDK fields (notebook_params, …) with no
 * job_parameters option. Setting the wrong taskType would send params the job silently
 * ignores — the T3 failure. The send-mapping must be resolved per job's actual task type
 * before triggers fire; validation (the guard) runs before mapping and is correct now.
 */

const s = () => z.string().optional();
const b = () => z.boolean().optional();
const i = () => z.number().int().optional();

/**
 * Shared convert building blocks — used by the `convert` alias here AND the MCP's tightened
 * mtm_convert schema (app/server/mcp/job-tools.ts). Factored so the two schemas cannot drift.
 */
export const CONVERT_SELECTOR_FIELDS = {
  target_schema: s(),
  wave_ids: s(), // csv
  bucket: s(),
  include_streaming: b(),
  confirm_compaction_paused: b(),
  convert_max_tables_per_wave: i(),
  recent_optimize_minutes: i(),
} as const;

/** CLI/notebook-only gate-downgrade knobs — deliberately absent from the MCP tool schema. */
export const CONVERT_DOWNGRADE_FIELDS = {
  require_pre_convert_actions_cleared: b(),
  require_compaction_frozen: b(),
  require_streams_stopped: b(),
  require_writers_stopped: b(),
} as const;

/** Attach job_04's three selector rules (verbatim messages) to any object with the selector fields. */
export function applyConvertSelectorRules<T extends z.ZodObject<z.ZodRawShape>>(schema: T) {
  return schema
    // Selector rules (encoded here, not in the UI): at least one scope; wave_ids is
    // exclusive with target_schema/bucket; target_schema+bucket together is allowed.
    // "no matching wave" is a runtime error from select_waves, not a static rule.
    .refine(
      (v) => !!v.wave_ids || !!v.target_schema || !!v.bucket,
      { message: 'Provide a scope: at least one of wave_ids, target_schema, or bucket.' },
    )
    .refine(
      (v) => !(!!v.wave_ids && (!!v.target_schema || !!v.bucket)),
      { message: 'wave_ids is exclusive with target_schema/bucket — pass wave_ids alone, or scope by target_schema and/or bucket.' },
    )
    // bucket is a filter, not a standalone selector (mirrors lineage.resolve_wave_targets):
    // a non-streaming bucket alone would convert that bucket across every schema in the plan.
    // streaming is exempt — a target_schema scope excludes streaming waves, so bucket=streaming
    // is the one bucket that is a valid standalone selector.
    .refine(
      (v) => !(!!v.bucket && v.bucket !== 'streaming' && !v.target_schema && !v.wave_ids),
      { message: "bucket is a filter (except 'streaming') — add target_schema, or use wave_ids." },
    );
}

/**
 * 08 / 08b finish-action fields (jobs.md §13/§14). `defer` sets re-share / Compat actions the tool
 * can't close aside (DEFERRED, stops holding the wave); `reopen` returns them to OPEN. `action_keys`
 * (comma-separated) narrows either to named actions — the app's per-action buttons send exactly one.
 */
export const FINISH_ACTION_FIELDS = {
  finish_action: z.enum(['run', 'defer', 'reopen']).optional(),
  defer_reason: s(),
  action_keys: s(),
} as const;

/**
 * M6.1 bulk selector: every wave in this parallel batch (requires plan_run_id). The jobs take it
 * FIRST, then the wave pin, then target_schema (they ignore the others; they do not raise if both
 * are given). Omit when blank. App-only while bulk is withheld: the MCP schemas omit it.
 */
export const BATCH_SELECTOR_FIELD = { batch_id: s() } as const;

// Authorize (03d, alias lease_ownership): self-grant the convert identity's missing privileges.
// Same run-now selector shape as freeze/unfreeze_verify. operator is NOT here — Plan 1 injects it
// server-side; .strict() would reject a client-sent one. The job's table_owner_group parameter is
// deprecated (ignored), so the app never sends it.
export const LEASE_OWNERSHIP_FIELDS = {
  plan_run_id: s(),
  wave_id: s(),
  target_schema: s(),
  confirm_execute: b(),
} as const;

export const RESHARE_FIELDS = {
  plan_run_id: s(),
  wave_id: s(),
  target_schema: s(),
  confirm_execute: b(),
  ...FINISH_ACTION_FIELDS,
} as const;

export const ENABLE_COMPAT_FIELDS = {
  plan_run_id: s(),
  wave_id: s(),
  target_schema: s(),
  confirm_execute: b(),
  compat_location: s(),
  compat_location_map: s(), // JSON-encoded string (job params must be primitives)
  ...FINISH_ACTION_FIELDS,
} as const;

// 08c POST_CONVERT maintenance: enable Predictive Optimization (opt-in) + automatic liquid
// clustering (opt-out). confirm_execute defaults to dry-run in the UI. operator is NOT here —
// job-trigger.ts injects it after the strict parse. Mirrors backend/resources/
// job_08c_enable_optimization.yml params.
export const ENABLE_OPTIMIZATION_FIELDS = {
  plan_run_id: s(),
  wave_id: s(),
  target_schema: s(),
  enable_po_on_convert: b(),
  auto_liquid_policy: z.enum(['enable_eligible', 'recommend', 'off']).optional(),
  confirm_execute: b(),
} as const;

/**
 * The profiler's read-only discovery knobs (profile + optional source scan). The MCP
 * mtm_run_profiler advertises ONLY these, so the read-only tool can never record an opt-in.
 */
export const PROFILER_DISCOVERY_FIELDS = {
  run_profiler: b(),
  run_source_scan: b(),
  force_rescan: b(),
  catalog_filter: s(), // glob
  schema_filter: s(), // glob
  scan_mode: z.enum(['targeted_jobs', 'broad']).optional(),
  workspace_paths: s(), // source scan, advanced
  exclude_paths: s(), // source scan, advanced
  max_notebooks: i(), // source scan, advanced
} as const;

/**
 * Path-based redirect opt-in (job_00_profiler's gated 01b task). `none` is the job default and
 * excludes the task. The engine re-checks eligibility in the job; these rules only reject calls
 * the job would refuse or ignore. Shared with the MCP mtm_opt_in_path_redirect schema.
 */
export const PATH_REDIRECT_ACTIONS = ['none', 'accept', 'revoke'] as const;
export const PATH_REDIRECT_FIELDS = {
  path_redirect_action: z.enum(PATH_REDIRECT_ACTIONS).optional(),
  path_redirect_tables: s(), // full names, comma-separated (mirrors path_access.parse_optin_tables)
  path_redirect_reason: s(),
  confirm_execute: b(),
} as const;

/** Mirrors path_access.parse_optin_tables: split on commas, semicolons, or whitespace. */
export const namesOptInTable = (v: unknown): boolean =>
  typeof v === 'string' && v.split(/[,;\s]+/).some((t) => t.trim() !== '');

const isOptInAction = (a: unknown): boolean => a === 'accept' || a === 'revoke';

export function applyPathRedirectRules<T extends z.ZodObject<z.ZodRawShape>>(schema: T) {
  return schema
    // The one no-op combo: run_profiler=false with neither the scan nor an opt-in requested.
    // Defaults (all omitted) are profiler-only. Source-scan-only and opt-in-only are both valid.
    .refine(
      (v) => !(v.run_profiler === false && v.run_source_scan !== true && !isOptInAction(v.path_redirect_action)),
      { message: 'With run_profiler=false you must set run_source_scan=true or a path_redirect_action (nothing would run otherwise).' },
    )
    .refine(
      (v) => !isOptInAction(v.path_redirect_action) || namesOptInTable(v.path_redirect_tables),
      { message: 'path_redirect_action needs path_redirect_tables (there is no implicit opt-in-all).' },
    )
    .refine(
      (v) =>
        v.path_redirect_action !== 'accept' ||
        (typeof v.path_redirect_reason === 'string' && v.path_redirect_reason.trim() !== ''),
      { message: 'path_redirect_action=accept requires a non-blank path_redirect_reason.' },
    )
    .refine(
      (v) => isOptInAction(v.path_redirect_action) || (!v.path_redirect_tables && !v.path_redirect_reason && v.confirm_execute === undefined),
      { message: 'path_redirect_tables, path_redirect_reason, and confirm_execute apply only with path_redirect_action=accept|revoke.' },
    );
}

/**
 * Team pins (job_08_conductor's gated pin task, `migration.focus`). `none` is the job default and
 * excludes the task, so a plain recommendation refresh (`{}`) is unchanged. The job re-validates
 * against the current plan (scope must be in it, ≤10 pins); these rules only reject calls the job
 * would refuse or ignore. Shared with the MCP mtm_set_focus_pins schema.
 */
export const PIN_ACTIONS = ['none', 'add', 'remove', 'move', 'clear'] as const;
export const PIN_FIELDS = {
  pin_action: z.enum(PIN_ACTIONS).optional(),
  pin_scope: s(), // one group_key (a catalog.schema, or a catalog under catalog grain)
  // 1 = first. A whole number (MCP) or its string (the app's trigger params are strings).
  pin_rank: z.union([z.number().int().min(1), z.string().regex(/^[1-9][0-9]*$/)]).optional(),
  pin_reason: s(),
} as const;

const isPinChange = (a: unknown): boolean => typeof a === 'string' && a !== 'none';
const nonBlank = (v: unknown): boolean => typeof v === 'string' && v.trim() !== '';

export function applyPinRules<T extends z.ZodObject<z.ZodRawShape>>(schema: T) {
  return schema
    .refine(
      (v) => !(v.pin_action === 'add' || v.pin_action === 'remove' || v.pin_action === 'move') || nonBlank(v.pin_scope),
      { message: 'pin_action=add|remove|move needs pin_scope (one schema in the current plan).' },
    )
    .refine((v) => v.pin_action !== 'add' || nonBlank(v.pin_reason), {
      message: 'pin_action=add requires a non-blank pin_reason.',
    })
    .refine((v) => v.pin_action !== 'move' || v.pin_rank !== undefined, {
      message: 'pin_action=move needs pin_rank.',
    })
    .refine(
      (v) => isPinChange(v.pin_action) || (v.pin_scope === undefined && v.pin_rank === undefined && v.pin_reason === undefined),
      { message: 'pin_scope, pin_rank, and pin_reason apply only with pin_action=add|remove|move|clear.' },
    );
}

/**
 * The 409 active-run guard is skipped for a team-pin change only: job_08_conductor queues
 * overlapping runs (every stage tails it, so a refresh is often running), and its pin task reads
 * the pin list fresh. A plain refresh keeps the guard.
 */
export function skipsActiveRunGuard(alias: string, params: Record<string, unknown>): boolean {
  return alias === 'conductor' && isPinChange(params.pin_action);
}

/** True when a comma-separated action_keys value names at least one key (mirrors actions.parse_action_keys). */
const namesActionKey = (v: unknown): boolean =>
  typeof v === 'string' && v.split(',').some((k) => k.trim() !== '');

/** The finish-action rules the jobs enforce, encoded here so a bad call is a loud 400. */
export function applyFinishActionRules<T extends z.ZodObject<z.ZodRawShape>>(schema: T) {
  return schema
    .refine(
      (v) =>
        v.finish_action !== 'defer' ||
        (typeof v.defer_reason === 'string' && v.defer_reason.trim() !== ''),
      { message: 'finish_action=defer requires a non-blank defer_reason.' },
    )
    // A set-but-empty value (',' / '  ') would narrow to nothing, which the job reads as "every
    // action in scope" — reject it outright rather than let it widen a defer/reopen.
    .refine(
      (v) => !v.action_keys || namesActionKey(v.action_keys),
      { message: 'action_keys names no action key (separators/whitespace only) — blank it or name the keys.' },
    )
    .refine(
      (v) => !namesActionKey(v.action_keys) || v.finish_action === 'defer' || v.finish_action === 'reopen',
      { message: 'action_keys narrows finish_action=defer|reopen only.' },
    );
}

export const migrationJobs: IJobsConfig = {
  jobs: {
    // The `profiler` alias is the DISCOVERY CHUNK (job_00_profiler): the profiler plus an
    // OPTIONAL, gated tier-2 source scan (job_01_source_scan, folded in — no separate binding).
    // Two toggles pick which halves run — profiler only (default), source scan only, or both.
    // The scan knobs (scan_mode/workspace_paths/exclude_paths/max_notebooks) are consumed only
    // when run_source_scan=true; scan_source is NOT settable here (the job's condition gate
    // decides, then hardcodes it). A third gate records the path-based redirect opt-in
    // (path_redirect_action=accept|revoke), the job's one mutating, audited mode — so `profiler`
    // IS in OPERATOR_ALIASES.
    profiler: {
      params: applyPathRedirectRules(z.object({
        ...PROFILER_DISCOVERY_FIELDS,
        ...PATH_REDIRECT_FIELDS,
      }).strict()),
    },
    wave_planner: {
      params: z.object({
        pilot_schema: s(),
        pilot_max_tables: i(),
        wave_max_tables: i(),
        wave_max_tb: z.number().optional(), // float
        wave_grain: z.enum(['catalog_schema', 'catalog']).optional(),
      }).strict(),
    },
    dry_run: {
      params: z.object({
        catalog_filter: s(),
        schema_filter: s(),
        size_band_filter: z.enum(['*', '<=100GB', '~1TB', '~10TB', '>10TB']).optional(),
        recent_optimize_minutes: i(),
      }).strict(),
    },
    freeze: {
      params: z.object({
        plan_run_id: s(),
        wave_id: s(),
        target_schema: s(),
        confirm_execute: b(),
      }).strict(),
    },
    lease_ownership: {
      params: z.object({ ...LEASE_OWNERSHIP_FIELDS, ...BATCH_SELECTOR_FIELD }).strict(),
    },
    // T3: recent_write_minutes is widget-only (excluded). allow_restart is fixed false (excluded).
    // plan_run_id/wave_id are supplied INTERNALLY by select_waves. Scope is target_schema / wave_ids
    // / bucket only. Selector + downgrade fields + rules are factored (see above) so the MCP's
    // tightened mtm_convert schema reuses the SAME fields/refinements and cannot drift.
    convert: {
      params: applyConvertSelectorRules(
        z.object({ ...CONVERT_SELECTOR_FIELDS, ...CONVERT_DOWNGRADE_FIELDS }).strict(),
      ),
    },
    validate: {
      params: z.object({
        catalog_filter: s(),
        schema_filter: s(),
      }).strict(),
    },
    unfreeze_verify: {
      params: z.object({
        plan_run_id: s(),
        wave_id: s(),
        target_schema: s(),
        confirm_execute: b(),
      }).strict(),
    },
    // ── CONFIRMATION-FLAG ASYMMETRY — READ BEFORE EDITING rollback / cleanup ───────────
    // Most mutating jobs are safe-by-default at the job layer: freeze/unfreeze_verify/
    // reshare/enable_compat default confirm_execute=false (dry-run), and convert requires
    // confirm_compaction_paused=true. rollback and cleanup have NO job-level confirmation
    // flag — they rely ENTIRELY on these app-layer guards:
    //   rollback → a non-blank `reason` (required below)
    //   cleanup  → a non-empty `full_name` (T1, required below)
    // Weaken either and the only guard against an accidental rollback / estate-wide
    // VACUUM is gone.
    rollback: {
      params: z.object({
        full_name: s(),
        plan_run_id: s(),
        wave_id: s(),
        reason: z.string().min(1), // required, non-blank — the ONLY guard on rollback
      }).strict(),
    },
    // T1: full_name is MANDATORY at the app layer — blank = irreversible estate-wide
    // VACUUM, and the app is the only guard (paused schedule runs in batch mode). The
    // call is unconstructable without it.
    cleanup: {
      params: z.object({
        full_name: z.string().min(1),
        max_tables: i(),
      }).strict(),
    },
    // Recommendation refresh ({}) plus the gated team-pin change (pin_action != none).
    conductor: {
      params: applyPinRules(z.object({ ...PIN_FIELDS }).strict()),
    },
    reshare: {
      params: applyFinishActionRules(z.object({ ...RESHARE_FIELDS, ...BATCH_SELECTOR_FIELD }).strict()),
    },
    enable_compat: {
      params: applyFinishActionRules(z.object({ ...ENABLE_COMPAT_FIELDS, ...BATCH_SELECTOR_FIELD }).strict()),
    },
    enable_optimization: {
      params: z.object({ ...ENABLE_OPTIMIZATION_FIELDS, ...BATCH_SELECTOR_FIELD }).strict(),
    },
    campaign: {
      params: z.object({
        target_schema: s(),
        confirm_compaction_paused: b(),
        recent_optimize_minutes: i(),
        include_streaming: b(),
      }).strict(),
    },
    // ── BULK (parallel-wave) jobs — N1: the app can NEVER weaken a gate ────────────────
    // bulk_convert deliberately OMITS every gate-downgrade knob the job accepts
    // (require_compaction_frozen, require_streams_stopped, require_writers_stopped,
    // require_pre_convert_actions_cleared, require_owning_workspace) and the tuning knobs the app never sets
    // (convert_max_tables_per_wave, recent_optimize_minutes). Because .strict() REJECTS any
    // unlisted param, leaving them off makes it STRUCTURALLY impossible for the app to send a
    // `require_*=false` downgrade — that escape hatch stays notebook/CLI-only. The other bulk
    // jobs (freeze/validate/unfreeze) accept only their run-now batch selectors + confirm flag.
    bulk_freeze: {
      params: z.object({
        plan_run_id: s(),
        batch_id: s(),
        confirm_execute: b(),
      }).strict(),
    },
    bulk_convert: {
      params: z.object({
        plan_run_id: s(),
        batch_id: s(),
        confirm_compaction_paused: b(),
        owning_workspace: s(),
      }).strict(),
    },
    bulk_validate: {
      params: z.object({
        plan_run_id: s(),
        batch_id: s(),
      }).strict(),
    },
    bulk_unfreeze: {
      params: z.object({
        plan_run_id: s(),
        batch_id: s(),
        confirm_execute: b(),
      }).strict(),
    },
  },
};
