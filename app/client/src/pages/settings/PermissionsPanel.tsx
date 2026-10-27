import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  Input,
  Spinner,
} from '@databricks/appkit-ui/react';
import { CircleCheck, CircleX, Copy, Download, RefreshCw, X } from 'lucide-react';
import { Link } from 'react-router';
import { SectionHeader } from '../../components/SectionHeader';
import { PlanConfirmDialog } from '../../components/PlanConfirmDialog';
import { useCatalog } from '../../context/CatalogProvider';
import { DIALOG_COPY, LABEL_COPY, SECTION_COPY } from '../../lib/sectionCopy';
import {
  CONVERT_ALL_AT_ONCE,
  CONVERT_PER_WAVE,
  GO,
  GRANT_SETS,
  NO_GO,
  WARN,
  parseScopes,
  uncoveredAsk,
} from '../../lib/provision.pure';
import {
  type GrantResponse,
  type HandBackResponse,
  type PermissionsView,
  type ScopeRow,
  type ViewLine,
  convertHolders,
  errorText,
  grantBody,
  grantSummary,
  grantorSentence,
  identityLabel,
  panelMode,
  planSql,
  requestText,
  runnableLines,
  scopeRows,
  scopesWithPicks,
  statusLabel,
  viewUrl,
} from '../../lib/permissionsPanel.pure';
import type { AppGrantsPlan, AuthoritySummary } from './useAppGrantsPlan';
import { ScopePicker } from './ScopePicker';
import {
  type OboDiscoveryLite,
  clearSelection,
  mergeScopes,
  selectAll,
  toggleCatalog,
  toggleScope,
} from './grantsPanel.pure';

/**
 * Settings → Permissions: the install engine's app door. The server runs every statement as the
 * signed-in user (never the app's identity), pinned to the plan_id previewed here, audit row first
 * and read back after. Nothing on this panel decides who can grant: that comes from the server.
 * `appPlan` (read as the app) only feeds the convert summary.
 */
