import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  Spinner,
  cn,
} from '@databricks/appkit-ui/react';
import { ChevronRight, CircleCheck, CircleX, Download, Copy, RefreshCw, X } from 'lucide-react';
import { Link } from 'react-router';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, type SectionCopy } from '../../lib/sectionCopy';
import { grantHint } from '../../lib/grantHints.pure';
import {
  MAX_SELECTED_SCOPES,
  addTyped,
  capacityLeft,
  catalogSql,
  catalogTickState,
  clearSelection,
  copyBlock,
  coverageText,
  discoveryState,
  filterScopes,
  groupScopesByCatalog,
  groupTier2ByCatalog,
  loadSelection,
  mergeScopes,
  parseTypedScope,
  planUrl,
  presentTone,
  removeTyped,
  saveSelection,
  scanCoveredKeys,
  scopeKey,
  selectAll,
  selectableKeys,
  tier2Sql,
  tier2Summary,
  toggleCatalog,
  toggleScope,
  visibleTier2,
  withRefresh,
  wouldPassLimit,
  type CatalogGrants,
  type MergedScope,
  type OboDiscoveryLite,
  type ScopeCatalogGroup,
  type ScopeSelection,
  type ScopeView,
  type Tier2View,
} from './grantsPanel.pure';

type Present = 'yes' | 'no' | 'unknown';
interface GrantStatus {
  privilege: string;
  securableType: string;
  securableFqn: string;
  stmt: string;
  present: Present;
}
interface GrantsPlan {
  sp_client_id: string | null;
  jobs_identity: string | null;
  jobs_identity_mixed: boolean;
  control: { catalog: string; schema: string };
  tier1: GrantStatus[];
  tier2: { catalog: string; schema: string; source: 'scan' | 'selected'; grants: GrantStatus[] }[];
  authority_summary: {
    actor: string | null;
    total: number;
    authorized: number;
    self_authorizable: number;
    needs_grant: number;
    unknown: number;
    not_checked: number;
  } | null;
  targets_known: boolean;
}

/** Plan refetches wait this long after the last tick, so clicking through chips sends one request. */
const PLAN_DEBOUNCE_MS = 400;
/** Step 2 catalog groups start open only when there are this few of them. */
const OPEN_CATALOGS_UP_TO = 3;
/** Schemas shown per catalog group before "Show more". */
const SCHEMAS_PER_STEP = 10;
/** Picker catalog rows start collapsed once more schemas than this are listed (unless filtering). */
const COLLAPSE_SCHEMAS_OVER = 40;
const TIER2_SQL_FILENAME = 'app-sp-schema-grants.sql';
const LIMIT_NOTE_ID = 'grants-scope-limit-note';

function browserStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Settings → "Grants the App SP needs". The app GENERATES the grant SQL and VERIFIES what has
 * landed (SHOW GRANTS); it NEVER executes a GRANT or an ownership change (SR12 / §0.2). Honest states: the
 * target list is unknown before the profiler runs; the OBO list is the admin's visibility, never
 * "the estate". Ticked or typed scopes get Tier 2 SQL before the profiler runs (opt-in, never all).
 */
