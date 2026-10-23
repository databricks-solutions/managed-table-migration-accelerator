import { describe, it, expect } from 'vitest';
import { normalizeWorkspacePath, workspaceObjectUrl, jobUrl } from './workspaceLinks.pure';

const HOST = 'https://x.cloud.databricks.com';

describe('normalizeWorkspacePath', () => {
  it('prepends /Workspace when absent', () => {
    expect(normalizeWorkspacePath('/Users/a@b.com/nb')).toBe('/Workspace/Users/a@b.com/nb');
  });

  it('leaves a path already under /Workspace unchanged', () => {
    expect(normalizeWorkspacePath('/Workspace/etl/pipeline')).toBe('/Workspace/etl/pipeline');
  });

  it('adds a leading slash for a relative path', () => {
    expect(normalizeWorkspacePath('Users/a/nb')).toBe('/Workspace/Users/a/nb');
  });
});

describe('workspaceObjectUrl', () => {
  it('builds a #workspace deep link, normalizing the /Workspace root', () => {
    expect(workspaceObjectUrl(HOST, '/Users/a/nb')).toBe(`${HOST}/#workspace/Workspace/Users/a/nb`);
  });

  it('does not double the /Workspace prefix', () => {
    expect(workspaceObjectUrl(HOST, '/Workspace/etl/nb')).toBe(`${HOST}/#workspace/Workspace/etl/nb`);
  });

  it('adds ?o=<workspaceId> before the # fragment when the id is present', () => {
    expect(workspaceObjectUrl(HOST, '/Users/a/nb', '1234567890123456')).toBe(
      `${HOST}/?o=1234567890123456#workspace/Workspace/Users/a/nb`,
    );
  });

  it('omits ?o= when the workspace id is blank/absent', () => {
    expect(workspaceObjectUrl(HOST, '/Users/a/nb', null)).toBe(`${HOST}/#workspace/Workspace/Users/a/nb`);
    expect(workspaceObjectUrl(HOST, '/Users/a/nb', '')).toBe(`${HOST}/#workspace/Workspace/Users/a/nb`);
  });

  it('returns null when the host is absent', () => {
    expect(workspaceObjectUrl(null, '/Users/a/nb')).toBeNull();
    expect(workspaceObjectUrl(undefined, '/Users/a/nb')).toBeNull();
    expect(workspaceObjectUrl('', '/Users/a/nb')).toBeNull();
  });

  it('returns null when the path is absent', () => {
    expect(workspaceObjectUrl(HOST, null)).toBeNull();
    expect(workspaceObjectUrl(HOST, undefined)).toBeNull();
    expect(workspaceObjectUrl(HOST, '')).toBeNull();
  });
});

describe('jobUrl', () => {
  it('builds a job-page deep link', () => {
    expect(jobUrl(HOST, '123')).toBe(`${HOST}/jobs/123`);
  });

  it('adds ?o=<workspaceId> when present', () => {
    expect(jobUrl(HOST, '123', '1234567890123456')).toBe(`${HOST}/jobs/123?o=1234567890123456`);
  });

  it('returns null when host or job id is absent', () => {
    expect(jobUrl(null, '123')).toBeNull();
    expect(jobUrl(HOST, null)).toBeNull();
    expect(jobUrl(HOST, '')).toBeNull();
  });
});
