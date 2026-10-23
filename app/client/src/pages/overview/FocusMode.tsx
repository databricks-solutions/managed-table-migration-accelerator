import { useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
import {
  Badge,
  Button,
  Card,
  CardContent,
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@databricks/appkit-ui/react';
import type { QueryRegistry } from '@databricks/appkit-ui/react';
import { ArrowLeft, Check, ChevronsUpDown, CircleCheck, CircleDashed, Layers, Pin, TriangleAlert } from 'lucide-react';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { FocusModeContext } from './focusModeContext';
import { FocusPinControl } from './TeamPins';
import { RederiveAction } from '../../components/RederiveAction';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { ACTION_COPY, FOCUS_COPY, SECTION_COPY, fillCopy, stepBadge } from '../../lib/sectionCopy';
import {
  driftNotes,
  focusHref,
  heldNotes,
  num,
  otherSchemaFinishing,
  pickerSections,
  scopeLabelParts,
  statusLine,
  wavePosition,
  waveStrip,
  type EstatePickLike,
} from '../../lib/focus.pure';

export type ScopedAction = QueryRegistry['migration_next_actions_by_scope']['result'][number];

// --------------------------------------------------------------------------- context

/** Marks the guided card's subtree as focused on `focus` (see focusModeContext). */
export function FocusModeProvider({
  focus,
  status,
  children,
}: {
  focus: string;
  status?: string | null;
  children: ReactNode;
}) {
  return <FocusModeContext.Provider value={{ focus, status }}>{children}</FocusModeContext.Provider>;
}

// --------------------------------------------------------------------------- picker

/** "Work on another schema ▾" — a searchable list of the plan's scopes, grouped by where each
 *  stands, team-pinned scopes first. Picking one opens that scope's own guided step. */
export function FocusPicker({
  rows,
  current,
  onDark = false,
}: {
  rows: readonly ScopedAction[];
  current: string | null;
  onDark?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const sections = pickerSections(rows);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          data-tour="focus-picker"
          disabled={rows.length === 0}
          className={
            onDark
              ? 'w-fit gap-2 border-white/20 bg-transparent text-[var(--fg-on-dark)] hover:bg-white/10 hover:text-[var(--fg-on-dark)]'
              : 'w-fit gap-2'
          }
        >
          <Layers className="size-4" aria-hidden />
          {current ?? FOCUS_COPY.workOnAnother}
          <ChevronsUpDown className="size-4 opacity-60" aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[32rem] p-0" align="start">
        <Command>
          <CommandInput placeholder="Search by schema or catalog…" />
          <CommandList>
            <CommandEmpty>No matching schema.</CommandEmpty>
            {sections.map((s) => (
              <CommandGroup key={s.group} heading={s.label}>
                {s.rows.map((r) => (
                  <CommandItem
                    key={r.scope_key}
                    value={r.scope_key}
                    onSelect={() => {
                      setOpen(false);
                      void navigate(focusHref(r.scope_key));
                    }}
                  >
                    <Check className={`mr-2 size-4 shrink-0 ${r.scope_key === current ? 'opacity-100' : 'opacity-0'}`} />
                    <div className="flex min-w-0 flex-1 items-center gap-2">
                      {/* Schema first: rows usually share a long catalog name, which would
                          truncate every label to the same prefix. */}
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate font-mono text-xs font-medium">
                          {scopeLabelParts(r.scope_key).primary}
                        </span>
                        {scopeLabelParts(r.scope_key).secondary ? (
                          <span className="truncate font-mono text-[10px] text-muted-foreground">
                            {scopeLabelParts(r.scope_key).secondary}
                          </span>
                        ) : null}
                      </span>
                      {num(r.pin_rank) > 0 ? (
                        <Badge variant="outline" className="shrink-0 gap-1 text-[10px]">
                          <Pin className="size-3" aria-hidden />
                          {num(r.pin_rank)}
                        </Badge>
                      ) : null}
                      <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                        {stepBadge(r.next_step_kind)} · {wavePosition(r)}
                      </span>
                    </div>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

// --------------------------------------------------------------------------- focused header

/**
 * The focused schema's frame around the (unchanged) guided card: the schema, the way back to the
 * recommended step, what everyone else is recommended, the schema's waves in order, and any work
 * that is handled elsewhere or needs a re-plan. Read-only — every action stays in the card.
 */
export function FocusHeader({
  row,
  rows,
  estate,
  focus,
}: {
  row: ScopedAction;
  rows: readonly ScopedAction[];
  estate: EstatePickLike | null;
  focus: string;
}) {
  const planQ = useMigrationQuery('wave_plan_status', {});
  const strip = waveStrip(planQ.data ?? [], focus, row.next_wave_id || null);
  const held = heldNotes(row);
  const drift = driftNotes(row);
  const status = statusLine(row);
  const warning = otherSchemaFinishing(estate, focus);
  const [cat, sch] = splitScope(focus);
  return (
    <Card data-tour="focus-header">
      <SectionHeader
        copy={SECTION_COPY.overview.focusHeader}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <FocusPicker rows={rows} current={focus} />
            <Button asChild variant="ghost" size="sm" className="gap-1">
              <Link to={focusHref(null)}>
                <ArrowLeft className="size-4" aria-hidden />
                {FOCUS_COPY.backToEstate}
              </Link>
            </Button>
          </div>
        }
      />
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="font-mono font-semibold">{focus}</span>
          {num(row.pin_rank) > 0 ? (
            <Badge variant="outline" className="gap-1">
              <Pin className="size-3" aria-hidden />
              {num(row.pin_rank)}
            </Badge>
          ) : null}
          <span className="text-muted-foreground">{wavePosition(row, strip.length)}</span>
          {estate?.next_group_key && estate.next_group_key !== focus ? (
            <span className="text-xs text-muted-foreground">
              {fillCopy(FOCUS_COPY.estatePick, { scope: estate.next_group_key })}
            </span>
          ) : null}
        </div>

        <FocusPinControl scope={focus} />

        {strip.length > 0 ? (
          <div className="flex flex-col gap-2">
            <SectionHeader copy={SECTION_COPY.overview.focusWaves} variant="label" />
            <ol className="flex flex-wrap items-center gap-2" aria-label={SECTION_COPY.overview.focusWaves.title}>
              {strip.map((w, i) => (
                <li key={w.waveId} className="flex items-center gap-2">
                  {i > 0 ? <span className="h-px w-4 bg-border" aria-hidden /> : null}
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${
                      w.state === 'current' ? 'border-[var(--brand)] bg-[var(--brand)]/10 font-medium' : ''
                    }`}
                  >
                    {w.state === 'done' ? (
                      <CircleCheck className="size-3.5 text-[var(--success-foreground)]" aria-hidden />
                    ) : (
                      <CircleDashed className="size-3.5 opacity-60" aria-hidden />
                    )}
                    <span className="font-mono">{w.waveId}</span>
                    <span className="text-muted-foreground">
                      {w.bucket} · {w.tables} {w.tables === 1 ? 'table' : 'tables'} ·{' '}
                      {w.state === 'current' ? FOCUS_COPY.waveCurrent : w.state === 'done' ? FOCUS_COPY.waveDone : FOCUS_COPY.waveLater}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          </div>
        ) : null}

        {status ? <p className="text-sm text-muted-foreground">{status}</p> : null}

        {warning ? (
          <div className="flex items-start gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-sm text-[var(--warning-foreground)]">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>{warning}</span>
          </div>
        ) : null}

        {drift.length > 0 ? (
          <div className="flex flex-col gap-2 rounded-lg border border-[var(--info-border)] bg-[var(--info-bg)] px-3 py-2 text-sm text-[var(--info-foreground)]">
            {drift.map((d) => (
              <span key={d}>{d}</span>
            ))}
            <div className="flex flex-wrap gap-2">
              <RederiveAction
                alias="profiler"
                copy={ACTION_COPY.reprofileSchema}
                scopeEditor={{ defaultCatalog: cat, defaultSchema: sch, requireScope: true }}
              />
              <Button asChild variant="outline" size="sm">
                <Link to="/waves">{stepBadge('replan')}</Link>
              </Button>
            </div>
          </div>
        ) : null}

        {held.length > 0 ? (
          <div className="flex flex-col gap-1">
            <SectionHeader copy={SECTION_COPY.overview.focusHeld} variant="label" />
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {held.map((h) => (
                <li key={h.key}>
                  {h.href ? (
                    <Link to={h.href} className="underline underline-offset-2">
                      {h.text}
                    </Link>
                  ) : (
                    h.text
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <p className="text-[10px] text-muted-foreground">
          {fillCopy(FOCUS_COPY.freshness, { time: fmtTime(row.as_of) })}
        </p>
      </CardContent>
    </Card>
  );
}

/** The focused scope is not in the latest per-scope batch (not in the plan, or no batch yet). */
export function FocusMissing({ rows, focus }: { rows: readonly ScopedAction[]; focus: string }) {
  return (
    <Card data-tour="focus-header">
      <SectionHeader
        copy={SECTION_COPY.overview.focusMissing}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <FocusPicker rows={rows} current={null} />
            <Button asChild variant="ghost" size="sm" className="gap-1">
              <Link to={focusHref(null)}>
                <ArrowLeft className="size-4" aria-hidden />
                {FOCUS_COPY.backToEstate}
              </Link>
            </Button>
          </div>
        }
      />
      <CardContent className="flex flex-col gap-2 text-sm text-muted-foreground">
        <span className="font-mono">{focus}</span>
        {rows.length === 0 ? <span>{FOCUS_COPY.noScopedRows}</span> : null}
      </CardContent>
    </Card>
  );
}

/** The estate view's entry point into focus mode, beside the schema list ("By schema"): the picker
 *  plus a hint that says what it does. */
export function FocusEntry({ rows }: { rows: readonly ScopedAction[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <FocusPicker rows={rows} current={null} />
      <InfoHint
        label={SECTION_COPY.overview.focusPicker.title}
        text={SECTION_COPY.overview.focusPicker.subtitle}
        className="text-muted-foreground"
      />
    </div>
  );
}

function splitScope(scope: string): [string, string] {
  const i = scope.indexOf('.');
  return i > 0 ? [scope.slice(0, i), scope.slice(i + 1)] : [scope, ''];
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}
