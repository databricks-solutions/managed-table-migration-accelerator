/**
 * Allowlist for copyCoverage.guard.test.ts. Every entry needs a reason. Section titles,
 * subtitles, and dialog copy belong in lib/sectionCopy.ts — never add them here.
 */

export interface LiteralAllow {
  /** Path under client/src. */
  file: string;
  /** Substring of the offending line. */
  match: string;
  reason: string;
}

/** Literal `title="…"`-style props and `<CardTitle>` text that are not section copy. */
export const LITERAL_ALLOWLIST: readonly LiteralAllow[] = [
  {
    file: 'pages/dashboard/DashboardPage.tsx',
    match: 'title="AI/BI estate dashboard"',
    reason: 'iframe accessible name, not visible copy',
  },
  {
    file: 'pages/waves/WavesPage.tsx',
    match: 'title="Some tables found new problems after planning. Re-plan to regroup them."',
    reason: 'hover tooltip on the plan-stale badge (data, not a section header)',
  },
  {
    file: 'pages/overview/OverviewPage.tsx',
    match: '<DarkStat label="Time"',
    reason: 'fact-grid value tooltip (fact grid is out of scope)',
  },
  {
    file: 'pages/overview/OverviewPage.tsx',
    match: '<DarkStat label="Max downtime"',
    reason: 'fact-grid value tooltip (fact grid is out of scope)',
  },
  {
    file: 'pages/overview/OverviewPage.tsx',
    match: '<DarkStat label="Blast radius"',
    reason: 'fact-grid value tooltip (fact grid is out of scope)',
  },
  {
    file: 'pages/how/components/Swimlane.tsx',
    match: 'title="What the app does"',
    reason: 'swimlane column label inside an academy diagram',
  },
  {
    file: 'pages/how/components/Swimlane.tsx',
    match: 'title="What you do"',
    reason: 'swimlane column label inside an academy diagram',
  },
  {
    file: 'pages/how/components/WaveJourney.tsx',
    match: 'title="Before convert"',
    reason: 'diagram column label inside the wave journey',
  },
  {
    file: 'pages/how/components/WaveJourney.tsx',
    match: 'title="After validate"',
    reason: 'diagram column label inside the wave journey',
  },
  {
    file: 'pages/how/components/CommandBuilder.tsx',
    match: 'title="Can mutate"',
    reason: 'tooltip on a command-builder marker (commands are out of scope)',
  },
  {
    file: 'pages/how/components/WorkstreamTimeline.tsx',
    match: 'title="Attributed-only"',
    reason: 'tooltip on a timeline glyph, not a header',
  },
];

/**
 * Per-file ratchet for FORBIDDEN_TERMS hits in visible body prose (JSX text and user-facing
 * props, excluding `<code>` / font-mono text). The count must match exactly: a higher count is a
 * regression, a lower one means you cleaned up — lower the number here.
 */
export const FORBIDDEN_RATCHET: Readonly<Record<string, { max: number; reason: string }>> = {
  'pages/how/HappyPathPage.tsx': {
    max: 3,
    reason: 'academy body prose (out of scope for headers): three long teaching sentences and the freeze-ledger restore',
  },
  'pages/how/WavesConceptPage.tsx': {
    max: 4,
    reason: 'academy body prose (out of scope for headers): explains the freeze ledger, the run identity, and the 03d/05b stages by name',
  },
};
