import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { cn } from '@databricks/appkit-ui/react';
import {
  DEFAULT_FILTER,
  filterSteps,
  flattenSteps,
  nextSelectedTrack,
  visibleTracks,
  type FlatStep,
  type WorkstreamFilter,
  type WorkstreamStep,
  type WorkstreamTrack,
} from '../workstreams.pure';

// Re-exported so the page (and other importers) keep a single type source.
export type { Phase, WorkstreamTrack } from '../workstreams.pure';

/**
 * WorkstreamTimeline — a horizontal axis with CONVERT at the centre. PRE_CONVERT steps sit to
 * the left, POST_CONVERT to the right; blocking steps use destructive tokens, advisory use
 * info. A filter row (phase / handling / app-can-run) narrows the visible steps via the pure
 * `filterSteps`; `visibleTracks` + `nextSelectedTrack` keep the detail panel coherent.
 *
 * Selecting any step chip opens its whole TRACK below — the pre→convert→post lifecycle
 * bracket — so clicking STOP_STREAM shows the required RESTART_STREAM that the old flat model
 * used to drop. Chips are keyboard-selectable buttons; the panel is a single live region.
 */
export function WorkstreamTimeline({ tracks }: { tracks: WorkstreamTrack[] }) {
  const [filter, setFilter] = useState<WorkstreamFilter>(DEFAULT_FILTER);
  const [selected, setSelected] = useState<string>(tracks[0]?.id ?? '');

  const allSteps = useMemo(() => flattenSteps(tracks), [tracks]);
  const visibleSteps = useMemo(() => filterSteps(allSteps, filter), [allSteps, filter]);
  const visTracks = useMemo(() => visibleTracks(tracks, filter), [tracks, filter]);
  // Keep the current selection if its track survives the filter, else the first visible track.
  const effectiveSelected = nextSelectedTrack(visTracks, selected);

  const pre = visibleSteps.filter((s) => s.phase === 'PRE_CONVERT');
  const post = visibleSteps.filter((s) => s.phase === 'POST_CONVERT');
  const activeTrack = tracks.find((t) => t.id === effectiveSelected) ?? null;

  return (
    <div className="flex flex-col gap-5">
      <FilterBar filter={filter} onChange={setFilter} shown={visibleSteps.length} total={allSteps.length} />
      <Legend />

      {visibleSteps.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border bg-background p-6 text-center text-sm text-muted-foreground">
          No workstream steps match these filters.
        </div>
      ) : (
        <div className="grid gap-3 lg:grid-cols-[1fr_auto_1fr] lg:items-stretch">
          <Column heading="Before convert" align="end" steps={pre} selected={effectiveSelected} onSelect={setSelected} />

          <div className="flex items-center justify-center lg:flex-col lg:gap-2">
            <div className="hidden h-full w-px bg-border lg:block" aria-hidden />
            <span className="rounded-full border-2 border-[var(--brand)] bg-[var(--brand)]/10 px-3 py-1 text-xs font-semibold text-[var(--brand)]">
              CONVERT
            </span>
            <div className="hidden h-full w-px bg-border lg:block" aria-hidden />
          </div>

          <Column heading="After convert" align="start" steps={post} selected={effectiveSelected} onSelect={setSelected} />
        </div>
      )}

      {activeTrack ? <TrackDetailPanel track={activeTrack} /> : null}
    </div>
  );
}

const PHASE_OPTIONS: { value: WorkstreamFilter['phase']; label: string }[] = [
  { value: 'ALL', label: 'All' },
  { value: 'PRE_CONVERT', label: 'Before' },
  { value: 'POST_CONVERT', label: 'After' },
];

const HANDLING_OPTIONS: { value: WorkstreamFilter['handling']; label: string }[] = [
  { value: 'ALL', label: 'All' },
  { value: 'BLOCKING', label: 'Blocking' },
  { value: 'ADVISORY', label: 'Advisory' },
];

