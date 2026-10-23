/**
 * directGuides.pure — the static, self-contained step-by-step content for the "Run it directly"
 * module's guides sub-tab. Three walkthroughs, grounded verbatim in the repo docs so the academy
 * never drifts from the tooling:
 *   - cockpit   → docs/10-cockpit-and-conductor.md  (drive the whole migration from 09_cockpit.py)
 *   - adhoc     → docs/11-using-notebooks.md         (run one read-only/planning stage on its own)
 *   - notebooks → docs/13-estate-migration-playbook.md (the full estate, stage by stage)
 *
 * Pure data only — no React, no I/O. The StepGuides component renders it; the test pins its
 * integrity (ids, coverage, ordered stages) so a doc/tooling change is reflected here on purpose.
 */

export type GuideId = 'cockpit' | 'adhoc' | 'notebooks';

export interface GuideStep {
  title: string;
  body: string;
  /** The stage notebook this step runs, e.g. "09_cockpit.py". Omitted for pure prose steps. */
  notebook?: string;
  /** A copy-paste snippet — cockpit widget settings or the CLI equivalent. */
  code?: string;
}

export interface Guide {
  id: GuideId;
  /** Tab label. */
  label: string;
  /** One-line summary shown under the active tab. */
  tagline: string;
  /** Lead paragraph. */
  intro: string;
  steps: GuideStep[];
  /** Closing caveat. */
  note: string;
}

const COCKPIT: Guide = {
  id: 'cockpit',
  label: 'Cockpit — end to end',
  tagline: 'Drive the whole migration from one notebook, approving each recommended step.',
  intro:
    'The cockpit (09_cockpit.py) is the human launcher, not a job. It reads the control plane, prints the single next step the conductor recommends, and launches the matching deployed job when you approve. It never runs SET MANAGED itself — the safety lives in the jobs.',
  steps: [
    {
      title: 'Deploy the bundle once',
      body: 'One command deploys the jobs, dashboard, Genie handshake, and the app. Or clone the bundle into Databricks Git to run the notebooks in-workspace. Never use databricks apps deploy.',
      code: 'databricks bundle deploy -t <target> --profile <profile>',
    },
    {
      title: 'Install-time setup (first run only)',
      body: 'Creates the control plane (manifest, actions, ledger, views), the Genie space, and the App service-principal grants. The exact grant statements the App SP needs are also generated in the app (Connection & policy → Permissions the app needs), including the copy-paste to make the SP part of the migration owner group. Not part of the conversion path — you run it once.',
      notebook: '99_setup.py',
    },
    {
      title: 'Open the cockpit and read the briefing',
      body: 'Open the notebook and Run all. With action=show (the default) it reads migration_next_actions / migration_progress / wave_status and prints what has been done, where the estate stands, and the recommended next step with its full implications. Nothing is launched.',
      notebook: '09_cockpit.py',
      code: 'action=show',
    },
    {
      title: 'Approve the recommended step',
      body: 'action=advance launches the single job the conductor recommends via run_now (with a duplicate-trigger guard and a clickable run link). When there is no recommendation yet, advance runs job_08_conductor first to compute one. Convert and resume additionally require the mutation gate.',
      notebook: '09_cockpit.py',
      code: 'action=advance  confirm_convert=true   # confirm_convert only needed for convert/resume',
    },
    {
      title: 'Repeat — the loop closes itself',
      body: 'Every stage that moves a wave along — the brackets and finish steps included — recomputes the conductor at its tail, so the recommendation refreshes on its own. Keep approving advance to walk profile → plan → prep → (freeze) → (lease) → convert → validate → (unfreeze) → (re-share) → (compat) → (optimize). The conductor finishes the wave it converted before it recommends the next one.',
      notebook: '09_cockpit.py',
      code: 'action=advance',
    },
    {
      title: 'Work on one schema (focus)',
      body: 'Set target_schema to a schema in the plan and the cockpit follows THAT schema: its own next step and wave, from the conductor’s per-schema recommendation (one wave at a time, finishing a converted wave before the next). Every launch is pinned to that wave. Clear target_schema to go back to the step recommended for everyone.',
      notebook: '09_cockpit.py',
      code: 'action=show  target_schema=<catalog.schema>   # then action=advance',
    },
    {
      title: 'Set team priorities (optional)',
      body: 'Pin schemas the whole team should take first. The recommendation follows the first pinned schema only after work already in flight (a started wave, a converted wave’s finish steps), never skips a check, and a pin clears itself when its schema is done. One schema per call; adding needs a reason.',
      notebook: '08_conductor.py',
      code: 'databricks bundle run job_08_conductor -t <target> --params pin_action=add,pin_scope=<catalog.schema>,pin_reason="<why>"\n# move: pin_action=move,pin_scope=<s>,pin_rank=1   remove: pin_action=remove,pin_scope=<s>   clear: pin_action=clear',
    },
    {
      title: 'Reach for manual operations only when needed',
      body: 'The escape hatch: replan, rollback, cleanup, export the by-hand runbook, or print the blocker digest. Execute-gated re-share/compat launches need confirm_execute=true; per-table compat needs a human-supplied location.',
      notebook: '09_cockpit.py',
      code: 'action=advanced  manual=export_runbook  runbook_volume_path=/Volumes/main/migration/exports',
    },
  ],
  note: 'The cockpit only launches deployed jobs and enforces the same fail-closed gates — it never mutates a table inline. There is no manual=conductor (advance already runs it when needed).',
};

