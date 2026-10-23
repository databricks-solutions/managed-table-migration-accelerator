import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  Spinner,
} from '@databricks/appkit-ui/react';
import { CircleCheck, CircleX, Download, Copy } from 'lucide-react';
import { Link } from 'react-router';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, fillCopy, type SectionCopy } from '../../lib/sectionCopy';
import { grantHint } from '../../lib/grantHints.pure';
import { copyBlock, mergeScopes, presentTone, type MergedScope } from './grantsPanel.pure';

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
  tier2: { catalog: string; schema: string; grants: GrantStatus[] }[];
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
interface OboDiscovery {
  available: boolean;
  reason?: string;
  visibility: { catalog: string; schema: string; externalTableCount: number }[];
}

/**
 * Settings → "Grants the App SP needs". The app GENERATES the grant SQL and VERIFIES what has
 * landed (SHOW GRANTS); it NEVER executes a GRANT or an ownership change (SR12 / §0.2). Honest states: the
 * target list is unknown before the profiler runs; the OBO list is the admin's visibility, never
 * "the estate".
 */
export function GrantsPanel() {
  const [plan, setPlan] = useState<GrantsPlan | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [obo, setObo] = useState<OboDiscovery | null>(null);
  const [typedInput, setTypedInput] = useState('');
  const [typed, setTyped] = useState<{ catalog: string; schema: string }[]>([]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch('/api/grants/plan', { headers: { accept: 'application/json' } });
        const data = (await r.json()) as GrantsPlan | { error: string };
        if (cancelled) return;
        if ('error' in data) setPlanErr(data.error);
        else setPlan(data);
      } catch (err) {
        if (!cancelled) setPlanErr(err instanceof Error ? err.message : String(err));
      }
      try {
        const r = await fetch('/api/estate/obo-discovery', { headers: { accept: 'application/json' } });
        const data = (await r.json()) as OboDiscovery;
        if (!cancelled) setObo(data);
      } catch {
        if (!cancelled) setObo({ available: false, reason: 'discovery request failed', visibility: [] });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const scopes: MergedScope[] = useMemo(
    () => mergeScopes(obo?.visibility ?? [], typed),
    [obo, typed],
  );

  function addTyped() {
    const raw = typedInput.trim();
    const m = /^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$/.exec(raw);
    if (!m) return;
    setTyped((prev) => [...prev, { catalog: m[1], schema: m[2] }]);
    setTypedInput('');
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
        ) : (
          <>
            <SpLine spClientId={plan.sp_client_id} />
            <GrantGroup
              copy={SECTION_COPY.settings.grantsTier1}
              grants={plan.tier1}
              tier={1}
            />
            <Tier2Section plan={plan} />
            <OwnershipSection plan={plan} />
            <OboSection
              obo={obo}
              scopes={scopes}
              typedInput={typedInput}
              onTypedInput={setTypedInput}
              onAddTyped={addTyped}
            />
          </>
        )}
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

function Tier2Section({ plan }: { plan: GrantsPlan }) {
  if (!plan.targets_known) {
    return (
      <Alert>
        <AlertTitle>Tier 2 — target namespaces unknown yet</AlertTitle>
        <AlertDescription>
          Run the profiler (Rescan estate, above) so the migration scope is known. Until then,
          add catalogs/schemas manually below to generate their Tier 2 grants.
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {plan.tier2.map((t) => (
        <GrantGroup
          key={`${t.catalog}.${t.schema}`}
          copy={SECTION_COPY.settings.grantsTier2}
          title={fillCopy(SECTION_COPY.settings.grantsTier2.title, { scope: `${t.catalog}.${t.schema}` })}
          grants={t.grants}
          tier={2}
        />
      ))}
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

function OboSection({
  obo,
  scopes,
  typedInput,
  onTypedInput,
  onAddTyped,
}: {
  obo: OboDiscovery | null;
  scopes: MergedScope[];
  typedInput: string;
  onTypedInput: (v: string) => void;
  onAddTyped: () => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader copy={SECTION_COPY.settings.grantsScope} variant="inline" />
      {obo === null ? (
        <p className="text-xs text-muted-foreground">Checking what you can see…</p>
      ) : !obo.available ? (
        <p className="text-xs text-muted-foreground">
          Estate discovery unavailable ({obo.reason ?? 'unknown'}). Add catalogs/schemas manually.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          External tables <strong>you (the signed-in admin) can currently see</strong> — this is
          your view, not the whole estate. Grant access per schema to cover tables not listed here.
          The estate scan finds the full set after grants land.
        </p>
      )}
      <div className="flex flex-wrap gap-1">
        {scopes.map((s) => (
          <Badge key={`${s.catalog}.${s.schema}`} variant="outline" className="gap-1 font-mono text-[11px]">
            {s.catalog}.{s.schema}
            {s.source === 'manual' ? <span className="text-muted-foreground">(added)</span> : null}
          </Badge>
        ))}
        {scopes.length === 0 ? <span className="text-xs text-muted-foreground">No scopes yet.</span> : null}
      </div>
      <div className="flex items-center gap-2">
        <input
          className="w-64 rounded-md border border-border bg-background px-2 py-1 font-mono text-[12px]"
          placeholder="catalog.schema"
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
    </div>
  );
}
