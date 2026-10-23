import { Alert, AlertDescription, AlertTitle } from '@databricks/appkit-ui/react';
import { Wrench } from 'lucide-react';
import { JobTriggerDialog } from './JobTriggerDialog';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../lib/sectionCopy';

/**
 * ControlPlaneStaleCard — the actionable self-heal surface for SCHEMA_STALE.
 *
 * When the SP readiness probe positively classifies a control-read failure as a MISSING COLUMN
 * (the table exists, but an app update shipped a column the control plane hasn't reconciled yet),
 * we render this instead of a dead-end red error. The one-click action triggers the conductor
 * (`job_08_conductor`, already bound as the `conductor` alias): it is READ-ONLY — it never runs
 * SET MANAGED — and its `ensure_next_actions_table` does the `ALTER TABLE … ADD COLUMNS` and writes
 * a fresh row. ActiveRunsProvider re-reads every screen when the run finishes, so this card clears
 * on its own once the conductor completes (no page reload).
 *
 * This is the read-path complement to the guard that classifies the skew.
 */
export function ControlPlaneStaleCard() {
  return (
    <Alert>
      <Wrench className="size-4" aria-hidden />
      <AlertTitle>{SECTION_COPY.shared.controlPlaneStale.title}</AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-3">
        <span>
          {SECTION_COPY.shared.controlPlaneStale.subtitle} {SECTION_COPY.shared.controlPlaneStale.detail}
        </span>
        <JobTriggerDialog alias="conductor" triggerSize="sm" {...dialogProps(DIALOG_COPY.refreshControlPlane)} />
      </AlertDescription>
    </Alert>
  );
}
