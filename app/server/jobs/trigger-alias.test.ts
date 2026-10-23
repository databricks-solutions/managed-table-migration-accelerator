import { describe, expect, it } from 'vitest';
import { triggerAlias, TriggerError } from './trigger-alias.js';

describe('triggerAlias (pre-SDK validation)', () => {
  it('throws 404 TriggerError for an unknown alias', async () => {
    await expect(triggerAlias('nope', {}, { headers: {}, requestId: 'r1' }))
      .rejects.toMatchObject({ status: 404 });
  });
  it('throws 400 TriggerError when params fail the alias schema', async () => {
    // convert with an empty scope fails the selector refinement BEFORE any SDK call.
    await expect(triggerAlias('convert', {}, { headers: {}, requestId: 'r1' }))
      .rejects.toBeInstanceOf(TriggerError);
    await expect(triggerAlias('convert', {}, { headers: {}, requestId: 'r1' }))
      .rejects.toMatchObject({ status: 400 });
  });
});
