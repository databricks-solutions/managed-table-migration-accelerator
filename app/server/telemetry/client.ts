import { createWorkspaceClient } from '@databricks/appkit';
import { PRODUCT, VERSION, telemetryEnabled } from './product.js';

type ClientOpts = NonNullable<Parameters<typeof createWorkspaceClient>[0]>;

/**
 * The app's one workspace-client factory: `createWorkspaceClient`, stamped with
 * `uc-ext2managed/<version>` as the SDK product when telemetry is on (opt-out:
 * `MTM_TELEMETRY_ENABLED=false`). The stamp is a header only — no extra call, no payload.
 * Every app-side `createWorkspaceClient` goes through here (guarded by client.guard.test.ts).
 */
export function createTaggedWorkspaceClient(
  opts: ClientOpts = {},
  env: NodeJS.ProcessEnv = process.env
): ReturnType<typeof createWorkspaceClient> {
  if (!telemetryEnabled(env)) return createWorkspaceClient(opts);
  return createWorkspaceClient({
    ...opts,
    clientOptions: { ...opts.clientOptions, product: PRODUCT, productVersion: VERSION },
  });
}
