import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { isValidCompatLocation, normalizeCompatLocation } from './compatLocation.pure';

describe('isValidCompatLocation', () => {
  it('accepts concrete cloud-storage URIs', () => {
    for (const s of [
      'abfss://c@acct.dfs.core.windows.net/compat',
      's3://bucket/compat',
      's3a://bucket/compat',
      'gs://bucket/compat',
      'wasbs://c@acct.blob.core.windows.net/compat',
      'dbfs:/mnt/compat',
    ]) {
      expect(isValidCompatLocation(s)).toBe(true);
    }
  });

  it('accepts an absolute UC Volume / filesystem path', () => {
    expect(isValidCompatLocation('/Volumes/main/migration/compat')).toBe(true);
  });

  it('trims surrounding whitespace before validating', () => {
    expect(isValidCompatLocation('  s3://bucket/compat  ')).toBe(true);
  });

  it('rejects empty / whitespace / null', () => {
    for (const s of ['', '   ', null, undefined]) {
      expect(isValidCompatLocation(s)).toBe(false);
    }
  });

  it('rejects placeholder text with angle brackets', () => {
    expect(isValidCompatLocation('abfss://<container>@acct/path')).toBe(false);
    expect(isValidCompatLocation('<empty-external-location>')).toBe(false);
  });

  it('rejects strings with no recognized scheme or leading slash', () => {
    for (const s of ['abfss', 'not-a-path', 'container/path', 'http://x/y']) {
      expect(isValidCompatLocation(s)).toBe(false);
    }
  });

  it('accepts schemes case-insensitively', () => {
    expect(isValidCompatLocation('ABFSS://c@acct.dfs.core.windows.net/x')).toBe(true);
    expect(isValidCompatLocation('S3://bucket/x')).toBe(true);
  });
});

describe('normalizeCompatLocation', () => {
  it('trims and drops trailing slashes, like job_08b compat._location_key', () => {
    expect(normalizeCompatLocation('  s3://bucket/t/  ')).toBe('s3://bucket/t');
    expect(normalizeCompatLocation('abfss://c@a.dfs.core.windows.net/p///')).toBe('abfss://c@a.dfs.core.windows.net/p');
    expect(normalizeCompatLocation('/Volumes/c/s/v')).toBe('/Volumes/c/s/v');
    expect(normalizeCompatLocation(null)).toBe('');
  });

  it('a bare scheme or root normalizes to something the guard rejects', () => {
    expect(isValidCompatLocation(normalizeCompatLocation('s3://'))).toBe(false);
    expect(isValidCompatLocation(normalizeCompatLocation('/'))).toBe(false);
  });
});

describe('Compat copy never offers one path for a whole schema', () => {
  it('the workstream rail says a shared path serves a single table only', () => {
    const src = readFileSync(new URL('../pages/workstreams/CompatModeSteps.tsx', import.meta.url), 'utf8');
    expect(src).not.toMatch(/one shared path for the whole schema/i);
    expect(src).toMatch(/refuses a path shared by more than one table/);
  });
});