function FilterBar({
  filter,
  onChange,
  shown,
  total,
}: {
  filter: WorkstreamFilter;
  onChange: (f: WorkstreamFilter) => void;
  shown: number;
  total: number;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      <SegmentedControl
        label="Phase"
        options={PHASE_OPTIONS}
        value={filter.phase}
        onSelect={(phase) => onChange({ ...filter, phase })}
      />
      <SegmentedControl
        label="Handling"
        options={HANDLING_OPTIONS}
        value={filter.handling}
        onSelect={(handling) => onChange({ ...filter, handling })}
      />
      <button
        type="button"
        role="switch"
        aria-checked={filter.appRunOnly}
        onClick={() => onChange({ ...filter, appRunOnly: !filter.appRunOnly })}
        className={cn(
          'how-press flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[11px] font-medium',
          filter.appRunOnly
            ? 'border-[var(--brand)] bg-[var(--brand)]/10 text-[var(--brand)]'
            : 'border-border bg-background text-muted-foreground',
        )}
      >
        <span
          className={cn(
            'size-2 rounded-full',
            filter.appRunOnly ? 'bg-[var(--brand)]' : 'bg-muted-foreground/40',
          )}
          aria-hidden
        />
        App can run
      </button>
      <span className="ml-auto text-[11px] text-muted-foreground">
        {shown} of {total} shown
      </span>
    </div>
  );
}

function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onSelect,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onSelect: (v: T) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</span>
      <div className="flex items-center gap-0.5 rounded-lg border border-border bg-background p-0.5" role="group" aria-label={label}>
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => onSelect(o.value)}
            aria-pressed={value === o.value}
            className={cn(
              'how-press rounded-md px-2.5 py-1 text-[11px] font-medium',
              value === o.value ? 'bg-[var(--brand)] text-[var(--fg-on-dark)]' : 'text-muted-foreground',
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span className="size-2.5 rounded-sm border border-[var(--destructive-border)] bg-[var(--destructive-bg)]" aria-hidden />
        Blocking — convert waits
      </span>
      <span className="flex items-center gap-1.5">
        <span className="size-2.5 rounded-sm border border-[var(--info-border)] bg-[var(--info-bg)]" aria-hidden />
        Advisory — you handle it
      </span>
      <span className="flex items-center gap-1.5">
        <span className="size-2.5 rounded-sm border border-border bg-muted" aria-hidden />
        App can run it (gated)
      </span>
      <span className="flex items-center gap-1.5">
        <span aria-hidden>⇄</span>
        Attributed-only — you handle unattributed
      </span>
    </div>
  );
}

