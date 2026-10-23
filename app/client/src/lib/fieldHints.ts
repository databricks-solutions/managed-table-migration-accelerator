/**
 * Tooltip copy for the next-step card fields + post-action flags. Grounded in
 * backend/docs/genie/planning_and_eligibility.md and overview_and_glossary.md. NOTE: the "Compute" field is
 * deliberately omitted — compute/warehouse guidance is handled separately.
 */
export const FIELD_HINTS = {
  job: 'The Databricks job that performs this step. You approve it; the job does the work.',
  selector: 'The scope passed to the job — a schema (catalog.schema) or a specific wave.',
  reversible:
    'Whether it can be undone. Conversions reverse for 14 days via UNSET MANAGED; after that the old storage is cleaned up.',
  time: 'Estimated data-copy wall-clock, from table sizes. An estimate, not a guarantee.',
  maxDowntime:
    'Estimated worst-case unavailability for any single table during its switch. Name-based readers keep working; this is the upper bound.',
  blastRadius:
    'Distinct downstream consumers from Unity Catalog lineage (sampled, ~90-day). A floor, not exact — "no evidence" is not "no consumers".',
  owners: 'Owning teams/users of the affected tables — who to coordinate with.',
  tables: 'How many tables this step affects (remaining in scope).',
  streamingRestart:
    'After conversion, streams reading/writing these tables must be restarted from their checkpoint. Hollow = owed; check = none needed.',
  reshare:
    'Delta Sharing recipients are not auto-updated; shared tables must be removed and re-added (keeping history). Owed = a shared table is in scope.',
  compat:
    'Non-Databricks read-only consumers must keep reading after conversion. Point them at the UC open APIs first. Compatibility Mode is the fallback, and its empty-location path is a human decision.',
  belowFloor:
    'A reader/writer runs on a Databricks runtime below the 15.4 LTS floor and could fail to read the converted table — upgrade it. Detected from job lineage only, so "none" is unconfirmed, not proven safe.',
  readinessCheck:
    'The last dry-run / schema readiness check for this scope. It re-verifies each table’s eligibility and the freeze coverage and records a GO / NO-GO — it converts nothing. It’s point-in-time, so the convert job re-checks at mutation time and fails closed on a gap.',
  freezeBracket:
    'Before converting, the freeze bracket pauses optimizers (OPTIMIZE / Predictive Optimization), stops streams, and pauses schedulable writer jobs (scheduled/event-triggered) writing these tables so no concurrent writer corrupts the switch. Each pause records prior state and is restored afterward. Coverage is point-in-time — the job re-verifies it right before it mutates.',
  writerJobs:
    'Scheduled or event-triggered jobs that write this table (from lineage). The freeze bracket pauses the attributed ones and restarts them after; ad-hoc / notebook / user / external writers can’t be paused — pick an off-peak window (an in-window write fails and must be retried; nothing is lost).',
  writerVerdict:
    'An ADVISORY schedule guess — SAFE_NOW / WARN_WINDOW / MUST_STOP / UNKNOWN — from a writer job’s real schedule. It never enables convert; only machine-verified stop-coverage does. UNKNOWN means the schedule couldn’t be read, not "safe".',
  willChange:
    'The DRY_RUN_OK tables in this scope that this launch will SET MANAGED — the same count the job acts on. Tables not yet dry-run, or already converted, are not included.',
  sizeBand:
    'The table’s size bucket from DESCRIBE DETAIL, used to estimate copy time and to cap how much a single wave carries. A band, not exact bytes.',
  waveFlags:
    'Signals that drove this table’s bucketing — a streaming writer, path-based access, or external (non-Databricks) readers. An amber dot means the signal is newer than this plan (re-plan to re-bucket); convert re-checks it at mutation time regardless.',
  eligibility:
    'The engine’s assessment for this table — ELIGIBLE, BLOCKED, or NEEDS_REVIEW — from the profiler and dry-run. BLOCKED means a hard prerequisite failed; NEEDS_REVIEW means a signal needs a human decision before the table can be planned.',
  tableState:
    'Where the table sits in the migration lifecycle (DISCOVERED → ASSESSED → DRY_RUN_OK → CONVERTING → CONVERTED → VALIDATED …). It reflects recorded control state, not a live read of the table.',
  flaggedEvidence:
    'Why the scanner flagged this table for the workstream — the access/runtime note or blocker it recorded. Evidence is sampled and best-effort, so its absence is never proof the table is safe.',
  pathVerify:
    'What this table waits on before it rejoins the plan. The code scan clears it: the scan must have found the storage path before and no longer find it. For an eligible table held only by Databricks jobs, an opt-in to path-based redirect also releases it. Tools outside Databricks can’t be scanned, so they clear only after 90 days with no path use.',
} as const;

