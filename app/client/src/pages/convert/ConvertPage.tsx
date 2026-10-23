import { useRef, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router';
import {
  Badge,
  Button,
  Card,
  CardContent,
  Checkbox,
  Input,
  Label,
} from '@databricks/appkit-ui/react';
import { sql } from '@databricks/appkit-ui/js';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { PagedTable, type Column } from '../../components/PagedTable';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { ConvertGateDialog, type WaveCoverage } from '../../components/ConvertGateDialog';
import { appliedFreezeForScope } from '../../components/appliedFreeze.pure';
import { LocationPlanForScope } from '../../components/LocationPlanForScope';
import { coverageForWave, formatAge, isCoverageStale } from '../../lib/coverage';
import { ownershipForWave } from '../../lib/ownershipCoverage.pure';
import { readinessProvenance } from '../../lib/readiness.pure';
import { latestCampaignVerdict, type CampaignGateRowLike } from '../../lib/campaignVerdict.pure';
import { scopeReadinessState, type ScopeReadiness } from '../../lib/scopeReadiness.pure';
import { verdictCellModel } from '../../lib/scopePreview.pure';
import { WavePicker } from '../../components/WavePicker';
import { BucketPicker } from '../../components/BucketPicker';
import { SchemaPicker } from '../../components/SchemaPicker';
import { InfoHint } from '../../components/InfoHint';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, FOCUS_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { focusHref } from '../../lib/focus.pure';
import { CARD_HINTS } from '../../lib/fieldHints';
import { computeConvertImpact, convertImpactSentence } from '../../lib/impact';
import { convertScopeState } from '../../lib/convertScope.pure';
import { scopePhrase, type SchemaPlanRowLike } from '../../lib/schemaOptions.pure';
import { CleanupCard, RollbackCard } from './postConvertActions';

/**
 * Convert (expert) — hand-scoped conversion. The scope preview is computed CLIENT-SIDE by
 * joining wave_plan_status with wave_gate_latest (T2): it fires NO job. A wave with no gate
 * row shows UNEVALUATED — never a fabricated GO/BLOCKED. recent_write_minutes is widget-only
 * (T3), so it renders read-only. Both on-ramps run the same job_04_convert gate.
 */
export function ConvertPage() {
  // Pre-scope from an on-ramp: ?schema=<catalog.schema> (Overview next_selector) or
  // ?wave=<id>&streaming=1 (a streaming wave routed here from the Waves board's "Open the
  // gate"). Lazy initializers read them once on mount so the operator lands scoped.
  const [searchParams] = useSearchParams();
  const [targetSchema, setTargetSchema] = useState(() => searchParams.get('schema') ?? '');
  const [bucket, setBucket] = useState('');
  const [waveIds, setWaveIds] = useState(() => searchParams.get('wave') ?? '');
  const [includeStreaming, setIncludeStreaming] = useState(
    () => searchParams.get('streaming') === '1',
  );
  const [campaignSchema, setCampaignSchema] = useState('');
  const [campaignIncludeStreaming, setCampaignIncludeStreaming] = useState(false);
  const campaignCardRef = useRef<HTMLDivElement>(null);

  // From an UNEVALUATED wave in the readiness board: seed the campaign card with the wave's
  // schema and bring it into view, so "how do I earn a verdict?" is one click, not a hunt.
  function checkReadinessFor(schema: string) {
    setCampaignSchema(schema);
    campaignCardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // Freeze-bracket coverage per wave lives only in wave_gate_latest — used to drive the gate
  // when the scope is a single wave; otherwise the gate shows UNEVALUATED (honest, not GO).
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const planRowsQ = useMigrationQuery('wave_plan_status', {});
  const planRows = planRowsQ.data ?? [];

  const ts = targetSchema.trim();
  const bk = bucket.trim();
  const wv = waveIds.trim();
  // Selector rules (mirror the server refine + engine resolve_wave_targets): wave_ids is
  // exclusive with schema/bucket, and a non-streaming bucket is a filter (not a standalone
  // scope) — only bucket=streaming may stand alone. See convertScope.pure.ts.
  const {
    hasScope,
    scopeConflict,
    bucketOnly,
    disabledReason,
    schemaDisabled,
    bucketDisabled,
    waveDisabled,
  } = convertScopeState({ ts, bk, wv });
  const scopeLabel = wv ? `waves ${wv}` : [ts, bk].filter(Boolean).join(' · ') || '—';
  // A single wave id (from the picker or the ?wave on-ramp) has real coverage; a schema or
  // bucket scope resolves to many waves at run time, so coverage stays UNEVALUATED here.
  const singleWave = wv !== '' && !wv.includes(',');
  const coverage: WaveCoverage | undefined = singleWave
    ? coverageForWave(gateQ.data ?? [], wv)
    : undefined;
  // Readiness provenance is per-wave, so it is only meaningful for a single-wave scope —
  // a schema/bucket scope resolves to many waves at run time (mirrors `coverage` above).
  const readiness = singleWave ? readinessProvenance(gateQ.data ?? [], wv) : undefined;
  // Applied-freeze continuity: a freeze that already ran for this wave (freeze_ledger FROZEN
  // rows). Per-wave, so single-wave scope only (a schema/bucket scope resolves to many waves →
  // undefined). Drives the gate's "Freeze applied — N paused" summary + Convert recommendation.
  const ledgerQ = useMigrationQuery('freeze_ledger_open', {});
  const appliedFreeze = singleWave
    ? appliedFreezeForScope(ledgerQ.data ?? [], { waveId: wv })
    : undefined;
  // Convert-permission coverage (pre-convert, stored convert_authority verdicts) — per-wave, so
  // single-wave scope only (mirrors `coverage`/`readiness`). A schema/bucket scope → no panel.
  const ownQ = useMigrationQuery('convert_authority_for_wave', {
    wave_id: sql.string(singleWave ? wv : '_'),
  });
  const ownership = singleWave ? ownershipForWave(ownQ.data) : undefined;
  // Advisory readiness for a SCHEMA scope: the last campaign GO/NO-GO for this schema, used to
  // nudge "run the readiness check first" before the gate. Schema-only (campaign is
  // schema-scoped); a wave scope already shows per-wave provenance in the gate dialog. Never
  // gates convert (SR1/SR8) and never fabricates a verdict (SR13).
  const scopeReadiness = ts ? scopeReadinessState(gateQ.data ?? [], ts) : null;
  // Freeze first is only constructible when the scope is a schema or a single wave — there is
  // no freeze-by-bucket parameter, so a bucket-only scope offers no in-app freeze. The 03c
  // notebook rejects wave_id alone ("no implicit freeze-all"), so a wave freeze MUST carry the
  // precise (plan_run_id, wave_id) pin; a comma-list wv (multi-wave) can't be frozen from here.
  const freezePlanRunId = singleWave
    ? (planRows.find((r) => r.wave_id === wv)?.plan_run_id ?? null)
    : null;
  const freezeParams =
    singleWave && freezePlanRunId
      ? { plan_run_id: freezePlanRunId, wave_id: wv }
      : ts
        ? { target_schema: ts }
        : undefined;
  const confirmTarget = wv || ts || bk;

  // Scope the loaded wave rows exactly as resolve_wave_targets would, then compute an honest
  // runnable-first count. Schema scope matches group_key (NOT wave_schema), matching the job.
  // The pilot wave's group_key is its real catalog.schema (design-deltas T14), so a schema scope
  // INCLUDES the pilot wave; convert only the pilot by its wave id or bucket=pilot.
  const scopeWaves = planRows.filter((r) => {
    if (wv) return wv.split(',').map((s) => s.trim()).includes(r.wave_id);
    if (ts && r.group_key !== ts) return false;
    if (bk && r.bucket !== bk) return false;
    return true;
  });
  const impact = computeConvertImpact(scopeWaves, includeStreaming);
  const affectedCount = impact.willAct;
  const impactSentence = convertImpactSentence(scopeLabel, impact);

  // Blast-radius visibility: a streaming-only scope (bucket=streaming, no schema/wave) resolves
  // across EVERY schema in the plan, so surface how many distinct schemas (group_keys) it spans.
  const streamingOnly = bk === 'streaming' && ts === '' && wv === '';
  const streamingSchemas = streamingOnly
    ? new Set(scopeWaves.map((r) => r.group_key).filter(Boolean)).size
    : 0;
  const streamingSpansManySchemas = streamingOnly && streamingSchemas > 1;

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader copy={SECTION_COPY.convert.convertGroup} variant="inline" className="border-b border-border pb-1.5" />

      <Card ref={campaignCardRef}>
        <SectionHeader copy={SECTION_COPY.convert.readiness} hintCard={CARD_HINTS.campaign} />
        <CardContent className="flex flex-col gap-3">
          <Field label="target_schema" htmlFor="cp-schema">
            <div className="max-w-md">
              <SchemaPicker
                id="cp-schema"
                value={campaignSchema}
                onSelect={setCampaignSchema}
                placeholder="Select a schema…"
              />
            </div>
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={campaignIncludeStreaming}
              onCheckedChange={(v) => setCampaignIncludeStreaming(v === true)}
            />
            include_streaming
          </label>
          <p className="text-xs text-muted-foreground">
            The check is schema-scoped, so streaming waves are <strong>excluded</strong> from the
            GO/NO-GO unless you opt in here — otherwise they’re silently skipped.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <JobTriggerDialog
              alias="campaign"
              {...dialogProps(DIALOG_COPY.schemaCheck)}
              triggerVariant="secondary"
              triggerDisabled={campaignSchema.trim() === ''}
              disabledReason="Pick a schema first"
              notes={
                <span>
                  {campaignIncludeStreaming
                    ? 'Streaming waves in this schema are included in the verdict.'
                    : 'Streaming waves in this schema are excluded from the verdict (enable include_streaming to assess them).'}
                </span>
              }
              params={{
                target_schema: campaignSchema.trim(),
                confirm_compaction_paused: true,
                include_streaming: campaignIncludeStreaming || undefined,
              }}
            />
            {campaignSchema.trim() !== '' ? (
              <Link
                to={focusHref(campaignSchema.trim())}
                className="text-sm font-medium text-[var(--brand)] underline-offset-2 hover:underline"
              >
                {FOCUS_COPY.workOnThis} →
              </Link>
            ) : null}
          </div>
          <CampaignVerdictLine
            gateRows={gateQ.data ?? []}
            schema={campaignSchema.trim()}
            planRows={planRows}
          />
        </CardContent>
      </Card>

      <Card data-tour="convert-scope">
        <SectionHeader copy={SECTION_COPY.convert.byScope} hintCard={CARD_HINTS.convertByScope} />
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="target_schema" htmlFor="cv-schema">
              <SchemaPicker
                id="cv-schema"
                value={targetSchema}
                onSelect={setTargetSchema}
                disabled={schemaDisabled}
                disabledHint="Clear the wave to scope by schema"
              />
            </Field>
            <Field label="bucket" htmlFor="cv-bucket">
              <BucketPicker
                id="cv-bucket"
                value={bucket}
                onSelect={setBucket}
                planRows={planRows}
                disabled={bucketDisabled}
                disabledHint="Clear the wave to filter by bucket"
              />
            </Field>
            <Field label="wave" htmlFor="cv-waves">
              <WavePicker
                id="cv-waves"
                value={waveIds}
                onSelect={(o) => setWaveIds(o?.wave_id ?? '')}
                disabled={waveDisabled}
                disabledHint="Clear the schema/bucket to pick a wave"
              />
            </Field>
          </div>

          <div className="flex flex-wrap items-center gap-6">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={includeStreaming}
                onCheckedChange={(v) => setIncludeStreaming(v === true)}
              />
              include_streaming
            </label>
            <Field label="recent_write_minutes (read-only)" htmlFor="cv-rwm">
              <Input id="cv-rwm" value="server default — not sent" readOnly disabled className="max-w-[220px]" />
            </Field>
          </div>

          {scopeConflict ? (
            <p className="text-xs text-[var(--destructive-foreground)]">
              wave_ids is exclusive with target_schema/bucket — pass wave_ids alone, or scope
              by schema and/or bucket.
            </p>
          ) : bucketOnly ? (
            <p className="text-xs text-[var(--destructive-foreground)]">
              bucket only narrows a schema — pick a schema or a wave to convert. (Streaming is
              the exception: <span className="font-mono">bucket=streaming</span> is the way to
              convert streaming waves a schema scope skips.)
            </p>
          ) : null}

          {streamingSpansManySchemas ? (
            <p className="text-xs text-muted-foreground">
              This streaming scope spans <strong>{streamingSchemas}</strong> schemas across the
              plan — every streaming wave, not one schema. Pin a wave to narrow it.
            </p>
          ) : null}

          {scopeReadiness ? (
            <ScopeReadinessNudge
              schema={ts}
              readiness={scopeReadiness}
              onCheckReadiness={checkReadinessFor}
              planRows={planRows}
            />
          ) : null}

          <div data-tour="convert-gate">
            <ConvertGateDialog
              {...dialogProps(DIALOG_COPY.convertScope)}
              triggerVariant="destructive"
              triggerDisabled={!hasScope || scopeConflict}
              disabledReason={disabledReason ?? 'Pick a scope first'}
              description={
                hasScope && !scopeConflict ? impactSentence.headline : DIALOG_COPY.convertScope.description
              }
              affected={{ count: affectedCount, scope: scopeLabel, names: [] }}
              // Empty-state (willAct===0) only once a valid scope is resolved — otherwise the
              // trigger is already disabled with "Provide a scope first".
              impact={hasScope && !scopeConflict ? impact : undefined}
              isStreaming={includeStreaming}
              coverage={coverage}
              appliedFreeze={appliedFreeze}
              readiness={readiness}
              confirmTarget={confirmTarget}
              // Preflight (run_as-aware ownership + prechecks) keyed EXACTLY to the scope: a single
              // wave via the `wave:<id>` token, else the schema. A multi-wave or bucket scope has no
              // single preflight target (undefined) → the ownership panel falls back to the ledger.
              preflightTarget={singleWave ? `wave:${wv}` : ts || undefined}
              confirmKind={wv ? 'wave' : ts ? 'schema' : 'bucket'}
              locationSlot={
                singleWave || (ts && !wv) ? (
                  <LocationPlanForScope
                    waveId={singleWave ? wv : undefined}
                    schema={!wv ? ts : undefined}
                    bare
                  />
                ) : undefined
              }
              notes={hasScope && !scopeConflict ? <span>{impactSentence.disclosure}</span> : null}
              convertParams={{
                target_schema: ts,
                bucket: bk,
                wave_ids: wv,
                include_streaming: includeStreaming,
                confirm_compaction_paused: true,
              }}
              freezeParams={freezeParams}
              ownership={ownership}
              leaseParams={freezeParams}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            The job converts the ready tables in the scope. Check the schema above and the
            readiness board below first.
          </p>
        </CardContent>
      </Card>

      <ScopePreview onCheckReadiness={checkReadinessFor} />

      <SectionHeader copy={SECTION_COPY.convert.rollbackGroup} variant="inline" className="mt-2 border-b border-border pb-1.5" />
      <RollbackCard />

      <SectionHeader copy={SECTION_COPY.convert.cleanupGroup} variant="inline" className="mt-2 border-b border-border pb-1.5" />
      <CleanupCard />
    </div>
  );
}

const UNEVALUATED_HINT =
  'Plan state is the wave’s lifecycle (READY, ROLLED_BACK…). Gate verdict is the last time a ' +
  'gate actually evaluated the wave — a wave can be READY yet never gate-checked. UNEVALUATED ' +
  'means no gate has run on this wave yet, so there is no GO/BLOCKED to show (we never ' +
  'fabricate one). Run a dry-run or a schema readiness check to earn a verdict.';

/** T2: the readiness board — client-side join of wave_plan_status × wave_gate_latest. No job. */
function ScopePreview({ onCheckReadiness }: { onCheckReadiness: (schema: string) => void }) {
  const planQ = useMigrationQuery('wave_plan_status', {});
  const gateQ = useMigrationQuery('wave_gate_latest', {});

  type PlanRow = NonNullable<typeof planQ.data>[number];
  type GateRow = NonNullable<typeof gateQ.data>[number];

  const gateByWave = new Map<string, GateRow>();
  for (const g of gateQ.data ?? []) gateByWave.set(g.wave_id, g);

  const columns: Column<PlanRow>[] = [
    {
      key: 'wave',
      header: 'Wave',
      render: (r) => (
        <div className="flex flex-col">
          <span className="text-sm font-medium text-foreground">{r.wave_label}</span>
          <span className="font-mono text-[10px] text-muted-foreground">{r.wave_id}</span>
        </div>
      ),
    },
    {
      key: 'tables',
      header: 'Tables',
      className: 'tabular-nums',
      render: (r) => r.total_tables,
    },
    {
      key: 'state',
      header: 'Plan state',
      render: (r) => <Badge variant="outline">{r.wave_state}</Badge>,
    },
    {
      key: 'verdict',
      header: 'Gate verdict',
      render: (r) => {
        const cell = verdictCellModel(r, gateByWave.get(r.wave_id));
        if (cell.kind === 'unevaluated') {
          return (
            <div className="flex flex-col items-start gap-0.5">
              <span className="inline-flex items-center gap-1">
                <Badge variant="secondary">UNEVALUATED</Badge>
                <InfoHint text={UNEVALUATED_HINT} label="What UNEVALUATED means" />
              </span>
              {cell.canCheckSchema && cell.schema ? (
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0 text-[11px]"
                  onClick={() => onCheckReadiness(cell.schema as string)}
                >
                  Check readiness…
                </Button>
              ) : null}
            </div>
          );
        }
        return (
          <div className="flex flex-col gap-0.5">
            <Badge
              className={
                cell.tone === 'success'
                  ? 'w-fit border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]'
                  : undefined
              }
              variant={cell.tone === 'success' ? 'outline' : 'destructive'}
            >
              {cell.verdict}
            </Badge>
            {cell.ageLabel ? (
              <span className="text-[11px] text-muted-foreground">
                evaluated {cell.ageLabel}
                {cell.stale ? ' · may be stale' : ''}
              </span>
            ) : cell.stale ? (
              <span className="text-[11px] text-muted-foreground">time unknown · may be stale</span>
            ) : null}
            {cell.reason ? (
              <span className="text-[11px] text-muted-foreground">{cell.reason}</span>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <Card data-tour="convert-preview">
      <SectionHeader copy={SECTION_COPY.convert.preview} hintCard={CARD_HINTS.scopePreview} />
      <CardContent>
        <DataSurface
          result={planQ}
          emptyTitle="No plan yet"
          emptyDescription="Run the wave planner to populate waves, then evaluate their gates."
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              getRowKey={(r) => r.wave_id}
              caption={`${rows.length} wave${rows.length === 1 ? '' : 's'} in the current plan`}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/** The last GO / NO-GO the readiness check wrote for the typed schema — honest UNEVALUATED
 *  (no verdict) when none has run. Never fabricates a verdict. */
function CampaignVerdictLine({
  gateRows,
  schema,
  planRows,
}: {
  gateRows: CampaignGateRowLike[];
  schema: string;
  planRows: SchemaPlanRowLike[];
}) {
  if (schema === '') return null;
  const v = latestCampaignVerdict(gateRows, schema);
  if (!v) {
    return (
      <p className="text-xs text-muted-foreground">
        No readiness check has run for <ScopeName groupKey={schema} planRows={planRows} /> yet — its
        verdict is <strong>UNEVALUATED</strong>. Run the check above to earn a GO / NO-GO.
      </p>
    );
  }
  const stale = isCoverageStale(v.evaluatedAt);
  return (
    <div className="flex flex-col gap-1 text-xs">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">Last verdict for</span>
        <ScopeName groupKey={schema} planRows={planRows} />
        <Badge
          className={
            v.passed
              ? 'w-fit border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]'
              : undefined
          }
          variant={v.passed ? 'outline' : 'destructive'}
        >
          {v.verdict}
        </Badge>
        <span className="text-muted-foreground">
          evaluated {formatAge(v.evaluatedAt)}
          {stale ? ' · may be stale' : ''}
        </span>
        {stale ? (
          <InfoHint text="This verdict is older than the freshness window — a table's state may have changed since. Re-run the readiness check to refresh it." />
        ) : null}
      </div>
      {v.reason ? <span className="text-muted-foreground">{v.reason}</span> : null}
    </div>
  );
}

/**
 * Advisory readiness strip for a SCHEMA-scoped convert. Reflects the last campaign verdict and
 * nudges "run the readiness check first" when it is UNEVALUATED or stale. It never disables the
 * Convert button (SR1/SR8) — it only reflects a control-table verdict (SR13). Rendered only for
 * a schema scope; wave scopes get per-wave provenance inside the gate dialog instead.
 */
function ScopeReadinessNudge({
  schema,
  readiness,
  onCheckReadiness,
  planRows,
}: {
  schema: string;
  readiness: ScopeReadiness;
  onCheckReadiness: (schema: string) => void;
  planRows: SchemaPlanRowLike[];
}) {
  const { kind, verdict } = readiness;

  if (kind === 'go') {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] px-3 py-2 text-xs text-[var(--success-foreground)]">
        <span>
          Readiness: <strong>GO</strong> for <ScopeName groupKey={schema} planRows={planRows} />
          {verdict?.evaluatedAt ? <> · evaluated {formatAge(verdict.evaluatedAt)}</> : null}.
        </span>
      </div>
    );
  }

  if (kind === 'nogo') {
    return (
      <div className="flex flex-col gap-1 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-xs text-[var(--warning-foreground)]">
        <span>
          Last readiness check for <ScopeName groupKey={schema} planRows={planRows} /> was{' '}
          <strong>NO-GO</strong>
          {verdict?.evaluatedAt ? <> ({formatAge(verdict.evaluatedAt)})</> : null}. The gate skips
          blocked tables — convert still runs the ready ones.
          {verdict?.reason ? <> {verdict.reason}.</> : null}
        </span>
        <ReadinessCheckLink
          schema={schema}
          onCheckReadiness={onCheckReadiness}
          label="Re-check readiness…"
        />
      </div>
    );
  }

  const stale = kind === 'stale';
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-xs text-[var(--warning-foreground)]">
      <span>
        {stale ? (
          <>
            The last readiness check for <ScopeName groupKey={schema} planRows={planRows} /> may be stale
            {verdict?.evaluatedAt ? <> (evaluated {formatAge(verdict.evaluatedAt)})</> : null} —
            re-run it before converting.
          </>
        ) : (
          <>
            <strong>Best practice:</strong> run the readiness check for{' '}
            <ScopeName groupKey={schema} planRows={planRows} /> first — no GO / NO-GO has been evaluated
            yet. It changes no tables.
          </>
        )}
      </span>
      <ReadinessCheckLink
        schema={schema}
        onCheckReadiness={onCheckReadiness}
        label={stale ? 'Re-check readiness…' : 'Run readiness check first…'}
      />
    </div>
  );
}

/** The seed-and-scroll link into the campaign card above. Reuses the page's `checkReadinessFor`. */
function ReadinessCheckLink({
  schema,
  onCheckReadiness,
  label,
}: {
  schema: string;
  onCheckReadiness: (schema: string) => void;
  label: string;
}) {
  return (
    <Button
      variant="link"
      size="sm"
      className="h-auto w-fit p-0 text-[11px] text-[var(--warning-foreground)] underline"
      onClick={() => onCheckReadiness(schema)}
    >
      {label}
    </Button>
  );
}

/**
 * Render a raw `target_schema` value as a friendly, plan-resolved scope phrase — "the pilot
 * wave (cat.sales)" for the pilot scope, a mono `catalog.schema` otherwise — so no prose
 * surface implies "pilot" is a schema. The engine still receives the raw value elsewhere.
 */
function ScopeName({ groupKey, planRows }: { groupKey: string; planRows: SchemaPlanRowLike[] }) {
  const { text, mono } = scopePhrase(groupKey, planRows);
  return mono ? <span className="font-mono">{text}</span> : <span>{text}</span>;
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor} className="select-text font-mono text-[11px]">
        {label}
      </Label>
      {children}
    </div>
  );
}
