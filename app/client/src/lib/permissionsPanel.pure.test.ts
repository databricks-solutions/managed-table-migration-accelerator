import { describe, expect, it } from 'vitest';
import {
  type PermissionsView,
  type ViewLine,
  convertHolders,
  convertSummaryCase,
  planHeading,
  errorText,
  grantSummary,
  grantorSentence,
  identityLabel,
  jobRunLoop,
  jobsHeldText,
  panelMode,
  permissionsBanner,
  planSql,
  requestGroups,
  requestText,
  runnableLines,
  scopeRows,
  scopesWithPicks,
  statusLabel,
  viewUrl,
} from './permissionsPanel.pure';
import { installCommand } from './provision.pure';

const SP = 'a1b2c3d4-0000-1111-2222-333344445555';

const line = (o: Partial<ViewLine>): ViewLine => ({
  set: 'Read', grantee: SP, grantee_kind: 'service_principal', privilege: 'SELECT', securable_type: 'SCHEMA',
  securable: 'sales.orders', statement: `GRANT SELECT ON SCHEMA \`sales\`.\`orders\` TO \`${SP}\``, reason: 'r',
  executor: 'installer', stage: 'main', scope: 'sales.orders', wave_id: null, status: 'missing', grantor: null, ...o,
});

function view(lines: ViewLine[], over: Partial<PermissionsView> = {}): PermissionsView {
  return {
    state: 'ready', reason: null,
    rights: { installer: 'ana', checks: [], coverage: { schemas: 0, tables: 0, grantable_schemas: 0, grantable_tables: 0, uncovered: [], candidates: [], headline: '', band: 'none' }, caveat: null, can_install: true, suggested_scopes: '' },
    who: { user: 'ana@example.com', metastore_admin: false, jobs_identity: { kind: 'service_principal', name: SP }, job_count: 20, installer_group: null, deployment: '[e2e] mtm', install_notebook: null },
    scopes: ['sales.orders', 'hr.pay'], stored_scopes: ['sales.orders'], scopes_from: 'stored', convert_rights: 'all_at_once', wave: null,
    plan_id: 'abcdefabcdef', counts: {}, summary: {}, lines, pending: 0, missing: 0, unchecked: 0, need_someone_else: 0,
    handback: { revokes: [], refused: [], plan_id: 'x' }, record_errors: [], estate_error: null, cli_command: '',
    check_command: 'databricks bundle run install -t e2e --profile <your-profile>',
    ...over,
  };
}

describe('panelMode', () => {
  it('leads with the server state, then what the user can do', () => {
    expect(panelMode(view([], { state: 'not_installed' }))).toBe('not_installed');
    expect(panelMode(view([], { state: 'cannot_act' }))).toBe('cannot_act');
    expect(panelMode(view([line({ status: 'present' })]))).toBe('nothing_to_do');
    expect(panelMode(view([line({})]))).toBe('can_grant');
    expect(panelMode(view([line({ grantor: 'hr-admins' })]))).toBe('ask_someone');
    expect(panelMode(view([line({ securable_type: 'JOBS', commands: ['databricks permissions update jobs 1'] })]))).toBe('ask_someone');
  });

  it('runs only missing lines nobody else is needed for', () => {
    const v = view([line({}), line({ status: 'present', privilege: 'USE SCHEMA' }), line({ grantor: 'x', securable: 'hr.pay' })]);
    expect(runnableLines(v).map((l) => l.privilege)).toEqual(['SELECT']);
  });
});

describe('scopeRows', () => {
  it('shows the worst status per set for each scope, Look by catalog', () => {
    const v = view([
      line({ set: 'Look', privilege: 'BROWSE', securable_type: 'CATALOG', securable: 'sales', scope: 'sales.*', status: 'present' }),
      line({ set: 'Read', status: 'present' }),
      line({ set: 'Read', privilege: 'USE SCHEMA', status: 'unknown' }),
      line({ set: 'Change', privilege: 'MANAGE', status: 'missing' }),
      line({ set: 'Read', scope: 'hr.pay', securable: 'hr.pay', status: 'missing' }),
    ]);
    const [sales, hr] = scopeRows(v);
    expect(sales).toEqual({ scope: 'sales.orders', stored: true, sets: { Control: 'none', Look: 'present', Read: 'unknown', Change: 'missing' } });
    expect(hr.stored).toBe(false);
    expect(hr.sets.Read).toBe('missing');
  });
});

