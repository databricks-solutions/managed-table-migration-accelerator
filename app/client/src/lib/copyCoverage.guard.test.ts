import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { FORBIDDEN_TERMS } from './copyRules.pure';
import { FORBIDDEN_RATCHET, LITERAL_ALLOWLIST } from './copyCoverage.allowlist';

const srcRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return walk(p);
    return e.name.endsWith('.tsx') && !e.name.includes('.test.') ? [p] : [];
  });

const files = [
  ...walk(join(srcRoot, 'pages')),
  ...readdirSync(join(srcRoot, 'components'))
    .filter((f) => f.endsWith('.tsx') && !f.includes('.test.'))
    .map((f) => join(srcRoot, 'components', f)),
].map((p) => ({ rel: relative(srcRoot, p).split('\\').join('/'), src: readFileSync(p, 'utf8') }));

const lineOf = (src: string, index: number) => src.slice(0, index).split('\n').length;

const LITERAL_PATTERNS = [
  /<(CardTitle|CardDescription)\b[^>]*>\s*[A-Za-z]/g,
  /\b(title|subtitle|description|explanation|triggerLabel)="[^"]*[A-Za-z]/g,
];

describe('copy coverage guard', () => {
  it('pages and components take titles and subtitles from lib/sectionCopy.ts', () => {
    const offenders: string[] = [];
    for (const { rel, src } of files) {
      const lines = src.split('\n');
      for (const pattern of LITERAL_PATTERNS) {
        for (const m of src.matchAll(pattern)) {
          const n = lineOf(src, m.index ?? 0);
          const line = lines[n - 1];
          const allowed = LITERAL_ALLOWLIST.some((a) => a.file === rel && line.includes(a.match));
          if (!allowed) offenders.push(`${rel}:${n}  ${line.trim()}`);
        }
      }
    }
    expect(offenders, 'Move this copy into lib/sectionCopy.ts, or allowlist it with a reason').toEqual([]);
  });

  it('every allowlist entry still matches a line, and carries a reason', () => {
    const stale = LITERAL_ALLOWLIST.filter((a) => {
      const f = files.find((x) => x.rel === a.file);
      return !a.reason.trim() || !f || !f.src.includes(a.match);
    }).map((a) => `${a.file}: ${a.match}`);
    expect(stale).toEqual([]);
  });

  it('visible prose stays within the forbidden-term ratchet', () => {
    const counts: Record<string, number> = {};
    const hits: string[] = [];
    const visibleProp =
      /\b(?:title|subtitle|description|label|placeholder|aria-label|hint|text|note|emptyTitle|emptyDescription)="([^"]*)"/g;
    const dataField = /\b(?:title|subtitle|description|label|note|body|detail|teaser|hint)\s*:\s*'((?:[^'\\]|\\.)*)'/g;
    const jsxText = /(<\/?[A-Za-z][\w.]*(?:\s[^<>]*)?\/?>)([^<>{}]*[A-Za-z][^<>{}]*)(?=[<{])/g;
    const codeLike = /\b(const|let|return|function|typeof|useState|useRef|ReactNode|ReturnType|NonNullable)\b|=>|===|\[number\]|\)\s*\?\s*\(|\??:\s*(string|number|boolean)\b/;
    for (const { rel, src } of files) {
      const clean = src
        .replace(/\{\/\*[\s\S]*?\*\/\}|\/\*[\s\S]*?\*\/|(?<=^|\s)\/\/.*$/gm, (c) => c.replace(/[^\n]/g, ' '))
        .replace(/&[a-z]+;/g, (e) => e.replace(/./g, ' '));
      const texts: { text: string; index: number }[] = [];
      for (const m of clean.matchAll(jsxText)) {
        const [, tag, text] = m;
        if (/^<code\b/.test(tag) || tag.includes('font-mono') || codeLike.test(text)) continue;
        texts.push({ text, index: (m.index ?? 0) + tag.length });
      }
      for (const m of clean.matchAll(visibleProp)) texts.push({ text: m[1], index: m.index ?? 0 });
      for (const m of clean.matchAll(dataField)) texts.push({ text: m[1], index: m.index ?? 0 });
      for (const { text, index } of texts) {
        for (const { pattern, why } of FORBIDDEN_TERMS) {
          const hit = pattern.exec(text);
          if (!hit) continue;
          counts[rel] = (counts[rel] ?? 0) + 1;
          hits.push(`${rel}:${lineOf(clean, index)}  "${hit[0]}" (${why})`);
        }
      }
    }
    const drift = Object.keys({ ...counts, ...FORBIDDEN_RATCHET })
      .filter((rel) => (counts[rel] ?? 0) !== (FORBIDDEN_RATCHET[rel]?.max ?? 0))
      .map((rel) => {
        const now = counts[rel] ?? 0;
        const max = FORBIDDEN_RATCHET[rel]?.max ?? 0;
        const detail = hits.filter((h) => h.startsWith(`${rel}:`)).join('\n    ');
        return now > max
          ? `${rel}: ${now} hits, ratchet allows ${max}. Say it in plain words:\n    ${detail}`
          : `${rel}: ${now} hits, ratchet is ${max}. Lower it in copyCoverage.allowlist.ts:\n    ${detail}`;
      });
    expect(drift).toEqual([]);
  });
});
