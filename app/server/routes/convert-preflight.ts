import type { Application, Request, Response } from 'express';
import { preflightConvertAlias, resolvePreflightTarget, runPreflight } from '../mcp/preflight.js';

/**
 * GET /api/convert/preflight?target=<catalog.schema|catalog.schema.table|wave:<id>>[&for=bulk_convert]
 * — the UI's read-only window onto the SAME preflight the MCP tool serves (runPreflight), so the app
 * gate and Genie agree by construction. Returns the PreflightOutput verdict (permission/freeze/
 * managed-location/dry-run) with `impacts.authority` from the stored convert_authority verdicts.
 * `for=convert|bulk_convert` is validated (anything else is a 400) but reads the same verdicts:
 * both convert jobs deploy with the same run_as. Triggers nothing; the convert job re-checks every gate.
 */
export function registerConvertPreflightRoute(
  app: Application,
  deps: { run?: (target: string) => Promise<unknown> } = {},
): void {
  const run = deps.run ?? runPreflight;
  app.get('/api/convert/preflight', (req: Request, res: Response) => {
    void (async () => {
      const target = resolvePreflightTarget({
        target: typeof req.query.target === 'string' ? req.query.target : undefined,
      });
      if (!target) {
        res.status(400).json({ error: 'target is required (catalog.schema or catalog.schema.table)' });
        return;
      }
      if (!preflightConvertAlias(req.query.for)) {
        res.status(400).json({ error: 'for must be convert or bulk_convert' });
        return;
      }
      res.json(await run(target));
    })().catch((err: unknown) => {
      res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
    });
  });
}
