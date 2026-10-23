import { cn } from '@databricks/appkit-ui/react';
import {
  SPINE,
  LIFECYCLE_ORDER,
  dotLeftPercent,
  ribbonFill,
  viewForStep,
  type Stage,
} from '../estateMap.pure';
import { ACTOR_META, ACTOR_ORDER } from './estateActors';

/**
 * EstateMap — the pinned bird's-eye graphic on the landing. It is driven entirely by
 * `viewForStep(activeStep)` (pure) and re-renders as the reader scrolls the narrative: the
 * responsible actor lane lights, a table-dot travels the flow spine, the freeze bracket
 * clamps around convert, the fails-closed gate opens once its blockers drop, and the state
 * ribbon fills. It is decorative for assistive tech (`aria-hidden`) — the scrolling narrative
 * text is the accessible source of truth. All motion is transform/opacity (see how.css).
 */

const STAGE_META: Record<Stage, { num: string; label: string; mutates?: boolean }> = {
  discover: { num: '00', label: 'Discover' },
  plan: { num: '02', label: 'Plan' },
  prepare: { num: '03', label: 'Prepare' },
  convert: { num: '04', label: 'Convert', mutates: true },
  validate: { num: '05', label: 'Validate' },
  cleanup: { num: '07', label: 'Clean up' },
};

const FREEZE_OPS = ['STOP_STREAM', 'PAUSE_JOB', 'DISABLE_PO'];
const LEASE_OPS = ['GRANT', 'REVOKE'];

const STATE_SHORT: Record<(typeof LIFECYCLE_ORDER)[number], string> = {
  DISCOVERED: 'Discovered',
  DRY_RUN_OK: 'Dry-run OK',
  CONVERTING: 'Converting',
  CONVERTED: 'Converted',
  VALIDATED: 'Validated',
  CLEANED: 'Cleaned',
};

