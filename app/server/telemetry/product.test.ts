import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { PRODUCT, VERSION, safeToken, telemetryEnabled } from './product';

// Parity only: the app may read tooling files; tooling never reads app files.
const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, '../..');
const MARKER = readFileSync(resolve(APP, '../backend/src/uc_ext2managed/__init__.py'), 'utf8');

describe('product identity', () => {
  it('matches the backend marker package and package.json', () => {
    expect(/^PRODUCT = "([^"]+)"/m.exec(MARKER)?.[1]).toBe(PRODUCT);
    expect(/^__version__ = "([^"]+)"/m.exec(MARKER)?.[1]).toBe(VERSION);
    const pkg = JSON.parse(readFileSync(resolve(APP, 'package.json'), 'utf8')) as { version: string };
    expect(pkg.version).toBe(VERSION);
  });
});

describe('telemetryEnabled (opt-out)', () => {
  it.each([
    [{ MTM_TELEMETRY_ENABLED: 'true' }, true],
    [{ MTM_TELEMETRY_ENABLED: 'TRUE' }, true],
    [{ MTM_TELEMETRY_ENABLED: 'false' }, false],
    [{ MTM_TELEMETRY_ENABLED: ' off ' }, false],
    [{ MTM_TELEMETRY_ENABLED: '0' }, false],
    [{ MTM_TELEMETRY_ENABLED: '' }, false], // unset / blank: send nothing
    [{}, false],
    [{ MTM_TELEMETRY_ENABLED: 'true', DO_NOT_TRACK: '1' }, false],
    [{ MTM_TELEMETRY_ENABLED: 'true', DO_NOT_TRACK: '0' }, true],
  ])('%j -> %s', (env, expected) => {
    expect(telemetryEnabled(env as NodeJS.ProcessEnv)).toBe(expected);
  });
});

describe('safeToken', () => {
  it.each(['convert', 'bulk_convert', 'v2', '1.0.0'])('accepts %s', (t) => expect(safeToken(t)).toBe(t));
  it.each(['main.sales/t', 'a b', 'x\ny', 'jane@example.com', '', 'é', 42, null, 'A'.repeat(97)])('rejects %j', (t) =>
    expect(safeToken(t)).toBeNull()
  );
});