const ADHOC: Guide = {
  id: 'adhoc',
  label: 'Adhoc — one stage',
  tagline: 'Run a single read-only or planning stage on its own — profile, plan, or scan.',
  intro:
    'Every stage is a plain Databricks notebook under backend/notebooks/. To run just one thing, open that stage’s notebook, set its widgets, and Run all — or launch the matching job from the CLI. The stages below are read-only or planning-only, so they are safe to run anytime; they never run SET MANAGED.',
  steps: [
    {
      title: 'Profile the estate',
      body: 'Read-only discovery of every EXTERNAL table: sets the risk flags and seeds the action queue. No widgets needed for a full-estate profile. This is the one stage everything else reads from.',
      notebook: '00_profiler.py',
      code: 'databricks bundle run job_00_profiler -t <target>',
    },
    {
      title: 'Scan path-based access (tier-2 detail)',
      body: 'Optional, OFF by default. The deeper WHO/WHERE/WHICH path-access evidence that feeds FIX_PATH_ACCESS remediations — path-based access holds the table for review (NEEDS_REVIEW) until its readers/writers use name-based access (self-healing). Scope it to a notebook or directory. Read-only.',
      notebook: '01_source_scan.py',
      code: 'databricks bundle run job_01_source_scan -t <target>',
    },
    {
      title: 'Plan the waves',
      body: 'Buckets and slices tables into capped waves, stamps a fresh plan_run_id, and rebuilds the migration_progress / wave_status views. Fully rewrites the plan on every run — no stale plan survives.',
      notebook: '02_wave_planner.py',
      code: 'databricks bundle run job_02_wave_planner -t <target>',
    },
    {
      title: 'Rescan compaction for a schema',
      body: 'Optional, read-only, one schema. Refreshes OPTIMIZE/compaction attribution and the action queue so a later freeze knows exactly what to pause.',
      notebook: '03b_compaction_scan.py',
      code: 'databricks bundle run job_03b_compaction_scan -t <target> --params target_schema=<catalog.schema>',
    },
    {
      title: 'Dry-run a schema',
      body: 'Advances passing tables to DRY_RUN_OK and marks the rest BLOCKED with a precise remediation. Read-only with respect to the tables — it only writes control-plane state you can act on.',
      notebook: '03_dry_run.py',
      code: 'databricks bundle run job_03_dry_run -t <target> --params target_schema=<catalog.schema>',
    },
  ],
  note: 'These are the read-only / planning stages — safe to run in isolation. The mutating stages (03c freeze, 03d Authorize, 04 convert, 05b unfreeze, 06 rollback, 07 cleanup) belong to the full run. Every widget and default is in backend/docs/contracts/jobs.md.',
};

