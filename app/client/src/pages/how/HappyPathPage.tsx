import { Undo2, Trash2, Clock, Info } from 'lucide-react';
import { Link } from 'react-router';
import { ModuleShell } from './components/ModuleShell';
import { Scrollytelling, type ScrollStep } from './components/Scrollytelling';
import { Swimlane, type SwimlaneStage } from './components/Swimlane';
import { NextLinks } from './components/NextLinks';
import { useInView } from '../../hooks/useInView';
import { ModuleHeading } from './components/ModuleHeading';
import { SECTION_COPY } from '../../lib/sectionCopy';

/**
 * Happy path — the normal-table lifecycle end to end, taught as scrollytelling: a pinned
 * Swimlane (app-vs-you) advances through the lifecycle states as the reader scrolls the
 * steps. Then the implications, the rollback safety net, and cleanup. Terminology matches the
 * App and backend contracts exactly.
 */

const STAGES: SwimlaneStage[] = [
  {
    id: 'discover',
    label: 'Discover',
    state: 'DISCOVERED',
    job: 'job_00_profiler',
    appDoes: [
      'Scans the whole estate: sizes, lineage, history, runtimes, sharing.',
      'Builds the manifest and seeds the Workstreams queue.',
      'Reads only — never changes a table.',
    ],
    youDo: ['Run (or approve) the estate scan.', 'Re-profile a schema when its state looks stale.'],
  },
  {
    id: 'dry_run',
    label: 'Dry-run',
    state: 'DRY_RUN_OK',
    job: 'job_03_dry_run',
    appDoes: [
      'Runs 15 pre-flight checks per table.',
      'Marks each DRY_RUN_OK or BLOCKED with a reason.',
    ],
    youDo: ['Review blocked tables.', 'Clear the blockers, then re-run the check.'],
  },
  {
    id: 'freeze',
    label: 'Freeze',
    state: 'FROZEN (optional)',
    job: 'job_03c_freeze',
    appDoes: [
      'Optional, skippable bracket before convert.',
      'Pauses compaction / Predictive Optimization, stops streams, and pauses schedulable writer jobs that write the table.',
      'Writes a freeze_ledger so unfreeze can restore each source after validate.',
    ],
    youDo: ['Run it when the wave is AWAITING_ACTIONS (open compaction / stream / writer-job freezes).', 'Skip it when nothing needs pausing.'],
  },
  {
    id: 'lease',
    label: 'Authorize',
    state: 'AUTHORIZED (optional)',
    job: 'job_03d_lease_ownership',
    appDoes: [
      'Optional, skippable — only when the convert identity is missing a permission it can grant itself.',
      'Grants only what is missing so SET MANAGED does not fail closed on permissions. Ownership never changes.',
      'Records each grant so cleanup can revoke exactly what was added.',
    ],
    youDo: ['Authorize first if Overview flags a missing permission.', 'Send the grant script for what Authorize cannot grant.'],
  },
  {
    id: 'convert',
    label: 'Convert',
    state: 'CONVERTING → CONVERTED',
    job: 'job_04_convert',
    mutates: true,
    appDoes: [
      'Runs ALTER TABLE … SET MANAGED.',
      'Re-verifies every gate and fails closed on a gap.',
      'Never pauses anything itself.',
    ],
    youDo: ['Pick the scope (schema / wave / bucket).', 'Type-to-confirm — this is the one irreversible-ish step.'],
  },
  {
    id: 'validate',
    label: 'Validate',
    state: 'VALIDATED',
    job: 'job_05_validate',
    appDoes: [
      'Compares the managed table to the pre-convert snapshot.',
      'Checks type, readability, size, row count, history.',
    ],
    youDo: ['Trigger validate.', 'Review any failures (the table stays CONVERTED).'],
  },
  {
    id: 'finish',
    label: 'Finish',
    state: 'VALIDATED (optional steps)',
    job: 'job_05b · job_08 · job_08b · job_08c',
    appDoes: [
      'Unfreezes what the freeze paused and restarts streams and writer jobs (job_05b_unfreeze_verify).',
      'Re-shares Databricks-to-Databricks shares (job_08_reshare).',
      'Settles non-Databricks readers: marks those on the UC open APIs not needed, and enables Compatibility Mode only for a reader that cannot use them (job_08b_enable_compat).',
      'Turns on automatic liquid clustering (job_08c_enable_optimization). Predictive Optimization is already on after the convert; a ZORDERed table keeps its ZORDER columns as the starting keys.',
      'Recommends each step that applies, in order, on Overview and the Waves board before it suggests a new wave.',
    ],
    youDo: [
      'Approve each recommended step; the ones that don’t apply are skipped.',
      'Defer a re-share or Compat you can’t close yet (a reason is required), or skip clustering for now (no table changes; the statements stay on the runbook).',
    ],
  },
  {
    id: 'cleanup',
    label: 'Clean up',
    state: 'CLEANED',
    job: 'job_07_post_convert_cleanup',
    appDoes: [
      'After 14 days, VACUUMs the old external-location files.',
      'Predictive-Optimization-aware; irreversible.',
    ],
    youDo: ['Usually nothing.', "Don't VACUUM inside the 14-day window."],
  },
];