describe('request and plan text', () => {
  it('groups what someone else runs by who, with job commands as-is', () => {
    const v = view([
      line({}),
      line({ grantor: '`hr-admins`, or a metastore admin', securable: 'hr.pay', statement: 'GRANT SELECT ON SCHEMA `hr`.`pay` TO `x`' }),
      line({ securable_type: 'JOBS', grantor: "the jobs' owner or a workspace admin", statement: '-- Jobs permission (not SQL)', commands: ['databricks permissions update jobs 11 --json x'] }),
    ]);
    expect(requestGroups(v)).toEqual([
      { grantor: '`hr-admins`, or a metastore admin', statements: ['GRANT SELECT ON SCHEMA `hr`.`pay` TO `x`;'] },
      { grantor: "the jobs' owner or a workspace admin", statements: ['databricks permissions update jobs 11 --json x'] },
    ]);
    const text = requestText(v);
    expect(text).toContain('plan abcdefabcdef');
    expect(text).toContain('Requested by ana@example.com.');
    expect(text).not.toContain('sales');
    expect(requestText(view([line({})]))).toBe('');
  });

  it('downloads every pending line as SQL, job commands commented', () => {
    const sql = planSql(view([line({}), line({ status: 'present', privilege: 'USE SCHEMA', statement: 'GRANT USE SCHEMA' }),
      line({ securable_type: 'JOBS', statement: '-- Jobs', commands: ['databricks permissions update jobs 11'] })]));
    expect(sql).toContain('-- Migration grant plan abcdefabcdef');
    expect(sql).toContain('TO `a1b2c3d4-0000-1111-2222-333344445555`;');
    expect(sql).not.toContain('GRANT USE SCHEMA');
    expect(sql).toContain('-- databricks permissions update jobs 11');
  });

  it('leaves not-checked lines out of the request and the plan, and only notes them with the check', () => {
    const v = view([
      line({ grantor: 'hr-admins', securable: 'hr.pay', statement: 'GRANT SELECT ON SCHEMA `hr`.`pay` TO `x`' }),
      line({ status: 'unknown', securable_type: 'JOBS', grantor: "the jobs' owner or a workspace admin", job_ids: ['11'],
        statement: '-- Jobs permission (not SQL): CAN_MANAGE_RUN on the 1 jobs', commands: ['databricks permissions update jobs 11 --json x'] }),
    ]);
    expect(requestGroups(v).map((g) => g.grantor)).toEqual(['hr-admins']);
    const text = requestText(v);
    expect(text).toContain('Not checked from the app (run databricks bundle run install -t e2e --profile <your-profile> to confirm):');
    expect(text).toContain('  Jobs permission (not SQL): CAN_MANAGE_RUN on the 1 jobs');
    expect(text).not.toContain('permissions update');
    const sql = planSql(v);
    expect(sql).not.toContain('permissions update');
    expect(sql).toContain('-- Not checked from the app');
    expect(requestText(view([line({ status: 'unknown', grantor: 'x' })]))).toBe('');
  });

  it('asks for a missing jobs permission as one loop, labelled not SQL, with the Jobs UI path', () => {
    const jobs = line({ securable_type: 'JOBS', grantee: 'ana@example.com', grantee_kind: 'user', grantor: "the jobs' owner or a workspace admin",
      job_ids: ['11', '12'], statement: '-- Jobs permission (not SQL)', commands: ['databricks permissions update jobs 11', 'databricks permissions update jobs 12'] });
    const [loop] = requestGroups(view([jobs]));
    expect(loop.statements).toEqual([
      '# Not SQL: job permissions are workspace settings, not Unity Catalog grants.',
      '# Or in the Jobs UI: open each job, then Permissions, then add ana@example.com with Can Manage Run.',
      'for id in 11 12; do databricks permissions update jobs "$id" --json \'{"access_control_list": [{"user_name": "ana@example.com", "permission_level": "CAN_MANAGE_RUN"}]}\'; done',
    ]);
    expect(jobRunLoop(jobs)[2]).toContain('for id in 11 12;');
    const sql = planSql(view([jobs]));
    expect(sql).toContain('-- Not SQL: job permissions are workspace settings');
    expect(sql).toContain('-- for id in 11 12; do');
  });

  it('labels identities and the schemas holding convert rights', () => {
    expect(identityLabel({ kind: 'service_principal', name: SP })).toBe(`service principal ${SP}`);
    expect(identityLabel(null)).toBe('not resolved');
    expect(convertHolders(view([line({ set: 'Change', privilege: 'MANAGE', status: 'present' })]))).toEqual(['sales.orders']);
  });
});

describe('results and requests', () => {
  it('summarizes a grant in plain sentences', () => {
    const s = grantSummary({ plan_id: 'p', results: [{ ...line({}), outcome: 'granted' }, { ...line({}), outcome: 'cannot_grant' }, { ...line({}), outcome: 'failed' }],
      audit_errors: [], record_errors: [], stopped: null, lines: [], summary: { present: 3, missing: 1, unknown: 0 } });
    expect(s).toBe('Granted 1. 1 failed. 1 need someone else. Checked after: 1 still missing.');
  });

  it('builds the preview url and error text', () => {
    expect(viewUrl({})).toBe('/api/permissions');
    expect(viewUrl({ scopes: 'a.b,c.*', convert_rights: '' })).toBe('/api/permissions?scopes=a.b%2Cc.*');
    expect(errorText(409, { error: 'The plan changed.' })).toBe('The plan changed.');
    expect(errorText(500, null)).toBe('The request failed (500).');
  });

  it('builds the CLI command with only non-default options, profile left as a placeholder', () => {
    expect(installCommand('e2e', { scopes: 'a.b,c.*', convert_rights: 'all_at_once', plan_id: 'abcdefabcdef', confirm_grants: 'true' }))
      .toBe('MTM_CONFIRM_GRANTS=true MTM_SCOPES=a.b,c.* MTM_PLAN_ID=abcdefabcdef databricks bundle run install -t e2e --profile <your-profile>');
    expect(installCommand(null, { grantee_group: 'data eng' })).toBe("MTM_GRANTEE_GROUP='data eng' databricks bundle run install -t <target> --profile <your-profile>");
  });
});

