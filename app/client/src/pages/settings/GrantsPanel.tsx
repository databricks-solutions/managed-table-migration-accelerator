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
import { CircleCheck, CircleX, Copy, RefreshCw } from 'lucide-react';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, type SectionCopy } from '../../lib/sectionCopy';
import { grantHint } from '../../lib/grantHints.pure';
import { copyBlock, presentTone } from './grantsPanel.pure';
import type { AppGrantsPlan, GrantStatus, Present } from './useAppGrantsPlan';

/**
 * Settings → "The app's own access": the app service principal's control-table grants, which the
 * setup job issues. The app VERIFIES them (SHOW GRANTS) and never executes a GRANT (SR12 / §0.2).
 * Everything the migration jobs need is the Permissions panel's plan.
 */
export function GrantsPanel({ state }: { state: AppGrantsPlan }) {
  const { plan, error, busy, refresh } = state;
  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.settings.grants}
        actions={
          <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={refresh}>
            {busy ? <Spinner className="size-3.5" aria-hidden /> : <RefreshCw className="size-3.5" aria-hidden />}
            Refresh
          </Button>
        }
      />
      <CardContent className="flex flex-col gap-6">
        {error ? (
          <Alert variant="destructive">
            <CircleX className="size-4" aria-hidden />
            <AlertTitle>Could not check the app’s grants</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : plan === null ? (
          <Alert>
            <Spinner className="size-4" aria-hidden />
            <AlertTitle>Checking the app’s grants…</AlertTitle>
            <AlertDescription>Resolving the service principal and reading its grants.</AlertDescription>
          </Alert>
        ) : null}
        {plan ? (
          <>
            <SpLine spClientId={plan.sp_client_id} />
            <GrantGroup copy={SECTION_COPY.settings.grantsTier1} grants={plan.tier1} />
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

function GrantGroup({ copy, grants }: { copy: SectionCopy; grants: GrantStatus[] }) {
  const block = copyBlock(grants);
  return (
    <div className="flex flex-col gap-2">
      <SectionHeader
        copy={copy}
        variant="inline"
        actions={
          <Button variant="outline" size="sm" onClick={() => void navigator.clipboard.writeText(block)}>
            <Copy className="size-3.5" aria-hidden /> Copy
          </Button>
        }
      />
      <ul className="flex flex-col gap-1">
        {grants.map((g) => (
          <GrantRow key={g.stmt} grant={g} />
        ))}
      </ul>
    </div>
  );
}

function GrantRow({ grant }: { grant: GrantStatus }) {
  const hint = grantHint(1, grant.privilege);
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
  const label = present === 'yes' ? 'Landed' : present === 'no' ? 'Missing' : 'Not checked';
  return (
    <Badge variant="outline" className={`w-fit shrink-0 gap-1 ${cls[tone]}`}>
      {present === 'yes' ? <CircleCheck className="size-3" aria-hidden /> : null}
      {label}
    </Badge>
  );
}
