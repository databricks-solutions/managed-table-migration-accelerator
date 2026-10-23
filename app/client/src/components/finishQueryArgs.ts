import { sql } from '@databricks/appkit-ui/js';
import type { FinishScope } from '../lib/finishScope.pure';

/** The action_queue_{open,deferred}_for_wave args for a finish scope. AppKit applies no @param
 *  defaults at runtime, so every param (batch_id included) is bound, '' meaning "no filter". */
export function finishQueryArgs(scope: FinishScope) {
  return {
    scope_catalog: sql.string(scope.query.scope_catalog),
    scope_schema: sql.string(scope.query.scope_schema),
    plan_run_id: sql.string(scope.query.plan_run_id),
    wave_id: sql.string(scope.query.wave_id),
    batch_id: sql.string(scope.query.batch_id),
  };
}
