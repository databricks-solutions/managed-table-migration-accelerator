import { Badge, Card, CardContent } from '@databricks/appkit-ui/react';
import { sql } from '@databricks/appkit-ui/js';
import { useSearchParams } from 'react-router';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY, fillCopy } from '../../lib/sectionCopy';
import { PagedTable, type Column } from '../../components/PagedTable';
import { Stepper, type StepperStep } from '../../components/Stepper';
import {
  parseScopeSelector,
  freezeDowntimeSummary,
  whatPausesGroups,
  whatPausesItemLabel,
  willPauseAffected,
  headsUpNotes,
  freezePlanRowPill,
  type FreezePillTone,
} from '../overview/freezeCard.pure';

/**
 * Freeze page = PLANNED (what job_03c will pause for a scope, from freeze_plan_for_scope) when a
 * `?scope=catalog.schema` is present — the "Review freeze plan" target from the guided freeze card
 * — ABOVE the APPLIED freeze ledger ("Paused & stopped", freeze_ledger_open). Read-only.
 */
export function FreezePage() {
  const [searchParams] = useSearchParams();
  const scope = parseScopeSelector(searchParams.get('scope'));
  return (
    <div className="flex flex-col gap-6">
      {scope.ok ? (
        <PlannedFreezeSection scopeCatalog={scope.scopeCatalog} scopeSchema={scope.scopeSchema} label={scope.label} />
      ) : null}
      <AppliedFreezeLedger />
    </div>
  );
}

const _PILL_CLASS: Record<FreezePillTone, string> = {
  safe: 'border-[var(--success-border)] text-[var(--success-foreground)]',
  downtime: 'border-[var(--warning-border)] text-[var(--warning-foreground)]',
  wontRun: 'border-border text-muted-foreground',
  manual: 'border-[var(--warning-border)] text-[var(--warning-foreground)]',
  unknown: 'border-[var(--warning-border)] text-[var(--warning-foreground)]',
};

/**
 * PLANNED — the freeze plan for one catalog.schema (what will pause, before you freeze). Always
 * calls the scoped query (rules-of-hooks); FreezePage decides whether to mount it. Reuses the same
 * pure reducers as the guided freeze card, plus a per-row safety pill.
 */
function PlannedFreezeSection({
  scopeCatalog,
  scopeSchema,
  label,
}: {
  scopeCatalog: string;
  scopeSchema: string;
  label: string;
}) {
  const plan = useMigrationQuery('freeze_plan_for_scope', {
    scope_catalog: sql.string(scopeCatalog),
    scope_schema: sql.string(scopeSchema),
  });
  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.freeze.plan}
        subtitle={fillCopy(SECTION_COPY.freeze.plan.subtitle, { scope: label })}
      />
      <CardContent>
        <DataSurface
          result={plan}
          emptyTitle={`No attributed entities to pause for ${label}`}
          emptyDescription="Nothing recurring is attributed to this scope — freezing is a no-op here (or attribution hasn't landed yet)."
        >
          {(rows) => {
            const summary = freezeDowntimeSummary(rows, null);
            const groups = whatPausesGroups(rows);
            const affected = willPauseAffected(rows);
            const heads = headsUpNotes(rows);
            return (
              <div className="flex flex-col gap-4">
                <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-sm text-[var(--warning-foreground)]">
                  <span className="font-semibold">Downtime read</span> —{' '}
                  <span className="font-semibold">{summary.nSafe + summary.nWontRun} safe</span> ·{' '}
                  <span className="font-semibold">{affected.count} auto-paused</span>
                  {summary.nHeadsUp > 0 ? (
                    <>
                      {' '}· <span className="font-semibold">{summary.nHeadsUp} need a heads-up</span> —{' '}
                      {heads.join(' and ')} run continuously (stopped until unfreeze). Tell their consumers.
                    </>
                  ) : summary.nUnknown === 0 ? (
                    <> · nothing runs continuously in this scope.</>
                  ) : null}
                  {summary.nUnknown > 0 ? (
                    <>
                      {' '}· <span className="font-semibold">{summary.nUnknown} unclassified</span> — review these by hand. They are not assumed safe.
                    </>
                  ) : null}
                </div>

                <div className="flex flex-col gap-3">
                  {groups.map((g) => (
                    <div key={g.kind}>
                      <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                        {g.kind}
                      </div>
                      <ul className="mt-1 flex flex-col gap-1">
                        {g.items.map((it) => {
                          const pill = freezePlanRowPill(it.safety_class);
                          return (
                            <li
                              key={`${g.kind}:${it.full_name}:${it.entity_id ?? it.entity_name}`}
                              className="flex flex-wrap items-center gap-2 text-sm"
                            >
                              <Badge variant="outline" className={_PILL_CLASS[pill.tone]}>
                                {pill.label}
                              </Badge>
                              <span className="font-mono text-[12px]">{whatPausesItemLabel(it)}</span>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/**
 * APPLIED — the freeze ledger: streams/writers paused around a conversion, and the bracket they
 * sit inside (Freeze applied → Convert → Unfreeze + verify). Open ledger rows mean we're
 * mid-bracket, so the tracker shows convert as the active step. Read-only.
 */
function AppliedFreezeLedger() {
  const q = useMigrationQuery('freeze_ledger_open', {});
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    {
      key: 'target',
      header: 'Target',
      className: 'font-mono text-[12px]',
      render: (r) => r.full_name || r.target_id,
    },
    {
      key: 'target_kind',
      header: 'Kind',
      className: 'text-sm text-muted-foreground',
      render: (r) => r.target_kind,
    },
    {
      key: 'op_kind',
      header: 'Operation',
      render: (r) => <span className="text-sm">{r.op_kind}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      render: (r) => <Badge variant="outline">{r.status}</Badge>,
    },
    {
      key: 'prior',
      header: 'Prior state',
      className: 'text-sm text-muted-foreground',
      render: (r) => r.prior_pause_status,
    },
    {
      key: 'frozen_at',
      header: 'Frozen at',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.frozen_at,
    },
  ];

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.freeze.paused} />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="Nothing is paused or stopped right now"
          emptyDescription="When a wave freezes its streams/writers to convert, the paused entities and their bracket show here."
        >
          {(rows) => {
            const bracket: StepperStep[] = [
              { id: 'freeze', label: 'Freeze applied', status: 'done', sublabel: `${rows.length} open` },
              { id: 'convert', label: 'Convert', status: 'active' },
              { id: 'unfreeze', label: 'Unfreeze + verify', status: 'pending' },
            ];
            return (
              <div className="flex flex-col gap-5">
                <Stepper variant="bracket" steps={bracket} />
                <PagedTable
                  rows={rows}
                  columns={columns}
                  getRowKey={(r) => r.ledger_key}
                  caption={`${rows.length} open freeze record${rows.length === 1 ? '' : 's'}`}
                />
              </div>
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}
