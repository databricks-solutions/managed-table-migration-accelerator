/**
 * workstreams.data — the six Workstream tracks taught by the academy, each keyed to the real
 * `migration_actions` kinds and phases the engine emits (backend/src/migration/actions.py).
 * Kept as pure data (no JSX) so the pure test can import it and assert kind coverage against
 * `ACTION_KINDS`. Sample statements mirror the generator's remediation verbatim enough to
 * teach; the live `/runbook` page shows the exact per-table text.
 */

import type { WorkstreamTrack } from './workstreams.pure';

export const TRACKS: WorkstreamTrack[] = [
  {
    id: 'path_access',
    label: 'Path-based access',
    signal: 'has_path_based_access · path_redirect_verdict · path_redirect_optin_status',
    what: 'A consumer reads or writes the table by storage path instead of by name. Any such consumer holds the table for review (NEEDS_REVIEW). The code scan lifts the hold once it found the path before and no longer does. A tool outside Databricks cannot be scanned, so its hold lifts only after 90 days with no path use. Path-based redirect is the exception for Databricks code you cannot change: when every job on the path runs 18.1+ (reads) / 18.2+ (writes) or serverless, you may opt in, and the hold lifts while those jobs stay the same.',
    steps: [
      {
        kind: 'FIX_PATH_ACCESS',
        phase: 'PRE_CONVERT',
        blocking: true,
        appRole: 'ADVISORY',
        sample: 'spark.table("prod.sales.orders")  # was: spark.read.format("delta").load("s3://…")',
        detail:
          'The app pinpoints the exact notebook/job/cell/line via the source scan and gives you the name-based rewrite — it never edits your code. Opting in to path-based redirect (eligible tables only, with a reason) closes it. Redirect adds latency per access, keeps the old Delta log live, and needs path-based streams restarted.',
      },
    ],
    repeatAfterRollback: false,
    youDo: 'Rewrite each consumer to name-based access (catalog.schema.table), then re-scan. The scan verdict clears the table, never an empty result on its own.',
    liveLinks: [
      { to: '/workstreams/path_access', label: 'Path findings' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'compaction',
    label: 'Compaction / OPTIMIZE / VACUUM',
    signal: 'has_recurring_compaction · compaction_sources',
    what: 'An OPTIMIZE/compaction or VACUUM job, or Predictive Optimization, works on the table. Running one while SET MANAGED moves the location can break the conversion.',
    steps: [
      {
        kind: 'FREEZE_COMPACTION',
        phase: 'PRE_CONVERT',
        blocking: true,
        appRole: 'RUNS_GATED',
        job: 'job_03c_freeze',
        attributedOnly: true,
        sample: 'ALTER SCHEMA main.sales DISABLE PREDICTIVE OPTIMIZATION;  -- or pause the attributed job schedule',
        detail:
          'The app pauses the attributed job/pipeline or disables schema-level Predictive Optimization and records the prior state in the freeze ledger. Convert re-checks real FROZEN coverage before it mutates.',
      },
      {
        kind: 'UNFREEZE_COMPACTION',
        phase: 'POST_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_05b_unfreeze_verify',
        attributedOnly: true,
        sample: 'ALTER SCHEMA main.sales ENABLE PREDICTIVE OPTIMIZATION;  -- or resume the paused schedule',
        detail:
          'The app restores exactly what was frozen — it re-enables PO only if it was on, and resumes only schedules it paused.',
      },
    ],
    repeatAfterRollback: false,
    youDo: 'Confirm the freeze; handle any unattributed (manual notebook/user) compaction yourself — the app only operates attributed jobs/pipelines and schema-level PO.',
    liveLinks: [
      { to: '/freeze', label: 'Paused & stopped' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'streaming',
    label: 'Streaming writers',
    signal: 'has_streaming_writer · streaming_plan',
    what: 'A structured stream or DLT pipeline writes the table. The writer must be stopped around convert and restarted afterwards so it resumes from its checkpoint against the new location.',
    steps: [
      {
        kind: 'STOP_STREAM',
        phase: 'PRE_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_03c_freeze',
        attributedOnly: true,
        sample: 'Stop the attributed job/pipeline for the conversion window (checkpoint preserved).',
        detail:
          'The app stops attributed streams/pipelines; convert machine-verifies attributed stops before mutating. Advisory, not blocking — but an un-stopped writer will fail its own coverage gate.',
      },
      {
        kind: 'RESTART_STREAM',
        phase: 'POST_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_05b_unfreeze_verify',
        attributedOnly: true,
        sample: 'Restart with the same config / full_refresh=False so it resumes from its checkpoint.',
        detail:
          'Convert never restarts streams (allow_restart=false), so restart is a required, separate step. Unattributed streams are never auto-resumed — you restart those. Repeat after any rollback.',
      },
    ],
    repeatAfterRollback: true,
    youDo: 'Stop unattributed streams yourself, and restart every stream after convert. Repeat the restart after a rollback.',
    liveLinks: [
      { to: '/freeze', label: 'Paused & stopped' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'writer_jobs',
    label: 'Writer jobs',
    signal: 'writer_classes · writer_sources',
    what: 'A scheduled or event-triggered Databricks job writes the table (via lineage). Schedulable writers are paused around convert and restarted after. Ad-hoc / notebook / user / external writers can NOT be paused — pick an off-peak window; a write during the block window fails and must be retried (nothing is lost).',
    steps: [
      {
        kind: 'STOP_WRITER_JOB',
        phase: 'PRE_CONVERT',
        blocking: true,
        appRole: 'RUNS_GATED',
        job: 'job_03c_freeze',
        attributedOnly: true,
        sample: 'Pause the job schedule/trigger/continuous block and cancel any active run for the conversion window; prior pause state captured for exact restore.',
        detail: 'The app pauses attributed scheduled/event writer jobs; convert machine-verifies the stop (WRITER_STOP_COVERAGE) before mutating. Ad-hoc/user/external writers are advisory only — never auto-paused.',
      },
      {
        kind: 'RESTART_WRITER_JOB',
        phase: 'POST_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_05b_unfreeze_verify',
        attributedOnly: true,
        sample: 'Resume the job ONLY if it was UNPAUSED before (never blanket-restart maintenance jobs).',
        detail: 'Unfreeze restores each paused writer job exactly from the freeze_ledger — resume-only-if-UNPAUSED. Repeat after any rollback.',
      },
    ],
    repeatAfterRollback: true,
    youDo: 'Stop unattributed / ad-hoc writers yourself (pick an off-peak window), and confirm each paused job restarts after convert.',
    liveLinks: [
      { to: '/workstreams/writer_jobs', label: 'Writer-jobs cockpit' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'consumer_dbr',
    label: 'Below-floor runtimes',
    signal: 'has_below_floor_consumer',
    what: 'A downstream consumer runs below the DBR 15.4 floor for reading managed tables reliably.',
    steps: [
      {
        kind: 'UPGRADE_CONSUMER_DBR',
        phase: 'PRE_CONVERT',
        blocking: false,
        appRole: 'ADVISORY',
        sample: 'Set the cluster/job spark_version to a 15.4-LTS+ image; below-floor readers may need DROP FEATURE inCommitTimestamp after convert.',
        detail:
          'The app flags below-floor consumers from lineage and runtime evidence — it cannot change consumer runtimes. Unresolved consumers are not confirmed safe.',
      },
    ],
    repeatAfterRollback: false,
    youDo: 'Upgrade those consumers to DBR 15.4 LTS+ before they read the converted table.',
    liveLinks: [
      { to: '/blocked', label: 'Blocked & review' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'delta_sharing',
    label: 'Delta Sharing (D2D)',
    signal: 'is_delta_shared',
    what: 'The table is shared via Databricks-to-Databricks Delta Sharing. Recipients are not auto-updated to the new managed location after convert.',
    steps: [
      {
        kind: 'RESHARE_D2D',
        phase: 'POST_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_08_reshare',
        sample:
          'ALTER SHARE <SHARE_NAME> REMOVE TABLE main.sales.orders;\nALTER SHARE <SHARE_NAME> ADD TABLE main.sales.orders WITH HISTORY;',
        detail:
          'The app builds and (gated) runs the exact REMOVE / ADD … WITH HISTORY statements, then verifies share membership. The profiler only records a shared boolean today, so fill <SHARE_NAME> first.',
      },
    ],
    repeatAfterRollback: false,
    youDo: 'Fill the share name and approve the re-share; recipients re-accept on their side.',
    liveLinks: [
      { to: '/workstreams', label: 'Live queue' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
  {
    id: 'compat',
    label: 'External readers',
    signal: 'needs_compatibility_mode',
    what: 'Non-Databricks clients need to keep reading after convert. Preferred: point them at the UC open APIs (Delta clients via the Unity REST API, Iceberg clients via the Iceberg REST catalog), which read the one managed copy, then mark the step not needed. Only if a reader cannot, Compatibility Mode serves a second read-only copy at an empty external location only you can choose.',
    steps: [
      {
        kind: 'ENABLE_COMPAT',
        phase: 'POST_CONVERT',
        blocking: false,
        appRole: 'RUNS_GATED',
        job: 'job_08b_enable_compat',
        sample:
          "ALTER TABLE main.sales.orders SET TBLPROPERTIES ('enabledFormats'='compatibility', 'location'='<empty external path>', 'targetRefreshInterval'='1 HOUR');",
        detail:
          'Open-APIs readers: the app marks the action not needed (a defer with the fixed open-APIs reason). Compatibility Mode fallback: the app builds the SET TBLPROPERTIES statement and (gated) runs it, then verifies — it never invents a storage location. A blank/placeholder path stays manual.',
      },
    ],
    repeatAfterRollback: false,
    youDo: 'Confirm whether each reader can use the UC open APIs. For any that cannot, supply an empty registered external location, then approve the execute step.',
    liveLinks: [
      { to: '/workstreams/compat_mode', label: 'External readers' },
      { to: '/runbook', label: 'Do by hand' },
    ],
  },
];