const STEPS: ScrollStep[] = [
  {
    id: 'discover',
    title: 'Discover — the estate scan',
    body: (
      <>
        The profiler (<code className="font-mono text-[12px]">job_00_profiler</code>) is{' '}
        <strong>estate-level</strong>: one pass over Unity Catalog metadata, <code className="font-mono text-[12px]">DESCRIBE DETAIL</code>,
        lineage, history, consumer runtimes, and sharing. It builds the manifest and seeds the
        Workstreams queue. Need a fresh read of one schema? Use <em>Re-profile a schema</em>. It
        reads only.
      </>
    ),
  },
  {
    id: 'dry_run',
    title: 'Dry-run — 15 pre-flight checks',
    body: (
      <>
        Before anything converts, <code className="font-mono text-[12px]">job_03_dry_run</code> assesses each table and stamps{' '}
        <code className="font-mono text-[12px]">DRY_RUN_OK</code> or <code className="font-mono text-[12px]">BLOCKED</code> with a reason and a
        remediation. A blocked table waits for you; it is never force-converted.
      </>
    ),
  },
  {
    id: 'freeze',
    title: 'Freeze — the optional pre-convert bracket',
    body: (
      <>
        If a wave is <code className="font-mono text-[12px]">AWAITING_ACTIONS</code> — open compaction, streams, or
        schedulable writer jobs writing the table — <code className="font-mono text-[12px]">job_03c_freeze</code> pauses them and records a{' '}
        <code className="font-mono text-[12px]">freeze_ledger</code> so unfreeze can restore each source after validate. It is{' '}
        <strong>optional and skippable</strong>: run it only when something needs pausing, and nothing is touched that a
        restart can&apos;t undo.
      </>
    ),
  },
  {
    id: 'lease',
    title: 'Authorize — grant what is missing (optional)',
    body: (
      <>
        Convert fails closed unless the convert identity holds ownership or MANAGE, plus SELECT and MODIFY, on every table.{' '}
        <code className="font-mono text-[12px]">job_03d_lease_ownership</code> (Authorize) grants only what it can grant
        itself and records each grant. The grant script names who must grant the rest. Ownership never changes.{' '}
        <strong>Skippable</strong> when every table is already authorized — Overview tells you when to authorize first.
      </>
    ),
  },
  {
    id: 'convert',
    title: 'Convert — the one gate',
    body: (
      <>
        <code className="font-mono text-[12px]">job_04_convert</code> runs{' '}
        <code className="font-mono text-[12px]">ALTER TABLE … SET MANAGED</code>. You pick the scope and type-to-confirm; the job
        re-checks every safety gate at mutation time and <strong>fails closed</strong> on a gap.
        This is the only step that changes tables — and it is reversible for 14 days.
      </>
    ),
  },
  {
    id: 'validate',
    title: 'Validate — prove the conversion',
    body: (
      <>
        <code className="font-mono text-[12px]">job_05_validate</code> is read-only. It compares the managed table against the
        pre-convert snapshot and advances <code className="font-mono text-[12px]">CONVERTED → VALIDATED</code>. A failure keeps the
        table CONVERTED so you can investigate — nothing is lost.
      </>
    ),
  },
  {
    id: 'finish',
    title: 'Finish the wave — the optional post-convert steps',
    body: (
      <>
        A validated wave isn&apos;t finished until its post-convert steps are done. Each runs{' '}
        <strong>only when it applies</strong>, in this order:{' '}
        <code className="font-mono text-[12px]">job_05b_unfreeze_verify</code> restores what the freeze paused
        from the ledger, <code className="font-mono text-[12px]">job_08_reshare</code> re-adds tables to their Delta
        shares, <code className="font-mono text-[12px]">job_08b_enable_compat</code> settles external readers (UC open
        APIs first, Compatibility Mode as the fallback), and{' '}
        <code className="font-mono text-[12px]">job_08c_enable_optimization</code> turns on automatic liquid clustering
        (Predictive Optimization is already on after the convert). All four change reversible metadata only. Overview walks you through them as the{' '}
        <em>Finishing</em> stage, then moves on to the next wave. If a re-share has no attributable share, or you
        haven&apos;t chosen a Compat location yet, <strong>Defer</strong> it with a reason — it stays on the runbook and
        Reopen brings it back. You can also <em>skip clustering for now</em>: no table changes, and the statements stay on the runbook to run later.
      </>
    ),
  },
  {
    id: 'cleanup',
    title: 'Clean up — finalize after 14 days',
    body: (
      <>
        Once the rollback window closes, <code className="font-mono text-[12px]">job_07_post_convert_cleanup</code> VACUUMs the old
        external files (Predictive-Optimization-aware). This is <strong>irreversible</strong> and
        ends rollback viability — the table reaches <code className="font-mono text-[12px]">CLEANED</code>.
      </>
    ),
  },
];

