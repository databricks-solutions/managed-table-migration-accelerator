import { cn } from '@databricks/appkit-ui/react';
import { CircleCheck, Clock, TriangleAlert } from 'lucide-react';
import { coverageForWave, formatAge, sourceLabel } from '../lib/coverage';
import { SectionHeader } from './SectionHeader';
import { SECTION_COPY } from '../lib/sectionCopy';

/** The gate rows shape `coverageForWave` reads (wave_gate_latest). Shared by the Writer-jobs
 *  cockpit and the estate Coverage page so writer stop-coverage renders identically in both. */
export type GateRows = Parameters<typeof coverageForWave>[0];

const WRITER_STOP_COVERAGE = 'WRITER_STOP_COVERAGE';

/**
 * Writer stop-coverage, per wave, straight from wave_gate_latest via coverageForWave. A
 * WRITER_STOP_COVERAGE row present + passed → held; present + failed → gap (with the gap count +
 * the byte-identical stop report). No row → nothing is claimed. The convert job re-checks and
 * fails closed at mutation time regardless. Reflect-only: derived from control tables; renders
 * nothing it cannot prove, and never a bare green.
 */
export function WriterCoveragePanel({ gateRows }: { gateRows: GateRows }) {
  const waveIds = [
    ...new Set(
      gateRows.filter((r) => r.gate_kind === WRITER_STOP_COVERAGE && r.wave_id).map((r) => r.wave_id as string),
    ),
  ];
  if (waveIds.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 p-3">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          <Clock className="size-3.5" aria-hidden />
          {SECTION_COPY.shared.writerCoverageNone.title}
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">{SECTION_COPY.shared.writerCoverageNone.subtitle}</p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader copy={SECTION_COPY.shared.writerCoverage} variant="label" className="text-[10px] tracking-[0.1em]" />
      <div className="flex flex-col gap-2">
        {waveIds.map((waveId) => {
          const c = coverageForWave(gateRows, waveId);
          const at = c.writerAt ?? null;
          const src = sourceLabel(c.writerSource);
          return (
            <div
              key={waveId}
              className={cn(
                'rounded-lg border p-3 text-xs',
                c.writer === 'covered'
                  ? 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]'
                  : c.writer === 'gap'
                    ? 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]'
                    : 'border-border bg-muted/40 text-muted-foreground',
              )}
            >
              <div className="flex items-center gap-2 font-semibold">
                {c.writer === 'covered' ? (
                  <CircleCheck className="size-4" aria-hidden />
                ) : c.writer === 'gap' ? (
                  <TriangleAlert className="size-4" aria-hidden />
                ) : (
                  <Clock className="size-4" aria-hidden />
                )}
                <span className="font-mono">{waveId}</span>
                <span>
                  {c.writer === 'covered'
                    ? 'writers stopped'
                    : c.writer === 'gap'
                      ? `${c.writerGaps ?? '?'} writer${c.writerGaps === 1 ? '' : 's'} uncovered`
                      : 'not evaluated'}
                </span>
              </div>
              {c.writer === 'gap' && c.writerReason ? (
                <p className="mt-1 font-mono text-[11px] opacity-90">{c.writerReason}</p>
              ) : null}
              {at || c.writerSource ? (
                <p className="mt-1 opacity-90">
                  Evaluated {formatAge(at)}, per {src}.
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