export function EstateMap({ activeStep }: { activeStep: number }) {
  const view = viewForStep(activeStep);
  const activeLanes = new Set(view.activeLanes);

  return (
    <div
      aria-hidden
      className="flex flex-col gap-5 rounded-lg bg-[var(--surface-deep)] p-4 text-[var(--fg-on-dark)]"
    >
      {/* -- Actor swimlanes ------------------------------------------------------------ */}
      <div className="flex flex-col gap-1.5">
        <Caption>Who drives this step</Caption>
        <div className="grid grid-cols-2 gap-1.5">
          {ACTOR_ORDER.map((a) => {
            const meta = ACTOR_META[a];
            const on = activeLanes.has(a);
            return (
              <div
                key={a}
                data-active={on}
                className={cn(
                  'how-lane flex items-center gap-2 rounded-md border px-2.5 py-1.5',
                  on ? 'border-[var(--border-on-dark)] bg-white/5' : 'border-transparent',
                )}
              >
                <span className={cn('size-2 shrink-0 rounded-full', meta.dot)} />
                <span className="flex min-w-0 flex-col leading-tight">
                  <span className="truncate text-[11px] font-semibold text-[var(--fg-on-dark)]">{meta.label}</span>
                  <span className="truncate text-[10px] text-[var(--fg-on-dark-2)]">{meta.verb}</span>
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* -- Flow spine + travelling dot + freeze/Authorize brackets -------------------- */}
      <div className="flex flex-col gap-2">
        <Caption>The flow — a table travels the spine</Caption>
        <div className="relative pt-11">
          {/* Authorize bracket — the WIDER, outer hold (03 -> 07); its grants are revoked only at cleanup */}
          <div
            data-state={view.leaseBracket}
            className="how-bracket how-bracket--lease absolute top-0 flex h-4 items-center justify-center rounded-md border border-[var(--brand)]/40 bg-[var(--brand)]/10"
            style={{ left: '34%', width: '59%' }}
          >
            <span className="text-[8px] font-semibold uppercase tracking-wide text-[var(--brand)]">
              Authorize
            </span>
          </div>

          {/* freeze bracket — nested inner, wraps convert; released at validate */}
          <div
            data-state={view.bracket}
            className="how-bracket absolute top-5 flex h-4 items-center justify-center rounded-md border border-[var(--info-border)] bg-[var(--info)]/15"
            style={{ left: '41%', width: '34%' }}
          >
            <span className="text-[8px] font-semibold uppercase tracking-wide text-[var(--info)]">
              Freeze
            </span>
          </div>

          {/* travelling dot on a track above the nodes */}
          <div className="relative mb-2 h-2">
            <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-[var(--border-on-dark)]" />
            <span
              className="how-dot absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--brand)] shadow-[0_0_0_3px_rgba(255,54,33,0.25)]"
              style={{ ['--dot-left' as string]: `${dotLeftPercent(view.activeStage)}%` }}
            />
          </div>

          {/* nodes */}
          <ol className="flex items-start justify-between gap-1">
            {SPINE.map((stage) => {
              const meta = STAGE_META[stage];
              const on = view.activeStage === stage;
              return (
                <li key={stage} className="flex flex-1 flex-col items-center gap-1 text-center">
                  <span
                    data-active={on}
                    className={cn(
                      'how-spine-node flex size-8 items-center justify-center rounded-full border text-[10px] font-semibold',
                      on
                        ? meta.mutates
                          ? 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]'
                          : 'border-[var(--fg-on-dark)] bg-white/10 text-[var(--fg-on-dark)]'
                        : 'border-[var(--border-on-dark)] text-[var(--fg-on-dark-2)]',
                    )}
                  >
                    {meta.num}
                  </span>
                  <span
                    className={cn(
                      'text-[9px] leading-tight',
                      on ? 'text-[var(--fg-on-dark)]' : 'text-[var(--fg-on-dark-2)]',
                    )}
                  >
                    {meta.label}
                  </span>
                </li>
              );
            })}
          </ol>
        </div>

        {/* freeze + Authorize op chips that snap in while their bracket is clamped */}
        <div className="flex flex-wrap gap-1.5">
          {FREEZE_OPS.map((op) => (
            <code
              key={op}
              data-on={view.bracket === 'clamped'}
              className="how-freeze-chip rounded bg-white/10 px-1.5 py-0.5 font-mono text-[9px] text-[var(--fg-on-dark-2)]"
            >
              {op}
            </code>
          ))}
          {LEASE_OPS.map((op) => (
            <code
              key={op}
              data-on={view.leaseBracket === 'clamped'}
              className="how-freeze-chip rounded bg-[var(--brand)]/15 px-1.5 py-0.5 font-mono text-[9px] text-[var(--fg-on-dark-2)]"
            >
              {op}
            </code>
          ))}
        </div>
      </div>

      {/* -- Fails-closed gate ---------------------------------------------------------- */}
      <div className="flex flex-col gap-1.5">
        <Caption>The one gate — fails closed</Caption>
        <div
          data-state={view.gate}
          className={cn(
            'how-gate flex items-center justify-between gap-3 rounded-md border px-3 py-2',
            view.gate === 'open'
              ? 'border-[var(--success-border)] bg-[var(--success)]/10'
              : view.gate === 'blocked'
                ? 'border-[var(--destructive-border)] bg-[var(--destructive)]/10'
                : 'border-[var(--border-on-dark)]',
          )}
        >
          <span className="text-[11px] font-medium text-[var(--fg-on-dark)]">
            {view.gate === 'open' ? 'SET MANAGED — gate open' : view.gate === 'blocked' ? 'Blocked — gate held shut' : 'Gate idle'}
          </span>
          <span className="flex items-center gap-1">
            {[0, 1, 2].map((n) => (
              <span
                key={n}
                className="how-blocker h-3 w-1.5 rounded-full bg-[var(--destructive)]"
              />
            ))}
          </span>
        </div>
      </div>

      {/* -- Lifecycle state ribbon ----------------------------------------------------- */}
      <div className="flex flex-col gap-1.5">
        <Caption>Lifecycle state</Caption>
        <div className="relative h-2 overflow-hidden rounded-full bg-[var(--surface-deep-2)]">
          <div
            className="how-ribbon-fill absolute inset-y-0 left-0 w-full rounded-full bg-gradient-to-r from-[var(--brand)] to-[var(--success)]"
            style={{ ['--fill' as string]: ribbonFill(view.ribbonReached) }}
          />
        </div>
        <div className="flex justify-between">
          {LIFECYCLE_ORDER.map((s) => (
            <span
              key={s}
              className={cn(
                'text-[8px]',
                s === view.ribbonReached ? 'font-semibold text-[var(--fg-on-dark)]' : 'text-[var(--label-on-dark)]',
              )}
            >
              {STATE_SHORT[s]}
            </span>
          ))}
        </div>
        <div
          data-show={view.showRollbackBranch}
          className="how-rollback mt-1 flex items-center gap-1.5 text-[9px] text-[var(--fg-on-dark-2)]"
        >
          <span className="h-px w-4 bg-[var(--border-on-dark)]" />
          Rollback available — 14-day window
        </div>
      </div>
    </div>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[8px] font-semibold uppercase tracking-[0.14em] text-[var(--label-on-dark)]">
      {children}
    </span>
  );
}
