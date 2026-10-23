// Writes app/docs/copy-review.md from client/src/lib/sectionCopy.ts. Run: npm run copy:export
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { renderCopyReview } from '../client/src/lib/copyReview.pure';

const out = resolve(dirname(fileURLToPath(import.meta.url)), '../docs/copy-review.md');
writeFileSync(out, renderCopyReview());
console.log(`wrote ${out}`);