/**
 * A structured card explainer: the What / When to use / Keep in mind shape rendered by
 * `InfoCard` (a HoverCard) as three formatted sections. This replaced the old pre-joined
 * `\n\n` strings crammed into a native-`title` tooltip, which could not render structure.
 */
export type CardHint = {
  what: string;
  when: string;
  keepInMind: string;
};

/**
 * Card-level explainers for the Convert page cards, shown via `InfoCard` next to each card title.
 * Each hint follows a What / When to use / Keep in mind shape, grounded in the card's own
 * CardDescription, backend/docs/state-machine.md, backend/docs/contracts/jobs.md, and
 * lib/convertScope.pure.ts. No fabricated, table-specific facts.
 */
export const CARD_HINTS: Record<string, CardHint> = {
  convertExpert: {
    what:
      'The expert (operator) path for converting external tables to managed tables, scoped by ' +
      'hand. The preview is read-only; conversion runs the same gated job_04 as the guided path.',
    when:
      'When you want to drive conversion yourself by schema or wave, instead of following the ' +
      'guided next step on Overview.',
    keepInMind:
      'Conversion is irreversible except via the engine\u2019s 14-day rollback window. The same ' +
      'safety gates (managed-location, freeze bracket, convert permission) always apply.',
  },

  campaign: {
    what:
      'Prepares one schema and returns GO / NO-GO. It chains dry-run then conductor over the ' +
      'schema, advancing ready tables to DRY_RUN_OK, refreshing the recommendation, and writing ' +
      'a gate verdict. It converts nothing.',
    when: 'Start here before converting a schema \u2014 to earn a verdict and surface any blockers.',
    keepInMind:
      'The check is schema-scoped, so streaming waves are excluded unless you tick ' +
      'include_streaming. It advances control state (to DRY_RUN_OK) but never sets a table to ' +
      'managed \u2014 converting stays a separate, deliberate step below.',
  },

  convertByScope: {
    what:
      'The only action here that changes tables \u2014 it converts the ready (DRY_RUN_OK) tables ' +
      'in scope via job_04. Scope by schema (optionally narrowed by a bucket) or by explicit ' +
      'wave ids \u2014 not both.',
    when: 'After a GO from the readiness check and after reviewing the scope preview board below.',
    keepInMind:
      'wave_ids is exclusive with target_schema/bucket. A bucket only filters a schema and ' +
      'cannot stand alone \u2014 the exception is bucket=streaming. Streaming is left out unless ' +
      'you opt in. Conversion is irreversible outside the 14-day window; the job re-checks the ' +
      'freeze bracket at run time and fails closed on a gap.',
  },

  scopePreview: {
    what:
      'A read-only readiness board by wave \u2014 it fires no job. Plan state is the wave\u2019s ' +
      'lifecycle (READY, ROLLED_BACK, \u2026); Gate verdict is the last time a gate actually ' +
      'evaluated the wave.',
    when: 'Review before converting to see which waves are ready versus UNEVALUATED.',
    keepInMind:
      'UNEVALUATED means no gate has run on that wave yet \u2014 it is never a fabricated ' +
      'GO/BLOCKED. Run a dry-run or a schema readiness check to earn a verdict.',
  },
};