export function GrantsPanel() {
  const [selection, setSelection] = useState<ScopeSelection>(() => loadSelection(browserStorage()));
  const [plan, setPlan] = useState<GrantsPlan | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [obo, setObo] = useState<OboDiscoveryLite | null>(null);
  const [refreshCount, setRefreshCount] = useState(0);
  const [typedInput, setTypedInput] = useState('');
  const [typedError, setTypedError] = useState<string | null>(null);
  const firstPlanLoad = useRef(true);
  const [planRefresh, setPlanRefresh] = useState(0);
  const refreshPlanNext = useRef(false);

  useEffect(() => {
    saveSelection(browserStorage(), selection);
  }, [selection]);

  const url = useMemo(() => planUrl(selection), [selection]);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const delay = firstPlanLoad.current ? 0 : PLAN_DEBOUNCE_MS;
    firstPlanLoad.current = false;
    const target = refreshPlanNext.current ? withRefresh(url) : url;
    refreshPlanNext.current = false;
    setPlanBusy(true);
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const r = await fetch(target, { headers: { accept: 'application/json' }, signal: ctrl.signal });
          const data = (await r.json()) as GrantsPlan | { error: string };
          if (cancelled) return;
          if ('error' in data) {
            setPlanErr(data.error);
          } else {
            setPlan(data);
            setPlanErr(null);
          }
        } catch (err) {
          if (!cancelled) setPlanErr(err instanceof Error ? err.message : String(err));
        } finally {
          if (!cancelled) setPlanBusy(false);
        }
      })();
    }, delay);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [url, planRefresh]);

  useEffect(() => {
    let cancelled = false;
    setObo(null);
    const path = refreshCount > 0 ? '/api/estate/obo-discovery?refresh=1' : '/api/estate/obo-discovery';
    void (async () => {
      try {
        const r = await fetch(path, { headers: { accept: 'application/json' } });
        const data = (await r.json()) as OboDiscoveryLite;
        if (!cancelled) setObo(data);
      } catch {
        if (!cancelled) setObo({ available: false, reason: 'discovery request failed', visibility: [] });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshCount]);

  const covered = useMemo(() => (plan ? scanCoveredKeys(plan.tier2) : new Set<string>()), [plan]);
  const scopes: MergedScope[] = useMemo(
    () => mergeScopes(obo?.visibility ?? [], selection, covered),
    [obo, selection, covered],
  );

  function onAddTyped() {
    const parsed = parseTypedScope(typedInput);
    if (!parsed.ok) {
      setTypedError(parsed.reason);
      return;
    }
    setSelection((prev) => addTyped(prev, parsed.scope));
    setTypedInput('');
    setTypedError(null);
  }

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.settings.grants} />
      <CardContent className="flex flex-col gap-6">
        {planErr ? (
          <Alert variant="destructive">
            <CircleX className="size-4" aria-hidden />
            <AlertTitle>Could not build the grant plan</AlertTitle>
            <AlertDescription>{planErr}</AlertDescription>
          </Alert>
        ) : plan === null ? (
          <Alert>
            <Spinner className="size-4" aria-hidden />
            <AlertTitle>Building grant plan…</AlertTitle>
            <AlertDescription>Resolving the SP identity and checking landed grants.</AlertDescription>
          </Alert>
        ) : null}
        {plan ? (
          <>
            <SpLine spClientId={plan.sp_client_id} />
            <GrantGroup copy={SECTION_COPY.settings.grantsTier1} grants={plan.tier1} tier={1} />
          </>
        ) : null}
        <ScopePicker
          obo={obo}
          scopes={scopes}
          selection={selection}
          typedInput={typedInput}
          typedError={typedError}
          onTypedInput={(v) => {
            setTypedInput(v);
            if (typedError) setTypedError(null);
          }}
          onAddTyped={onAddTyped}
          onToggle={(key) => setSelection((prev) => toggleScope(prev, key))}
          onToggleCatalog={(group) => setSelection((prev) => toggleCatalog(prev, group))}
          onRemove={(key) => setSelection((prev) => removeTyped(prev, key))}
          onSelectAll={(keys) => setSelection((prev) => selectAll(prev, keys))}
          onClear={() => setSelection((prev) => clearSelection(prev))}
          onRefresh={() => {
            setRefreshCount((n) => n + 1);
            refreshPlanNext.current = true;
            setPlanRefresh((n) => n + 1);
          }}
        />
        {plan ? (
          <>
            <Tier2Section plan={plan} busy={planBusy} selection={selection} />
            <OwnershipSection plan={plan} />
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

function SpLine({ spClientId }: { spClientId: string | null }) {
  if (spClientId) {
    return (
      <p className="text-sm text-muted-foreground">
        App service principal: <span className="font-mono text-foreground">{spClientId}</span>
      </p>
    );
  }
  return (
    <Alert>
      <AlertTitle>Service principal id not resolved</AlertTitle>
      <AlertDescription>
        The SQL below uses <code>&lt;app-sp-client-id&gt;</code>. Find it in the app’s Databricks
        resource page (its service principal), or run <code>SELECT current_user()</code> as the
        app, and substitute it before running the grants.
      </AlertDescription>
    </Alert>
  );
}

function JobsIdentityLine({ plan }: { plan: GrantsPlan }) {
  if (plan.jobs_identity) {
    return (
      <p className="text-sm text-muted-foreground">
        These grants are for the identity the jobs run as:{' '}
        <span className="font-mono text-foreground">{plan.jobs_identity}</span>.
      </p>
    );
  }
  return (
    <Alert>
      <AlertTitle>{plan.jobs_identity_mixed ? 'The jobs run as different identities' : 'Jobs’ identity not resolved'}</AlertTitle>
      <AlertDescription>
        The SQL below uses <code>&lt;jobs-run-as&gt;</code>.{' '}
        {plan.jobs_identity_mixed
          ? 'Redeploy so every job uses the jobs_run_as variable, then refresh.'
          : 'Open any migration job’s Run as setting and substitute it before running the grants.'}
      </AlertDescription>
    </Alert>
  );
}

function GrantGroup({
  copy,
  title,
  grants,
  tier,
}: {
  copy: SectionCopy;
  title?: string;
  grants: GrantStatus[];
  tier: 1 | 2;
}) {
  const block = copyBlock(grants);
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader
        copy={copy}
        variant="inline"
        title={title}
        actions={
          <Button variant="outline" size="sm" onClick={() => void navigator.clipboard.writeText(block)}>
            <Copy className="size-3.5" aria-hidden /> Copy
          </Button>
        }
      />
      <ul className="flex flex-col gap-1">
        {grants.map((g) => (
          <GrantRow key={g.stmt} grant={g} tier={tier} />
        ))}
      </ul>
    </div>
  );
}

function GrantRow({ grant, tier }: { grant: GrantStatus; tier: 1 | 2 }) {
  const hint = grantHint(tier, grant.privilege);
  return (
    <li className="flex items-start gap-2">
      <PresentBadge present={grant.present} />
      <code className="min-w-0 flex-1 break-words font-mono text-[11px] text-foreground">{grant.stmt};</code>
      {hint ? <InfoHint text={hint} className="mt-0.5 text-muted-foreground" /> : null}
    </li>
  );
}

function PresentBadge({ present }: { present: Present }) {
  const tone = presentTone(present);
  const cls: Record<'success' | 'warning' | 'muted', string> = {
    success: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
    warning: 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
    muted: 'border-border bg-muted text-muted-foreground',
  };
  const label = present === 'yes' ? 'Landed' : present === 'no' ? 'Missing' : 'Unknown';
  return (
    <Badge variant="outline" className={`w-fit shrink-0 gap-1 ${cls[tone]}`}>
      {present === 'yes' ? <CircleCheck className="size-3" aria-hidden /> : null}
      {label}
    </Badge>
  );
}

function Tier2Section({ plan, busy, selection }: { plan: GrantsPlan; busy: boolean; selection: ScopeSelection }) {
  const [view, setView] = useState<Tier2View>('missing');
  const groups = useMemo(() => visibleTier2(plan.tier2, selection), [plan, selection]);
  const summary = useMemo(() => tier2Summary(groups), [groups]);
  const catalogs = useMemo(() => groupTier2ByCatalog(groups, view), [groups, view]);
  const selectedCount = selection.selected.length;
  if (groups.length === 0) {
    return (
      <Alert>
        <AlertTitle>Tier 2 — pick the schemas to grant</AlertTitle>
        <AlertDescription>
          Tick or add schemas above to get their grants now. Or run the profiler (Rescan estate,
          above) so the migration scope is known.
        </AlertDescription>
      </Alert>
    );
  }
  const missingSql = tier2Sql(groups, 'missing');
  return (
    <div className="flex flex-col gap-3">
      <SectionHeader
        copy={SECTION_COPY.settings.grantsTier2}
        variant="inline"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!missingSql}
              onClick={() => void navigator.clipboard.writeText(missingSql)}
            >
              <Copy className="size-3.5" aria-hidden /> Copy all missing
            </Button>
            <Button variant="outline" size="sm" onClick={() => void navigator.clipboard.writeText(tier2Sql(groups, 'all'))}>
              <Copy className="size-3.5" aria-hidden /> Copy all
            </Button>
            <Button variant="outline" size="sm" onClick={() => downloadSql(tier2Sql(groups, view), TIER2_SQL_FILENAME)}>
              <Download className="size-3.5" aria-hidden /> Download .sql
            </Button>
          </div>
        }
      />
      <JobsIdentityLine plan={plan} />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span aria-live="polite">
          <strong className="text-foreground">
            {count(summary.schemas)} {plural(summary.schemas, 'schema')}
          </strong>{' '}
          · {count(summary.landed)} landed · {count(summary.missing)} missing
          {summary.unknown > 0 ? ` · ${count(summary.unknown)} not checked` : ''}
        </span>
        <ViewToggle
          label="Which grants to show"
          value={view}
          onChange={setView}
          options={[
            { value: 'missing', label: 'Missing only' },
            { value: 'all', label: 'All' },
          ]}
        />
      </div>
      {busy ? <p className="text-xs text-muted-foreground">Updating the grants for your selection…</p> : null}
      {selectedCount > MAX_SELECTED_SCOPES ? (
        <p className="text-xs text-muted-foreground">
          Only the first {MAX_SELECTED_SCOPES} selected schemas, in name order, get grants here.
        </p>
      ) : null}
      {view === 'missing' && catalogs.length === 0 ? (
        <p className="text-xs text-muted-foreground">Every grant in Step 2 has landed.</p>
      ) : null}
      <div className="flex flex-col gap-2">
        {catalogs.map((c) => (
          <CatalogGrantsGroup key={c.catalog} entry={c} defaultOpen={catalogs.length <= OPEN_CATALOGS_UP_TO} />
        ))}
      </div>
      {view === 'missing' && summary.fullyGranted > 0 ? (
        <p className="text-xs text-muted-foreground">
          {count(summary.fullyGranted)} {plural(summary.fullyGranted, 'schema')} with every schema grant landed.{' '}
          <button type="button" className="underline hover:text-foreground" onClick={() => setView('all')}>
            Show all
          </button>
        </p>
      ) : null}
    </div>
  );
}

function CatalogGrantsGroup({ entry, defaultOpen }: { entry: CatalogGrants; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [limit, setLimit] = useState(SCHEMAS_PER_STEP);
  const more = entry.schemas.length - limit;
  return (
    <div className="rounded-md border border-border">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-sm"
        >
          <ChevronRight className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} aria-hidden />
          <span className="truncate font-mono">{entry.catalog}</span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {count(entry.schemaCount)} {plural(entry.schemaCount, 'schema')} ·{' '}
            {entry.open > 0 ? `${count(entry.open)} to grant` : 'all landed'}
          </span>
        </button>
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Copy the grants for ${entry.catalog}`}
          onClick={() => void navigator.clipboard.writeText(catalogSql(entry))}
        >
          <Copy className="size-3.5" aria-hidden /> Copy
        </Button>
      </div>
      {open ? (
        <div className="flex flex-col gap-3 border-t border-border px-3 py-2">
          {entry.useCatalog ? (
            <ul className="flex flex-col gap-1">
              <GrantRow grant={entry.useCatalog} tier={2} />
            </ul>
          ) : null}
          {entry.schemas.slice(0, limit).map((s) => (
            <div key={s.schema} className="flex flex-col gap-1">
              <p className="flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
                {s.catalog}.{s.schema}
                {s.source === 'selected' ? (
                  <span className="inline-flex items-center gap-1 font-sans">
                    · {SECTION_COPY.settings.grantsTier2Selected.title}
                    <InfoHint text={SECTION_COPY.settings.grantsTier2Selected.subtitle} />
                  </span>
                ) : null}
              </p>
              <ul className="flex flex-col gap-1">
                {s.grants.map((g) => (
                  <GrantRow key={g.stmt} grant={g} tier={2} />
                ))}
              </ul>
            </div>
          ))}
          {more > 0 ? (
            <Button variant="ghost" size="sm" className="w-fit" onClick={() => setLimit((n) => n + SCHEMAS_PER_STEP)}>
              Show {Math.min(SCHEMAS_PER_STEP, more)} more of {count(more)}
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
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

function downloadSql(text: string, filename: string) {
  const url = URL.createObjectURL(new Blob([`${text}\n`], { type: 'application/sql' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

const count = (n: number) => n.toLocaleString();
const plural = (n: number, word: string) => (n === 1 ? word : `${word}s`);

function OwnershipSection({ plan }: { plan: GrantsPlan }) {
  const a = plan.authority_summary;
  const blocked = a ? a.total - a.authorized : 0;
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader copy={SECTION_COPY.settings.grantsOwnership} variant="inline" />
      {a === null ? (
        <p className="text-xs text-muted-foreground">
          Convert permissions can’t be read yet. The profiler and the dry run check and store them. They’re handled in the{' '}
          <Link to="/workstreams/ownership" className="underline">
            Convert permission workstream
          </Link>
          .
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          <strong>
            {blocked} of {a.total} table{a.total === 1 ? '' : 's'} still to convert
          </strong>{' '}
          {a.actor ? (
            <>
              are not yet authorized for <span className="font-mono">{a.actor}</span>
            </>
          ) : (
            'are not yet authorized for the convert identity'
          )}
          : Authorize can grant {a.self_authorizable}, {a.needs_grant} need a grant from someone else,{' '}
          {a.unknown + a.not_checked} are not checked yet. See the{' '}
          <Link to="/workstreams/ownership" className="underline">
            Convert permission workstream
          </Link>
          .
        </p>
      )}
      <a href="/api/grants/convert-permissions.sql" download>
        <Button variant="outline" size="sm">
          <Download className="size-3.5" aria-hidden /> Download the grant script
        </Button>
      </a>
    </div>
  );
}

function ScopePicker({
  obo,
  scopes,
  selection,
  typedInput,
  typedError,
  onTypedInput,
  onAddTyped,
  onToggle,
  onToggleCatalog,
  onRemove,
  onSelectAll,
  onClear,
  onRefresh,
}: {
  obo: OboDiscoveryLite | null;
  scopes: MergedScope[];
  selection: ScopeSelection;
  typedInput: string;
  typedError: string | null;
  onTypedInput: (v: string) => void;
  onAddTyped: () => void;
  onToggle: (key: string) => void;
  onToggleCatalog: (group: ScopeCatalogGroup) => void;
  onRemove: (key: string) => void;
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
  const selectAllBlocked = wouldPassLimit(selection, visibleKeys);
  const isBlocked = (g: ScopeCatalogGroup) => catalogTickState(g) !== 'all' && wouldPassLimit(selection, g.selectable);
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
        copy={SECTION_COPY.settings.grantsScope}
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
          <p>Estate discovery unavailable ({obo?.reason ?? 'unknown'}). Add catalogs/schemas manually.</p>
        ) : state === 'empty' ? (
          <p>
            Your account can’t see any external tables. Add a schema below, or ask a catalog owner or
            metastore admin for access.
          </p>
        ) : state === 'partial' ? (
          <p>
            External tables you can see, from a partial scan. {coverageText(obo?.coverage)} Add a schema
            below if yours is missing, or refresh to scan again.
          </p>
        ) : (
          <p>
            External tables <strong>you (the signed-in admin) can currently see</strong> — this is your
            view, not the whole estate. Tick the schemas the app should get grants for.
          </p>
        )}
        {coveredCount > 0 ? (
          <p className="mt-1">
            {coveredCount === scopes.length
              ? 'Every schema here is already covered by the estate scan, so its grants are in Step 2 below. Add a schema the scan has not listed to grant it too.'
              : `${coveredCount} of these ${coveredCount === 1 ? 'is' : 'are'} already covered by the estate scan, so ${coveredCount === 1 ? 'its' : 'their'} grants are in Step 2 below.`}
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
                { value: 'open', label: `Not covered · ${count(viewCounts.open)}` },
                { value: 'ticked', label: `Ticked · ${count(viewCounts.ticked)}` },
                { value: 'all', label: `All · ${count(viewCounts.all)}` },
              ]}
            />
          </div>
          <div
            role="region"
            aria-label="Schemas to pick"
            className="max-h-72 overflow-y-auto rounded-md border border-border"
          >
            {catalogs.length === 0 ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                {query.trim()
                  ? 'No schema matches the filter.'
                  : view === 'ticked'
                    ? 'Nothing ticked yet.'
                    : 'Every schema here is covered by the scan. Pick All to see them.'}
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
                  onRemove={onRemove}
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
            <Button variant="ghost" size="sm" onClick={onClear} disabled={selection.selected.length === 0}>
              Clear
            </Button>
            <span>
              {pickedCount} selected · {capacityLeft(selection)} of {MAX_SELECTED_SCOPES} left
            </span>
          </div>
          {(visibleKeys.length > 0 && selectAllBlocked) || anyCatalogBlocked ? (
            <p id={LIMIT_NOTE_ID} className="text-xs text-muted-foreground">
              Grants are built for up to {MAX_SELECTED_SCOPES} picked schemas at a time, so Select all and a
              catalog’s tick box turn off when they would pass that. Narrow the filter or tick schemas one by one.
            </p>
          ) : null}
        </>
      ) : null}
      <div className="flex items-center gap-2">
        <input
          className="w-64 rounded-md border border-border bg-background px-2 py-1 font-mono text-[12px]"
          placeholder="catalog.schema"
          aria-label="Add a schema"
          aria-invalid={typedError ? true : undefined}
          value={typedInput}
          onChange={(e) => onTypedInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onAddTyped();
          }}
        />
        <Button variant="outline" size="sm" onClick={onAddTyped}>
          Add scope
        </Button>
      </div>
      {typedError ? (
        <p role="alert" className="text-xs text-[var(--warning-foreground)]">
          {typedError}
        </p>
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
  onRemove,
}: {
  group: ScopeCatalogGroup;
  open: boolean;
  blocked: boolean;
  onFlip: () => void;
  onTickAll: () => void;
  onToggle: (key: string) => void;
  onRemove: (key: string) => void;
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
            {group.covered > 0 ? ` · ${count(group.covered)} covered` : ''}
          </span>
        </button>
      </div>
      {open ? (
        <div className="flex flex-wrap gap-1.5 px-3 pb-2 pl-9">
          {group.scopes.map((s) => (
            <ScopeChip key={scopeKey(s)} scope={s} onToggle={onToggle} onRemove={onRemove} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ScopeChip({
  scope,
  onToggle,
  onRemove,
}: {
  scope: MergedScope;
  onToggle: (key: string) => void;
  onRemove: (key: string) => void;
}) {
  const key = scopeKey(scope);
  if (scope.covered) {
    return (
      <span
        aria-label={`${key}, already covered by the scan`}
        className="flex items-center gap-1 rounded-md border border-[var(--success-border)] bg-[var(--success-bg)] px-2 py-0.5 font-mono text-[11px] text-[var(--success-foreground)]"
      >
        <CircleCheck className="size-3" aria-hidden />
        {key}
        {scope.externalTableCount !== null ? <span>· {scope.externalTableCount}</span> : null}
        <span className="font-sans">· already covered by the scan</span>
        {scope.source === 'manual' ? (
          <button
            type="button"
            aria-label={`Remove ${key}`}
            onClick={() => onRemove(key)}
            className="ml-1 hover:text-foreground"
          >
            <X className="size-3" aria-hidden />
          </button>
        ) : null}
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
        {scope.source === 'manual' ? <span className="text-muted-foreground">(added)</span> : null}
      </button>
      {scope.source === 'manual' ? (
        <button
          type="button"
          aria-label={`Remove ${key}`}
          onClick={() => onRemove(key)}
          className="border-l border-border px-1 py-0.5 hover:text-foreground"
        >
          <X className="size-3" aria-hidden />
        </button>
      ) : null}
    </span>
  );
}
