import { describe, expect, it } from 'vitest';
import {
  type PermissionsView,
  type ViewLine,
  convertHolders,
  errorText,
  grantSummary,
  grantorSentence,
  identityLabel,
  panelMode,
  permissionsBanner,
  planSql,
  requestGroups,
  requestText,
  runnableLines,
  scopeRows,
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
    plan_id: 'abcdefabcdef', counts: {}, summary: {}, lines, pending: 0, need_someone_else: 0,
    handback: { revokes: [], refused: [], plan_id: 'x' }, record_errors: [], estate_error: null, cli_command: '',
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

  it('grantorSentence names only who can grant when the app could not read the user', () => {
    expect(grantorSentence('a metastore admin', false)).toBe('A metastore admin can grant this.');
  });
});
