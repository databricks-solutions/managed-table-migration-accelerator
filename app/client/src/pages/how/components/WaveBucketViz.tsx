import { useMemo, useState } from 'react';
import { useInView } from '../../../hooks/useInView';
import {
  BUCKET_LABEL,
  BUCKET_ORDER,
  CAP_BOUNDS,
  DEFAULT_CAPS,
  chunkIntoWaves,
  clampCaps,
  type DemoTable,
  type WaveCaps,
} from '../waveDemo.pure';

/** A small illustrative estate — one schema's tables spread across the buckets. */
const SAMPLE: DemoTable[] = [
  { id: 'p1', tb: 0.2, bucket: 'pilot' },
  { id: 'p2', tb: 0.3, bucket: 'pilot' },
  { id: 'p3', tb: 0.2, bucket: 'pilot' },
  { id: 'n1', tb: 1.5, bucket: 'normal' },
  { id: 'n2', tb: 2, bucket: 'normal' },
  { id: 'n3', tb: 3, bucket: 'normal' },
  { id: 'n4', tb: 1, bucket: 'normal' },
  { id: 'n5', tb: 2.5, bucket: 'normal' },
  { id: 's1', tb: 1, bucket: 'streaming' },
  { id: 's2', tb: 1.5, bucket: 'streaming' },
  { id: 'c1', tb: 2, bucket: 'compat' },
  { id: 'sh1', tb: 1, bucket: 'share' },
  { id: 'f1', tb: 12, bucket: 'final_large' },
];

/**
 * WaveBucketViz — shows the planner's shape: tables grouped into the six buckets (in execution
 * order) then chunked into capped waves. Three cap sliders (max tables, max TB, pilot cap)
 * re-chunk the plan live so the reader feels how tighter caps make more, smaller waves. Reveal
 * staggers on scroll. All plan math is the pure `waveDemo` (chunkIntoWaves / clampCaps).
 */
export function WaveBucketViz() {
  const { ref, inView } = useInView<HTMLDivElement>();
  const [caps, setCaps] = useState<WaveCaps>(DEFAULT_CAPS);
  const safeCaps = useMemo(() => clampCaps(caps), [caps]);
  const waves = useMemo(() => chunkIntoWaves(SAMPLE, safeCaps), [safeCaps]);

  const setCap = (key: keyof WaveCaps) => (value: number) => setCaps((c) => ({ ...c, [key]: value }));

  return (
    <div className="flex flex-col gap-4">
      {/* bucket order legend */}
      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        <span className="font-semibold uppercase tracking-[0.12em] text-muted-foreground">Wave order</span>
        {BUCKET_ORDER.map((b, i) => (
          <span key={b} className="flex items-center gap-1.5">
            <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-muted-foreground">
              {BUCKET_LABEL[b]}
            </span>
            {i < BUCKET_ORDER.length - 1 ? <span className="text-muted-foreground">›</span> : null}
          </span>
        ))}
      </div>

      {/* cap sliders — drag to re-shape the plan live */}
      <div className="flex flex-col gap-3 rounded-lg border border-border bg-background p-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <CapSlider
            label="Max tables / wave"
            value={safeCaps.maxTables}
            bounds={CAP_BOUNDS.maxTables}
            unit="tables"
            onChange={setCap('maxTables')}
          />
          <CapSlider
            label="Max TB / wave"
            value={safeCaps.maxTb}
            bounds={CAP_BOUNDS.maxTb}
            unit="TB"
            onChange={setCap('maxTb')}
          />
          <CapSlider
            label="Pilot cap"
            value={safeCaps.pilotMax}
            bounds={CAP_BOUNDS.pilotMax}
            unit="tables"
            onChange={setCap('pilotMax')}
          />
        </div>
        <p className="text-[11px] text-muted-foreground">
          {safeCaps.maxTables} tables / {safeCaps.maxTb} TB per wave, pilot capped at {safeCaps.pilotMax} →{' '}
          <strong className="text-foreground">
            {waves.length} {waves.length === 1 ? 'wave' : 'waves'}
          </strong>
        </p>
      </div>

      {/* resulting waves */}
      <div ref={ref} className="how-stagger flex flex-col gap-2" data-inview={inView}>
        {waves.map((w, i) => (
          <div
            key={w.waveId}
            style={{ ['--i' as string]: i }}
            className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3 sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex items-center gap-3">
              <code className="font-mono text-[11px] font-semibold text-foreground">{w.waveId}</code>
              <span className="rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                {BUCKET_LABEL[w.bucket]}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-1" aria-hidden>
                {w.tables.map((t) => (
                  <span
                    key={t.id}
                    className="size-2.5 rounded-full bg-[var(--brand)]/70"
                    title={`${t.id} · ${t.tb} TB`}
                  />
                ))}
              </div>
              <span className="text-[11px] text-muted-foreground">
                {w.tables.length} {w.tables.length === 1 ? 'table' : 'tables'} · {w.totalTb} TB
              </span>
              {w.exceedsByteCap ? (
                <span className="rounded-full bg-[var(--warning-bg)] px-2 py-0.5 text-[10px] font-semibold text-[var(--warning-foreground)]">
                  oversized singleton
                </span>
              ) : null}
            </div>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Streaming is always isolated into its own wave. Real caps are 300 tables / 25 TB. The
        planner stamps one plan and converts buckets in this order.
      </p>
    </div>
  );
}

function CapSlider({
  label,
  value,
  bounds,
  unit,
  onChange,
}: {
  label: string;
  value: number;
  bounds: { min: number; max: number; step: number };
  unit: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</span>
        <span className="font-mono text-[11px] font-semibold text-foreground">{value}</span>
      </span>
      <input
        type="range"
        min={bounds.min}
        max={bounds.max}
        step={bounds.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={label}
        aria-valuetext={`${value} ${unit}`}
        className="how-range w-full accent-[var(--brand)]"
      />
    </label>
  );
}
