/**
 * directPath.pure — the teaching data model + command builder behind the "Run it directly"
 * academy module. Pure and deterministic (no React, no I/O) so the CLI-string assembly and its
 * validation are unit-tested and the component just renders state.
 *
 * Everything here is pinned to the invoke contract in backend/docs/contracts/jobs.md: the 17
 * operator jobs, their run-now selectors/gates, and the selector rules the converter enforces.
 * The builder TEACHES the exact `databricks bundle run …` command — it never executes anything.
 */
import { OPEN_APIS_REASON } from '../../lib/compatOpenApis.pure';

export type Target = 'dev' | 'e2e' | 'prod';
export const TARGETS: Target[] = ['dev', 'e2e', 'prod'];

/** Lifecycle grouping for the stage picker (mirrors the jobs.md dependency DAG order). */
export type Phase = 'discover' | 'plan' | 'prepare' | 'convert' | 'verify' | 'recover' | 'post';

export const PHASE_ORDER: Phase[] = ['discover', 'plan', 'prepare', 'convert', 'verify', 'recover', 'post'];
export const PHASE_LABEL: Record<Phase, string> = {
  discover: 'Discover',
  plan: 'Plan',
  prepare: 'Prepare',
  convert: 'Convert',
  verify: 'Verify',
  recover: 'Recover',
  post: 'Post-convert',
};

/** A run-now parameter the UI renders for a stage. */
export interface StageField {
  key: string;
  label: string;
  kind: 'text' | 'bool' | 'enum';
  placeholder?: string;
  options?: string[];
  help?: string;
}

export interface Stage {
  bundleKey: string;
  label: string;
  phase: Phase;
  /** Whether the stage can mutate customer resources (at all — some only when confirm_execute). */
  mutates: boolean;
  purpose: string;
  runtime?: string;
  note?: string;
  fields: StageField[];
}

/** The defer/reopen params job_08_reshare and job_08b_enable_compat share (jobs.md §13–14). */
const FINISH_ACTION_FIELDS: StageField[] = [
  {
    key: 'finish_action',
    label: 'Finish action',
    kind: 'enum',
    options: ['', 'run', 'defer', 'reopen'],
    help: 'Blank = run. defer / reopen change the action status only; nothing is re-shared or altered.',
  },
  { key: 'defer_reason', label: 'Defer reason', kind: 'text', placeholder: 'required for defer' },
  { key: 'action_keys', label: 'Action key', kind: 'text', placeholder: 'one action_key (blank = whole scope)' },
];

/**
 * The 17 operator-facing bundle jobs (job_99_setup is install-time and the two E2E seeders are
 * excluded on purpose — they must never appear as an operator command). Fields are the run-now
 * params from jobs.md; deploy-fixed params (manifest_catalog/schema, parallelism) are omitted.
 */
