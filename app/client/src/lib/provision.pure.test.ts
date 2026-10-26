import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  type EstateRow,
  type GrantPlanInput,
  type LedgerRow,
  type PlanLine,
  type Probes,
  canGrantOn,
  coverage,
  estateRowsFrom,
  grantPlan,
  handbackActionKey,
  handbackPlan,
  handbackPlanId,
  heldCovers,
  jobRunCommands,
  groupMemberSql,
  lineGrantable,
  lineKey,
  lineKeyString,
  optionsFixture,
  ownersFrom,
  parseOptions,
  planCounts,
  planId,
  pyJson,
  revokeSql,
  rightsReport,
  schemaGrantor,
  schemaStates,
  sha256Hex,
  verify,
  verifySummary,
} from './provision.pure';

// Lockstep with backend/src/migration/provision.py through its golden fixture: the notebook, the
// CLI and the app must plan, fingerprint and judge every install identically.
const FIXTURE = JSON.parse(
  readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../../backend/src/tests/fixtures/provision_cases.json'), 'utf8'),
) as {
  options: unknown[];
  plans: { name: string; input: GrantPlanInput; lines: PlanLine[]; plan_id: string; counts: Record<string, number> }[];
  rights: { name: string; door: string; probes: Probes; report: unknown }[];
  revokes: { name: string; privilege: string; securable_type: string; securable: string; grantee: string; statement: string }[];
  parse: { name: string; raw: Record<string, string | boolean>; door: string; options: unknown; errors: string[] }[];
  verify: { name: string; lines: PlanLine[]; held: { key: string[]; held: string[] | null }[]; results: unknown[]; summary: unknown }[];
  grantable: Record<string, unknown>[];
  handback: Record<string, unknown>[];
  sql: { job_run_commands: string[]; line: PlanLine; audit_detail: string; group_member: string };
};

describe('provision mirror equals the engine (golden fixture)', () => {
  it('options', () => {
    expect(optionsFixture()).toEqual(FIXTURE.options);
  });

  it.each(FIXTURE.plans.map((p) => [p.name, p] as const))('grant_plan %s', (_name, p) => {
    const lines = grantPlan(p.input);
    expect(lines).toEqual(p.lines);
    expect(planId(lines)).toBe(p.plan_id);
    expect(planCounts(lines)).toEqual(p.counts);
  });

  it.each(FIXTURE.rights.map((r) => [r.name, r] as const))('rights_report %s', (_name, r) => {
    expect(rightsReport(r.probes, r.door)).toEqual(r.report);
  });

  it.each(FIXTURE.revokes.map((r) => [r.name, r] as const))('revoke_sql %s', (_name, r) => {
    expect(revokeSql(r.privilege, r.securable_type, r.securable, r.grantee)).toBe(r.statement);
  });

  it.each(FIXTURE.parse.map((c) => [c.name, c] as const))('parse_options %s', (_name, c) => {
    const [opts, errors] = parseOptions(c.raw, c.door);
    expect(opts).toEqual(c.options);
    expect(errors).toEqual(c.errors);
  });

  it.each(FIXTURE.verify.map((c) => [c.name, c] as const))('verify %s', (_name, c) => {
    const held = new Map(c.held.map((h) => [JSON.stringify(h.key), h.held] as const));
    const results = verify(c.lines, held);
    expect(results).toEqual(c.results);
    expect(verifySummary(results)).toEqual(c.summary);
  });

  it('line_grantable', () => {
    for (const c of FIXTURE.grantable.filter((g) => 'results' in g)) {
      const ctx = c.context as Parameters<typeof lineGrantable>[1];
      const lines = c.lines as PlanLine[];
      const results = c.results as { key: string[]; grantable: [boolean | null, string] }[];
      lines.forEach((ln, i) => {
        expect(lineKey(ln)).toEqual(results[i].key);
        expect(lineGrantable(ln, ctx)).toEqual(results[i].grantable);
      });
    }
  });

  it('can_grant_on and schema_grantor', () => {
    const c = FIXTURE.grantable.find((g) => g.name === 'can_grant_on') as {
      owners: { catalog_owner: Record<string, string>; schema_owner: Record<string, string | null>; catalog_manage: Record<string, string[]>; schema_manage: Record<string, string[]> };
      principals: string[];
      checks: { schema: string; can_grant: boolean | null; grantor: string }[];
    };
    for (const k of c.checks) {
      const cat = k.schema.split('.')[0];
      expect(canGrantOn(c.principals, {
        schema_owner: c.owners.schema_owner[k.schema] ?? null,
        catalog_owner: c.owners.catalog_owner[cat] ?? null,
        manage_holders: [...(c.owners.schema_manage[k.schema] ?? []), ...(c.owners.catalog_manage[cat] ?? [])],
        metastore_admin: false,
      })).toBe(k.can_grant);
      expect(schemaGrantor(c.owners.schema_owner[k.schema] ?? null, c.owners.catalog_owner[cat] ?? null)).toBe(k.grantor);
    }
  });

  it('handback_plan, its plan_id and action keys', () => {
    for (const c of FIXTURE.handback.filter((h) => 'plan' in h)) {
      const plan = handbackPlan(c.ledger as LedgerRow[], c.states as never, c.scopes as string[], c.window_ends as Record<string, string>);
      expect(plan).toEqual(c.plan);
      expect(handbackPlanId(plan.revokes)).toBe(c.plan_id);
      expect(plan.revokes.map(handbackActionKey)).toEqual(c.action_keys);
    }
  });

  it('schema_states', () => {
    const c = FIXTURE.handback.find((h) => h.name === 'schema_states') as { manifest: never[]; states: unknown };
    expect(schemaStates(c.manifest)).toEqual(c.states);
  });

  it('job_run_commands and audit detail JSON', () => {
    expect(jobRunCommands({ grantee: 'mtm-jobs', grantee_kind: 'group', job_ids: ['7', '8'] })).toEqual(FIXTURE.sql.job_run_commands);
    const ln = FIXTURE.sql.line;
    const detail = pyJson({
      front_door: 'app', plan_id: 'd04c4bab08e2', set: ln.set, grantee: ln.grantee, grantee_kind: ln.grantee_kind,
      privilege: ln.privilege, securable_type: ln.securable_type, securable: ln.securable, scope: ln.scope,
      added: true, prior_held: false, grantor: '`hr-owners`',
    }, { sortKeys: true });
    expect(detail).toBe(FIXTURE.sql.audit_detail);
    expect(groupMemberSql("o'neil-installers")).toBe(FIXTURE.sql.group_member);
  });
});