const NOTEBOOKS: Guide = {
  id: 'notebooks',
  label: 'Full run — notebooks',
  tagline: 'The whole estate, stage by stage: pilot first, isolate streaming, one wave at a time.',
  intro:
    'Run each stage notebook (or its job) in order, repeating the wave loop per schema. The discipline is the point: pilot first, isolate streaming, dry-run before execute, and trust only machine-verified gates. Convert re-asserts every gate and fails closed regardless of how it was launched.',
  steps: [
    {
      title: 'Prereqs and runtime',
      body: 'Confirm the runtime floor — SET MANAGED needs DBR 17.0+ or Serverless (17.2+ for TRUNCATE UNIFORM HISTORY); all stage jobs run serverless. Confirm a managed location is configured (schema → catalog → metastore); it is a fail-closed gate. Run setup once.',
      notebook: '99_setup.py',
    },
    {
      title: 'Profile the estate',
      body: 'Discover every EXTERNAL table, set the risk flags, and seed the action queue. Optionally add the tier-2 path-access scan (01_source_scan.py). Read-only.',
      notebook: '00_profiler.py',
    },
    {
      title: 'Plan the waves',
      body: 'Bucket and slice tables into capped waves, stamp a plan_run_id, and rebuild the progress / wave_status views. Read them to see per-schema and per-wave standing.',
      notebook: '02_wave_planner.py',
    },
    {
      title: 'Prep a schema / wave',
      body: 'Dry-run the schema you intend to convert; passing tables advance to DRY_RUN_OK, the rest are BLOCKED with a remediation. Work the runbook to clear blockers (path access, external writers, managed location). Optionally rescan compaction with 03b so freeze knows what to pause.',
      notebook: '03_dry_run.py',
    },
    {
      title: 'Freeze the bracket',
      body: 'If the wave has attributed compaction jobs, Predictive Optimization, or streams, freeze them. Dry-run first, then confirm_execute=true. It reversibly pauses exactly those actors and records prior state in freeze_ledger.',
      notebook: '03c_freeze.py',
      code: 'confirm_execute=true   # dry-run (false) first to review the exact statements',
    },
    {
      title: 'Authorize (only where needed)',
      body: 'SET MANAGED needs the run_as identity to hold USE CATALOG and USE SCHEMA, ownership or MANAGE on the table, its schema or its catalog (direct, inherited or through a group), and SELECT and MODIFY unless it owns the table. 03d re-checks every table into convert_authority, then GRANTs only the missing privileges it can grant itself, recording each in ownership_ledger; 07 revokes exactly what was added. It never changes ownership, never runs SET MANAGED, never touches data. Anything it cannot grant is printed as a grant script that names who must run it. Dry-run first, then confirm_execute=true.',
      notebook: '03d_lease_ownership.py',
      code: 'confirm_execute=true   # dry-run (false) first to review the exact GRANT statements',
    },
    {
      title: 'Convert one wave',
      body: 'Run convert with a selector and the gate flag. Waves are atomic and resumable; the job re-asserts the managed-location, recent-OPTIMIZE, compaction-coverage, stream-stop, and convert-permission gates and fails closed on a gap. Convert one wave at a time; keep streaming isolated.',
      notebook: '04_convert.py',
      code: 'target_schema=<catalog.schema>,confirm_compaction_paused=true\n# streaming, isolated:  bucket=streaming,confirm_compaction_paused=true',
    },
    {
      title: 'Validate',
      body: 'Advance CONVERTED → VALIDATED and write validation_results.',
      notebook: '05_validate.py',
    },
    {
      title: 'Unfreeze and verify',
      body: 'Restore exactly from the ledger (paused stays paused, PO back only if it was on, streams from their checkpoint, pipelines without full refresh) and restart streams. Verifies each restore against platform truth and auto-closes the paired POST_CONVERT actions.',
      notebook: '05b_unfreeze_verify.py',
      code: 'confirm_execute=true',
    },
    {
      title: 'Post-convert workstreams',
      body: 'Re-share each converted D2D table back into its share WITH HISTORY. For external read-only consumers, prefer the UC open APIs (Unity REST for Delta clients, Iceberg REST for Iceberg clients): mark those tables not needed with job_08b finish_action=defer and the fixed open-APIs defer_reason. Only for a reader that cannot use them, enable Compatibility Mode (needs a human-supplied empty external location). Both runs are dry-run by default. When one can’t be closed yet (no attributable share, no location chosen), set it aside with finish_action=defer and a reason: it stops holding the wave, stays on the runbook, and finish_action=reopen brings it back.',
      notebook: '08_reshare.py',
      code: 'confirm_execute=true   # 08_reshare.py and 08b_enable_compat.py\n# set aside:  finish_action=defer,defer_reason=<why>,confirm_execute=true',
    },
    {
      title: 'Turn on liquid clustering',
      body: 'The last finish step. Turns on automatic liquid clustering and verifies it: a clean table gets CLUSTER BY AUTO, a ZORDERed table keeps its ZORDER columns as the starting keys, manual keys switch to AUTO, and a partitioned table stays manual. Predictive Optimization is already on after the convert; enable_po_on_convert=true turns it on only where it reads off. An executed run stamps each table so the wave’s optimize step finishes. To finish without changing any table, run it with auto_liquid_policy=recommend: the statements stay on the runbook.',
      notebook: '08c_enable_optimization.py',
      code: 'enable_po_on_convert=true,confirm_execute=true',
    },
    {
      title: 'Let the conductor drive (optional)',
      body: 'The conductor recommends the single next step into migration_next_actions, finishing the current wave (unfreeze → re-share → compat → optimize, each only where it applies) before it starts the next. Drive from the cockpit (action=advance) or the app, approving each step. This is the same loop as the cockpit guide.',
      notebook: '09_cockpit.py',
      code: 'action=advance',
    },
    {
      title: 'After 14 days, clean up',
      body: 'Cleanup VACUUMs PO-disabled tables once past the 14-day window (never inside it) and revokes only the grants Authorize added for the convert (recorded in ownership_ledger) — they are held all the way through the rollback window so a rollback stays possible. Ownership never changes. Rollback (06_rollback.py) is available only within those 14 days, is manual, and requires a written reason.',
      notebook: '07_post_convert_cleanup.py',
    },
  ],
  note: 'One wave at a time: finish the wave’s post-convert steps, re-read wave_status, then loop back to “Prep a schema / wave” for the next wave or schema. The authoritative parameter reference is backend/docs/contracts/jobs.md.',
};

export const GUIDES: Guide[] = [COCKPIT, ADHOC, NOTEBOOKS];

export const GUIDE_IDS: GuideId[] = GUIDES.map((g) => g.id);

export function getGuide(id: GuideId): Guide | undefined {
  return GUIDES.find((g) => g.id === id);
}
