import { AppWindow, ShieldCheck, TerminalSquare, NotebookPen } from 'lucide-react';
import { ModuleShell } from './components/ModuleShell';
import { CommandBuilder } from './components/CommandBuilder';
import { StepGuides } from './components/StepGuides';
import { CommandBlock } from './components/CommandBlock';
import { NextLinks } from './components/NextLinks';
import { useInView } from '../../hooks/useInView';
import { ESTATE_NOTE } from './directPath.pure';
import { ModuleHeading } from './components/ModuleHeading';
import { SECTION_COPY } from '../../lib/sectionCopy';

/**
 * Run it directly — the operating-modes module. When the app is not an option (air-gapped,
 * SP-only, permissions), the same bundle jobs run headless. The centerpiece is the command
 * builder; the cockpit walkthrough covers the notebook path. Everything is pinned to the jobs.md
 * invoke contract. Static/educational — no queries, and it never runs a command.
 */
export function DirectPathPage() {
  return (
    <ModuleShell
      eyebrow="Module · 4 of 4"
      copy={SECTION_COPY.how.directModule}
      backTo="/how-it-works"
      backLabel="How it works"
    >
      <ThreeModes />

      <section className="rounded-xl border border-border bg-card p-5">
        <ModuleHeading copy={SECTION_COPY.how.commandBuilder} className="mb-4" />
        <CommandBuilder />
      </section>

      <Recipes />
      <StepGuides />
      <SafetyInHeadless />

      <NextLinks
        links={[
          { to: '/runbook', label: 'Do by hand', description: 'The exact manual statements for open work.' },
          { to: '/runs', label: 'Runs & history', description: 'Watch launched runs and the audit feed.' },
          { to: '/overview', label: 'Overview', description: 'The guided next step, if you switch back.' },
        ]}
      />
    </ModuleShell>
  );
}

function ThreeModes() {
  const { ref, inView } = useInView<HTMLDivElement>();
  const modes = [
    {
      icon: AppWindow,
      title: 'App (guided)',
      body: 'Approve the recommended step. The app starts the job and shows the result. This is the default.',
    },
    {
      icon: TerminalSquare,
      title: 'CLI (headless)',
      body: 'databricks bundle run job_… -t <target> --params … — scriptable, per stage, no app needed.',
    },
    {
      icon: NotebookPen,
      title: 'Notebooks (cockpit)',
      body: '09_cockpit.py or the individual stage notebooks — run inside a Databricks workspace.',
    },
  ];
  return (
    <section ref={ref} className="how-reveal flex flex-col gap-3" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.threeWays} />
      <div className="grid gap-3 sm:grid-cols-3">
        {modes.map(({ icon: Icon, title, body }) => (
          <div key={title} className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
            <span className="flex size-9 items-center justify-center rounded-lg bg-[var(--brand)]/10">
              <Icon className="size-5 text-[var(--brand)]" aria-hidden />
            </span>
            <span className="text-sm font-semibold text-foreground">{title}</span>
            <span className="text-xs leading-relaxed text-muted-foreground">{body}</span>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Whichever you use, the safety is in the jobs, not the surface — the convert gate re-checks
        coverage and fails closed regardless of how it was launched.
      </p>
    </section>
  );
}

function Recipes() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.adhocVsEstate} className="mb-3" />

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-2">
          <span className="text-[11px] font-semibold text-foreground">Adhoc — one table (rollback within 14 days)</span>
          <CommandBlock command={'databricks bundle run job_06_rollback -t dev \\\n  --params full_name=main.sales.orders,reason="bad convert"'} />
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-[11px] font-semibold text-foreground">Whole schema — convert (streaming excluded)</span>
          <CommandBlock command={'databricks bundle run job_04_convert -t dev \\\n  --params target_schema=main.sales,confirm_compaction_paused=true'} />
        </div>
      </div>

      <div className="mt-4 flex flex-col gap-2">
        <span className="text-[11px] font-semibold text-foreground">Whole estate — the safe loop</span>
        <CommandBlock command={'databricks bundle run job_02_wave_planner -t dev\ndatabricks bundle run job_03_dry_run     -t dev\n# then, per schema:\ndatabricks bundle run job_04_convert     -t dev --params target_schema=<catalog.schema>,confirm_compaction_paused=true\n# and a separate streaming pass:\ndatabricks bundle run job_04_convert     -t dev --params bucket=streaming,confirm_compaction_paused=true'} />
        <p className="text-[11px] text-muted-foreground">{ESTATE_NOTE}</p>
      </div>
    </section>
  );
}

function SafetyInHeadless() {
  const { ref, inView } = useInView<HTMLDivElement>();
  const items = [
    'No implicit convert-all — a selector (target_schema, bucket, or wave_ids) is always required.',
    'Convert fails closed: it re-verifies compaction/stream coverage at mutation time, headless or not.',
    'Execute stages are dry-run by default — 03c / 03d / 05b / 08 re-share / 08b compat / 08c optimize need confirm_execute=true.',
    'Rollback is 14 days only; VACUUM (cleanup) is irreversible and only runs after the window.',
    'manifest_catalog / manifest_schema are deploy-fixed — a run-now command cannot redirect the control plane.',
  ];
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading
        copy={SECTION_COPY.how.headlessGates}
        icon={<ShieldCheck className="size-4 text-[var(--brand)]" aria-hidden />}
      />
      <ul className="mt-3 grid gap-2 sm:grid-cols-2">
        {items.map((t) => (
          <li key={t} className="flex gap-2 rounded-lg border border-border bg-background p-3 text-[12px] text-foreground">
            <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-[var(--brand)]" aria-hidden />
            {t}
          </li>
        ))}
      </ul>
    </section>
  );
}
