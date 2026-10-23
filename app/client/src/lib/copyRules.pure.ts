/**
 * Plain-language rules for every visible title, subtitle, button helper, and dialog description.
 * Pure — the copy test runs these over every entry in sectionCopy.ts, and the guard test runs
 * FORBIDDEN_TERMS over the page sources. Exact job names belong in a copy entry's `technical`
 * field, which these rules never check.
 */

export const FORBIDDEN_TERMS: ReadonlyArray<{ pattern: RegExp; why: string }> = [
  { pattern: /\bconductor\b/i, why: 'internal engine name' },
  {
    pattern: /\b(wave_state|next_step_kind|plan_run_id|wave_parallel_groups|wave_gate_latest)\b/,
    why: 'column or table name',
  },
  { pattern: /\bjob_\d{2}[a-z]?\w*/, why: 'job alias — put it in `technical`' },
  { pattern: /\b0\d[a-e]?\b/, why: 'stage number' },
  { pattern: /\b(run_as|App SP)\b/, why: 'identity jargon' },
  { pattern: /\b(ledger|selector)\b/i, why: 'internal mechanism' },
  { pattern: /\b(TBLPROPERTIES|D2D|PO|(UN)?SET MANAGED)\b/, why: 'say it in plain words' },
  {
    pattern: /\b(lease ownership|take ownership|temporary ownership|borrows table ownership)\b/i,
    why: 'ownership never changes — say convert permission or Authorize',
  },
  { pattern: /\bOWNER\sTO\b/, why: 'ownership never changes — say convert permission or Authorize' },
  { pattern: /\b[A-Z]+_[A-Z_]+\b/, why: 'raw state or enum' },
  { pattern: /;/, why: 'one idea per sentence' },
];

const FILLER: readonly RegExp[] = [
  /\bthis (section|page|card) shows\b/i,
  /\bin order to\b/i,
  /\bplease note\b/i,
  /\bit is important\b/i,
];

const PHRASAL_VERBS: readonly RegExp[] = [/\bkick(s|ed)? off\b/i, /\bspin(s)? up\b/i, /\breach(es)? out\b/i, /\bdive into\b/i];

const MAX_WORDS_PER_SENTENCE = 15;

export type CopyKind = 'title' | 'subtitle' | 'helper' | 'dialog' | 'primer';

const CAPS: Record<CopyKind, { chars: number; words?: number; sentences?: number }> = {
  title: { chars: 40, words: 5 },
  subtitle: { chars: 120, sentences: 2 },
  helper: { chars: 80, words: 10 },
  dialog: { chars: 160, sentences: 2 },
  primer: { chars: 320, sentences: 4 },
};
const SUBTITLE_FIRST_SENTENCE_CHARS = 100;

export function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/** Words, not separators: a bare "·" or "—" between words is not counted. */
function wordCount(text: string): number {
  return text.split(/\s+/).filter((w) => /[\p{L}\p{N}{]/u.test(w)).length;
}

/** Jargon, filler, phrasal verbs, and over-long sentences in one piece of visible copy. */
export function copyViolations(text: string): string[] {
  const out: string[] = [];
  for (const { pattern, why } of FORBIDDEN_TERMS) {
    const m = pattern.exec(text);
    if (m) out.push(`"${m[0]}": ${why}`);
  }
  for (const p of FILLER) {
    const m = p.exec(text);
    if (m) out.push(`"${m[0]}": filler — say it directly`);
  }
  for (const p of PHRASAL_VERBS) {
    const m = p.exec(text);
    if (m) out.push(`"${m[0]}": phrasal verb — use one plain verb`);
  }
  for (const s of sentences(text)) {
    const n = wordCount(s);
    if (n > MAX_WORDS_PER_SENTENCE) out.push(`sentence has ${n} words (max ${MAX_WORDS_PER_SENTENCE}): "${s}"`);
  }
  return out;
}

/** Length caps per kind of copy (characters, words, sentences). */
export function lengthViolations(text: string, kind: CopyKind): string[] {
  const cap = CAPS[kind];
  const out: string[] = [];
  if (text.length > cap.chars) out.push(`${kind} has ${text.length} characters (max ${cap.chars})`);
  if (cap.words !== undefined) {
    const n = wordCount(text);
    if (n > cap.words) out.push(`${kind} has ${n} words (max ${cap.words})`);
  }
  if (cap.sentences !== undefined) {
    const all = sentences(text);
    if (all.length > cap.sentences) out.push(`${kind} has ${all.length} sentences (max ${cap.sentences})`);
    const first = all[0] ?? '';
    if (kind === 'subtitle' && first.length > SUBTITLE_FIRST_SENTENCE_CHARS) {
      out.push(`subtitle's first sentence has ${first.length} characters (max ${SUBTITLE_FIRST_SENTENCE_CHARS})`);
    }
  }
  return out;
}