describe('permissionsBanner', () => {
  it('counts schemas the jobs cannot read or convert, else missing lines, and stays quiet otherwise', () => {
    const two = view([line({}), line({ set: 'Change', privilege: 'MANAGE', securable: 'hr.pay', scope: 'hr.pay' })]);
    expect(permissionsBanner(two)?.text).toBe('The migration is missing permissions on 2 schemas.');
    const control = view([line({ set: 'Control', scope: null, securable: 'ctl.migration' })]);
    expect(permissionsBanner(control)?.text).toBe('The migration is missing 1 permission.');
    expect(permissionsBanner(view([line({ status: 'present' }), line({ status: 'unknown' })]))).toBeNull();
    expect(permissionsBanner(view([line({})], { state: 'cannot_act' }))).toBeNull();
    expect(permissionsBanner(null)).toBeNull();
  });

  it('grantorSentence starts the grantor phrase with a capital', () => {
    expect(grantorSentence("the deployment folder's owner or a workspace admin"))
      .toBe("You can’t grant this. The deployment folder's owner or a workspace admin can.");
    expect(grantorSentence('`hr-owners`, or a metastore admin')).toBe('You can’t grant this. `hr-owners`, or a metastore admin can.');
  });

  it('statusLabel reads an unread grant as not checked, never as missing', () => {
    expect(statusLabel('unknown')).toBe('not checked');
    expect(statusLabel('missing')).toBe('missing');
  });

  it('scopesWithPicks adds the picks to the stored scope, and sends nothing when nothing is picked', () => {
    expect(scopesWithPicks(['sales.orders'], [])).toBe('');
    expect(scopesWithPicks(['sales.orders'], ['HR.pay', 'sales.orders', 'fin.*'])).toBe('sales.orders,hr.pay,fin.*');
  });
});

describe('convertSummaryCase', () => {
  it('reads an empty manifest as not profiled, not as 0 of 0', () => {
    expect(convertSummaryCase({ total: 0, profiled: 0 })).toBe('unprofiled');
  });
  it('reads a profiled estate with nothing left as finished', () => {
    expect(convertSummaryCase({ total: 0, profiled: 12 })).toBe('all_done');
  });
  it('counts when tables are still to convert, and when an older server sends no profiled count', () => {
    expect(convertSummaryCase({ total: 3, profiled: 5 })).toBe('counts');
    expect(convertSummaryCase({ total: 3 })).toBe('counts');
  });
  it('is unread without a summary', () => {
    expect(convertSummaryCase(null)).toBe('unread');
  });
});

describe('jobsHeldText', () => {
  const jobs = (status: ViewLine['status'], evidence: ViewLine['evidence']) => {
    const v = view([line({ securable_type: 'JOBS', privilege: 'CAN_MANAGE_RUN', status, evidence })]);
    return { ...v, who: { ...v.who, jobs_identity: { kind: 'user', name: 'ana@example.com' } } };
  };
  it('names the evidence behind a held jobs line', () => {
    expect(jobsHeldText(jobs('present', { kind: 'recorded', door: 'cli', at: '2026-10-20 09:00:00' })))
      .toBe('Can Manage Run held: checked by the CLI install on 2026-10-20.');
    expect(jobsHeldText(jobs('present', { kind: 'recorded', door: 'notebook', at: null })))
      .toBe('Can Manage Run held: checked by the notebook install.');
    expect(jobsHeldText(jobs('present', { kind: 'deployer' }))).toBe('Can Manage Run held: you deployed these jobs, so you own them.');
    const sp = view([line({ securable_type: 'JOBS', status: 'present', evidence: { kind: 'deployer' } })]);
    expect(jobsHeldText(sp)).toBe('Can Manage Run held: the jobs’ identity deployed them, so it owns them.');
    expect(jobsHeldText(jobs('present', { kind: 'live' }))).toBe('Can Manage Run held on every job.');
  });
  it('says nothing when the line is missing or not checked', () => {
    expect(jobsHeldText(jobs('missing', { kind: 'live' }))).toBeNull();
    expect(jobsHeldText(jobs('unknown', null))).toBeNull();
  });
});

describe('planHeading', () => {
  it('counts not-checked lines apart from missing ones', () => {
    expect(planHeading(0, 1)).toBe('Plan · 1 not checked');
    expect(planHeading(2, 0)).toBe('Plan · 2 missing');
    expect(planHeading(2, 1)).toBe('Plan · 2 missing · 1 not checked');
    expect(planHeading(0, 0)).toBe('Plan · nothing missing');
  });
});