function Column({
  heading,
  align,
  steps,
  selected,
  onSelect,
}: {
  heading: string;
  align: 'start' | 'end';
  steps: FlatStep[];
  selected: string;
  onSelect: (trackId: string) => void;
}) {
  return (
    <div className={cn('flex flex-col gap-2', align === 'end' ? 'lg:items-end' : 'lg:items-start')}>
      <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {heading}
      </span>
      {steps.map((s) => {
        const isSel = s.trackId === selected;
        const tone = s.blocking
          ? 'border-[var(--destructive-border)] bg-[var(--destructive-bg)] text-[var(--destructive-foreground)]'
          : 'border-[var(--info-border)] bg-[var(--info-bg)] text-[var(--info-foreground)]';
        return (
          <button
            key={s.kind}
            type="button"
            onClick={() => onSelect(s.trackId)}
            aria-pressed={isSel}
            className={cn(
              'how-press flex w-full max-w-xs items-center gap-2 rounded-lg border px-3 py-2 text-left',
              tone,
              isSel && 'ring-2 ring-[var(--brand)] ring-offset-1 ring-offset-background',
            )}
          >
            <span className="flex flex-col">
              <span className="flex items-center gap-1.5 text-xs font-semibold">
                {s.trackLabel}
                {s.attributedOnly ? <span aria-hidden title="Attributed-only">⇄</span> : null}
              </span>
              <code className="font-mono text-[10px] opacity-80">{s.kind}</code>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function TrackDetailPanel({ track }: { track: WorkstreamTrack }) {
  return (
    <div className="rounded-xl border border-border bg-background p-4" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-foreground">{track.label}</h4>
        <code className="font-mono text-[10px] text-muted-foreground">{track.signal}</code>
        {track.repeatAfterRollback ? (
          <span className="rounded-full bg-[var(--brand)]/10 px-2 py-0.5 text-[10px] font-semibold text-[var(--brand)]">
            Repeat after rollback
          </span>
        ) : null}
      </div>
      <p className="mt-2 text-sm text-muted-foreground">{track.what}</p>

      <LifecycleBracket track={track} />

      <div className="mt-3 rounded-lg border border-border bg-card p-3">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--info)]">
          What you do
        </span>
        <p className="mt-1 text-[12px] text-foreground">{track.youDo}</p>
      </div>

      {track.liveLinks.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">See it live</span>
          {track.liveLinks.map((l) => (
            <Link
              key={l.to}
              to={l.to}
              className="how-press rounded-lg border border-border bg-card px-2.5 py-1 text-[11px] font-medium text-foreground"
            >
              {l.label}
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The lifecycle bracket: PRE steps, a SET MANAGED divider, then POST steps — so a track's
 * pre→convert→post shape (e.g. STOP_STREAM → convert → RESTART_STREAM) reads as one thing.
 */
function LifecycleBracket({ track }: { track: WorkstreamTrack }) {
  const pre = track.steps.filter((s) => s.phase === 'PRE_CONVERT');
  const post = track.steps.filter((s) => s.phase === 'POST_CONVERT');
  return (
    <div className="mt-3 flex flex-col gap-2">
      {pre.map((s) => (
        <StepCard key={s.kind} step={s} />
      ))}
      <div className="flex items-center gap-2" aria-hidden>
        <span className="rounded-full border-2 border-[var(--brand)] bg-[var(--brand)]/10 px-2.5 py-0.5 text-[10px] font-semibold text-[var(--brand)]">
          Convert
        </span>
        <span className="h-px flex-1 bg-border" />
      </div>
      {post.map((s) => (
        <StepCard key={s.kind} step={s} />
      ))}
    </div>
  );
}

function StepCard({ step }: { step: WorkstreamStep }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <code className="font-mono text-[11px] font-semibold text-foreground">{step.kind}</code>
        {step.blocking ? (
          <Badge tone="destructive">Blocking</Badge>
        ) : (
          <Badge tone="info">Advisory</Badge>
        )}
        <Badge tone={step.appRole === 'RUNS_GATED' ? 'muted' : 'ghost'}>
          {step.appRole === 'RUNS_GATED' ? 'App runs (gated)' : 'App advises only'}
        </Badge>
        {step.job ? <code className="font-mono text-[10px] text-muted-foreground">{step.job}</code> : null}
        {step.attributedOnly ? <Badge tone="ghost">Attributed-only</Badge> : null}
      </div>
      <p className="text-[12px] text-muted-foreground">{step.detail}</p>
      <code className="block overflow-x-auto whitespace-pre-wrap rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground">
        {step.sample}
      </code>
    </div>
  );
}

function Badge({ tone, children }: { tone: 'destructive' | 'info' | 'muted' | 'ghost'; children: ReactNode }) {
  const cls =
    tone === 'destructive'
      ? 'bg-[var(--destructive-bg)] text-[var(--destructive-foreground)]'
      : tone === 'info'
        ? 'bg-[var(--info-bg)] text-[var(--info-foreground)]'
        : tone === 'muted'
          ? 'bg-muted text-muted-foreground'
          : 'border border-border text-muted-foreground';
  return <span className={cn('rounded-full px-2 py-0.5 text-[10px] font-semibold', cls)}>{children}</span>;
}
