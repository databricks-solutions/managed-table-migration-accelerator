import { ArrowRight, HardDrive, MoveRight, TriangleAlert } from 'lucide-react';
import { locationPlanViews, type LocationPlanView, type SchemaLocationRow } from '../lib/locationPlan.pure';
import { SECTION_COPY } from '../lib/sectionCopy';

/**
 * LocationPlanPanel — the honest, scope-level "where your data lives now → where convert
 * moves it" surface. Source root comes from the profiler (`table_manifest.storage_location`),
 * the managed root from the dry-run (`location_map`, Check 12). Everything shown is a ROOT,
 * never a per-table path (UC assigns those at convert time). The four managed-status states
 * mirror `locations.effective_managed_location` so this can never disagree with the convert
 * hard-gate; `NOT_EVALUATED` is the pre-dry-run "the dry-run resolves this" state.
 *
 * `bare` embeds the rows inside an existing box (ConvertGateDialog's "What happens to your
 * data"); the default wraps them in a titled panel (Waves detail / Overview prep look-ahead).
 * Renders nothing when there are no rows (never a fabricated placeholder).
 */
/** Token set for the two surfaces this panel appears on: the light dialog/board and the dark
 *  Overview recommendation card. Kept here (not props everywhere) so a row can never mix. */
interface Palette {
  strong: string;
  muted: string;
  border: string;
}
const LIGHT: Palette = { strong: 'text-foreground', muted: 'text-muted-foreground', border: 'border-border' };
const DARK: Palette = {
  strong: 'text-[var(--fg-on-dark)]',
  muted: 'text-[var(--label-on-dark)]',
  border: 'border-white/10',
};

export function LocationPlanPanel({
  rows,
  title = SECTION_COPY.overview.whereDataMoves.title,
  bare = false,
  onDark = false,
}: {
  rows: ReadonlyArray<SchemaLocationRow>;
  title?: string;
  bare?: boolean;
  /** Render with on-dark tokens (the Overview recommendation card). Default = light surface. */
  onDark?: boolean;
}) {
  const views = locationPlanViews(rows);
  if (views.length === 0) return null;
  const pal = onDark ? DARK : LIGHT;

  const body = (
    <div className="flex flex-col gap-2.5">
      {views.map((v) => (
        <LocationRow key={v.scopeLabel} v={v} pal={pal} />
      ))}
    </div>
  );

  if (bare) return body;

  return (
    <div className={`rounded-lg border ${pal.border} p-3`}>
      <div className={`flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] ${pal.muted}`}>
        <HardDrive className="size-3.5" aria-hidden />
        {title}
      </div>
      <div className="mt-2">{body}</div>
    </div>
  );
}

function LocationRow({ v, pal }: { v: LocationPlanView; pal: Palette }) {
  const warn = v.tone === 'warning';
  return (
    <div className={warn ? 'rounded-md border border-[var(--warning-border)] bg-[var(--warning-bg)] p-2' : ''}>
      <div className="flex items-center justify-between gap-2">
        <span className={`font-mono text-[11px] font-semibold ${pal.strong}`}>{v.scopeLabel}</span>
        <span className={`text-[10px] tabular-nums ${pal.muted}`}>
          {v.tableCount} table{v.tableCount === 1 ? '' : 's'}
        </span>
      </div>

      {/* From (source root) → To (managed root or status phrase) */}
      <div className="mt-1 flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-2">
        <div className="min-w-0 flex-1">
          <div className={`text-[9px] font-semibold uppercase tracking-[0.08em] ${pal.muted}`}>From</div>
          <div className={`break-all font-mono text-[11px] ${pal.strong}`}>{v.sourceLabel}</div>
        </div>
        <MoveRight className={`mt-3 hidden size-3.5 shrink-0 sm:block ${pal.muted}`} aria-hidden />
        <ArrowRight className={`mt-0.5 size-3.5 shrink-0 sm:hidden ${pal.muted}`} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className={`text-[9px] font-semibold uppercase tracking-[0.08em] ${pal.muted}`}>To (managed)</div>
          {v.managedRoot ? (
            <div className={`break-all font-mono text-[11px] ${pal.strong}`}>
              {v.managedRoot}
              {v.managedScopeLabel ? (
                <span className={`ml-1 font-sans text-[10px] ${pal.muted}`}>({v.managedScopeLabel})</span>
              ) : null}
            </div>
          ) : (
            <div
              className={
                warn
                  ? 'flex items-center gap-1 text-[11px] font-medium text-[var(--warning-foreground)]'
                  : `text-[11px] italic ${pal.muted}`
              }
            >
              {warn ? <TriangleAlert className="size-3" aria-hidden /> : null}
              {v.managedStatus === 'NOT_EVALUATED'
                ? 'resolved by the dry-run'
                : v.managedStatus === 'NOT_CONFIGURED'
                  ? 'not configured'
                  : 'could not be resolved'}
            </div>
          )}
        </div>
      </div>

      <p className={warn ? 'mt-1 text-[11px] text-[var(--warning-foreground)]' : `mt-1 text-[11px] ${pal.muted}`}>
        {v.copy}
      </p>
    </div>
  );
}
