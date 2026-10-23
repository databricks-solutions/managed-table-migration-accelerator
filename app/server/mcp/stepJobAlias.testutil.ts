import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ENTRY = /^\s+(STEP_[A-Z_]+): "([a-z_]+)",$/;

/**
 * Parse STEP_JOB_ALIAS (conductor kind -> job alias) out of conductor.py source text. Every non-blank,
 * non-comment line of the block must be a well-formed entry, so a format drift fails loudly instead of
 * yielding a partial map the parity tests would happily iterate.
 */
export function parseStepJobAlias(src: string): Record<string, string> {
  const consts = new Map<string, string>();
  for (const m of src.matchAll(/^(STEP_[A-Z_]+) = "([a-z_]+)"$/gm)) consts.set(m[1], m[2]);
  const block = /^STEP_JOB_ALIAS: dict = \{\n([\s\S]*?)^\}/m.exec(src);
  if (!block) throw new Error('STEP_JOB_ALIAS not found in backend/src/migration/conductor.py');
  const out: Record<string, string> = {};
  for (const line of block[1].split('\n')) {
    if (line.trim() === '' || line.trim().startsWith('#')) continue;
    const m = ENTRY.exec(line);
    if (!m) throw new Error(`STEP_JOB_ALIAS has an entry the parity helper cannot parse: ${JSON.stringify(line)}`);
    const kind = consts.get(m[1]);
    if (!kind) throw new Error(`STEP_JOB_ALIAS names an unknown constant ${m[1]}`);
    out[kind] = m[2];
  }
  if (Object.keys(out).length === 0) throw new Error('STEP_JOB_ALIAS parsed to zero entries');
  return out;
}

/**
 * Test-only: the backend conductor.STEP_JOB_ALIAS read straight from the Python source, so the TS
 * kind/alias tables are pinned to the backend without a hand-kept copy.
 */
export function backendStepJobAlias(): Record<string, string> {
  const conductor = resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/src/migration/conductor.py');
  return parseStepJobAlias(readFileSync(conductor, 'utf8'));
}
