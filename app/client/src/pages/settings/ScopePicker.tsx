import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Spinner, cn } from '@databricks/appkit-ui/react';
import { ChevronRight, CircleCheck, RefreshCw, X } from 'lucide-react';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY } from '../../lib/sectionCopy';
import {
  MAX_SELECTED_SCOPES,
  capacityLeft,
  catalogTickState,
  coverageText,
  discoveryState,
  filterScopes,
  groupScopesByCatalog,
  scopeKey,
  selectableKeys,
  wouldPassLimit,
  type MergedScope,
  type OboDiscoveryLite,
  type ScopeCatalogGroup,
  type ScopeView,
} from './grantsPanel.pure';

/** Picker catalog rows start collapsed once more schemas than this are listed (unless filtering). */
const COLLAPSE_SCHEMAS_OVER = 40;
const LIMIT_NOTE_ID = 'scope-picker-limit-note';

const count = (n: number) => n.toLocaleString();
const plural = (n: number, word: string) => (n === 1 ? word : `${word}s`);

/**
 * The schemas with external tables the signed-in admin can see (their visibility, never "the
 * estate"), grouped by catalog. Ticking one adds it to the Permissions panel's plan; a schema
 * already in the stored scopes is shown as covered, never as a tick.
 */
export function ScopePicker({
  obo,
  scopes,
  selected,
  onToggle,
  onToggleCatalog,
  onSelectAll,
  onClear,
  onRefresh,
}: {
  obo: OboDiscoveryLite | null;
  scopes: MergedScope[];
  selected: readonly string[];
  onToggle: (key: string) => void;
  onToggleCatalog: (group: ScopeCatalogGroup) => void;
  onSelectAll: (keys: string[]) => void;
  onClear: () => void;
  onRefresh: () => void;
}) {
  const [query, setQuery] = useState('');
  const [view, setView] = useState<ScopeView>('open');
  const [flipped, setFlipped] = useState<ReadonlySet<string>>(new Set());
  const state = discoveryState(obo);
  const coveredCount = scopes.filter((s) => s.covered).length;
  const pickedCount = scopes.filter((s) => s.selected && !s.covered).length;
  const filtered = useMemo(() => filterScopes(scopes, query, view), [scopes, query, view]);
  const catalogs = useMemo(() => groupScopesByCatalog(filtered), [filtered]);
  const visibleKeys = selectableKeys(filtered);
  const selectAllBlocked = wouldPassLimit(selected, visibleKeys);
  const isBlocked = (g: ScopeCatalogGroup) => catalogTickState(g) !== 'all' && wouldPassLimit(selected, g.selectable);
  const anyCatalogBlocked = catalogs.some(isBlocked);
  const openByDefault = query.trim() !== '' || filtered.length <= COLLAPSE_SCHEMAS_OVER;
  const isOpen = (catalog: string) => openByDefault !== flipped.has(catalog);
  const flip = (catalog: string) =>
    setFlipped((prev) => {
      const next = new Set(prev);
      if (next.has(catalog)) next.delete(catalog);
      else next.add(catalog);
      return next;
    });
  const viewCounts: Record<ScopeView, number> = {
    open: scopes.length - coveredCount,
    ticked: scopes.filter((s) => s.selected).length,
    all: scopes.length,
  };
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader
        copy={SECTION_COPY.settings.permissionsPicker}
        variant="inline"
        actions={
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={state === 'loading'}>
            <RefreshCw className="size-3.5" aria-hidden /> Refresh
          </Button>
        }
      />
      <div aria-live="polite" className="text-xs text-muted-foreground">
        {state === 'loading' ? (
          <p className="flex items-center gap-2">
            <Spinner className="size-3.5" aria-hidden /> Checking what you can see…
          </p>
        ) : state === 'unavailable' ? (
          <p>Estate discovery unavailable ({obo?.reason ?? 'unknown'}). Type the schemas below instead.</p>
        ) : state === 'empty' ? (
          <p>
            Your account can’t see any external tables. Type a schema below, or ask a catalog owner or
            metastore admin for access.
          </p>
        ) : state === 'partial' ? (
          <p>
            External tables you can see, from a partial scan. {coverageText(obo?.coverage)} Type a schema
            below if yours is missing, or refresh to scan again.
          </p>
        ) : (
          <p>
            External tables <strong>you can currently see</strong>. This is your view, not the whole
            estate.
          </p>
        )}
        {coveredCount > 0 ? (
          <p className="mt-1">
            {coveredCount === scopes.length
              ? 'Every schema here is already in the jobs’ access.'
              : `${coveredCount} of these ${coveredCount === 1 ? 'is' : 'are'} already in the jobs’ access.`}
          </p>
        ) : null}
      </div>
      {scopes.length > 0 ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="w-64 rounded-md border border-border bg-background px-2 py-1 font-mono text-[12px]"
              placeholder="Filter by catalog or schema"
              aria-label="Filter schemas"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <ViewToggle
              label="Which schemas to list"
              value={view}
              onChange={setView}
              options={[
                { value: 'open', label: `Not in scope · ${count(viewCounts.open)}` },
                { value: 'ticked', label: `Ticked · ${count(viewCounts.ticked)}` },
                { value: 'all', label: `All · ${count(viewCounts.all)}` },
              ]}
            />
          </div>
          <div role="region" aria-label="Schemas to pick" className="max-h-72 overflow-y-auto rounded-md border border-border">
            {catalogs.length === 0 ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                {query.trim()
                  ? 'No schema matches the filter.'
                  : view === 'ticked'
                    ? 'Nothing ticked yet.'
                    : 'Every schema here is already in the scope. Pick All to see them.'}
              </p>
            ) : (
              catalogs.map((c) => (
                <CatalogPickRow
                  key={c.catalog}
                  group={c}
                  open={isOpen(c.catalog)}
                  blocked={isBlocked(c)}
                  onFlip={() => flip(c.catalog)}
                  onTickAll={() => onToggleCatalog(c)}
                  onToggle={onToggle}
                />
              ))
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onSelectAll(visibleKeys)}
              disabled={visibleKeys.length === 0 || selectAllBlocked}
            >
              Select all
            </Button>
            <Button variant="ghost" size="sm" onClick={onClear} disabled={pickedCount === 0}>
              Clear
            </Button>
            <span>
              {pickedCount} selected · {capacityLeft(selected)} of {MAX_SELECTED_SCOPES} left
            </span>
          </div>
          {(visibleKeys.length > 0 && selectAllBlocked) || anyCatalogBlocked ? (
            <p id={LIMIT_NOTE_ID} className="text-xs text-muted-foreground">
              You can pick up to {MAX_SELECTED_SCOPES} schemas at a time, so Select all and a catalog’s
              tick box turn off when they would pass that. Narrow the filter or tick schemas one by one.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function CatalogPickRow({
  group,
  open,
  blocked,
  onFlip,
  onTickAll,
  onToggle,
}: {
  group: ScopeCatalogGroup;
  open: boolean;
  blocked: boolean;
  onFlip: () => void;
  onTickAll: () => void;
  onToggle: (key: string) => void;
}) {
  const tick = catalogTickState(group);
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (box.current) box.current.indeterminate = tick === 'some';
  }, [tick]);
  return (
    <div className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <input
          ref={box}
          type="checkbox"
          aria-label={`Tick every schema in ${group.catalog}`}
          aria-describedby={blocked ? LIMIT_NOTE_ID : undefined}
          checked={tick === 'all'}
          disabled={group.selectable.length === 0 || blocked}
          onChange={onTickAll}
        />
        <button
          type="button"
          aria-expanded={open}
          onClick={onFlip}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-xs"
        >
          <ChevronRight className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} aria-hidden />
          <span className="truncate font-mono text-foreground">{group.catalog}</span>
          <span className="shrink-0 text-muted-foreground">
            {count(group.scopes.length)} {plural(group.scopes.length, 'schema')}
            {group.ticked > 0 ? ` · ${count(group.ticked)} ticked` : ''}
            {group.covered > 0 ? ` · ${count(group.covered)} in scope` : ''}
          </span>
        </button>
      </div>
      {open ? (
        <div className="flex flex-wrap gap-1.5 px-3 pb-2 pl-9">
          {group.scopes.map((s) => (
            <ScopeChip key={scopeKey(s)} scope={s} onToggle={onToggle} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ScopeChip({ scope, onToggle }: { scope: MergedScope; onToggle: (key: string) => void }) {
  const key = scopeKey(scope);
  if (scope.covered) {
    return (
      <span
        aria-label={`${key}, already in the jobs’ access`}
        className="flex items-center gap-1 rounded-md border border-[var(--success-border)] bg-[var(--success-bg)] px-2 py-0.5 font-mono text-[11px] text-[var(--success-foreground)]"
      >
        <CircleCheck className="size-3" aria-hidden />
        {key}
        {scope.externalTableCount !== null ? <span>· {scope.externalTableCount}</span> : null}
        <span className="font-sans">· in scope</span>
      </span>
    );
  }
  return (
    <span
      className={cn(
        'flex items-center rounded-md border font-mono text-[11px]',
        scope.selected
          ? 'border-[var(--brand)] bg-[var(--brand)]/10 text-[var(--brand)]'
          : 'border-border bg-card text-muted-foreground',
      )}
    >
      <button
        type="button"
        aria-pressed={scope.selected}
        onClick={() => onToggle(key)}
        className="flex items-center gap-1 px-2 py-0.5 hover:text-foreground"
      >
        {scope.selected ? <CircleCheck className="size-3" aria-hidden /> : null}
        {key}
        {scope.externalTableCount !== null ? (
          <span className="text-muted-foreground">· {scope.externalTableCount}</span>
        ) : null}
        {scope.source === 'manual' ? <span className="text-muted-foreground">(typed)</span> : null}
      </button>
      {scope.source === 'manual' ? (
        <button
          type="button"
          aria-label={`Remove ${key}`}
          onClick={() => onToggle(key)}
          className="border-l border-border px-1 py-0.5 hover:text-foreground"
        >
          <X className="size-3" aria-hidden />
        </button>
      ) : null}
    </span>
  );
}

function ViewToggle<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-md border border-border p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'rounded px-2 py-0.5 text-xs',
            value === o.value ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
