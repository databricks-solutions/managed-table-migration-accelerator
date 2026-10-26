import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  BOOT_PLACEHOLDER,
  GENIE_REFRESH_MS,
  armGenieSpaces,
  currentGenieSpace,
  liveGenieSpaces,
  noteGenieSpace,
  refreshIfStale,
  resetGenieSpaceForTest,
} from './genieSpace.js';

afterEach(() => resetGenieSpaceForTest());

describe('live Genie space lookup (one deploy)', () => {
  it('passes the plugin construct check before arming, then reads the live id', () => {
    const spaces = liveGenieSpaces(() => Promise.resolve({ genieSpaceId: null, dashboardId: null }));
    expect(Object.entries(spaces)).toEqual([['default', BOOT_PLACEHOLDER]]);
    armGenieSpaces();
    noteGenieSpace(null, Date.now());
    expect(spaces.default).toBe('');
    noteGenieSpace('01f0abc', Date.now());
    expect(spaces.default).toBe('01f0abc');
    const copy = { ...{ spaces } };
    expect(copy.spaces.default).toBe('01f0abc');
  });

  it('a failed or empty read never clears a known space', () => {
    noteGenieSpace('01f0abc', 0);
    noteGenieSpace(null, 1);
    expect(currentGenieSpace()).toBe('01f0abc');
  });

  it('re-reads in the background only when stale, one read at a time', async () => {
    const read = vi.fn(() => Promise.resolve({ genieSpaceId: '01f0new', dashboardId: null }));
    liveGenieSpaces(read);
    noteGenieSpace(null, 1_000);
    refreshIfStale(1_000 + GENIE_REFRESH_MS - 1);
    expect(read).not.toHaveBeenCalled();
    refreshIfStale(1_000 + GENIE_REFRESH_MS);
    refreshIfStale(1_000 + GENIE_REFRESH_MS);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(currentGenieSpace()).toBe('01f0new'));
  });
});