export const STAGES: Stage[] = [
  {
    bundleKey: 'job_00_profiler',
    label: 'Profiler',
    phase: 'discover',
    mutates: false,
    purpose: 'Discover EXTERNAL tables, assess eligibility, bootstrap the control plane.',
    fields: [
      { key: 'force_rescan', label: 'Force rescan', kind: 'bool', help: 'Ignore the history_scanned_at resume marker.' },
      { key: 'catalog_filter', label: 'Catalog filter', kind: 'text', placeholder: '*' },
      { key: 'schema_filter', label: 'Schema filter', kind: 'text', placeholder: '*' },
    ],
  },
  {
    bundleKey: 'job_01_source_scan',
    label: 'Source scan',
    phase: 'discover',
    mutates: false,
    purpose: 'Optional: scan notebook/job source for path-based access literals.',
    fields: [
      { key: 'scan_source', label: 'Scan source', kind: 'bool', help: 'Off by default; the primary on/off toggle.' },
      { key: 'scan_mode', label: 'Scan mode', kind: 'enum', options: ['targeted_jobs', 'broad'] },
    ],
  },
  {
    bundleKey: 'job_02_wave_planner',
    label: 'Wave planner',
    phase: 'plan',
    mutates: false,
    purpose: 'Assign eligible tables to capped, ordered waves and stamp a fresh plan_run_id.',
    note: 'Fails closed if any table is CONVERTING (would strand an in-flight wave).',
    fields: [
      { key: 'pilot_schema', label: 'Pilot schema', kind: 'text', placeholder: 'catalog.schema (blank = densest)' },
      { key: 'pilot_max_tables', label: 'Pilot cap', kind: 'text', placeholder: '15' },
      { key: 'wave_max_tables', label: 'Wave max tables', kind: 'text', placeholder: '300' },
      { key: 'wave_max_tb', label: 'Wave max TB', kind: 'text', placeholder: '25 (<=0 disables)' },
      { key: 'wave_grain', label: 'Wave grain', kind: 'enum', options: ['catalog_schema', 'catalog'] },
    ],
  },
  {
    bundleKey: 'job_03_dry_run',
    label: 'Dry-run',
    phase: 'prepare',
    mutates: false,
    purpose: 'Run the 15 pre-flight checks; move tables to DRY_RUN_OK or BLOCKED.',
    fields: [
      { key: 'catalog_filter', label: 'Catalog filter', kind: 'text', placeholder: '*' },
      { key: 'schema_filter', label: 'Schema filter', kind: 'text', placeholder: '*' },
      { key: 'size_band_filter', label: 'Size band', kind: 'text', placeholder: '* | <=100GB | ~1TB | ~10TB | >10TB' },
    ],
  },
  {
    bundleKey: 'job_03b_compaction_scan',
    label: 'Compaction rescan',
    phase: 'prepare',
    mutates: false,
    purpose: 'Optional: re-scan compaction actors and refresh the scoped action queue.',
    note: 'Prefer an exact target_schema immediately before converting it.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
    ],
  },
  {
    bundleKey: 'job_03c_freeze',
    label: 'Freeze',
    phase: 'prepare',
    mutates: true,
    purpose: 'Pause attributed compaction and stop attributed streams; record exact prior state.',
    note: 'Dry-run unless confirm_execute=true. Needs plan+wave or target_schema.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool', help: 'False plans only; true performs the freeze.' },
    ],
  },
  {
    bundleKey: 'job_03d_lease_ownership',
    label: 'Authorize',
    phase: 'prepare',
    mutates: true,
    purpose:
      'Re-check what the run_as identity needs to SET MANAGED each in-scope table, then GRANT only the missing privileges it can grant itself. Ownership never changes.',
    note: 'Each grant is recorded; 07 cleanup revokes exactly what was added (held through the 14-day rollback window). Dry-run unless confirm_execute=true. Needs plan+wave, batch_id or target_schema.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool', help: 'False re-checks and records the permissions only; true runs the GRANT statements.' },
    ],
  },
  {
    bundleKey: 'job_04_convert',
    label: 'Convert',
    phase: 'convert',
    mutates: true,
    purpose: 'Resolve a selector to plan-pinned waves and run ALTER TABLE … SET MANAGED.',
    runtime: 'Serverless or DBR 17.0+ (UniForm truncate 17.2+)',
    note: 'Reversible for 14 days. Re-verifies every gate — managed-location, recent-OPTIMIZE, compaction-coverage, stream-stop, and ownership-lease coverage — and fails closed at mutation time.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      {
        key: 'bucket',
        label: 'Bucket',
        kind: 'enum',
        options: ['', 'pilot', 'normal', 'streaming', 'compat', 'share', 'final_large'],
        help: 'A filter, not a standalone selector — except bucket=streaming.',
      },
      { key: 'wave_ids', label: 'Wave ids', kind: 'text', placeholder: 'wave_03 (one via CLI)' },
      { key: 'include_streaming', label: 'Include streaming', kind: 'bool' },
      { key: 'confirm_compaction_paused', label: 'confirm_compaction_paused', kind: 'bool', help: 'Runtime-required as true.' },
    ],
  },
  {
    bundleKey: 'job_05_validate',
    label: 'Validate',
    phase: 'verify',
    mutates: false,
    purpose: 'Validate converted tables with 8 checks; advance full passes to VALIDATED.',
    fields: [
      { key: 'catalog_filter', label: 'Catalog filter', kind: 'text', placeholder: '*' },
      { key: 'schema_filter', label: 'Schema filter', kind: 'text', placeholder: '*' },
    ],
  },
  {
    bundleKey: 'job_05b_unfreeze_verify',
    label: 'Unfreeze + verify',
    phase: 'verify',
    mutates: true,
    purpose: 'Restore jobs/PO/streams exactly from the freeze ledger, then verify.',
    note: 'Dry-run unless confirm_execute=true. Needs plan+wave or target_schema.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool' },
    ],
  },
  {
    bundleKey: 'job_06_rollback',
    label: 'Rollback',
    phase: 'recover',
    mutates: true,
    purpose: 'Return converted tables to EXTERNAL with UNSET MANAGED (within 14 days).',
    runtime: 'Serverless or DBR 17.0+',
    note: 'Only within 14 days of a successful convert. Needs full_name or plan+wave, and a reason.',
    fields: [
      { key: 'full_name', label: 'Full name', kind: 'text', placeholder: 'catalog.schema.table' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'reason', label: 'Reason', kind: 'text', placeholder: 'why you are rolling back' },
    ],
  },
  {
    bundleKey: 'job_07_post_convert_cleanup',
    label: 'Cleanup (VACUUM)',
    phase: 'post',
    mutates: true,
    purpose: 'After the rollback window, VACUUM old external data for validated tables.',
    runtime: 'Serverless or DBR 17.0+',
    note: 'Irreversible. Only after the 14-day window — never inside it.',
    fields: [
      { key: 'full_name', label: 'Full name', kind: 'text', placeholder: 'catalog.schema.table (blank = all eligible)' },
      { key: 'max_tables', label: 'Max tables', kind: 'text', placeholder: '50' },
    ],
  },
  {
    bundleKey: 'job_08_conductor',
    label: 'Conductor',
    phase: 'post',
    mutates: false,
    purpose: 'Compute the recommended next step (estate and per schema). Never touches a table.',
    note: 'Leave pin_action=none to only refresh. add / remove / move / clear changes the team priorities (one schema per call, a reason for add). Pins only reorder the recommendation, after work already in flight.',
    fields: [
      { key: 'pin_action', label: 'Pin action', kind: 'enum', options: ['', 'add', 'remove', 'move', 'clear'] },
      { key: 'pin_scope', label: 'Pin scope', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'pin_rank', label: 'Pin rank', kind: 'text', placeholder: '1 = first' },
      { key: 'pin_reason', label: 'Pin reason', kind: 'text', placeholder: 'why it goes first (required for add)' },
    ],
  },
  {
    bundleKey: 'job_08_reshare',
    label: 'Re-share (D2D)',
    phase: 'post',
    mutates: true,
    purpose: 'Remove and re-add converted D2D-shared tables WITH HISTORY, then verify.',
    note: 'Dry-run unless confirm_execute=true. Needs plan+wave or target_schema. finish_action=defer sets a re-share the tool cannot close aside (with a reason) so it stops holding the wave; reopen undoes it.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool' },
      ...FINISH_ACTION_FIELDS,
    ],
  },
  {
    bundleKey: 'job_08b_enable_compat',
    label: 'External readers (open APIs or Compatibility Mode)',
    phase: 'post',
    mutates: true,
    purpose: 'Settle how external readers connect: mark readers on the UC open APIs not needed, or enable Compatibility Mode with a human-supplied location.',
    note: `Preferred: readers on the UC open APIs need no second copy — run finish_action=defer with defer_reason="${OPEN_APIS_REASON}" (quote it: the reason has a semicolon). Compatibility Mode fallback: dry-run unless confirm_execute=true. One EMPTY location per table: compat_location serves a single-table scope only. For several tables use the app’s external-readers step, or set compat_location_map in the Jobs UI. A blank/placeholder location stays manual. finish_action=defer with your own reason sets the step aside until a location is chosen; reopen undoes either.`,
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool' },
      { key: 'compat_location', label: 'Compat location', kind: 'text', placeholder: 'empty external path' },
      ...FINISH_ACTION_FIELDS,
    ],
  },
  {
    bundleKey: 'job_08c_enable_optimization',
    label: 'Turn on liquid clustering',
    phase: 'post',
    mutates: true,
    purpose: 'Turn on automatic liquid clustering on converted tables, then verify. A ZORDERed table keeps its ZORDER columns as the starting keys; manual keys switch to AUTO; a partitioned table stays manual.',
    note: 'Dry-run unless confirm_execute=true. Needs plan+wave or target_schema. Predictive Optimization is already on after the convert; enable_po_on_convert=true (the guided app step) turns it on only where it reads off. auto_liquid_policy=recommend changes no table (the app\u2019s "Skip clustering for now").',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'plan_run_id', label: 'Plan run id', kind: 'text', placeholder: '<uuid>' },
      { key: 'wave_id', label: 'Wave id', kind: 'text', placeholder: 'wave_03' },
      { key: 'enable_po_on_convert', label: 'enable_po_on_convert', kind: 'bool', help: 'True turns Predictive Optimization on where it reads off (it is already on after the convert unless someone turned it off); false leaves it alone.' },
      {
        key: 'auto_liquid_policy',
        label: 'Liquid clustering',
        kind: 'enum',
        options: ['', 'enable_eligible', 'recommend', 'off'],
        help: 'Blank = job default (enable_eligible: cluster eligible tables). recommend = change no table, keep the statements on the runbook. off = leave clustering alone.',
      },
      { key: 'confirm_execute', label: 'confirm_execute', kind: 'bool' },
    ],
  },
  {
    bundleKey: 'job_10_campaign',
    label: 'Campaign (GO / NO-GO)',
    phase: 'prepare',
    mutates: false,
    purpose: 'Prepare one schema: resolve scope, dry-run, refresh Conductor, return GO/NO-GO.',
    note: 'Does not convert. Requires a target_schema.',
    fields: [
      { key: 'target_schema', label: 'Target schema', kind: 'text', placeholder: 'catalog.schema' },
      { key: 'confirm_compaction_paused', label: 'confirm_compaction_paused', kind: 'bool' },
      { key: 'include_streaming', label: 'Include streaming', kind: 'bool' },
    ],
  },
];