describe('provision mirror details', () => {
  it('sha256Hex matches node crypto (ASCII, UTF-8, block edges)', () => {
    for (const s of ['', 'abc', 'é ß 日本', 'x'.repeat(55), 'x'.repeat(56), 'x'.repeat(64), 'x'.repeat(1000)]) {
      expect(sha256Hex(s)).toBe(createHash('sha256').update(s, 'utf8').digest('hex'));
    }
  });

  it('pyJson escapes non-ASCII like ensure_ascii and keeps DEL', () => {
    expect(pyJson({ b: 'é', a: '\u007f' }, { sortKeys: true })).toBe('{"a": "\u007f", "b": "\\u00e9"}');
    expect(pyJson([1, true, null], { compact: true })).toBe('[1,true,null]');
  });

  it('a grant made by hand after review does not change plan_id (lines are the intended set)', () => {
    const input: GrantPlanInput = { catalog: 'main', schema: 'migration', jobs_identity: { kind: 'group', name: 'Jobs' }, scopes: ['a.b'] };
    const lines = grantPlan(input);
    expect(planId(lines)).toBe(planId(grantPlan({ ...input, jobs_identity: { kind: 'group', name: 'JOBS' } })));
    expect(planId(lines)).not.toBe(planId(grantPlan({ ...input, scopes: ['a.c'] })));
  });

  it('heldCovers: unreadable is null, MANAGE needs MANAGE or a superset', () => {
    expect(heldCovers(null, 'MANAGE')).toBeNull();
    expect(heldCovers(['SELECT'], 'MANAGE')).toBe(false);
    expect(heldCovers(['ALL_PRIVILEGES'], 'MANAGE')).toBe(true);
    expect(heldCovers(['CAN_VIEW'], 'CAN_MANAGE_RUN')).toBe(false);
  });

  it('owners and estate rows from information_schema reads', () => {
    const owners = ownersFrom(
      [{ catalog_name: 'Sales', catalog_owner: 'sales-admins' }],
      [{ catalog_name: 'sales', schema_name: 'Orders', schema_owner: 'ann@x.com' }],
      [{ catalog_name: 'hr', grantee: 'data-eng' }],
      null,
    );
    const rows: EstateRow[] = estateRowsFrom(
      [
        { table_catalog: 'sales', table_schema: 'orders', tables: '12' },
        { table_catalog: 'hr', table_schema: 'pay', tables: '3' },
        { table_catalog: 'ops', table_schema: 'x', tables: '1' },
      ],
      owners,
      ['ann@x.com'],
      false,
    );
    expect(rows.map((r) => [r.catalog, r.schema, r.can_grant, r.grantor])).toEqual([
      ['hr', 'pay', false, 'a metastore admin'],
      ['ops', 'x', false, 'a metastore admin'],
      ['sales', 'orders', true, null],
    ]);
    expect(coverage(rows).headline).toBe('You can grant on 1 of 3 schemas with external tables (12 of 16 tables).');
    expect(ownersFrom(null, [], [], []).readable).toBe(false);
  });

  it('lineKeyString is the Map key verify reads', () => {
    const [ln] = grantPlan({ catalog: 'main', schema: 'migration', jobs_identity: { kind: 'group', name: 'G' }, action: 'grant_convert', wave_schemas: ['a.b'] });
    expect(verify([ln], new Map([[lineKeyString(ln), ['MANAGE']]]))[0].status).toBe('present');
  });
});
