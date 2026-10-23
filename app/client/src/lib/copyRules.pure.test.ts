import { describe, expect, it } from 'vitest';
import { copyViolations, lengthViolations } from './copyRules.pure';

describe('copyViolations', () => {
  it('passes plain copy', () => {
    expect(copyViolations('Picks the next step again. Changes no tables.')).toEqual([]);
  });

  it('names the internal term it found', () => {
    const v = copyViolations('Re-runs the conductor');
    expect(v).toHaveLength(1);
    expect(v[0]).toMatch(/conductor/);
  });

  it.each([
    ['Runs job_08c for the schema.', 'job_08c'],
    ['Unfreeze restores at 05b.', '05b'],
    ['Turns PO on.', 'PO'],
    ['The run_as identity owns it.', 'run_as'],
    ['Marks tables DRY_RUN_OK.', 'DRY_RUN_OK'],
    ['Adds the App SP to the group.', 'App SP'],
    ['Reads the freeze ledger.', 'ledger'],
    ['Runs SET MANAGED.', 'SET MANAGED'],
    ['Runs UNSET MANAGED.', 'UNSET MANAGED'],
    ['Pins the plan_run_id.', 'plan_run_id'],
    ['Lease ownership first.', 'Lease ownership'],
    ['Take ownership of the tables.', 'Take ownership'],
    ['Grants temporary ownership.', 'temporary ownership'],
    ['The app borrows table ownership.', 'borrows table ownership'],
    ['Runs ALTER TABLE OWNER TO.', 'OWNER TO'],
  ])('flags %j', (text, term) => {
    expect(copyViolations(text).join(' ')).toContain(term);
  });

  it('passes the convert-permission wording that replaced the lease', () => {
    expect(copyViolations('Authorize the convert. Ownership never changes.')).toEqual([]);
  });

  it('flags a semicolon', () => {
    expect(copyViolations('Pause the jobs; then convert.').join(' ')).toMatch(/semicolon|one idea/);
  });

  it('flags a sentence over 15 words', () => {
    const sixteen = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen.';
    expect(copyViolations(sixteen).join(' ')).toMatch(/16 words/);
    const fifteen = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen.';
    expect(copyViolations(fifteen)).toEqual([]);
  });

  it.each(['This section shows the waves.', 'Run it in order to convert.', 'Please note the window.'])(
    'flags filler in %j',
    (text) => {
      expect(copyViolations(text).join(' ')).toMatch(/filler/);
    },
  );

  it('does not count placeholders or plain numbers as stage numbers', () => {
    expect(copyViolations('{n} tables converted in 14 days.')).toEqual([]);
  });
});

describe('lengthViolations', () => {
  it('accepts a short title', () => {
    expect(lengthViolations('Your next step', 'title')).toEqual([]);
  });

  it('caps a primer at four sentences', () => {
    expect(lengthViolations('One. Two. Three. Four.', 'primer')).toEqual([]);
    expect(lengthViolations('One. Two. Three. Four. Five.', 'primer').join(' ')).toMatch(/5 sentences/);
  });

  it('rejects a title over 5 words or 40 characters', () => {
    expect(lengthViolations('The recommended next conversion action for you', 'title')).not.toEqual([]);
    expect(lengthViolations('Abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqr', 'title')).not.toEqual([]);
  });

  it('accepts a one-sentence subtitle with a short instruction', () => {
    expect(lengthViolations('The step we recommend now. Press the button to continue.', 'subtitle')).toEqual([]);
  });

  it('rejects a subtitle with three sentences or a first sentence over 100 characters', () => {
    expect(lengthViolations('One. Two. Three.', 'subtitle')).not.toEqual([]);
    expect(lengthViolations(`${'word '.repeat(22).trim()}.`, 'subtitle')).not.toEqual([]);
  });

  it('rejects helper text over 10 words', () => {
    expect(lengthViolations('Picks the next step again. Changes no tables.', 'helper')).toEqual([]);
    expect(lengthViolations('one two three four five six seven eight nine ten eleven', 'helper')).not.toEqual([]);
  });

  it('rejects a dialog description over 2 sentences or 160 characters', () => {
    expect(lengthViolations('Pauses the jobs. Restores them after.', 'dialog')).toEqual([]);
    expect(lengthViolations('One. Two. Three.', 'dialog')).not.toEqual([]);
    expect(lengthViolations(`${'abcdefghi '.repeat(17).trim()}.`, 'dialog')).not.toEqual([]);
  });
});
