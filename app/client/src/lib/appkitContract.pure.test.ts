/**
 * Two behaviours the app relies on from its UI dependencies, pinned so an upgrade that changes
 * them fails here rather than in the browser.
 */
import { sql } from '@databricks/appkit-ui/js';
import { describe, expect, it } from 'vitest';
import { cn } from './utils';

describe('UI dependency contract', () => {
  it('cn lets a later select-text override select-none (labels stay selectable; design-deltas)', () => {
    expect(cn('select-none', 'select-text')).toBe('select-text');
    expect(cn('px-2 select-none', 'select-text px-4')).toBe('select-text px-4');
  });

  it('sql.string builds the STRING marker the query hook and the e2e contract probe send', () => {
    expect(sql.string('main')).toEqual({ __sql_type: 'STRING', value: 'main' });
    expect(sql.string('')).toEqual({ __sql_type: 'STRING', value: '' });
  });
});
