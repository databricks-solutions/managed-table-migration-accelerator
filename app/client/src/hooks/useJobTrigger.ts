import { useState } from 'react';
import type { TriggerParams } from '../lib/triggerParams.pure';

/**
 * useJobTrigger — the ONE way the app fires a mutating job.
 *
 * The route contract is POST /api/jobs/:alias/trigger with body { request_id, params }.
 * params MUST be nested under `params` (the route reads `body.params`), NOT spread as
 * siblings of request_id, or every param is dropped and the .strict() Zod schema rejects
 * the empty body with a 400. A repeated identical trigger is a harmless idempotent replay,
 * not a second run — that is what request_id buys.
 *
 * Callers (JobTriggerDialog, ConvertGateDialog) own their own confirm UI; this hook owns
 * only the fetch + the phase machine, so the 400/404 (deterministic) vs 409/5xx/network
 * (retryable) classification lives in exactly one place.
 */

export type TriggerPhase =
  | { status: 'idle' }
  | { status: 'submitting' }
  | { status: 'success'; runId?: string; runPageUrl?: string }
  // A duplicate carries the active run's id + link so the caller can seed the same shared
  // state (block + open-run link) as a fresh success — the 409 body returns both.
  | { status: 'conflict'; message: string; runId?: string; runPageUrl?: string }
  // `retryable=false` is a deterministic rejection (400 invalid params / 404 unknown
  // alias): the identical request will always fail, so the caller disables the confirm.
  // `true` is a transient failure (409 handled separately, 5xx transport, network) worth
  // clicking again.
  | { status: 'error'; message: string; retryable: boolean };

export type { TriggerParams } from '../lib/triggerParams.pure';

export function useJobTrigger() {
  const [phase, setPhase] = useState<TriggerPhase>({ status: 'idle' });

  async function trigger(alias: string, params?: TriggerParams): Promise<TriggerPhase> {
    setPhase({ status: 'submitting' });
    let next: TriggerPhase;
    try {
      const requestId = crypto.randomUUID();
      const res = await fetch(`/api/jobs/${alias}/trigger`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ request_id: requestId, params: cleanParams(params) }),
      });
      const data = (await res.json().catch(() => ({}))) as {
        error?: string;
        run_id?: string;
        run_page_url?: string;
        issues?: Array<{ path: string; message: string }>;
      };
      if (res.status === 409) {
        // Active run is expected operator feedback, not an error.
        next = {
          status: 'conflict',
          message: data.error ?? 'A run is already active.',
          runId: data.run_id,
          runPageUrl: data.run_page_url,
        };
      } else if (!res.ok) {
        // Surface the route's Zod issue list so the reason is visible; mark 400/404
        // non-retryable so the caller can lock the confirm.
        const retryable = res.status !== 400 && res.status !== 404;
        const detail = (data.issues ?? [])
          .map((i) => (i.path ? `${i.path}: ${i.message}` : i.message))
          .join('; ');
        const base = data.error ?? `Trigger failed (${res.status})`;
        next = { status: 'error', message: detail ? `${base} — ${detail}` : base, retryable };
      } else {
        next = { status: 'success', runId: data.run_id, runPageUrl: data.run_page_url };
      }
    } catch (err) {
      // Network/transport failure never reached the route — retryable.
      next = {
        status: 'error',
        message: err instanceof Error ? err.message : String(err),
        retryable: true,
      };
    }
    setPhase(next);
    return next;
  }

  function reset() {
    setPhase({ status: 'idle' });
  }

  return { phase, trigger, reset };
}

/** Drop undefined/empty params so an optional knob left blank is never sent. */
export function cleanParams(p?: TriggerParams): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  if (!p) return out;
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== '') out[k] = v;
  }
  return out;
}
