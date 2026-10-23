import { describe, it, expect } from 'vitest';
import { dashboardEmbedUrl, dashboardWorkspaceUrl } from './dashboard.pure';

const HOST = 'https://my-workspace.cloud.databricks.com';
const ID = '01f1a035d6461aae960d9afb30ee3c25';

describe('dashboardEmbedUrl', () => {
  it('builds the /embed/dashboardsv3/<id> iframe source', () => {
    expect(dashboardEmbedUrl(HOST, ID)).toBe(`${HOST}/embed/dashboardsv3/${ID}`);
  });

  it('returns null when host or id is missing', () => {
    expect(dashboardEmbedUrl(null, ID)).toBeNull();
    expect(dashboardEmbedUrl(HOST, null)).toBeNull();
    expect(dashboardEmbedUrl(undefined, undefined)).toBeNull();
    expect(dashboardEmbedUrl('', '')).toBeNull();
  });
});

describe('dashboardWorkspaceUrl', () => {
  it('builds the full-page /dashboardsv3/<id> workspace link', () => {
    expect(dashboardWorkspaceUrl(HOST, ID)).toBe(`${HOST}/dashboardsv3/${ID}`);
  });

  it('returns null when host or id is missing', () => {
    expect(dashboardWorkspaceUrl(null, ID)).toBeNull();
    expect(dashboardWorkspaceUrl(HOST, null)).toBeNull();
  });
});
