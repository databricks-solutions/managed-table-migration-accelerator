/**
 * compatLocationMap.pure — assembles the per-table Compatibility-Mode location map for job_08b.
 * Pure + deterministic (no React, no I/O). Turns the paths an operator types (one per flagged
 * table) into the `compat_location_map` JSON param, and reports the validation facts the Compat
 * cards (the workstream card and the shared finish CTA) need to gate the trigger honestly.
 *
 * HONESTY / SAFETY: reuses the single-path guard (isValidCompatLocation) — placeholders and blanks
 * are never sent, so the app never invents a storage location (job_08b's resolve_location stays the
 * authoritative gate; a table with no resolvable path stays MANUAL). Two tables must not share one
 * empty external location (collision), so duplicate paths are surfaced for the caller to block on.
 */
import { isValidCompatLocation, normalizeCompatLocation } from './compatLocation.pure';

export interface CompatLocationPlan {
  /** JSON string for the compat_location_map job param — only valid, filled entries; '' when none. */
  json: string;
  /** number of tables with a valid location assigned (these go into the map). */
  filled: number;
  /** number of tables left blank — excluded from the map; they stay MANUAL. */
  blank: number;
  /** full_names whose typed path is non-empty but invalid (placeholder / bad scheme). */
  invalid: string[];
  /** paths assigned to more than one table (collision — each table needs its own location). */
  duplicates: string[];
}

export function buildCompatLocationPlan(
  entries: readonly { full_name: string; location: string }[],
): CompatLocationPlan {
  const map: Record<string, string> = {};
  const invalid: string[] = [];
  const byPath = new Map<string, number>();
  let blank = 0;

  for (const { full_name, location } of entries) {
    const path = normalizeCompatLocation(location);
    if (!path) {
      blank += 1;
      continue;
    }
    if (!isValidCompatLocation(path)) {
      invalid.push(full_name);
      continue;
    }
    map[full_name] = path;
    byPath.set(path, (byPath.get(path) ?? 0) + 1);
  }

  const duplicates = [...byPath.entries()]
    .filter(([, count]) => count > 1)
    .map(([path]) => path)
    .sort();
  const filled = Object.keys(map).length;

  return { json: filled > 0 ? JSON.stringify(map) : '', filled, blank, invalid, duplicates };
}

export const COMPAT_READ_ERROR =
  'Couldn’t load the Compatibility Mode actions for this scope, so the per-table locations can’t be collected. Refresh to retry.';
export const COMPAT_NONE_OPEN = 'No open Compatibility Mode action in this scope — nothing to enable.';

export interface CompatCtaState {
  /** Distinct full_names of the open ENABLE_COMPAT actions in scope, sorted (one input each). */
  tables: string[];
  plan: CompatLocationPlan;
  disabled: boolean;
  reason?: string;
}

/**
 * The shared finish CTA's Compat state (D7). One input per table; the trigger sends only
 * compat_location_map, so no single path can be handed to more than one table. job_08b's
 * plan_compat stays the authoritative gate (it refuses a location shared by >1 table).
 */
export function compatCtaState(input: {
  rows: readonly { full_name: string | null | undefined }[];
  paths: Readonly<Record<string, string>>;
  readFailed: boolean;
  loading: boolean;
}): CompatCtaState {
  const tables = [...new Set(input.rows.map((r) => (r.full_name ?? '').trim()).filter((f) => f !== ''))].sort();
  const plan = buildCompatLocationPlan(tables.map((full_name) => ({ full_name, location: input.paths[full_name] ?? '' })));
  const off = (reason: string): CompatCtaState => ({ tables, plan, disabled: true, reason });
  if (input.readFailed) return off(COMPAT_READ_ERROR);
  if (input.loading) return off('Loading the Compatibility Mode actions for this scope…');
  if (tables.length === 0) return off(COMPAT_NONE_OPEN);
  if (plan.duplicates.length > 0) {
    return off('Each table needs its own empty location — Compatibility Mode refuses a path shared by more than one table. Remove the duplicate paths.');
  }
  if (plan.invalid.length > 0) return off('One or more paths are invalid (placeholders like <container> are rejected).');
  if (plan.filled === 0) {
    return off('Enter an empty external-location path for at least one table (abfss://…, s3://…, gs://…, or /Volumes/…).');
  }
  return { tables, plan, disabled: false };
}

/** How one table's outside readers connect. Open APIs is the default. */
export type ReaderChoice = 'open_apis' | 'compat';

export interface CompatChoiceState {
  /** Distinct full_names of the open ENABLE_COMPAT actions in scope, sorted. */
  tables: string[];
  /** Each table's resolved choice (open_apis unless the operator picked compat). */
  choice: Record<string, ReaderChoice>;
  /** The "mark not needed" run: tables whose readers use the open APIs. */
  openApis: { tables: string[]; keys: string[]; disabled: boolean; reason?: string };
  /** The Compatibility Mode run: only the tables marked compat, one path each. */
  compat: CompatCtaState;
}

/**
 * The finish card's per-table choice. Every table is in exactly one run: open APIs (one 08b
 * defer with the fixed reason) or Compatibility Mode (one 08b run with compat_location_map).
 * The Compatibility Mode run stays disabled until every table marked compat has its own path —
 * the operator said that table needs it, so a blank is a missing answer, not "leave it manual".
 */
export function compatChoiceState(input: {
  rows: readonly { full_name: string | null | undefined; action_key: string | null | undefined }[];
  choices: Readonly<Record<string, ReaderChoice>>;
  paths: Readonly<Record<string, string>>;
  readFailed: boolean;
  loading: boolean;
}): CompatChoiceState {
  const keysByTable = new Map<string, string[]>();
  for (const r of input.rows) {
    const t = (r.full_name ?? '').trim();
    const k = (r.action_key ?? '').trim();
    if (t === '' || k === '') continue;
    keysByTable.set(t, [...(keysByTable.get(t) ?? []), k]);
  }
  const tables = [...keysByTable.keys()].sort();
  const choice: Record<string, ReaderChoice> = {};
  for (const t of tables) choice[t] = input.choices[t] === 'compat' ? 'compat' : 'open_apis';
  const openTables = tables.filter((t) => choice[t] === 'open_apis');
  const compatTables = tables.filter((t) => choice[t] === 'compat');

  const gate = input.readFailed ? COMPAT_READ_ERROR : input.loading ? 'Loading the actions for this scope…' : null;
  const openApis = {
    tables: openTables,
    keys: openTables.flatMap((t) => keysByTable.get(t) ?? []),
    ...(gate
      ? { disabled: true, reason: gate }
      : openTables.length === 0
        ? { disabled: true, reason: 'No table is marked as using the open APIs.' }
        : { disabled: false }),
  };

  const compatBase = compatCtaState({
    rows: compatTables.map((full_name) => ({ full_name })),
    paths: input.paths,
    readFailed: input.readFailed,
    loading: input.loading,
  });
  let compat = compatBase;
  if (!gate && compatTables.length === 0) {
    compat = { ...compatBase, disabled: true, reason: 'No table is marked as needing Compatibility Mode.' };
  } else if (!compatBase.disabled && compatBase.plan.blank > 0) {
    compat = {
      ...compatBase,
      disabled: true,
      reason: 'Enter an empty location for every table marked Compatibility Mode, or switch it to open APIs.',
    };
  }
  return { tables, choice, openApis, compat };
}
