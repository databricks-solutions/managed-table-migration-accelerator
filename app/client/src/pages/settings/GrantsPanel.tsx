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
import { CircleCheck, CircleX, Download, Copy, RefreshCw, X } from 'lucide-react';
import { Link } from 'react-router';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, fillCopy, type SectionCopy } from '../../lib/sectionCopy';
import { grantHint } from '../../lib/grantHints.pure';
import {
  MAX_SELECTED_SCOPES,
  addTyped,
  clearSelection,
  copyBlock,
  coverageText,
  discoveryState,
  loadSelection,
  mergeScopes,
  parseTypedScope,
  planUrl,
  presentTone,
  removeTyped,
  saveSelection,
  scopeKey,
  selectAll,
  toggleScope,
  visibleTier2,
  type MergedScope,
  type OboDiscoveryLite,
  type ScopeSelection,
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

  useEffect(() => {
    saveSelection(browserStorage(), selection);
  }, [selection]);

  const url = useMemo(() => planUrl(selection), [selection]);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const delay = firstPlanLoad.current ? 0 : PLAN_DEBOUNCE_MS;
    firstPlanLoad.current = false;
    setPlanBusy(true);
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const r = await fetch(url, { headers: { accept: 'application/json' }, signal: ctrl.signal });
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
  }, [url]);

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

  const scopes: MergedScope[] = useMemo(() => mergeScopes(obo?.visibility ?? [], selection), [obo, selection]);

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
          selectedCount={selection.selected.length}
          typedInput={typedInput}
          typedError={typedError}
          onTypedInput={(v) => {
            setTypedInput(v);
            if (typedError) setTypedError(null);
          }}
          onAddTyped={onAddTyped}
          onToggle={(key) => setSelection((prev) => toggleScope(prev, key))}
          onRemove={(key) => setSelection((prev) => removeTyped(prev, key))}
          onSelectAll={() => setSelection((prev) => selectAll(prev, scopes.map(scopeKey)))}
          onClear={() => setSelection((prev) => clearSelection(prev))}
          onRefresh={() => setRefreshCount((n) => n + 1)}
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
        {grants.map((g) => {
          const hint = grantHint(tier, g.privilege);
          return (
            <li key={g.stmt} className="flex items-start gap-2">
              <PresentBadge present={g.present} />
              <code className="min-w-0 flex-1 break-words font-mono text-[11px] text-foreground">
                {g.stmt};
              </code>
              {hint ? <InfoHint text={hint} className="mt-0.5 text-muted-foreground" /> : null}
            </li>
          );
        })}
      </ul>
    </div>
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
  const groups = visibleTier2(plan.tier2, selection);
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
  return (
    <div className="flex flex-col gap-4">
      {busy ? <p className="text-xs text-muted-foreground">Updating the grants for your selection…</p> : null}
      {selectedCount > MAX_SELECTED_SCOPES ? (
        <p className="text-xs text-muted-foreground">
          Only the first {MAX_SELECTED_SCOPES} selected schemas, in name order, get grants here.
        </p>
      ) : null}
      {groups.map((t) => {
        const copy = t.source === 'selected' ? SECTION_COPY.settings.grantsTier2Selected : SECTION_COPY.settings.grantsTier2;
        return (
          <GrantGroup
            key={`${t.catalog}.${t.schema}`}
            copy={copy}
            title={fillCopy(copy.title, { scope: `${t.catalog}.${t.schema}` })}
            grants={t.grants}
            tier={2}
          />
        );
      })}
    </div>
  );
}

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
  selectedCount,
  typedInput,
  typedError,
  onTypedInput,
  onAddTyped,
  onToggle,
  onRemove,
  onSelectAll,
  onClear,
  onRefresh,
}: {
  obo: OboDiscoveryLite | null;
  scopes: MergedScope[];
  selectedCount: number;
  typedInput: string;
  typedError: string | null;
  onTypedInput: (v: string) => void;
  onAddTyped: () => void;
  onToggle: (key: string) => void;
  onRemove: (key: string) => void;
  onSelectAll: () => void;
  onClear: () => void;
  onRefresh: () => void;
}) {
  const state = discoveryState(obo);
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
      </div>
      {scopes.length > 0 ? (
        <>
          <div className="flex flex-wrap gap-1.5">
            {scopes.map((s) => (
              <ScopeChip key={scopeKey(s)} scope={s} onToggle={onToggle} onRemove={onRemove} />
            ))}
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Button variant="ghost" size="sm" onClick={onSelectAll}>
              Select all
            </Button>
            <Button variant="ghost" size="sm" onClick={onClear} disabled={selectedCount === 0}>
              Clear
            </Button>
            <span>{selectedCount} selected</span>
          </div>
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
