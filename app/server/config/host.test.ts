import { describe, expect, it } from 'vitest';
import { normalizeHost, normalizeWorkspaceId } from './host.js';

describe('normalizeHost', () => {
  it('returns null when unset, empty, or whitespace', () => {
    expect(normalizeHost(undefined)).toBeNull();
    expect(normalizeHost(null)).toBeNull();
    expect(normalizeHost('')).toBeNull();
    expect(normalizeHost('   ')).toBeNull();
  });

  it('adds https:// when no scheme is present', () => {
    expect(normalizeHost('adb-123.4.azuredatabricks.net')).toBe(
      'https://adb-123.4.azuredatabricks.net',
    );
  });

  it('keeps an existing scheme (http or https)', () => {
    expect(normalizeHost('https://x.cloud.databricks.com')).toBe('https://x.cloud.databricks.com');
    expect(normalizeHost('http://localhost:8080')).toBe('http://localhost:8080');
  });

  it('strips a trailing slash', () => {
    expect(normalizeHost('https://x.cloud.databricks.com/')).toBe('https://x.cloud.databricks.com');
    expect(normalizeHost('https://x.cloud.databricks.com///')).toBe(
      'https://x.cloud.databricks.com',
    );
  });

  it('trims surrounding whitespace before normalising', () => {
    expect(normalizeHost('  x.cloud.databricks.com/  ')).toBe('https://x.cloud.databricks.com');
  });
});

describe('normalizeWorkspaceId', () => {
  it('returns an all-digits id (trimmed)', () => {
    expect(normalizeWorkspaceId('1234567890123456')).toBe('1234567890123456');
    expect(normalizeWorkspaceId('  1234567890123456  ')).toBe('1234567890123456');
  });

  it('returns null when unset, empty, or whitespace', () => {
    expect(normalizeWorkspaceId(undefined)).toBeNull();
    expect(normalizeWorkspaceId(null)).toBeNull();
    expect(normalizeWorkspaceId('')).toBeNull();
    expect(normalizeWorkspaceId('   ')).toBeNull();
  });

  it('rejects a non-numeric value (never a broken ?o=)', () => {
    expect(normalizeWorkspaceId('CHANGE-ME')).toBeNull();
    expect(normalizeWorkspaceId('123abc')).toBeNull();
    expect(normalizeWorkspaceId('o=123')).toBeNull();
  });
});
