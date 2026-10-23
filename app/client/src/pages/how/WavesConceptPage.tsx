import { Snowflake, ArrowLeftRight, Play, AlertTriangle, KeyRound } from 'lucide-react';
import { ModuleShell } from './components/ModuleShell';
import { WaveBucketViz } from './components/WaveBucketViz';
import { NextLinks } from './components/NextLinks';
import { Stepper, type StepperStep } from '../../components/Stepper';
import { useInView } from '../../hooks/useInView';
import { ModuleHeading } from './components/ModuleHeading';
import { SECTION_COPY } from '../../lib/sectionCopy';
import { RECOMMENDATION_ORDER } from './waveJourney.pure';

/**
 * Schemas & waves — why migration runs in batches, how the planner shapes them, what a replan
 * does to the plan and to campaign readiness, and the two reversible brackets around convert:
 * freeze (what it pauses and why) and Authorize (what it grants and why). Ownership never changes.
 * Grounded in the planner + freeze + convert-permission contracts; terminology matches the App and backend.
 */

const FREEZE_BRACKET: StepperStep[] = [
  { id: 'freeze', label: 'Freeze applied', status: 'done', sublabel: 'job_03c_freeze' },
  { id: 'convert', label: 'Convert', status: 'active', sublabel: 'job_04_convert' },
  { id: 'unfreeze', label: 'Unfreeze + verify', status: 'pending', sublabel: 'job_05b' },
];

const FREEZE_INCLUDES = [
  { op: 'PAUSE_JOB', text: 'Pause the attributed compaction, OPTIMIZE, or VACUUM job.' },
  { op: 'DISABLE_PO', text: 'Disable Predictive Optimization on the schema.' },
  { op: 'STOP_STREAM_JOB', text: 'Stop an attributed structured-streaming job.' },
  { op: 'STOP_PIPELINE', text: 'Stop an attributed DLT pipeline.' },
];

// The Authorize bracket is intentionally WIDER than freeze: its grants are held from 03d all the
// way through cleanup (07 revokes), not released at 05b — because in-window rollback (UNSET
// MANAGED) needs the same convert permission.
const LEASE_BRACKET: StepperStep[] = [
  { id: 'lease', label: 'Grants added', status: 'done', sublabel: 'job_03d (Authorize)' },
  { id: 'convert', label: 'Convert', status: 'active', sublabel: 'job_04_convert' },
  { id: 'restore', label: 'Grants revoked', status: 'pending', sublabel: 'job_07 (cleanup)' },
];

const LEASE_INCLUDES = [
  { op: 'GRANT', text: 'GRANT only the privileges the convert identity is missing and can grant itself, each recorded in ownership_ledger.' },
  { op: 'REVOKE', text: 'At cleanup, REVOKE exactly the grants Authorize added. A privilege it already held is never revoked.' },
];

export function WavesConceptPage() {
  return (
    <ModuleShell
      eyebrow="Module · 3 of 4"
      copy={SECTION_COPY.how.wavesModule}
      backTo="/how-it-works"
      backLabel="How it works"
    >
      <SchemaVsWave />

      <section className="rounded-xl border border-border bg-card p-5">
        <ModuleHeading copy={SECTION_COPY.how.buckets} className="mb-4" />
        <WaveBucketViz />
      </section>

      <Planning />
      <Campaign />
      <FocusOrder />
      <FreezeBracket />
      <LeaseBracket />

      <NextLinks
        links={[
          { to: '/waves', label: 'Waves', description: 'The live plan, per-wave readiness and drift.' },
          { to: '/freeze', label: 'Paused & stopped', description: 'What is paused right now.' },
        ]}
      />
    </ModuleShell>
  );
}