export function PermissionsPanel({ appPlan }: { appPlan: AppGrantsPlan }) {
  useEffect(() => {
    if (window.location.hash === '#permissions') document.getElementById('permissions')?.scrollIntoView();
  }, []);
  const { bumpRefresh } = useCatalog();
  /** Schemas picked this visit, on top of the stored migration scope (lower case). */
  const [picks, setPicks] = useState<string[]>([]);
  const [convertRights, setConvertRights] = useState<string>('');
  const [view, setView] = useState<PermissionsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [reload, setReload] = useState(0);
  const [result, setResult] = useState<string | null>(null);
  const [failures, setFailures] = useState<string[]>([]);
  const [obo, setObo] = useState<OboDiscoveryLite | null>(null);
  const [oboScans, setOboScans] = useState(0);

  const storedKey = (view?.stored_scopes ?? []).join(',');
  const scopesParam = useMemo(() => scopesWithPicks(storedKey ? storedKey.split(',') : [], picks), [storedKey, picks]);
  const options = useMemo(() => {
    const o: Record<string, string> = {};
    if (scopesParam) o.scopes = scopesParam;
    if (convertRights) o.convert_rights = convertRights;
    return o;
  }, [scopesParam, convertRights]);

  useEffect(() => {
    let cancelled = false;
    setObo(null);
    const path = oboScans > 0 ? '/api/estate/obo-discovery?refresh=1' : '/api/estate/obo-discovery';
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
  }, [oboScans]);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    void (async () => {
      try {
        const r = await fetch(viewUrl(options), { headers: { accept: 'application/json' } });
        const data = (await r.json().catch(() => ({}))) as PermissionsView | { error?: string };
        if (cancelled) return;
        if (!r.ok) {
          setError(errorText(r.status, data));
        } else {
          setView(data as PermissionsView);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [options, reload]);

  const post = useCallback(
    async <T,>(path: string, body: Record<string, string>): Promise<[T | null, string | null]> => {
      const r = await fetch(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify(body),
      });
      const data = (await r.json().catch(() => ({}))) as unknown;
      return r.ok ? [data as T, null] : [null, errorText(r.status, data)];
    },
    [],
  );

  async function grant(): Promise<string | null> {
    if (!view) return 'Nothing to grant.';
    const [out, err] = await post<GrantResponse>('/api/permissions/grant', grantBody(options, view.plan_id));
    if (err || !out) return err;
    setResult(out.stopped ?? grantSummary(out));
    setFailures(out.results.filter((r) => r.outcome === 'failed' || r.outcome === 'not_run')
      .map((r) => `${r.statement}: ${r.error ?? 'did not run'}`));
    setReload((n) => n + 1);
    bumpRefresh();
    return null;
  }

  async function handBack(): Promise<string | null> {
    if (!view) return 'Nothing to hand back.';
    const [out, err] = await post<HandBackResponse>('/api/permissions/hand-back', { plan_id: view.handback.plan_id });
    if (err || !out) return err;
    const done = out.results.filter((r) => r.outcome === 'revoked').length;
    setResult(`Handed back ${done} of ${out.results.length}.`);
    setFailures(out.results.filter((r) => r.outcome !== 'revoked').map((r) => `${r.statement}: ${r.error ?? 'did not run'}`));
    setReload((n) => n + 1);
    bumpRefresh();
    return null;
  }

  return (
    <Card id="permissions" className="scroll-mt-4">
      <SectionHeader
        copy={SECTION_COPY.settings.permissions}
        actions={
          <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => setReload((n) => n + 1)}>
            {busy ? <Spinner className="size-3.5" aria-hidden /> : <RefreshCw className="size-3.5" aria-hidden />}
            {LABEL_COPY.checkAgain}
          </Button>
        }
      />
      <CardContent className="flex flex-col gap-6">
        {error ? (
          <Alert variant="destructive">
            <CircleX className="size-4" aria-hidden />
            <AlertTitle>Could not check permissions</AlertTitle>
            <AlertDescription className="[overflow-wrap:anywhere]">{error}</AlertDescription>
          </Alert>
        ) : null}
        {!view && !error ? (
          <Alert>
            <Spinner className="size-4" aria-hidden />
            <AlertTitle>Checking what you can grant…</AlertTitle>
            <AlertDescription>Reading owners and grants with your sign-in.</AlertDescription>
          </Alert>
        ) : null}
        {result ? (
          <Alert>
            <CircleCheck className="size-4" aria-hidden />
            <AlertTitle>{result}</AlertTitle>
            {failures.length ? (
              <AlertDescription>
                <ul className="mt-1 list-disc pl-4 font-mono text-xs [overflow-wrap:anywhere]">
                  {failures.map((f) => <li key={f}>{f}</li>)}
                </ul>
              </AlertDescription>
            ) : null}
          </Alert>
        ) : null}
        {view ? (
          <PanelBody
            view={view}
            busy={busy}
            convertRights={convertRights || view.convert_rights}
            onConvertRights={setConvertRights}
            picker={{
              obo,
              picks,
              onPicks: setPicks,
              onRescan: () => setOboScans((n) => n + 1),
            }}
            authority={appPlan}
            onGrant={grant}
            onHandBack={handBack}
          />
        ) : null}
      </CardContent>
    </Card>
  );
}

interface PickerState {
  obo: OboDiscoveryLite | null;
  picks: string[];
  onPicks: (next: string[]) => void;
  onRescan: () => void;
}

function PanelBody({
  view,
  busy,
  convertRights,
  onConvertRights,
  picker,
  authority,
  onGrant,
  onHandBack,
}: {
  view: PermissionsView;
  busy: boolean;
  convertRights: string;
  onConvertRights: (v: string) => void;
  picker: PickerState;
  authority: AppGrantsPlan;
  onGrant: () => Promise<string | null>;
  onHandBack: () => Promise<string | null>;
}) {
  const mode = panelMode(view);
  if (mode === 'not_installed') {
    return (
      <div className="flex flex-col gap-4">
        <SectionHeader copy={SECTION_COPY.settings.permissionsFinish} variant="inline" />
        <CommandBlock label="CLI" text={view.cli_command} />
        {view.who.install_notebook ? <CommandBlock label="Notebook" text={view.who.install_notebook} /> : null}
        <RightsPart view={view} />
      </div>
    );
  }
  if (mode === 'cannot_act') {
    return (
      <div className="flex flex-col gap-4">
        <SectionHeader copy={SECTION_COPY.settings.permissionsCannotAct} variant="inline" />
        {view.reason ? <p className="text-sm text-muted-foreground">{view.reason}</p> : null}
        <CommandBlock label="CLI" text={view.cli_command} />
        {view.who.install_notebook ? <CommandBlock label="Notebook" text={view.who.install_notebook} /> : null}
        <p className="text-xs text-muted-foreground">
          The grants the plan expects. None of them could be checked, so some may already be in place.
        </p>
        <PlanLines lines={view.lines.filter((l) => l.status !== 'present')} known={false} />
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-6">
      <RightsPart view={view} />
      <JobsPart view={view} />
      <ScopePart view={view} busy={busy} picker={picker} />
      <ConvertPart
        view={view}
        convertRights={convertRights}
        onConvertRights={onConvertRights}
        onHandBack={onHandBack}
        authority={authority}
      />
      <PlanPart view={view} busy={busy} onGrant={onGrant} />
    </div>
  );
}

const STATUS_TONE: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  [GO]: 'secondary',
  [WARN]: 'outline',
  [NO_GO]: 'destructive',
};

function RightsPart({ view }: { view: PermissionsView }) {
  const [open, setOpen] = useState(false);
  const cov = view.rights.coverage;
  return (
    <section className="flex flex-col gap-2">
      <SectionHeader copy={SECTION_COPY.settings.permissionsRights} variant="inline" />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span>{cov.headline}</span>
        {cov.uncovered.length ? (
          <Button variant="link" size="sm" className="h-auto p-0" onClick={() => setOpen((o) => !o)}>
            {open ? 'Hide who to ask' : 'See who to ask'}
          </Button>
        ) : null}
      </div>
      {open ? (
        <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
          {cov.uncovered.map((c) => (
            <li key={c.catalog}>
              <span className="font-mono text-foreground">{c.catalog}</span> · {uncoveredAsk(c)}
            </li>
          ))}
        </ul>
      ) : null}
      <ul className="flex flex-col gap-1 text-sm">
        {view.rights.checks.map((c) => (
          <li key={c.key} className="flex flex-wrap items-baseline gap-2">
            <Badge variant={STATUS_TONE[c.status] ?? 'outline'} className="font-mono text-[10px]">{c.status}</Badge>
            <span>{c.label}</span>
            {c.detail ? <span className="text-muted-foreground">{c.detail}</span> : null}
            {c.ask ? <span className="text-muted-foreground">· ask {c.ask}</span> : null}
          </li>
        ))}
      </ul>
      {view.rights.caveat ? <p className="text-xs text-muted-foreground">{view.rights.caveat}</p> : null}
    </section>
  );
}

function JobsPart({ view }: { view: PermissionsView }) {
  const [open, setOpen] = useState(false);
  const change = `MTM_JOBS_IDENTITY=sp:<application-id> ${view.cli_command.replace(/^(MTM_\S+ )+/, '')}`;
  return (
    <section className="flex flex-col gap-2">
      <SectionHeader
        copy={SECTION_COPY.settings.permissionsJobs}
        variant="inline"
      />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono">{identityLabel(view.who.jobs_identity)}</span>
        <span className="text-muted-foreground">· all {view.who.job_count} jobs the app knows</span>
        <Button variant="outline" size="sm" onClick={() => setOpen((o) => !o)}>
          {LABEL_COPY.changeIdentity}
        </Button>
      </div>
      {open ? <CommandBlock label="CLI" text={change} /> : null}
    </section>
  );
}

const SET_MARK: Record<string, string> = { present: '✓', missing: 'missing', unknown: 'not checked', none: '—' };

function ScopePart({ view, busy, picker }: { view: PermissionsView; busy: boolean; picker: PickerState }) {
  const { obo, picks, onPicks, onRescan } = picker;
  const rows: ScopeRow[] = scopeRows(view);
  const [open, setOpen] = useState(() => rows.length === 0);
  const [typed, setTyped] = useState('');
  const [typedError, setTypedError] = useState<string | null>(null);
  const stored = useMemo(() => new Set(view.stored_scopes.map((s) => s.toLowerCase())), [view.stored_scopes]);
  const chips = useMemo(() => mergeScopes(obo?.visibility ?? [], picks, stored), [obo, picks, stored]);
  function add() {
    const [scopes, errors] = parseScopes(typed);
    if (errors.length) {
      setTypedError(errors.join(' '));
      return;
    }
    if (scopes.length) onPicks(selectAll(picks, scopes.filter((s) => !stored.has(s.toLowerCase()))));
    setTyped('');
    setTypedError(null);
  }
  return (
    <section className="flex flex-col gap-2">
      <SectionHeader
        copy={SECTION_COPY.settings.permissionsScope}
        variant="inline"
        actions={
          <Button variant="outline" size="sm" onClick={() => setOpen((o) => !o)}>
            {LABEL_COPY.addSchemas}
          </Button>
        }
      />
      {rows.length ? (
        <ul className="flex flex-col gap-1 text-sm">
          {rows.map((r) => (
            <li key={r.scope} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="min-w-40 font-mono">{r.scope}</span>
              {!r.stored ? (
                <span className="inline-flex items-center gap-1">
                  <Badge variant="outline" className="text-[10px]">new</Badge>
                  <button
                    type="button"
                    aria-label={`Remove ${r.scope}`}
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => onPicks(picks.filter((p) => p !== r.scope.toLowerCase()))}
                  >
                    <X className="size-3" aria-hidden />
                  </button>
                </span>
              ) : null}
              {GRANT_SETS.filter((s) => s !== 'Control').map((s) => (
                <span key={s} className={r.sets[s] === 'missing' ? 'text-destructive' : 'text-muted-foreground'}>
                  {s} {SET_MARK[r.sets[s]]}
                </span>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No schemas yet. Tick the ones to migrate below, or type them.</p>
      )}
      {open ? (
        <div className="flex flex-col gap-3 rounded-md border border-border p-3">
          <ScopePicker
            obo={obo}
            scopes={chips}
            selected={picks}
            onToggle={(key) => onPicks(toggleScope(picks, key))}
            onToggleCatalog={(group) => onPicks(toggleCatalog(picks, group))}
            onSelectAll={(keys) => onPicks(selectAll(picks, keys))}
            onClear={() => onPicks(clearSelection(picks))}
            onRefresh={onRescan}
          />
          <div className="flex gap-2">
            <Input
              value={typed}
              placeholder="catalog.schema or catalog.*"
              aria-label="Type a schema"
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') add();
              }}
            />
            <Button size="sm" disabled={busy || !typed.trim()} onClick={add}>
              {LABEL_COPY.addSchemas}
            </Button>
          </div>
          {typedError ? <p className="text-xs text-destructive">{typedError}</p> : null}
        </div>
      ) : null}
    </section>
  );
}

function ConvertPart({
  view,
  convertRights,
  onConvertRights,
  onHandBack,
  authority,
}: {
  view: PermissionsView;
  convertRights: string;
  onConvertRights: (v: string) => void;
  onHandBack: () => Promise<string | null>;
  authority: AppGrantsPlan;
}) {
  const holders = convertHolders(view);
  const allAtOnce = convertRights === CONVERT_ALL_AT_ONCE;
  const { revokes, refused } = view.handback;
  return (
    <section className="flex flex-col gap-2">
      <SectionHeader copy={SECTION_COPY.settings.permissionsConvert} variant="inline" />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span>{allAtOnce ? 'All at once · handed back at the end' : 'Per wave · granted before each wave converts'}</span>
        <Button variant="outline" size="sm" onClick={() => onConvertRights(allAtOnce ? CONVERT_PER_WAVE : CONVERT_ALL_AT_ONCE)}>
          {allAtOnce ? 'Switch to per wave' : 'Switch to all at once'}
        </Button>
      </div>
      {holders.length ? (
        <p className="text-sm text-muted-foreground">
          The jobs hold MANAGE on <span className="font-mono text-foreground">{holders.join(', ')}</span>.
        </p>
      ) : null}
      {revokes.length ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>{revokes.length} ready to hand back.</span>
          <PlanConfirmDialog
            copy={DIALOG_COPY.handBackRights}
            statements={revokes.map((r) => `${r.statement};`)}
            asUser={view.who.user}
            planId={view.handback.plan_id}
            variant="outline"
            onConfirm={onHandBack}
          />
        </div>
      ) : null}
      {!revokes.length && view.handback.waiting?.length ? (
        <p className="text-xs text-muted-foreground">
          Hand back opens once every table there is cleaned or rolled back.
        </p>
      ) : null}
      {refused.length ? (
        <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
          {refused.map((r) => <li key={r.ledger_key}>{r.securable} stays. {r.reason}</li>)}
        </ul>
      ) : null}
      <ConvertSummary summary={authority.plan?.authority_summary ?? null} loaded={authority.plan !== null} />
    </section>
  );
}

/** Per-table convert permission, from `convert_authority` (read as the app; the jobs write it). */
function ConvertSummary({ summary: a, loaded }: { summary: AuthoritySummary | null; loaded: boolean }) {
  if (!loaded) return null;
  const blocked = a ? a.total - a.authorized : 0;
  return (
    <div className="flex flex-col gap-2">
      {a === null ? (
        <p className="text-xs text-muted-foreground">
          Per-table convert permission can’t be read yet. The profiler and the dry run check and store it. It’s handled in the{' '}
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
      <a href="/api/grants/convert-permissions.sql" download className="w-fit">
        <Button variant="outline" size="sm">
          <Download className="size-3.5" aria-hidden /> Download the per-table grant script
        </Button>
      </a>
    </div>
  );
}

function PlanPart({ view, busy, onGrant }: { view: PermissionsView; busy: boolean; onGrant: () => Promise<string | null> }) {
  const [copied, setCopied] = useState(false);
  const mine = runnableLines(view);
  const request = requestText(view);
  const pending = view.lines.filter((l) => l.status !== 'present');
  return (
    <section className="flex flex-col gap-3">
      <SectionHeader
        copy={SECTION_COPY.settings.permissionsPlan}
        variant="inline"
        title={pending.length ? `Plan · ${pending.length} grants missing` : 'Plan · nothing missing'}
      />
      {view.wave?.error ? (
        <Alert variant="destructive">
          <AlertDescription>{view.wave.error}</AlertDescription>
        </Alert>
      ) : null}
      <PlanLines lines={pending} />
      {pending.length ? (
        <div className="flex flex-wrap gap-2">
          <PlanConfirmDialog
            copy={DIALOG_COPY.grantPermissions}
            vars={{ what: `${mine.length} permission${mine.length === 1 ? '' : 's'}` }}
            statements={mine.map((l) => `${l.statement};`)}
            asUser={view.who.user}
            planId={view.plan_id}
            disabled={busy}
            onConfirm={onGrant}
          >
            {mine.some((l) => l.set === 'Change') ? (
              <p className="text-muted-foreground">MANAGE lets the jobs convert these schemas. It is handed back at the end.</p>
            ) : null}
          </PlanConfirmDialog>
          <Button variant="outline" size="sm" className="gap-1.5" onClick={() => downloadText(planSql(view), `migration-grant-plan-${view.plan_id}.sql`)}>
            <Download className="size-3.5" aria-hidden />
            {LABEL_COPY.downloadPlan}
          </Button>
          {request ? (
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => {
                void navigator.clipboard.writeText(request).then(() => setCopied(true));
              }}
            >
              <Copy className="size-3.5" aria-hidden />
              {copied ? LABEL_COPY.copiedName : LABEL_COPY.copyRequest}
            </Button>
          ) : null}
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">
        Plan <span className="font-mono">{view.plan_id}</span>. The same plan from the notebook or the CLI has the same fingerprint.
      </p>
    </section>
  );
}

function PlanLines({ lines, known = true }: { lines: ViewLine[]; known?: boolean }) {
  if (!lines.length) return null;
  return (
    <ul className="flex flex-col gap-1.5 text-sm">
      {lines.map((l) => (
        <li key={`${l.statement}`} className="flex flex-col gap-0.5">
          <div className="flex flex-wrap items-baseline gap-2">
            <Badge variant={l.status === 'missing' ? 'destructive' : 'outline'} className="text-[10px]">
              {statusLabel(l.status)}
            </Badge>
            <span className="text-muted-foreground">{l.set}</span>
            <code className="font-mono text-xs [overflow-wrap:anywhere]">{l.commands?.length ? l.statement.replace(/^-- /, '') : l.statement}</code>
          </div>
          {known && l.grantor ? <span className="pl-1 text-xs text-muted-foreground">{grantorSentence(l.grantor)}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function CommandBlock({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex items-start gap-2">
      <span className="mt-1.5 w-16 shrink-0 text-xs text-muted-foreground">{label}</span>
      <pre className="flex-1 overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</pre>
      <Button variant="outline" size="sm" onClick={() => void navigator.clipboard.writeText(text)}>
        <Copy className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}

function downloadText(text: string, filename: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/sql' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
