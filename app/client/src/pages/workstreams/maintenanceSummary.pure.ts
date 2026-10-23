/**
 * maintenanceSummary.pure — turns the filtered POST_CONVERT maintenance action rows into per-kind
 * counts for the Maintenance workstream's progress line. Pure and deterministic (no
 * React, no I/O). Only the three maintenance kinds count toward the total; any other kind is
 * ignored so a mis-scoped caller can't inflate the number.
 */

export interface MaintenanceSummary {
  /** RETIRE_MAINTENANCE_JOB — redundant scheduled OPTIMIZE or VACUUM job left paused, recommend retiring. */
  retire: number;
  /** ENABLE_PREDICTIVE_OPTIMIZATION — PO was off; operator opt-in to turn it on. */
  enablePo: number;
  /** ENABLE_AUTO_LIQUID — CLUSTER BY AUTO on eligible converted tables. */
  autoLiquid: number;
  /** Total pending maintenance recommendations (the three kinds only). */
  total: number;
}

export function summarizeMaintenance(
  rows: readonly { action_kind: string }[],
): MaintenanceSummary {
  let retire = 0;
  let enablePo = 0;
  let autoLiquid = 0;
  for (const r of rows) {
    if (r.action_kind === 'RETIRE_MAINTENANCE_JOB') retire += 1;
    else if (r.action_kind === 'ENABLE_PREDICTIVE_OPTIMIZATION') enablePo += 1;
    else if (r.action_kind === 'ENABLE_AUTO_LIQUID') autoLiquid += 1;
  }
  return { retire, enablePo, autoLiquid, total: retire + enablePo + autoLiquid };
}
