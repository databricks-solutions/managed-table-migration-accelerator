import { Link } from 'react-router';
import { ArrowUpRight, KeyRound, Layers, Route, ShieldCheck, TerminalSquare, Waves as WavesIcon, type LucideIcon } from 'lucide-react';
import { cn } from '@databricks/appkit-ui/react';
import { Scrollytelling, type ScrollStep } from './components/Scrollytelling';
import { EstateMap } from './components/EstateMap';
import { Legend } from './components/Legend';
import { NextLinks } from './components/NextLinks';
import { WaveJourney } from './components/WaveJourney';
import { useInView } from '../../hooks/useInView';
import './how.css';
import { ModuleHeading } from './components/ModuleHeading';
import { SECTION_COPY } from '../../lib/sectionCopy';

/**
 * How it works — the landing. A single bird's-eye "estate map" (EstateMap) narrated by scroll:
 * one pinned graphic advances through the lifecycle as the reader scrolls the steps, then the
 * safety rail and the three deep-dive modules. Static/educational (no query data). Terminology
 * matches the App and backend verbatim (Workstreams, Convert, freeze) — never "papercuts".
 * The narrative text below is the accessible source of truth; EstateMap is aria-hidden.
 */

const STEPS: ScrollStep[] = [
  {
    id: 'discover',
    title: 'Discover — one estate scan',
    body: (
      <>
        <code className="font-mono text-[12px]">job_00_profiler</code> scans the whole estate — Unity
        Catalog metadata, <code className="font-mono text-[12px]">DESCRIBE DETAIL</code>, lineage,
        history, consumer runtimes, sharing — builds the manifest and seeds the Workstreams queue.
        You approve, the app dispatches, the jobs read. Nothing changes yet.
      </>
    ),
  },
  {
    id: 'plan',
    title: 'Plan — tables become waves',
    body: (
      <>
        <code className="font-mono text-[12px]">job_02_wave_planner</code> groups eligible tables into
        ordered, capped batches — waves — and records them in the control tables. One plan at a time:
        wave-scoped reads pin a single plan snapshot rather than mixing plans.
      </>
    ),
  },
  {
    id: 'prepare',
    title: 'Prepare: dry run, then optionally freeze and authorize',
    body: (
      <>
        <code className="font-mono text-[12px]">job_03_dry_run</code> runs 15 checks per table
        (<code className="font-mono text-[12px]">DRY_RUN_OK</code> or <code className="font-mono text-[12px]">BLOCKED</code>).
        Where a writer is live, <code className="font-mono text-[12px]">job_03c_freeze</code> pauses the
        compaction job, disables Predictive Optimization, and stops streams — recording prior state so it
        can be restored exactly. And where the convert identity is missing a permission,{' '}
        <code className="font-mono text-[12px]">job_03d_lease_ownership</code> (Authorize) grants it only
        what it is missing and records each grant. Ownership never changes. Both are separate launches,
        and both are reversible.
      </>
    ),
  },
  {
    id: 'convert',
    title: 'Convert — the one gate, fails closed',
    body: (
      <>
        <code className="font-mono text-[12px]">job_04_convert</code> runs{' '}
        <code className="font-mono text-[12px]">ALTER TABLE … SET MANAGED</code>. It re-verifies every gate
        at mutation time and <strong>fails closed</strong> if a schedulable compaction actor or an
        attributed stream is still live, or if the convert identity is not authorized for a table. This is the only
        step that changes a table — Guided and Expert are two modes, one gate.
      </>
    ),
  },
  {
    id: 'validate',
    title: 'Validate — prove it, then finish the wave',
    body: (
      <>
        <code className="font-mono text-[12px]">job_05_validate</code> (read-only) compares the managed
        table to its pre-convert snapshot: <code className="font-mono text-[12px]">CONVERTED → VALIDATED</code>.
        Then the wave finishes with whichever steps apply, in order:{' '}
        <code className="font-mono text-[12px]">job_05b</code> unfreezes what the freeze paused and restarts
        streams, <code className="font-mono text-[12px]">job_08</code> re-shares Delta Sharing tables,{' '}
        <code className="font-mono text-[12px]">job_08b</code> settles external readers (UC open APIs first,
        Compatibility Mode only for a reader that cannot use them),
        and <code className="font-mono text-[12px]">job_08c</code> turns on automatic liquid clustering
        (Predictive Optimization is already on after the convert). Overview recommends each of these for the wave you just converted (the{' '}
        <em>Finishing</em> stage) before it suggests a new wave. A re-share or Compat step the tool can&apos;t
        close yet can be <strong>deferred</strong> with a reason: it stops holding the wave, stays on the runbook,
        and can be reopened.
      </>
    ),
  },
  {
    id: 'rollback',
    title: 'Rollback — the 14-day net',
    body: (
      <>
        For 14 days, <code className="font-mono text-[12px]">job_06_rollback</code> can{' '}
        <code className="font-mono text-[12px]">UNSET MANAGED</code> and return the table to external. The
        window is advisory: after ~14 days Predictive Optimization deletes the old external files, so
        rollback is only possible inside it.
      </>
    ),
  },
  {
    id: 'cleanup',
    title: 'Clean up — finalize',
    body: (
      <>
        Once the window closes, <code className="font-mono text-[12px]">job_07_post_convert_cleanup</code>{' '}
        VACUUMs the old external files (irreversible): state <code className="font-mono text-[12px]">CLEANED</code>,
        and revokes only the grants Authorize added. The migration for that table is done.
      </>
    ),
  },
];

