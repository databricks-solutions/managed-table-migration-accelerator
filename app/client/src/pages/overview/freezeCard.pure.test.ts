import { describe, expect, it } from 'vitest';
import {
  freezeCardPosture,
  freezeCtaModel,
  freezeDowntimeSummary,
  freezePlanHref,
  freezePlanRowPill,
  headsUpNotes,
  parseScopeSelector,
  whatPausesGroups,
  whatPausesItemLabel,
  whatPausesKindLabel,
  willPauseAffected,
  type FreezePlanRow,
} from './freezeCard.pure';

const rows: FreezePlanRow[] = [
  { full_name: 'c.s.t1', entity_kind: 'COMPACTION_JOB', entity_id: '11', entity_name: 'nightly_optimize', op_kind: 'PAUSE_JOB', next_scheduled_run_at: '2026-09-22 02:00', safety_class: 'SAFE_WINDOW' },
  { full_name: 'c.s', entity_kind: 'PO', entity_id: null, entity_name: 'Predictive Optimization', op_kind: 'DISABLE_PO', next_scheduled_run_at: null, safety_class: 'WONT_RUN' },
  { full_name: 'c.s.t2', entity_kind: 'PIPELINE', entity_id: 'p1', entity_name: 'orders_dlt', op_kind: 'STOP_PIPELINE', next_scheduled_run_at: null, safety_class: 'DOWNTIME_UNTIL_UNFREEZE' },
  { full_name: 'c.s.t3', entity_kind: 'STREAM', entity_id: 'j17', entity_name: 'stream_writer_job_17', op_kind: 'STOP_STREAM_JOB', next_scheduled_run_at: null, safety_class: 'DOWNTIME_UNTIL_UNFREEZE' },
  { full_name: 'c.s.t4', entity_kind: 'MANUAL_COMPACTION', entity_id: null, entity_name: 'adhoc_nb', op_kind: null, next_scheduled_run_at: null, safety_class: 'MANUAL' },
  { full_name: 'c.s.t5', entity_kind: 'WRITER_JOB', entity_id: 'j21', entity_name: 'nightly_load', op_kind: 'PAUSE_WRITER_JOB', next_scheduled_run_at: '2026-09-22 03:00', safety_class: 'SAFE_WINDOW' },
  { full_name: 'c.s.t6', entity_kind: 'WRITER_JOB', entity_id: 'j22', entity_name: 'events_ingest', op_kind: 'PAUSE_WRITER_JOB', next_scheduled_run_at: null, safety_class: 'DOWNTIME_UNTIL_UNFREEZE' },
  { full_name: 'c.s.t7', entity_kind: 'ADHOC_WRITER', entity_id: null, entity_name: 'analyst_notebook', op_kind: null, next_scheduled_run_at: null, safety_class: 'MANUAL' },
  { full_name: 'c.s.t8', entity_kind: 'UNKNOWN_COMPACTION', entity_id: null, entity_name: 'mystery_actor', op_kind: null, next_scheduled_run_at: null, safety_class: 'UNKNOWN' },
];

describe('parseScopeSelector', () => {
  it('splits catalog.schema', () => {
    expect(parseScopeSelector('cat.sch')).toEqual({ ok: true, scopeCatalog: 'cat', scopeSchema: 'sch', label: 'cat.sch' });
  });
  it('rejects non-schema selectors', () => {
    expect(parseScopeSelector('bucket:streaming').ok).toBe(false);
    expect(parseScopeSelector(null).ok).toBe(false);
    expect(parseScopeSelector('cat').ok).toBe(false);
  });
});

describe('freezeDowntimeSummary', () => {
  it('counts safe / wont-run / heads-up / manual / unknown and lists names to warn', () => {
    const s = freezeDowntimeSummary(rows, 15);
    expect(s.windowMin).toBe(15);
    expect(s.nSafe).toBe(2); // COMPACTION_JOB + WRITER_JOB scheduled
    expect(s.nWontRun).toBe(1); // PO
    expect(s.nHeadsUp).toBe(3); // pipeline + stream + WRITER_JOB event
    expect(s.nManual).toBe(2); // MANUAL_COMPACTION + ADHOC_WRITER
    expect(s.nUnknown).toBe(1); // UNKNOWN_COMPACTION
    expect(s.headsUpNames).toEqual(['orders_dlt', 'stream_writer_job_17', 'events_ingest']);
  });
});

