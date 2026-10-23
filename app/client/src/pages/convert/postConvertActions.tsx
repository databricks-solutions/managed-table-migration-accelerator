import { useState } from 'react';
import {
  Card,
  CardContent,
  Input,
  Label,
} from '@databricks/appkit-ui/react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { WavePicker } from '../../components/WavePicker';
import { TablePicker } from '../../components/TablePicker';
import {
  cleanupOption,
  rollbackOption,
  chooseTable,
  chooseWave,
  hasTable,
  hasWave,
  scopeLabel,
  EMPTY_ROLLBACK_SCOPE,
  type RollbackScope,
} from '../../components/maintenance.pure';
import { useNowEveryMinute } from '../../hooks/useNowEveryMinute';

/**
 * Post-convert direct-mutation actions for the expert surface: the two irreversible triggers
 * that act on already-converted tables — rollback (UNSET MANAGED, within the 14-day window) and
 * cleanup (day-14 VACUUM). They live on Convert (expert), next to convert-by-scope, so the
 * read-only Runs & history page hosts no mutating triggers. Wiring, params, and guards are
 * unchanged from their prior home (see design-delta M1).
 */

/**
 * Post-convert cleanup — a day-14 VACUUM on ONE validated table. The operator picks from the
 * `cleanup_candidates` list (VALIDATED tables already past the 14-day window), never types a
 * full_name: a blank would be an estate-wide VACUUM, so the trigger is unconstructable without
 * a picked table. Irreversible — it ends rollback viability.
 */
export function CleanupCard() {
  const now = useNowEveryMinute();
  const q = useMigrationQuery('cleanup_candidates', {});
  const options = (q.data ?? []).map((r) => cleanupOption(r, now));
  const [table, setTable] = useState('');

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.convert.cleanup} />
      <CardContent className="flex max-w-xl flex-col gap-2">
        <Label htmlFor="cleanup-table">Table</Label>
        <TablePicker
          id="cleanup-table"
          value={table}
          onSelect={(o) => setTable(o?.full_name ?? '')}
          options={options}
          loading={q.loading}
          error={q.error !== null}
          placeholder="Search validated tables past the window…"
          emptyLabel="No tables past the 14-day window yet"
        />
        <JobTriggerDialog
          alias="cleanup"
          {...dialogProps(DIALOG_COPY.cleanup)}
          triggerVariant="destructive"
          triggerDisabled={table === ''}
          disabledReason="Pick a table first"
          affected={{ count: 1, scope: table, names: table ? [table] : [] }}
          irreversible
          params={{ full_name: table }}
          confirmVariant="destructive"
        />
      </CardContent>
    </Card>
  );
}

/**
 * Rollback — UNSET MANAGED for one table OR one wave, within the 14-day window. Scope is
 * mutually exclusive: picking a table clears/greys the wave picker and vice-versa (the
 * `maintenance.pure` reducer enforces the XOR), so the scope is never ambiguous. Table options
 * come from `rollback_candidates` (CONVERTED|VALIDATED with a success audit), annotated with
 * days-left; the wave picker sets wave_id AND plan_run_id from one row so the pair can't be
 * mismatched. A reason is required — that, plus type-to-confirm, is the deliberate guard.
 */
export function RollbackCard() {
  const now = useNowEveryMinute();
  const q = useMigrationQuery('rollback_candidates', {});
  const options = (q.data ?? []).map((r) => rollbackOption(r, now));
  const [reason, setReason] = useState('');
  const [scope, setScope] = useState<RollbackScope>(EMPTY_ROLLBACK_SCOPE);

  const label = scopeLabel(scope);
  const reasonOk = reason.trim() !== '';
  const confirmValue = hasTable(scope)
    ? scope.fullName
    : hasWave(scope)
      ? scope.waveId
      : undefined;

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.convert.rollback} />
      <CardContent className="flex max-w-xl flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rb-reason">Reason</Label>
          <Input
            id="rb-reason"
            value={reason}
            placeholder="Why are you rolling back?"
            onChange={(e) => setReason(e.target.value)}
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <Label htmlFor="rb-table">Scope — one table or one wave</Label>
            {label ? (
              <button
                type="button"
                className="text-xs font-medium text-muted-foreground underline underline-offset-2"
                onClick={() => setScope(EMPTY_ROLLBACK_SCOPE)}
              >
                clear
              </button>
            ) : null}
          </div>
          <TablePicker
            id="rb-table"
            value={scope.fullName}
            onSelect={(o) => setScope(chooseTable(o?.full_name ?? null))}
            options={options}
            loading={q.loading}
            error={q.error !== null}
            placeholder="Search tables in the rollback window…"
            emptyLabel="No tables in the rollback window"
            disabled={hasWave(scope)}
            disabledHint="Clear the wave to pick a table"
          />
          {/* Pick the wave rather than typing it — sets wave_id AND plan_run_id from the same
              row, so the correlated pair can never be mismatched. */}
          <WavePicker
            value={scope.waveId}
            placeholder="…or pick a wave"
            onSelect={(o) => setScope(chooseWave(o))}
            disabled={hasTable(scope)}
            disabledHint="Clear the table to pick a wave"
          />
        </div>

        <JobTriggerDialog
          alias="rollback"
          {...dialogProps(DIALOG_COPY.rollback)}
          triggerVariant="destructive"
          triggerDisabled={!reasonOk || label === ''}
          disabledReason={!reasonOk ? 'A reason is required to roll back' : 'Pick a table or a wave'}
          affected={label ? { count: 1, scope: label, names: hasTable(scope) ? [scope.fullName] : [] } : undefined}
          confirmText={confirmValue}
          irreversible
          params={{
            reason: reason.trim(),
            full_name: scope.fullName,
            wave_id: scope.waveId,
            plan_run_id: scope.planRunId,
          }}
          confirmVariant="destructive"
        />
      </CardContent>
    </Card>
  );
}
