import type { ControlConfig } from './control-config.js';

/**
 * The Genie space id, looked up when a request needs it rather than fixed at boot, so a fresh
 * install needs one deploy: deploy, run the installer (which creates the space), open the app.
 *
 * The AppKit genie plugin reads `config.spaces[alias]` synchronously on every request and refuses
 * an empty alias at construct, and its resource gate needs DATABRICKS_GENIE_SPACE_ID set at
 * createApp. So the plugin is always registered with `liveGenieSpaces()` (a getter over this
 * cache), the env var carries the boot id or BOOT_PLACEHOLDER, and every `/api/config` read (the
 * client reads it before mounting the chat) refreshes the cache. A stale cache also refreshes in
 * the background when the plugin reads it. An unknown id reads as '' after boot, which the plugin
 * answers with 404, never a call to a space that does not exist.
 */
export const BOOT_PLACEHOLDER = 'genie-space-not-created-yet';
export const GENIE_REFRESH_MS = 30_000;

type Read = () => Promise<ControlConfig>;

const state: { id: string | null; readAt: number; armed: boolean; inflight: Promise<void> | null; read: Read | null } = {
  id: null,
  readAt: 0,
  armed: false,
  inflight: null,
  read: null,
};

/** Record a handshake read. A null read (first run, or a failed read) never clears a known id. */
export function noteGenieSpace(id: string | null, now: number = Date.now()): void {
  if (id) state.id = id;
  state.readAt = now;
}

export function currentGenieSpace(): string | null {
  return state.id;
}

/** Start a background re-read when the cache is older than GENIE_REFRESH_MS (one at a time). */
export function refreshIfStale(now: number = Date.now()): void {
  if (!state.read || state.inflight || now - state.readAt < GENIE_REFRESH_MS) return;
  state.inflight = state.read()
    .then((c) => noteGenieSpace(c.genieSpaceId))
    .catch(() => noteGenieSpace(null))
    .finally(() => {
      state.inflight = null;
    });
}

/** The plugin's `spaces` map. Before `armGenieSpaces()` it reads the placeholder so construct passes. */
export function liveGenieSpaces(read: Read): Record<string, string> {
  state.read = read;
  return {
    get default(): string {
      if (!state.armed) return state.id ?? BOOT_PLACEHOLDER;
      refreshIfStale();
      return state.id ?? '';
    },
  };
}

/** Call once the plugin is constructed: from here an unknown id reads as '' (the plugin's 404). */
export function armGenieSpaces(): void {
  state.armed = true;
}

/** Test seam: reset the module state. */
export function resetGenieSpaceForTest(): void {
  Object.assign(state, { id: null, readAt: 0, armed: false, inflight: null, read: null });
}