describe('whatPausesGroups', () => {
  it('orders compaction → PO → pipelines → streams → writer jobs → manual → unclassified, dropping empty groups', () => {
    expect(whatPausesGroups(rows).map((g) => g.kind)).toEqual([
      'Maintenance jobs (OPTIMIZE / VACUUM)',
      'Predictive Optimization',
      'DLT pipelines',
      'Structured Streaming',
      'Writer jobs (scheduled / event-triggered)',
      "Manual — stop these yourself (freeze can't pause a person)",
      'Unclassified (review manually)',
    ]);
  });

  it('puts ad-hoc writers under the manual group and unknown under "Unclassified"', () => {
    const groups = whatPausesGroups(rows);
    const manual = groups.find((g) => g.kind === "Manual — stop these yourself (freeze can't pause a person)");
    const unknown = groups.find((g) => g.kind === 'Unclassified (review manually)');
    expect(manual?.items.map((i) => i.entity_name)).toEqual(['adhoc_nb', 'analyst_notebook']);
    expect(unknown?.items.map((i) => i.entity_name)).toEqual(['mystery_actor']);
  });

  it('places each matching row into exactly one group', () => {
    const key = (r: FreezePlanRow) => `${r.full_name}|${r.entity_kind}|${r.entity_name ?? ''}`;
    const groupedKeys = whatPausesGroups(rows).flatMap((g) => g.items.map(key));
    expect(groupedKeys).toHaveLength(rows.length);
    expect(new Set(groupedKeys).size).toBe(rows.length);
    expect(new Set(groupedKeys)).toEqual(new Set(rows.map(key)));
  });

  it('uses first-match-wins when a known entity kind has UNKNOWN safety', () => {
    const ambiguous: FreezePlanRow = {
      full_name: 'c.s.t9',
      entity_kind: 'WRITER_JOB',
      entity_id: 'j99',
      entity_name: 'unknown_writer_job',
      op_kind: 'PAUSE_WRITER_JOB',
      next_scheduled_run_at: null,
      safety_class: 'UNKNOWN',
    };

    const groups = whatPausesGroups([ambiguous]);
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe('Writer jobs (scheduled / event-triggered)');
    expect(groups[0].items).toEqual([ambiguous]);
  });
});