function SchemaVsWave() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal grid gap-3 sm:grid-cols-2" data-inview={inView}>
      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-semibold text-foreground">A schema</h3>
        <p className="mt-1.5 text-[12px] text-muted-foreground">
          A Unity Catalog <code className="font-mono text-[11px]">catalog.schema</code> — the unit you
          usually reason about and scope a conversion to.
        </p>
      </div>
      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-semibold text-foreground">A wave</h3>
        <p className="mt-1.5 text-[12px] text-muted-foreground">
          A capped, ordered batch of tables, identified by{' '}
          <code className="font-mono text-[11px]">(plan_run_id, wave_id)</code>. A pilot proves the
          process; streaming is always isolated into its own wave.
        </p>
      </div>
    </section>
  );
}

function Planning() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.replan} />
      <p className="mt-2 text-sm text-muted-foreground">
        <code className="font-mono text-[12px]">job_02_wave_planner</code> fully overwrites the plan and
        mints a new <code className="font-mono text-[12px]">plan_run_id</code>. The current plan is the
        single plan present in <code className="font-mono text-[12px]">wave_plan</code> — never{' '}
        {'\u201Cthe latest\u201D'}. Convert and rollback pin to an exact{' '}
        <code className="font-mono text-[12px]">(plan_run_id, wave_id)</code>.
      </p>
      <div className="mt-3 flex items-start gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-[var(--warning)]" aria-hidden />
        <p className="text-[12px] text-foreground">
          A re-plan invalidates those pins, so the planner <strong>aborts if any table is CONVERTING</strong>{' '}
          (it names the offenders). The app also disables Re-plan mid-conversion and asks you to type
          the current <code className="font-mono text-[11px]">plan_run_id</code> to confirm. Overriding
          the guard needs the explicit <code className="font-mono text-[11px]">confirm_replan</code> escape hatch.
        </p>
      </div>
    </section>
  );
}

function FocusOrder() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.focusOrder} />
      <p className="mt-2 text-sm text-muted-foreground">
        On the Overview, <strong>Work on another schema</strong> shows one schema&apos;s own next step and
        walks its waves one at a time. Each convert and pause covers that one wave. The step recommended
        for everyone does not change. <strong>Team priorities</strong> do change it, but only in this order:
      </p>
      <ol className="mt-3 flex flex-col gap-2">
        {RECOMMENDATION_ORDER.map((r, i) => (
          <li key={r.id} className="flex items-start gap-3 text-sm">
            <span className="mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full border border-border text-[11px] tabular-nums">
              {i + 1}
            </span>
            <span>
              <strong>{r.label}.</strong> <span className="text-muted-foreground">{r.why}</span>
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-[12px] text-muted-foreground">
        A pin only changes the order. Every check still runs, and a pin clears itself once its schema is done.
      </p>
    </section>
  );
}

function Campaign() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.campaign} />
      <p className="mt-2 text-sm text-muted-foreground">
        A campaign (<code className="font-mono text-[12px]">job_10_campaign</code>) is per-schema{' '}
        <em>prep</em>: it resolves scope, runs the dry-run, refreshes the recommendation, and writes a{' '}
        <code className="font-mono text-[12px]">GO / NO-GO</code> verdict. It <strong>never converts</strong>.
        A verdict is a point-in-time reading for a schema — so a re-plan that reshuffles wave membership
        underneath it means a prior GO must be re-checked (verdicts also go stale after a freshness window).
      </p>
    </section>
  );
}

