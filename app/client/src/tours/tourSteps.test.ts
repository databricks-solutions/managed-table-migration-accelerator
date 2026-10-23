import { describe, expect, it } from 'vitest';
import { TOURS, tourForRoute } from './tourSteps';

describe('tourForRoute', () => {
  it('returns the tour for a known route and null otherwise', () => {
    expect(tourForRoute('/overview')).toBe(TOURS['/overview']);
    expect(tourForRoute('/nope')).toBeNull();
  });
});

describe('TOURS structural contract', () => {
  it('every route has version >= 1 and at least one step', () => {
    for (const [route, tour] of Object.entries(TOURS)) {
      expect(tour.version, route).toBeGreaterThanOrEqual(1);
      expect(tour.steps.length, route).toBeGreaterThan(0);
    }
  });
  it('every step has non-empty title + description, and any element is a data-tour selector', () => {
    for (const [route, tour] of Object.entries(TOURS)) {
      for (const s of tour.steps) {
        expect(s.title.trim().length, route).toBeGreaterThan(0);
        expect(s.description.trim().length, route).toBeGreaterThan(0);
        if (s.element !== undefined) {
          expect(s.element.startsWith('[data-tour='), `${route}: ${s.element}`).toBe(true);
        }
      }
    }
  });
});