describe('whatPausesKindLabel', () => {
  it('maps each entity_kind to its operation-family label', () => {
    expect(whatPausesKindLabel('PO')).toBe('Predictive Optimization');
    expect(whatPausesKindLabel('COMPACTION_JOB')).toBe('Maintenance job (OPTIMIZE/VACUUM)');
    expect(whatPausesKindLabel('MANUAL_COMPACTION')).toBe('Ad-hoc OPTIMIZE/compaction');
    expect(whatPausesKindLabel('UNKNOWN_COMPACTION')).toBe('Compaction (unclassified)');
    expect(whatPausesKindLabel('PIPELINE')).toBe('DLT pipeline');
    expect(whatPausesKindLabel('STREAM')).toBe('Streaming job');
    expect(whatPausesKindLabel('WRITER_JOB')).toBe('Writer job');
    expect(whatPausesKindLabel('ADHOC_WRITER')).toBe('Ad-hoc writer');
  });

  it('is case-insensitive and falls back to the raw value (never blank)', () => {
    expect(whatPausesKindLabel('manual_compaction')).toBe('Ad-hoc OPTIMIZE/compaction');
    expect(whatPausesKindLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
    expect(whatPausesKindLabel('')).toBe('Attributed writer');
    expect(whatPausesKindLabel(null)).toBe('Attributed writer');
  });

  it('names the maintenance class a COMPACTION_JOB runs', () => {
    expect(whatPausesKindLabel('COMPACTION_JOB', 'COMPACTION')).toBe('OPTIMIZE job');
    expect(whatPausesKindLabel('COMPACTION_JOB', 'VACUUM')).toBe('VACUUM job');
    expect(whatPausesKindLabel('COMPACTION_JOB', 'COMPACTION,VACUUM')).toBe('OPTIMIZE + VACUUM job');
    expect(whatPausesKindLabel('COMPACTION_JOB', ' vacuum ')).toBe('VACUUM job');
    expect(whatPausesKindLabel('COMPACTION_JOB', null)).toBe('Maintenance job (OPTIMIZE/VACUUM)');
  });

  it('names manual VACUUM runs', () => {
    expect(whatPausesKindLabel('MANUAL_COMPACTION', 'VACUUM')).toBe('Ad-hoc VACUUM');
    expect(whatPausesKindLabel('MANUAL_COMPACTION', 'COMPACTION,VACUUM')).toBe('Ad-hoc OPTIMIZE + VACUUM');
    expect(whatPausesKindLabel('MANUAL_COMPACTION')).toBe('Ad-hoc OPTIMIZE/compaction');
  });

  it('names the classes of an unclassified actor', () => {
    expect(whatPausesKindLabel('UNKNOWN_COMPACTION', 'COMPACTION,VACUUM')).toBe('OPTIMIZE + VACUUM (unclassified)');
    expect(whatPausesKindLabel('UNKNOWN_COMPACTION', 'VACUUM')).toBe('VACUUM (unclassified)');
    expect(whatPausesKindLabel('UNKNOWN_COMPACTION', 'COMPACTION')).toBe('Compaction (unclassified)');
    expect(whatPausesKindLabel('UNKNOWN_COMPACTION')).toBe('Compaction (unclassified)');
  });
});

describe('whatPausesItemLabel', () => {
  it('leads with the operation + table and attributes a human as "last run by …" (the manual fix)', () => {
    const manual: FreezePlanRow = {
      full_name: 'serverless_cat.e2e_real_silver.orders',
      entity_kind: 'MANUAL_COMPACTION',
      entity_id: null,
      entity_name: 'owner@example.com',
      op_kind: null,
      next_scheduled_run_at: null,
      safety_class: 'MANUAL',
    };
    // The bug: this used to render as just the email. Now it names the operation AND the table,
    // and never hides full_name.
    expect(whatPausesItemLabel(manual)).toBe(
      'Ad-hoc OPTIMIZE/compaction · serverless_cat.e2e_real_silver.orders — last run by owner@example.com',
    );
  });

  it('shows the table even when the actor identity is missing', () => {
    const noActor: FreezePlanRow = {
      full_name: 'c.s.t',
      entity_kind: 'MANUAL_COMPACTION',
      entity_id: null,
      entity_name: null,
      op_kind: null,
      next_scheduled_run_at: null,
      safety_class: 'MANUAL',
    };
    expect(whatPausesItemLabel(noActor)).toBe('Ad-hoc OPTIMIZE/compaction · c.s.t');
  });

  it('appends op_kind + next run for auto-pausable rows and attributes the job id plainly', () => {
    const job: FreezePlanRow = {
      full_name: 'c.s.sales',
      entity_kind: 'WRITER_JOB',
      entity_id: 'j21',
      entity_name: 'nightly_load',
      op_kind: 'PAUSE_WRITER_JOB',
      next_scheduled_run_at: '2026-09-22 03:00',
      safety_class: 'SAFE_WINDOW',
    };
    expect(whatPausesItemLabel(job)).toBe(
      'Writer job · c.s.sales — nightly_load · PAUSE_WRITER_JOB · next run 2026-09-22 03:00',
    );
  });

  it('does not duplicate the actor when it equals the kind label (e.g. PO)', () => {
    const po: FreezePlanRow = {
      full_name: 'c.s',
      entity_kind: 'PO',
      entity_id: null,
      entity_name: 'Predictive Optimization',
      op_kind: 'DISABLE_PO',
      next_scheduled_run_at: null,
      safety_class: 'WONT_RUN',
    };
    // entity_name === kind label → actor clause suppressed; op_kind still shown.
    expect(whatPausesItemLabel(po)).toBe('Predictive Optimization · c.s · DISABLE_PO');
  });

  it('does not repeat the table when the actor id equals full_name', () => {
    const dupe: FreezePlanRow = {
      full_name: 'c.s.t',
      entity_kind: 'STREAM',
      entity_id: 'c.s.t',
      entity_name: 'c.s.t',
      op_kind: 'STOP_STREAM_JOB',
      next_scheduled_run_at: null,
      safety_class: 'DOWNTIME_UNTIL_UNFREEZE',
    };
    expect(whatPausesItemLabel(dupe)).toBe('Streaming job · c.s.t · STOP_STREAM_JOB');
  });

  it('labels a VACUUM job row in the what-pauses line', () => {
    const row: FreezePlanRow = {
      full_name: 'c.s.t1', entity_kind: 'COMPACTION_JOB', entity_id: '77', entity_name: 'nightly_vacuum',
      op_kind: 'PAUSE_JOB', next_scheduled_run_at: null, safety_class: 'SAFE_WINDOW', operation_classes: 'VACUUM',
    };
    expect(whatPausesItemLabel(row)).toBe('VACUUM job · c.s.t1 — nightly_vacuum · PAUSE_JOB');
  });
});

describe('willPauseAffected', () => {
  it('counts only the auto-pausable set (SAFE_WINDOW/DOWNTIME/WONT_RUN; excludes MANUAL/UNKNOWN)', () => {
    const a = willPauseAffected(rows);
    expect(a.count).toBe(6); // 2 safe + 1 wont-run + 3 heads-up
    expect(a.names).toEqual([
      'nightly_optimize', 'Predictive Optimization', 'orders_dlt', 'stream_writer_job_17',
      'nightly_load', 'events_ingest',
    ]);
  });
});

describe('headsUpNotes', () => {
  it('returns the continuous writers to warn (incl. event writer jobs)', () => {
    expect(headsUpNotes(rows)).toEqual(['orders_dlt', 'stream_writer_job_17', 'events_ingest']);
  });
});

describe('freezePlanHref', () => {
  it('builds a scoped /freeze link, leaving the catalog.schema dot intact', () => {
    expect(freezePlanHref('cat.sch')).toBe('/freeze?scope=cat.sch');
  });

  it('URL-encodes unsafe characters in the scope', () => {
    // encodeURIComponent leaves "." unencoded (unreserved) but encodes spaces/ampersands.
    expect(freezePlanHref('c a.t&x')).toBe('/freeze?scope=c%20a.t%26x');
  });
});

describe('freezePlanRowPill', () => {
  it('maps each safety_class to its review label + tone', () => {
    expect(freezePlanRowPill('SAFE_WINDOW')).toEqual({ label: 'Safe', tone: 'safe' });
    expect(freezePlanRowPill('DOWNTIME_UNTIL_UNFREEZE')).toEqual({
      label: 'Downtime until unfreeze', tone: 'downtime',
    });
    expect(freezePlanRowPill('WONT_RUN')).toEqual({ label: "Won't run", tone: 'wontRun' });
    expect(freezePlanRowPill('MANUAL')).toEqual({ label: 'Manual — you stop it', tone: 'manual' });
    expect(freezePlanRowPill('UNKNOWN')).toEqual({ label: 'Unclassified — review', tone: 'unknown' });
  });

  it('maps an unrecognized or empty safety_class to the unknown tone (never silently safe)', () => {
    expect(freezePlanRowPill('SOMETHING_NEW')).toEqual({ label: 'Unclassified — review', tone: 'unknown' });
    expect(freezePlanRowPill('')).toEqual({ label: 'Unclassified — review', tone: 'unknown' });
  });
});

describe('freezeCardPosture', () => {
  const r = (safety_class: string, entity_kind = 'X'): FreezePlanRow => ({
    full_name: 'c.s.t',
    entity_kind,
    entity_id: null,
    entity_name: 'e',
    op_kind: null,
    next_scheduled_run_at: null,
    safety_class,
  });

  it('FREEZABLE when at least one auto-pausable entity exists (safe / downtime / wont-run)', () => {
    expect(freezeCardPosture([r('SAFE_WINDOW'), r('MANUAL')])).toBe('FREEZABLE');
    expect(freezeCardPosture([r('DOWNTIME_UNTIL_UNFREEZE')])).toBe('FREEZABLE');
    expect(freezeCardPosture([r('WONT_RUN')])).toBe('FREEZABLE');
  });

  it('MANUAL_ONLY when rows exist but none are auto-pausable (all MANUAL/UNKNOWN)', () => {
    expect(freezeCardPosture([r('MANUAL')])).toBe('MANUAL_ONLY');
    expect(freezeCardPosture([r('UNKNOWN')])).toBe('MANUAL_ONLY');
    expect(freezeCardPosture([r('MANUAL'), r('UNKNOWN')])).toBe('MANUAL_ONLY');
  });

  it('EMPTY when nothing is attributed to the scope', () => {
    expect(freezeCardPosture([])).toBe('EMPTY');
  });
});

describe('freezeCtaModel', () => {
  it('FREEZABLE leads with the real freeze, not demoted', () => {
    expect(freezeCtaModel('FREEZABLE')).toEqual({
      primary: 'FREEZE',
      showFreeze: true,
      freezeDemoted: false,
    });
  });

  it('MANUAL_ONLY leads with Resolve open actions and keeps freeze only as a demoted escape', () => {
    expect(freezeCtaModel('MANUAL_ONLY')).toEqual({
      primary: 'RESOLVE_ACTIONS',
      showFreeze: true,
      freezeDemoted: true,
    });
  });

  it('EMPTY leads with Continue and does not offer the freeze trigger', () => {
    expect(freezeCtaModel('EMPTY')).toEqual({
      primary: 'CONTINUE',
      showFreeze: false,
      freezeDemoted: true,
    });
  });
});