function FreezeBracket() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading
        copy={SECTION_COPY.how.freezeBracket}
        icon={<Snowflake className="size-4 text-[var(--info)]" aria-hidden />}
      />
      <div className="mt-4">
        <Stepper variant="bracket" steps={FREEZE_BRACKET} />
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-background p-3">
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--info)]">
            What freeze includes
          </h3>
          <ul className="mt-2 flex flex-col gap-1.5">
            {FREEZE_INCLUDES.map((f) => (
              <li key={f.op} className="flex items-start gap-2 text-[12px] text-foreground">
                <code className="mt-0.5 rounded bg-muted px-1 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {f.op}
                </code>
                {f.text}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Each op records prior state so <code className="font-mono text-[10px]">job_05b</code> can
            restore exactly.
          </p>
        </div>

        <div className="rounded-lg border border-border bg-background p-3">
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--brand)]">
            Why it{'\u2019'}s necessary
          </h3>
          <p className="mt-2 text-[12px] text-foreground">
            <code className="font-mono text-[11px]">SET MANAGED</code> must not run while another engine
            is writing the table. A concurrent OPTIMIZE, liquid-clustering, PO-driven compaction, or an
            active stream can corrupt the conversion (e.g. the{' '}
            <code className="font-mono text-[11px]">DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED</code> failure). Freeze
            quiesces those writers first.
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
        <div className="flex items-center gap-2 text-[12px] font-medium text-foreground">
          <ArrowLeftRight className="size-4 text-muted-foreground" aria-hidden />
          The honest boundary
        </div>
        <ul className="flex flex-col gap-1.5 text-[12px] text-muted-foreground">
          <li className="flex items-start gap-2">
            <Play className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Freeze is optional and a <strong>separate launch</strong> — there is no {'\u201Cfreeze & convert\u201D'} button.
          </li>
          <li className="flex items-start gap-2">
            <Play className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Convert <strong>never pauses anything</strong>. It machine-verifies coverage against the freeze
            ledger and fails closed if a schedulable compaction actor or attributed stream is still live.
          </li>
        </ul>
      </div>
    </section>
  );
}

function LeaseBracket() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading
        copy={SECTION_COPY.how.leaseBracket}
        icon={<KeyRound className="size-4 text-[var(--info)]" aria-hidden />}
      />
      <div className="mt-4">
        <Stepper variant="bracket" steps={LEASE_BRACKET} />
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-background p-3">
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--info)]">
            What Authorize includes
          </h3>
          <ul className="mt-2 flex flex-col gap-1.5">
            {LEASE_INCLUDES.map((f) => (
              <li key={f.op} className="flex items-start gap-2 text-[12px] text-foreground">
                <code className="mt-0.5 rounded bg-muted px-1 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {f.op}
                </code>
                {f.text}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Each grant is recorded in <code className="font-mono text-[10px]">ownership_ledger</code> so
            <code className="font-mono text-[10px]"> job_07</code> revokes exactly what was added.
          </p>
        </div>

        <div className="rounded-lg border border-border bg-background p-3">
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--brand)]">
            Why it{'\u2019'}s necessary
          </h3>
          <p className="mt-2 text-[12px] text-foreground">
            <code className="font-mono text-[11px]">SET MANAGED</code> — and the rollback{"'"}s{' '}
            <code className="font-mono text-[11px]">UNSET MANAGED</code> — need the convert identity to hold{' '}
            <strong>ownership or MANAGE</strong> on the table, its schema or its catalog, plus USE CATALOG, USE
            SCHEMA, and SELECT and MODIFY unless it owns the table. Authorize grants what it can grant itself.
            The grant script names who must grant the rest. A table already authorized needs nothing.
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
        <div className="flex items-center gap-2 text-[12px] font-medium text-foreground">
          <ArrowLeftRight className="size-4 text-muted-foreground" aria-hidden />
          The honest boundary
        </div>
        <ul className="flex flex-col gap-1.5 text-[12px] text-muted-foreground">
          <li className="flex items-start gap-2">
            <Play className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Only <strong>grants</strong> change, never ownership — Authorize never runs{' '}
            <code className="font-mono text-[11px]">SET MANAGED</code>, never touches data, never VACUUMs.
          </li>
          <li className="flex items-start gap-2">
            <Play className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            The grants are held <strong>through the 14-day window</strong> and revoked at cleanup — not at 05b —
            so an in-window rollback stays possible.
          </li>
          <li className="flex items-start gap-2">
            <Play className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Dry-run by default and a <strong>separate launch</strong>. Tables already authorized are skipped.
          </li>
        </ul>
      </div>
    </section>
  );
}
