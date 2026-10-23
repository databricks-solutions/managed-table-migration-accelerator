import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { renderCopyReview } from './copyReview.pure';

const HERE = dirname(fileURLToPath(import.meta.url));

describe('app/docs/copy-review.md matches sectionCopy.ts', () => {
  it('is up to date (run `npm run copy:export`)', () => {
    const committed = readFileSync(resolve(HERE, '../../../docs/copy-review.md'), 'utf8');
    expect(committed).toBe(renderCopyReview());
  });
});
