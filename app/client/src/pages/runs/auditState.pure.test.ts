import { describe, expect, it } from 'vitest';
import { auditStateLabel } from './auditState.pure';

// AppKit's result delivery parses a STRING cell that starts with `{` or `[` into an object, so the
// layout JSON 08c records in from_state / to_state reaches the page as an object. Rendering that
// object as a React child crashed the Runs page (React error #31).
describe('auditStateLabel', () => {
  it('passes a lifecycle state through', () => {
    expect(auditStateLabel('CONVERTED')).toEqual({ label: 'CONVERTED', title: null });
  });

  it('summarises a recorded clustering layout', () => {
    const auto = { cluster_by_auto: true, clustering_columns: [], partition_columns: [], source: 'live' };
    expect(auditStateLabel(auto)?.label).toBe('clustering: AUTO');
    expect(auditStateLabel({ ...auto, cluster_by_auto: true, clustering_columns: ['a', 'b'] })?.label).toBe(
      'clustering: AUTO (a, b)');
    expect(auditStateLabel({ ...auto, cluster_by_auto: false, clustering_columns: ['region'] })?.label).toBe(
      'clustering: keys region');
    expect(auditStateLabel({ ...auto, cluster_by_auto: false })?.label).toBe('no clustering');
    expect(auditStateLabel({ ...auto, cluster_by_auto: null, clustering_columns: null })?.label).toBe(
      'clustering: unknown');
    expect(auditStateLabel(auto)?.title).toContain('"source":"live"');
  });

  it('also reads the layout when it arrives as the raw JSON string', () => {
    expect(auditStateLabel('{"cluster_by_auto": true, "clustering_columns": [], "source": "live"}')?.label).toBe(
      'clustering: AUTO');
  });

  it('never returns an object to render, and nothing for an empty value', () => {
    expect(auditStateLabel({ other: 1 })).toEqual({ label: '{"other":1}', title: '{"other":1}' });
    expect(auditStateLabel(['x'])?.label).toBe('["x"]');
    expect(auditStateLabel(null)).toBeNull();
    expect(auditStateLabel('')).toBeNull();
    expect(typeof auditStateLabel({ a: 'x'.repeat(200) })?.label).toBe('string');
    expect(auditStateLabel({ a: 'x'.repeat(200) })!.label.length).toBeLessThanOrEqual(48);
  });
});