const SAFETY_RAIL: { title: string; detail: string }[] = [
  { title: 'Two modes, one gate', detail: 'Guided and Expert run the same convert job with the same safety checks.' },
  { title: 'Pausing is separate', detail: 'Convert never pauses anything itself. It checks the pause and stops on a gap.' },
  { title: 'Old files stay 14 days', detail: 'Nothing is deleted before about day 14. If Predictive Optimization is off, schedule VACUUM yourself.' },
  { title: 'Rollback is 14 days', detail: 'You can undo a convert only while the old external files still exist.' },
  { title: 'Pausing changes little', detail: 'Only reversible settings change. Stream checkpoints are recorded, never touched.' },
  { title: 'Ownership never changes', detail: 'Authorize grants the convert identity only what it is missing. Cleanup revokes exactly those grants.' },
];

interface Module {
  to: string;
  title: string;
  teaser: string;
  icon: LucideIcon;
}

const MODULES: Module[] = [
  {
    to: '/how-it-works/happy-path',
    title: SECTION_COPY.how.happyPath.title,
    teaser: SECTION_COPY.how.happyPath.subtitle,
    icon: Route,
  },
  {
    to: '/how-it-works/workstreams',
    title: SECTION_COPY.how.workstreamsModule.title,
    teaser: SECTION_COPY.how.workstreamsModule.subtitle,
    icon: Layers,
  },
  {
    to: '/how-it-works/waves',
    title: SECTION_COPY.how.wavesModule.title,
    teaser: SECTION_COPY.how.wavesModule.subtitle,
    icon: WavesIcon,
  },
  {
    to: '/how-it-works/direct',
    title: SECTION_COPY.how.directModule.title,
    teaser: SECTION_COPY.how.directModule.subtitle,
    icon: TerminalSquare,
  },
];

export function HowItWorksPage() {
  return (
    <div className="how-academy mx-auto flex max-w-4xl flex-col gap-6">
      <Hero />
      <Legend />
      <WaveJourney />

      <section className="flex flex-col gap-2">
        <ModuleHeading copy={SECTION_COPY.how.atAGlance} />
        <div className="mt-2">
          <Scrollytelling steps={STEPS} renderGraphic={(active) => <EstateMap activeStep={active} />} />
        </div>
      </section>

      <SafetyRail />
      <WhoCanConvert />
      <ModulesGrid />

      <NextLinks
        links={[
          { to: '/overview', label: 'Overview', description: 'The guided next step for your estate.' },
          { to: '/dashboard', label: 'Estate dashboard', description: 'The live migration KPIs and burn-down.' },
        ]}
      />
    </div>
  );
}

function Hero() {
  return (
    <section className="rounded-xl border-0 bg-[var(--surface-deep)] p-6 text-[var(--fg-on-dark)]">
      <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-[var(--label-on-dark)]">
        How it works
      </span>
      <h1 className="mt-1 text-[24px] font-semibold text-[var(--fg-on-dark)]">{SECTION_COPY.how.hub.title}</h1>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--fg-on-dark-2)]">
        {SECTION_COPY.how.hub.subtitle}{' '}
        <strong className="text-[var(--fg-on-dark)]">{SECTION_COPY.how.hub.detail}</strong>
      </p>
    </section>
  );
}

function SafetyRail() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading
        copy={SECTION_COPY.how.safetyRail}
        icon={<ShieldCheck className="size-4 text-[var(--brand)]" aria-hidden />}
      />
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {SAFETY_RAIL.map((r) => (
          <div key={r.title} className="flex flex-col gap-1 rounded-lg border border-border bg-background p-3">
            <span className="text-[12px] font-semibold text-foreground">{r.title}</span>
            <span className="text-[11px] leading-snug text-muted-foreground">{r.detail}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

const CONVERT_RULE: { title: string; detail: string }[] = [
  { title: 'USE CATALOG and USE SCHEMA', detail: 'On the catalog and the schema that hold the table.' },
  { title: 'Ownership or MANAGE', detail: 'On the table, its schema or its catalog: direct, inherited, or through a group.' },
  { title: 'SELECT and MODIFY', detail: 'On the table, unless the convert identity owns it.' },
  { title: 'Who grants the rest', detail: 'Authorize grants what the convert identity can grant itself. The grant script names who must grant the rest.' },
];

function WhoCanConvert() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading
        copy={SECTION_COPY.how.whoCanConvert}
        icon={<KeyRound className="size-4 text-[var(--brand)]" aria-hidden />}
      />
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {CONVERT_RULE.map((r) => (
          <div key={r.title} className="flex flex-col gap-1 rounded-lg border border-border bg-background p-3">
            <span className="text-[12px] font-semibold text-foreground">{r.title}</span>
            <span className="text-[11px] leading-snug text-muted-foreground">{r.detail}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function ModulesGrid() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section className="flex flex-col gap-3">
      <ModuleHeading copy={SECTION_COPY.how.goDeeper} />
      <div ref={ref} className="how-stagger grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-inview={inView}>
        {MODULES.map((m, i) => {
          const Icon = m.icon;
          return (
            <Link
              key={m.to}
              to={m.to}
              style={{ ['--i' as string]: i }}
              className={cn(
                'how-press group flex flex-col gap-3 rounded-xl border border-border bg-card p-4',
                'hover:border-[var(--brand)]',
              )}
            >
              <span className="flex size-9 items-center justify-center rounded-lg bg-[var(--brand)]/10">
                <Icon className="size-5 text-[var(--brand)]" aria-hidden />
              </span>
              <span className="flex flex-col gap-1">
                <span className="flex items-center gap-1 text-sm font-semibold text-foreground">
                  {m.title}
                  <ArrowUpRight
                    className="size-3.5 text-muted-foreground transition-colors group-hover:text-[var(--brand)]"
                    aria-hidden
                  />
                </span>
                <span className="text-xs leading-relaxed text-muted-foreground">{m.teaser}</span>
              </span>
            </Link>
          );
        })}
      </div>
    </section>
  );
}
