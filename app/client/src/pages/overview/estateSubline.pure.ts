/**
 * Overview scope-clarity strings (PRESENTATION ONLY — pure, no calc beyond string assembly).
 *
 * The Overview tiles + subline describe the WHOLE planned estate, while the directive card
 * describes ONE wave/schema. These helpers name that split so a glance never confuses a
 * one-wave count for an estate figure. Every input is a precomputed field read straight off
 * `migration_next_actions` (`estate_*` + `next_selector_kind`) — the app does no aggregation.
 * AppKit's JSON transport sends those scalars as strings, so every count is coerced with `num`.
 */
import { num } from '../../lib/focus.pure';

/** A count as it arrives from the query: a number, or the string AppKit's transport sends. */
type WireCount = number | string;

export interface EstateSublineInput {
  /** Planned estate = wave_plan ⋈ manifest (`estate_total_tables`). */
  planned: WireCount;
  /** Discovered estate = COUNT(table_manifest) (`estate_discovered_tables`); superset of planned. */
  discovered: WireCount;
  /** Distinct schemas in the planned estate (`estate_total_schemas`). */
  schemas: WireCount;
  /** Tables migrated so far, cumulative (`estate_migrated_tables`) — the same count as the Migrated tile. */
  migrated: WireCount;
  /** Whole-estate remaining (`estate_remaining_tables`). */
  remaining: WireCount;
  /**
   * Discovered tables held OUT of the plan by eligibility — BLOCKED/NEEDS_REVIEW
   * (`estate_ineligible_tables`); the exact Blocked & review rows. Optional: when omitted
   * (older snapshots pre-column) the held-back clause is dropped rather than fabricated.
   */
  heldBack?: WireCount | null;
}

/**
 * The estate subline, e.g.
 * `"119 planned of 126 discovered across 10 schemas — 0 migrated · 119 remaining · 7 held back"`.
 *
 * The "of M discovered" clause is dropped when `discovered <= planned` (nothing sits outside
 * the plan, so the gap would be zero/negative and only add noise). The "· N held back" clause is
 * appended only when `heldBack` is a positive number — it reconciles the gap so a glance sees
 * `discovered = planned + held back`. `schemas` is pluralized.
 */
export function estateSubline({
  planned,
  discovered,
  schemas,
  migrated,
  remaining,
  heldBack,
}: EstateSublineInput): string {
  const schemaWord = num(schemas) === 1 ? 'schema' : 'schemas';
  const scope =
    num(discovered) > num(planned)
      ? `${planned} planned of ${discovered} discovered across ${schemas} ${schemaWord}`
      : `${planned} planned tables across ${schemas} ${schemaWord}`;
  const held = num(heldBack);
  const heldBackClause = held > 0 ? ` · ${held} held back` : '';
  return `${scope} — ${migrated} migrated · ${remaining} remaining${heldBackClause}`;
}

/**
 * The directive card's scope chip label, from `migration_next_actions.next_selector_kind`.
 * A single-schema (`target_schema`) recommendation is always exactly one wave in the guided
 * flow (the pilot / a single-wave group), so it reads "This wave · one schema". Returns null
 * for navigate-only / done states (no selector) so no chip renders.
 */
export function scopeChipLabel(selectorKind: string | null | undefined): string | null {
  switch (selectorKind) {
    case 'target_schema':
      return 'This wave · one schema';
    case 'wave_id':
      return 'This wave';
    case 'bucket':
      return 'This bucket';
    default:
      return null;
  }
}

/** The estate row fields the "Where the migration stands" tiles read (`migration_next_actions`). */
export interface SituationTilesInput {
  estate_pct_migrated: WireCount | null;
  estate_pct_validated: WireCount | null;
  estate_migrated_tables: WireCount | null;
  estate_validated_tables: WireCount | null;
  estate_remaining_tables: WireCount | null;
  estate_total_tables: WireCount | null;
  schemas_blocked: WireCount | null;
  estate_total_schemas: WireCount | null;
}

export interface SituationTile {
  label: string;
  /** The headline number. */
  value: string;
  /** What the number is out of, or its unit ("of 121", "tables"). */
  unit: string | null;
  /** A secondary reading (the percentage), or null. */
  caption: string | null;
}

/**
 * The four estate tiles. Every count says what it is out of, so a percentage never stands beside
 * a bare count with no total. The labels keep the words the browser checks read (e2e C1.1:
 * "Migrated", "Validated", "Remaining", "Schemas blocked"). Pure presentation of the conductor's
 * precomputed estate row: no aggregation.
 */
export function situationTiles(n: SituationTilesInput): SituationTile[] {
  const planned = num(n.estate_total_tables);
  const remaining = num(n.estate_remaining_tables);
  const pct = (v: WireCount | null) => `${v ?? 0}%`;
  return [
    { label: 'Migrated', value: String(num(n.estate_migrated_tables)), unit: `of ${planned}`, caption: pct(n.estate_pct_migrated) },
    { label: 'Validated', value: String(num(n.estate_validated_tables)), unit: `of ${planned}`, caption: pct(n.estate_pct_validated) },
    { label: 'Remaining', value: String(remaining), unit: remaining === 1 ? 'table' : 'tables', caption: null },
    {
      label: 'Schemas blocked in plan',
      value: String(num(n.schemas_blocked)),
      unit: `of ${num(n.estate_total_schemas)}`,
      caption: null,
    },
  ];
}
