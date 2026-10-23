import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const srcRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return walk(p);
    return e.name.endsWith('.tsx') && !e.name.includes('.test.') ? [p] : [];
  });

const files = [...walk(join(srcRoot, 'pages')), ...walk(join(srcRoot, 'components'))].map((p) => ({
  rel: relative(srcRoot, p).split('\\').join('/'),
  src: readFileSync(p, 'utf8'),
}));

const lineOf = (src: string, index: number) => src.slice(0, index).split('\n').length;

// A Label whose body renders one of these is showing a name the operator needs to copy.
const IDENTIFIER_EXPR = /\{\s*(?:[\w.]+\.)?(full_name|fullName|confirmTarget|confirmText|planRunId|label)\s*\}/;

// Labels allowed to keep htmlFor: clicking them focuses a control that does not take the
// document selection, so the identifier stays selectable.
const HTMLFOR_ALLOWLIST: { file: string; reason: string }[] = [
  {
    file: 'pages/convert/ConvertPage.tsx',
    reason: 'Field labels point at pickers or a disabled input, which do not take the selection',
  },
];

describe('selectability guard', () => {
  it('index.css overrides ::selection outside any @layer, with both colours set', () => {
    const css = readFileSync(join(srcRoot, 'index.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    const blocks = [...css.matchAll(/::selection\s*\{([^}]*)\}/g)].filter((m) => {
      const before = css.slice(0, m.index ?? 0);
      return before.split('{').length - before.split('}').length === 0;
    });
    expect(blocks.length, 'no top-level ::selection rule in index.css').toBeGreaterThan(0);
    const body = blocks[blocks.length - 1][1];
    expect(body).toMatch(/background-color\s*:/);
    expect(body).toMatch(/(^|[;\s])color\s*:/);
  });

  it('identifier labels are select-text and do not steal the selection via htmlFor', () => {
    const offenders: string[] = [];
    let identifierLabels = 0;
    for (const { rel, src } of files) {
      for (const m of src.matchAll(/<Label\b([^>]*)>([\s\S]*?)<\/Label>/g)) {
        const [, attrs, body] = m;
        if (!IDENTIFIER_EXPR.test(body)) continue;
        identifierLabels += 1;
        const at = `${rel}:${lineOf(src, m.index ?? 0)}`;
        if (!/\bselect-text\b/.test(attrs)) offenders.push(`${at} missing select-text`);
        if (/\bhtmlFor=/.test(attrs) && !HTMLFOR_ALLOWLIST.some((a) => a.file === rel)) {
          offenders.push(`${at} uses htmlFor (use id + aria-labelledby on the input)`);
        }
      }
    }
    expect(identifierLabels).toBeGreaterThanOrEqual(6);
    expect(offenders).toEqual([]);
  });
});
