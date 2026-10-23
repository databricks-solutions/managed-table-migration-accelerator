import { describe, expect, it } from 'vitest';
import { OPEN_APIS_REASON, preconvertReaderWarning } from '../../client/src/lib/compatOpenApis.pure.js';
import { readerDoNot } from './convertImpacts.pure';
import { buildEstateHints } from './estateNextStep.pure';
import { FINISH_DO_NOT, FINISH_STEP_WHY } from './finishSteps.pure';
import { TOOL_DESCRIPTION } from './job-tools';
import { MCP_INSTRUCTIONS } from './register-mcp';

// Every MCP surface that tells the model how to settle external readers leads with the UC open
// APIs (the exact defer_reason job_08b records), and states the same four Compatibility Mode
// facts job_08b enforces: one EMPTY path per table, sent as compat_location_map, a shared path
// refused, and never an invented path.
const SURFACES: Record<string, string> = {
  'mtm_enable_compat description': TOOL_DESCRIPTION.enable_compat,
  'server instructions (external readers bullet)': MCP_INSTRUCTIONS.slice(
    MCP_INSTRUCTIONS.indexOf('External readers = mtm_enable_compat'),
  ),
  'finish-step do_not (compat)': FINISH_DO_NOT.compat.join(' '),
};

describe('MCP external readers wording', () => {
  for (const [name, raw] of Object.entries(SURFACES)) {
    const text = raw.replace(/\s+/g, ' ');
    it(`${name}: one empty path per table, compat_location_map, shared refused, never invented`, () => {
      expect(text).toMatch(/EMPTY external[- ](location[- ])?path/i);
      expect(text).toMatch(/per table/i);
      expect(text).toMatch(/compat_location_map/);
      expect(text).toMatch(/shared[^.]*refused|refuses a location shared/i);
      expect(text).toMatch(/(never|do not) invent/i);
      expect(text).not.toMatch(/one (shared )?(path|location) for (the whole|all tables|every table)/i);
    });

    it(`${name}: open APIs first, with the exact defer_reason`, () => {
      expect(text.indexOf('open APIs')).toBeGreaterThanOrEqual(0);
      expect(text.indexOf('open APIs')).toBeLessThan(text.indexOf('Compatibility Mode'));
      expect(text).toContain(`defer_reason="${OPEN_APIS_REASON}"`);
    });
  }

  it('the finish-step reason names open APIs before Compatibility Mode', () => {
    expect(FINISH_STEP_WHY.compat.indexOf('open APIs')).toBeLessThan(FINISH_STEP_WHY.compat.indexOf('Compatibility Mode'));
  });
});

// Every MCP surface that leads to a convert carries the pre-convert reader rule, and every surface
// that names the row-filter / column-mask case gives its exit (cross-engine ABAC after convert).
const READER_RULE = 'Before converting, tell the user about external readers. Ask them to confirm.';
const CONVERT_SURFACES: Record<string, string> = {
  'mtm_convert description': TOOL_DESCRIPTION.convert,
  'mtm_bulk_convert description': TOOL_DESCRIPTION.bulk_convert,
  'server instructions': MCP_INSTRUCTIONS,
};

describe('MCP pre-convert external-readers wording', () => {
  for (const [name, raw] of Object.entries(CONVERT_SURFACES)) {
    it(`${name}: carries the reader rule`, () => {
      expect(raw.replace(/\s+/g, ' ')).toContain(READER_RULE);
    });
  }

  it('the do_not caveat embeds the canonical template verbatim and is advisory', () => {
    const [caveat] = readerDoNot(2, 1);
    expect(caveat).toContain(preconvertReaderWarning(2, 1));
    expect(caveat).toMatch(/advisory/);
    expect(readerDoNot(0, 0)).toEqual([]);
  });

  it('the estate hint and the bulk recommendation reuse the same template', () => {
    const hints = buildEstateHints({ next_step_kind: 'convert', readers_undecided: '1', readers_fgac_required: '0' });
    expect(hints[0]).toBe(`Before converting: ${preconvertReaderWarning(1, 0)}`);
    expect(buildEstateHints({ next_step_kind: 'compat', readers_undecided: '1' }).join(' ')).not.toContain(
      preconvertReaderWarning(1, 0),
    );
  });

  const ROW_FILTER_SURFACES: Record<string, string> = {
    ...CONVERT_SURFACES,
    'fgac template': preconvertReaderWarning(0, 1),
    'do_not caveat': readerDoNot(0, 1).join(' '),
  };
  for (const [name, raw] of Object.entries(ROW_FILTER_SURFACES)) {
    it(`${name}: names cross-engine ABAC wherever it names row filters or column masks`, () => {
      const text = raw.replace(/\s+/g, ' ');
      if (/row filters or column masks/i.test(text)) expect(text).toMatch(/cross-engine ABAC/);
      expect(text).not.toMatch(/no remedy|cannot be served at all/i);
    });
  }
});
