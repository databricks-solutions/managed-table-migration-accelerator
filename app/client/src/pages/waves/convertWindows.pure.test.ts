import { describe, expect, it } from 'vitest';
import { slackText } from './convertWindows.pure';

describe('slackText', () => {
  it('renders minutes when the window is computed', () => {
    expect(slackText(42)).toBe('42 min');
    expect(slackText(0)).toBe('0 min');
  });

  it('never renders "null min" for a wave with no writer schedule', () => {
    expect(slackText(null)).toBe('—');
    expect(slackText(undefined)).toBe('—');
  });
});