export const OPERATOR_BUNDLE_KEYS: string[] = STAGES.map((s) => s.bundleKey);

export interface BuilderState {
  bundleKey: string;
  target: Target;
  profile: string;
  params: Record<string, string>;
}

export interface Issue {
  level: 'error' | 'warning';
  message: string;
}

export interface BuildResult {
  command: string;
  issues: Issue[];
}

export function getStage(bundleKey: string): Stage | undefined {
  return STAGES.find((s) => s.bundleKey === bundleKey);
}

/** A clean builder state for a stage (dev target, no profile, empty params). */
export function defaultState(bundleKey: string): BuilderState {
  return { bundleKey, target: 'dev', profile: '', params: {} };
}

function val(state: BuilderState, key: string): string {
  return (state.params[key] ?? '').trim();
}

function isTrue(state: BuilderState, key: string): boolean {
  return val(state, key) === 'true';
}

/**
 * Assemble the exact `databricks bundle run …` command for a builder state, plus any validation
 * issues. Params are emitted in the stage's field order (deterministic): text/enum when
 * non-empty, bool only when true. `--params` is added only when at least one param is emitted.
 */
export function buildCommand(state: BuilderState): BuildResult {
  const stage = getStage(state.bundleKey);
  if (!stage) {
    return { command: '', issues: [{ level: 'error', message: `Unknown stage: ${state.bundleKey}` }] };
  }

  const parts: string[] = [];
  for (const f of stage.fields) {
    const v = val(state, f.key);
    if (f.kind === 'bool') {
      if (v === 'true') parts.push(`${f.key}=true`);
    } else if (v) {
      parts.push(`${f.key}=${v}`);
    }
  }

  let command = `databricks bundle run ${stage.bundleKey} -t ${state.target}`;
  const profile = state.profile.trim();
  if (profile) command += ` --profile ${profile}`;
  if (parts.length) command += ` --params ${parts.join(',')}`;

  return { command, issues: validate(state, stage) };
}

