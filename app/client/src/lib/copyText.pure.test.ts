import { describe, expect, it, vi } from 'vitest';
import { copyText } from './copyText.pure';

describe('copyText', () => {
  it('writes the text and resolves true', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    await expect(copyText('main.sales.orders', { writeText })).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith('main.sales.orders');
  });

  it('resolves false when there is no clipboard (non-secure context)', async () => {
    await expect(copyText('main.sales', undefined)).resolves.toBe(false);
    await expect(copyText('main.sales', null)).resolves.toBe(false);
  });

  it('resolves false, never throws, when the write is refused', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('NotAllowedError'));
    await expect(copyText('main.sales', { writeText })).resolves.toBe(false);
  });
});
