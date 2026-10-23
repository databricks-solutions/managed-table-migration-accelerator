import { Badge, Card, CardContent } from '@databricks/appkit-ui/react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY } from '../../lib/sectionCopy';
import { PagedTable, type Column } from '../../components/PagedTable';
import { EstateSubline } from '../../components/EstateSubline';
import { WriterCoveragePanel, type GateRows } from '../../components/WriterCoveragePanel';
import { stalenessLabel } from './coverage.pure';

/**
 * Coverage — honest, per-workspace estate coverage. Cards: stale manifest rows (not
 * re-seen by this workspace's latest covering profiler run), coverage by workspace
 * (coverage_report), and blind spots (coverage_matrix). Read-only; the backend views
 * emit stale rows only and never delete — absence of a recent sighting is not proof a
 * table is gone.
 */
export function CoveragePage() {
  const staleQ = useMigrationQuery('stale_manifest', {});
  type StaleRow = NonNullable<typeof staleQ.data>[number];

  const reconciledQ = useMigrationQuery('reconciled_manifest', {});
  type ReconciledRow = NonNullable<typeof reconciledQ.data>[number];

  const coverageQ = useMigrationQuery('coverage_report', {});
  const blindQ = useMigrationQuery('coverage_matrix_blind_spots', {});
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const gateRows = (gateQ.data ?? []) as GateRows;
  type CoverageRow = NonNullable<typeof coverageQ.data>[number];
  type BlindRow = NonNullable<typeof blindQ.data>[number];

  const staleColumns: Column<StaleRow>[] = [
    {
      key: 'full_name',
      header: 'Table',
      className: 'font-mono text-[12px]',
      render: (r) => r.full_name,
      copyValue: (r) => r.full_name,
    },
    {
      key: 'workspace_id',
      header: 'Workspace',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.workspace_id ?? '—',
    },
    {
      key: 'last_seen_at',
      header: 'Last seen',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.last_seen_at ?? 'never',
    },
    {
      key: 'staleness',
      header: 'Stale for',
      render: (r) => <Badge variant="outline">{stalenessLabel(r.staleness_days ?? null)}</Badge>,
    },
    {
      key: 'state',
      header: 'State',
      className: 'text-sm text-muted-foreground',
      render: (r) => r.state ?? '—',
    },
  ];

  const reconciledColumns: Column<ReconciledRow>[] = [
    {
      key: 'full_name',
      header: 'Table',
      className: 'font-mono text-[12px]',
      render: (r) => r.full_name,
      copyValue: (r) => r.full_name,
    },
    {
      key: 'disposition',
      header: 'Reconciled as',
      render: (r) => (
        <Badge variant={r.disposition === 'dropped' ? 'outline' : 'secondary'}>
          {r.disposition === 'converted_external'
            ? 'Converted outside app'
            : r.disposition === 'dropped'
              ? 'Dropped'
              : (r.disposition ?? '—')}
        </Badge>
      ),
    },
    {
      key: 'state',
      header: 'State',
      className: 'text-sm text-muted-foreground',
      render: (r) => r.state ?? '—',
    },
    {
      key: 'workspace_id',
      header: 'Workspace',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.workspace_id ?? '—',
    },
    {
      key: 'reconciled_at',
      header: 'Reconciled',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.reconciled_at ?? '—',
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <SectionHeader copy={SECTION_COPY.coverage.reconciled} />
        <CardContent>
          <DataSurface
            result={reconciledQ}
            emptyTitle="Nothing reconciled"
            emptyDescription="No table was converted, rolled back, or dropped outside this app since the last profiler run with reconcile enabled."
          >
            {(rows) => (
              <PagedTable
                rows={rows}
                columns={reconciledColumns}
                getRowKey={(r) => r.full_name}
                caption={`${rows.length} reconciled table${rows.length === 1 ? '' : 's'}`}
              />
            )}
          </DataSurface>
        </CardContent>
      </Card>

      <Card>
        <SectionHeader copy={SECTION_COPY.coverage.stale} />
        <CardContent>
          <DataSurface
            result={staleQ}
            emptyTitle="Nothing looks stale"
            emptyDescription="Every in-scope manifest row was re-seen by its workspace’s latest covering run (or no covering run exists yet, in which case staleness is not judged)."
          >
            {(rows) => (
              <PagedTable
                rows={rows}
                columns={staleColumns}
                getRowKey={(r) => r.full_name}
                caption={`${rows.length} stale row${rows.length === 1 ? '' : 's'}`}
              />
            )}
          </DataSurface>
        </CardContent>
      </Card>

      <Card>
        <SectionHeader copy={SECTION_COPY.coverage.byWorkspace}>
          {/* The estate-wide denominator (same shared line as Overview/Waves) sits next to the
              per-workspace, per-run discovery counts so "N planned of M discovered · K held back"
              reconciles with the "Tables (latest run)" column below. */}
          <EstateSubline />
        </SectionHeader>
        <CardContent>
          <DataSurface
            result={coverageQ}
            emptyTitle="No profiler runs recorded yet"
            emptyDescription="Run the profiler. Each run adds a row here."
          >
            {(rows) => (
              <div className="flex flex-col gap-3">
                <PagedTable
                  rows={rows}
                  columns={
                    [
                      { key: 'workspace_id', header: 'Workspace', className: 'font-mono text-[11px]', render: (r: CoverageRow) => r.workspace_id ?? '—' },
                      { key: 'visible', header: 'Catalogs visible', render: (r: CoverageRow) => r.catalogs_visible ?? '—' },
                      { key: 'scanned', header: 'Scanned', render: (r: CoverageRow) => r.catalogs_scanned },
                      { key: 'unscanned', header: 'Gap', render: (r: CoverageRow) => r.catalogs_unscanned ?? '—' },
                      { key: 'tables', header: 'Tables (latest run)', render: (r: CoverageRow) => r.tables_discovered_latest_run },
                      { key: 'finished_at', header: 'Latest run', className: 'font-mono text-[11px] text-muted-foreground', render: (r: CoverageRow) => r.finished_at },
                    ] as Column<CoverageRow>[]
                  }
                  getRowKey={(r) => r.latest_run_id}
                  caption={`${rows.length} workspace${rows.length === 1 ? '' : 's'}`}
                />
                {rows[0]?.coverage_disclaimer ? (
                  <p className="text-xs text-muted-foreground">{rows[0].coverage_disclaimer}</p>
                ) : null}
              </div>
            )}
          </DataSurface>
        </CardContent>
      </Card>

      <Card>
        <SectionHeader copy={SECTION_COPY.coverage.blindSpots} />
        <CardContent>
          <DataSurface
            result={blindQ}
            emptyTitle="No blind spots detected"
            emptyDescription="Either every bound catalog is covered, or the bindings scan hasn’t run in this workspace."
          >
            {(rows) => (
              <PagedTable
                rows={rows}
                columns={
                  [
                    { key: 'catalog', header: 'Catalog', className: 'font-mono text-[12px]', render: (r: BlindRow) => r.catalog },
                    { key: 'binding_type', header: 'Binding', render: (r: BlindRow) => r.binding_type },
                    { key: 'workspace_id', header: 'Workspace', className: 'font-mono text-[11px]', render: (r: BlindRow) => r.workspace_id ?? '—' },
                    { key: 'computed_at', header: 'Computed', className: 'font-mono text-[11px] text-muted-foreground', render: (r: BlindRow) => r.computed_at },
                  ] as Column<BlindRow>[]
                }
                getRowKey={(r) => `${r.catalog}:${r.workspace_id ?? ''}`}
                caption={`${rows.length} blind spot${rows.length === 1 ? '' : 's'}`}
              />
            )}
          </DataSurface>
        </CardContent>
      </Card>

      <Card>
        <SectionHeader copy={SECTION_COPY.coverage.writerStops} />
        <CardContent>
          <WriterCoveragePanel gateRows={gateRows} />
        </CardContent>
      </Card>
    </div>
  );
}
