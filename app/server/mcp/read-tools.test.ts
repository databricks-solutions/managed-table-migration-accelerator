import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { READ_TOOLS } from './read-tools.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

describe('READ_TOOLS', () => {
  it('names are unique and mtm_-prefixed', () => {
    const names = READ_TOOLS.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
    for (const n of names) expect(n.startsWith('mtm_')).toBe(true);
  });
  it('every backing query file exists on disk', () => {
    for (const t of READ_TOOLS) {
      const p = resolve(APP, `config/queries/${t.query}.sql`);
      expect(() => readFileSync(p, 'utf8')).not.toThrow();
    }
  });
  it('wave-scoped tools declare a wave_id arg', () => {
    const members = READ_TOOLS.find((t) => t.name === 'mtm_get_wave_members');
    expect(members?.args?.safeParse({ wave_id: 'w1' }).success).toBe(true);
    expect(members?.args?.safeParse({}).success).toBe(false);
  });
});
