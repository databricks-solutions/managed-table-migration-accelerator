import { migrationJobs } from '../jobs/job-params.js';
import { mcpTier } from '../mcp/tiers.js';
import { createTaggedWorkspaceClient } from './client.js';
import { KEY_PREFIX, safeToken, telemetryEnabled } from './product.js';

export type TriggerSurface = 'app' | 'mcp';
const SURFACES: readonly TriggerSurface[] = ['app', 'mcp'];

/** Bound the ping so a control-plane brownout can never hold a request (same as the backend). */
export const PING_TIMEOUT_MS = 5000;

/**
 * The User-Agent extras for one job launch, or null when nothing attributable can be sent.
 * Only closed vocabularies: the alias must be a key of the app's own job map, the surface is
 * `app|mcp`, the tier is `v1|v2`. Parameters, the operator and the request are never read.
 */
export function triggerSignals(
  alias: string,
  surface: TriggerSurface,
  env: NodeJS.ProcessEnv = process.env
): Record<string, string> | null {
  const known = Object.prototype.hasOwnProperty.call(migrationJobs.jobs ?? {}, alias);
  const aliasTok = known ? safeToken(alias) : null;
  if (!aliasTok || !SURFACES.includes(surface)) return null;
  return {
    [`${KEY_PREFIX}trigger`]: aliasTok,
    [`${KEY_PREFIX}surface`]: surface,
    [`${KEY_PREFIX}mcp_tier`]: mcpTier(env),
  };
}

interface PingClient {
  currentUser: { me: () => Promise<unknown> };
}

export interface PingDeps {
  env?: NodeJS.ProcessEnv;
  makeClient?: (extra: Record<string, string>) => PingClient;
  timeoutMs?: number;
}

const defaultMakeClient = (extra: Record<string, string>): PingClient =>
  createTaggedWorkspaceClient({ clientOptions: { userAgentExtra: extra } }) as unknown as PingClient;

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('telemetry ping timed out')), ms);
    timer.unref?.();
  });
  return Promise.race([p, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Record one job launch as a tagged `current_user.me()` call. NEVER rejects and never throws:
 * callers fire-and-forget it (`void pingTrigger(...)`) after a successful runNow, so telemetry
 * can neither fail nor delay a trigger.
 */
export async function pingTrigger(alias: string, surface: TriggerSurface, deps: PingDeps = {}): Promise<boolean> {
  try {
    const env = deps.env ?? process.env;
    if (!telemetryEnabled(env)) return false;
    const extra = triggerSignals(alias, surface, env);
    if (!extra) return false;
    const client = (deps.makeClient ?? defaultMakeClient)(extra);
    await withTimeout(client.currentUser.me(), deps.timeoutMs ?? PING_TIMEOUT_MS);
    return true;
  } catch {
    return false;
  }
}
