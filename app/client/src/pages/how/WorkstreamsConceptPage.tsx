import { ModuleShell } from './components/ModuleShell';
import { WorkstreamTimeline } from './components/WorkstreamTimeline';
import { CodeReveal } from './components/CodeReveal';
import { NextLinks } from './components/NextLinks';
import { useInView } from '../../hooks/useInView';
import { TRACKS } from './workstreams.data';
import { ModuleHeading } from './components/ModuleHeading';
import { SECTION_COPY } from '../../lib/sectionCopy';

/**
 * Workstreams — the edge cases that need handling around convert. The App calls these
 * "Workstreams" (never "papercuts"); under the hood each is a track raised by one manifest
 * signal and tracked in the migration_actions queue. Each track is a lifecycle of one or two
 * action steps that BRACKET convert — a PRE_CONVERT action and, for freeze/stop, its
 * POST_CONVERT partner (UNFREEZE_COMPACTION / RESTART_STREAM). The timeline puts CONVERT at
 * the centre; selecting any step opens its whole bracket so the required post-convert restart
 * is never hidden. Path-based access gets a dedicated code-reveal.
 */

/** The six workstream tracks and the manifest signal that raises each. */
const TRACK_CHIPS: { label: string; signal: string }[] = TRACKS.map((t) => ({
  label: t.label,
  signal: t.signal,
}));

/** Hard blockers that are NOT workstreams — they stop convert and live in Blocked & review. */
const BLOCKERS = ['Corrupted / unreadable table', 'No managed location'];

export function WorkstreamsConceptPage() {
  return (
    <ModuleShell
      eyebrow="Module · 2 of 4"
      copy={SECTION_COPY.how.workstreamsModule}
      backTo="/how-it-works"
      backLabel="How it works"
    >
      <QueueIntro />

      <section className="rounded-xl border border-border bg-card p-5">
        <ModuleHeading copy={SECTION_COPY.how.workstreamTimeline} className="mb-4" />
        <WorkstreamTimeline tracks={TRACKS} />
        <p className="mt-3 text-[12px] text-muted-foreground">
          When you work on one schema from the Overview, its workstream items show under{' '}
          <strong>Handled elsewhere</strong>, with a link back here. They never hold the main path.
        </p>
      </section>

      <section className="rounded-xl border border-border bg-card p-5">
        <ModuleHeading copy={SECTION_COPY.how.pathAccess} className="mb-4" />
        <CodeReveal
          before={'df = spark.read.format("delta") \\\n  .load("s3://lake/prod/sales/orders")'}
          after={'df = spark.table("prod.sales.orders")'}
          caption="The app finds the exact file and line via the source scan — it never rewrites your code for you."
        />
      </section>

      <NextLinks
        links={[
          { to: '/workstreams', label: 'Workstreams', description: 'The live queue and per-track evidence.' },
          { to: '/freeze', label: 'Paused & stopped', description: 'What to restart after convert.' },
          { to: '/blocked', label: 'Blocked & review', description: 'Tables held back and why.' },
          { to: '/runbook', label: 'Do by hand', description: 'The exact manual statements to run.' },
        ]}
      />
    </ModuleShell>
  );
}

function QueueIntro() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.actionQueue} />
      <p className="mt-2 text-sm text-muted-foreground">
        Every complication the profiler detects becomes a tracked item in the{' '}
        <code className="font-mono text-[12px]">migration_actions</code> work queue. The App groups
        them into six Workstreams (eight action kinds — freeze and stream each pair a
        before-convert action with its after-convert partner). The engine owns each item{'\u2019'}s
        status; you can only edit its notes and assignee — a cleared signal is never deleted, it
        flips to obsolete.
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        These six are the manifest-signal tracks. The live Workstreams page shows a seventh —
        Ownership — surfaced separately because it is <code className="font-mono text-[11px]">comms_feed</code>-backed
        (ownership changes) rather than a <code className="font-mono text-[11px]">table_manifest</code> signal.
      </p>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {TRACK_CHIPS.map((t) => (
          <span
            key={t.label}
            className="flex items-center gap-1.5 rounded-full border border-border bg-muted px-2.5 py-1 text-[11px] text-muted-foreground"
          >
            {t.label}
            <code className="font-mono text-[9px] opacity-70">{t.signal}</code>
          </span>
        ))}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        Not the same as <span className="font-medium text-foreground">blockers</span> — hard
        stops like{' '}
        {BLOCKERS.map((b, i) => (
          <span key={b}>
            <span className="font-medium text-foreground">{b}</span>
            {i < BLOCKERS.length - 1 ? ', ' : ''}
          </span>
        ))}{' '}
        are eligibility failures, not tracked actions. They live in Blocked &amp; review.
      </p>
    </section>
  );
}