export function HappyPathPage() {
  return (
    <ModuleShell
      eyebrow="Module · 1 of 4"
      copy={SECTION_COPY.how.happyPath}
      backTo="/how-it-works"
      backLabel="How it works"
    >
      <Scrollytelling
        steps={STEPS}
        renderGraphic={(active) => <Swimlane stages={STAGES} activeIndex={active} />}
      />

      <Implications />
      <RollbackNet />
      <OneSchemaAtATime />

      <NextLinks
        links={[
          { to: '/overview', label: 'Overview', description: 'The guided next step for your estate.' },
          { to: '/convert', label: 'Convert (expert)', description: 'Scope a conversion by schema, bucket, or wave.' },
          { to: '/runs', label: 'Runs & history', description: 'Watch active runs and the audit feed.' },
        ]}
      />
    </ModuleShell>
  );
}

function Implications() {
  const { ref, inView } = useInView<HTMLDivElement>();
  const items = [
    { icon: Clock, text: 'Rollback stays available for 14 days after convert.' },
    { icon: Trash2, text: 'Predictive Optimization auto-deletes the old external files ~day 14.' },
    { icon: Info, text: 'Streaming jobs that read or write the table must be restarted after convert.' },
  ];
  return (
    <section
      ref={ref}
      className="how-reveal rounded-xl border border-[var(--info-border)] bg-[var(--info-bg)] p-5"
      data-inview={inView}
    >
      <ModuleHeading copy={SECTION_COPY.how.convertMeans} />
      <ul className="mt-3 flex flex-col gap-2">
        {items.map(({ icon: Icon, text }) => (
          <li key={text} className="flex items-start gap-2 text-sm text-foreground">
            <Icon className="mt-0.5 size-4 shrink-0 text-[var(--info)]" aria-hidden />
            {text}
          </li>
        ))}
      </ul>
    </section>
  );
}

function RollbackNet() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section
      ref={ref}
      className="how-reveal rounded-xl border border-border bg-card p-5"
      data-inview={inView}
    >
      <ModuleHeading
        copy={SECTION_COPY.how.rollback}
        icon={<Undo2 className="size-4 text-[var(--brand)]" aria-hidden />}
      />
      <p className="mt-2 text-sm text-muted-foreground">
        If something looks wrong after convert, <code className="font-mono text-[12px]">job_06_rollback</code> runs{' '}
        <code className="font-mono text-[12px]">ALTER TABLE … UNSET MANAGED</code> and returns the table to external.
      </p>
      <ul className="mt-3 grid gap-2 sm:grid-cols-2">
        {[
          'Only within 14 days — after that, Predictive Optimization has deleted the old files, so there is nothing to point back to.',
          'Only for CONVERTED / VALIDATED tables with a successful convert inside the window.',
          'Membership comes from the successful convert audit log, not the mutable plan.',
          'A non-blank reason is required; the table moves ROLLING_BACK → ROLLED_BACK.',
        ].map((t) => (
          <li key={t} className="flex gap-2 rounded-lg border border-border bg-background p-3 text-[12px] text-foreground">
            <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-[var(--brand)]" aria-hidden />
            {t}
          </li>
        ))}
      </ul>
      <p className="mt-3 text-[11px] text-muted-foreground">
        High-friction by design, but always available inside the window.
      </p>
    </section>
  );
}

function OneSchemaAtATime() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.happyFocus} />
      <p className="mt-2 text-sm text-muted-foreground">
        A schema the planner split into several waves walks this path once per wave. On the Overview, pick{' '}
        <strong>Work on another schema</strong> to follow one schema: its own next step, each wave in turn, and
        what is handled elsewhere (streaming, review, blocked). Genie Code and the cockpit notebook show the
        same step for the same schema.{' '}
        <Link to="/how-it-works/waves" className="font-medium text-[var(--brand)] hover:underline">
          How the order is chosen
        </Link>
      </p>
    </section>
  );
}
