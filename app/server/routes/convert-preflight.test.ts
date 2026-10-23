import type { Application, Request, Response } from 'express';
import { describe, expect, it } from 'vitest';
import { registerConvertPreflightRoute } from './convert-preflight.js';

type Handler = (req: Request, res: Response) => void;

function fakeApp() {
  const handlers: Record<string, Handler> = {};
  const app = {
    get: (path: string, h: Handler) => {
      handlers[path] = h;
    },
  } as unknown as Application;
  return { app, handlers };
}

interface FakeRes {
  statusCode: number;
  body: unknown;
  status(c: number): FakeRes;
  json(b: unknown): FakeRes;
}

function fakeRes(): FakeRes {
  const res: FakeRes = {
    statusCode: 200,
    body: undefined,
    status(c: number) {
      this.statusCode = c;
      return this;
    },
    json(b: unknown) {
      this.body = b;
      return this;
    },
  };
  return res;
}

// Drain the handler's fire-and-forget async IIFE before asserting.
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe('GET /api/convert/preflight', () => {
  it('returns runPreflight output for a target', async () => {
    const { app, handlers } = fakeApp();
    registerConvertPreflightRoute(app, {
      run: (t) => Promise.resolve({ target: t, verdict: 'READY_TO_CONVERT' }),
    });
    const res = fakeRes();
    handlers['/api/convert/preflight'](
      { query: { target: 'c.s' } } as unknown as Request,
      res as unknown as Response,
    );
    await flush();
    expect(res.body).toEqual({ target: 'c.s', verdict: 'READY_TO_CONVERT' });
  });
  it('400s on an empty target', async () => {
    const { app, handlers } = fakeApp();
    registerConvertPreflightRoute(app, { run: () => Promise.resolve({}) });
    const res = fakeRes();
    handlers['/api/convert/preflight'](
      { query: {} } as unknown as Request,
      res as unknown as Response,
    );
    await flush();
    expect(res.statusCode).toBe(400);
  });
  it('runs the preflight for the target when for is omitted', async () => {
    const { app, handlers } = fakeApp();
    const seen: unknown[] = [];
    registerConvertPreflightRoute(app, {
      run: (t) => {
        seen.push(t);
        return Promise.resolve({ target: t });
      },
    });
    const res = fakeRes();
    handlers['/api/convert/preflight'](
      { query: { target: 'wave:w1' } } as unknown as Request,
      res as unknown as Response,
    );
    await flush();
    expect(seen).toEqual(['wave:w1']);
  });
  it('for=bulk_convert reads the same stored verdicts (same run_as)', async () => {
    const { app, handlers } = fakeApp();
    const seen: unknown[] = [];
    registerConvertPreflightRoute(app, {
      run: (t) => {
        seen.push(t);
        return Promise.resolve({ target: t });
      },
    });
    const res = fakeRes();
    handlers['/api/convert/preflight'](
      { query: { target: 'wave:w1', for: 'bulk_convert' } } as unknown as Request,
      res as unknown as Response,
    );
    await flush();
    expect(seen).toEqual(['wave:w1']);
  });
  it('400s on an unknown for value without running the preflight', async () => {
    const { app, handlers } = fakeApp();
    const seen: unknown[] = [];
    registerConvertPreflightRoute(app, {
      run: (t) => {
        seen.push(t);
        return Promise.resolve({ target: t });
      },
    });
    const res = fakeRes();
    handlers['/api/convert/preflight'](
      { query: { target: 'wave:w1', for: 'rollback' } } as unknown as Request,
      res as unknown as Response,
    );
    await flush();
    expect(res.statusCode).toBe(400);
    expect(seen).toEqual([]);
  });
});