/** Selector/gate validation, pinned to the jobs.md rules per stage. */
function validate(state: BuilderState, stage: Stage): Issue[] {
  const issues: Issue[] = [];
  const has = (k: string) => val(state, k).length > 0;
  const planWave = has('plan_run_id') && has('wave_id');
  const planOrWaveHalf = (has('plan_run_id') || has('wave_id')) && !planWave;

  switch (stage.bundleKey) {
    case 'job_04_convert': {
      const targetSchema = has('target_schema');
      const bucket = val(state, 'bucket');
      const waveIds = val(state, 'wave_ids');
      const hasBucket = bucket.length > 0;
      const hasWaveIds = waveIds.length > 0;

      if (!targetSchema && !hasBucket && !hasWaveIds) {
        issues.push({ level: 'error', message: 'Convert requires a selector: target_schema, bucket, or wave_ids.' });
      }
      if (hasWaveIds && (targetSchema || hasBucket)) {
        issues.push({ level: 'error', message: 'wave_ids is exclusive with target_schema / bucket.' });
      }
      if (hasBucket && bucket !== 'streaming' && !targetSchema && !hasWaveIds) {
        issues.push({
          level: 'error',
          message: `bucket=${bucket} is a filter, not a standalone selector — pair it with a target_schema (only bucket=streaming may stand alone).`,
        });
      }
      if (hasWaveIds && waveIds.includes(',')) {
        issues.push({
          level: 'warning',
          message: 'CLI --params is comma-delimited: pass ONE wave_id here. Set multiple wave_ids from the Jobs UI or run once per wave.',
        });
      }
      if (!isTrue(state, 'confirm_compaction_paused')) {
        issues.push({ level: 'error', message: 'Convert requires confirm_compaction_paused=true.' });
      }
      break;
    }
    case 'job_03c_freeze':
    case 'job_03d_lease_ownership':
    case 'job_05b_unfreeze_verify':
    case 'job_08_reshare':
    case 'job_08b_enable_compat':
    case 'job_08c_enable_optimization': {
      if (!has('target_schema') && !planWave) {
        issues.push({ level: 'error', message: 'Needs a plan_run_id + wave_id pair, or a target_schema.' });
      }
      if (planOrWaveHalf) {
        issues.push({ level: 'error', message: 'plan_run_id and wave_id must be supplied together.' });
      }
      if (!isTrue(state, 'confirm_execute')) {
        issues.push({ level: 'warning', message: 'Dry-run only (confirm_execute=false) — plans and persists, mutates nothing. Set confirm_execute=true to run it.' });
      }
      const finishAction = val(state, 'finish_action') || 'run';
      if (finishAction === 'defer' && !has('defer_reason')) {
        issues.push({ level: 'error', message: 'finish_action=defer requires a non-blank defer_reason (recorded in the audit log).' });
      }
      if (has('action_keys') && finishAction === 'run') {
        issues.push({ level: 'error', message: 'action_keys narrows finish_action=defer or reopen only.' });
      }
      if (val(state, 'action_keys').includes(',')) {
        issues.push({
          level: 'warning',
          message: 'CLI --params is comma-delimited: pass ONE action_key here, or blank it to act on the whole scope.',
        });
      }
      if (
        stage.bundleKey === 'job_08b_enable_compat' &&
        finishAction === 'run' &&
        isTrue(state, 'confirm_execute') &&
        !has('compat_location')
      ) {
        issues.push({ level: 'warning', message: 'No compat_location supplied — affected tables stay manual (the tool never invents a location).' });
      }
      if (
        stage.bundleKey === 'job_08b_enable_compat' &&
        finishAction === 'run' &&
        isTrue(state, 'confirm_execute') &&
        has('compat_location')
      ) {
        issues.push({ level: 'warning', message: 'compat_location is one path: if more than one table in this scope needs Compat, job_08b refuses a location shared by more than one table (they stay manual and the run fails). Give each table its own path with compat_location_map instead.' });
      }
      if (stage.bundleKey === 'job_08c_enable_optimization' && isTrue(state, 'confirm_execute')) {
        const policy = val(state, 'auto_liquid_policy') || 'enable_eligible';
        if (policy === 'recommend') {
          issues.push({
            level: 'warning',
            message: 'auto_liquid_policy=recommend: the run changes no table. Each table\'s clustering statements stay on the runbook to run later, and the executed run still finishes the wave\'s optimize step (the app\'s "Skip clustering for now").',
          });
        } else if (!isTrue(state, 'enable_po_on_convert')) {
          issues.push({
            level: 'warning',
            message: 'enable_po_on_convert=false: a table whose Predictive Optimization reads off stays off, and an executed run records "operator declined PO" for it; liquid clustering still runs on the eligible tables. To finish without changing any table, use auto_liquid_policy=recommend.',
          });
        }
      }
      break;
    }
    case 'job_06_rollback': {
      if (!has('full_name') && !planWave) {
        issues.push({ level: 'error', message: 'Rollback needs full_name, or a plan_run_id + wave_id pair.' });
      }
      if (planOrWaveHalf) {
        issues.push({ level: 'error', message: 'plan_run_id and wave_id must be supplied together.' });
      }
      if (!has('reason')) {
        issues.push({ level: 'error', message: 'Rollback requires a non-blank reason (recorded in the audit log).' });
      }
      issues.push({ level: 'warning', message: 'Only works within 14 days of a successful convert — after that the old external files are gone.' });
      break;
    }
    case 'job_07_post_convert_cleanup': {
      issues.push({ level: 'warning', message: 'VACUUM is irreversible and ends rollback viability — run only after the 14-day window.' });
      break;
    }
    case 'job_10_campaign': {
      if (!has('target_schema')) {
        issues.push({ level: 'error', message: 'Campaign requires a target_schema.' });
      }
      break;
    }
    default:
      break;
  }
  return issues;
}

export interface Preset {
  id: string;
  label: string;
  description: string;
  state: BuilderState;
}

/** The two headline recipes; the estate pass is described in ESTATE_NOTE (a loop, not one run). */
export const PRESETS: Preset[] = [
  {
    id: 'adhoc_rollback',
    label: 'Adhoc: roll back one table',
    description: 'A single-table recovery within the 14-day window.',
    state: {
      bundleKey: 'job_06_rollback',
      target: 'dev',
      profile: '',
      params: { full_name: 'main.sales.orders', reason: 'bad convert' },
    },
  },
  {
    id: 'whole_schema_convert',
    label: 'Whole schema: convert',
    description: 'Convert every eligible non-streaming wave in one schema.',
    state: {
      bundleKey: 'job_04_convert',
      target: 'dev',
      profile: '',
      params: { target_schema: 'main.sales', confirm_compaction_paused: 'true' },
    },
  },
];

export const ESTATE_NOTE =
  'Whole estate = drive the safe path per schema: profiler → wave_planner → dry_run → convert per target_schema, then a separate bucket=streaming pass for streaming waves. There is no implicit convert-all; iterate schemas (or run the campaign GO/NO-GO per schema) rather than one estate-wide convert.';
