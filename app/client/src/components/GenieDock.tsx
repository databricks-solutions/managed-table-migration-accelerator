import { useEffect, useState } from 'react';
import { GenieChat, Badge, Button } from '@databricks/appkit-ui/react';
import { Sparkles, X, Lock, Info } from 'lucide-react';
import { useCatalog } from '../context/CatalogProvider';
import { SectionHeader } from './SectionHeader';
import { SECTION_COPY } from '../lib/sectionCopy';

/**
 * GenieDock — the docked Genie panel (control_plane §3.13). A pill on EVERY route (rendered
 * once at the shell level, `fixed`) opens a right-hand drawer. Genie is READ-ONLY: it answers
 * from the control views and cannot trigger a job or change a table — the panel says so.
 *
 * The `genie()` plugin is always registered and looks the space up per request
 * (server/config/genieSpace.ts); `/api/config` reports `genieAvailable` live. This component
 * mounts AppKit's <GenieChat> when a space exists, and otherwise says to run the installer,
 * re-reading the config each time the panel opens so a space created meanwhile comes online.
 *
 * Suggested questions are STATIC client content (per spec) — informational prompts the
 * operator can type; the chat owns its own input, so we don't inject them programmatically.
 */

const SUGGESTED_QUESTIONS = [
  'Which waves are blocked, and why?',
  'How many external tables are left to convert?',
  'What is the next wave to convert?',
  'Which tables have open pre-convert actions?',
];

export function GenieDock() {
  const [open, setOpen] = useState(false);
  const { genieAvailable, refreshConfig } = useCatalog();

  // Esc closes the drawer — a dialog affordance the hand-rolled panel must provide itself.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          if (!genieAvailable) void refreshConfig();
        }}
        className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full bg-[var(--brand)] px-4 py-2.5 text-sm font-medium text-[var(--fg-on-dark)] shadow-lg transition-colors hover:bg-[var(--brand-hover)]"
        aria-label="Ask Genie"
      >
        <Sparkles className="size-4" aria-hidden />
        Ask Genie
      </button>

      {open ? (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/30"
            onClick={() => setOpen(false)}
            aria-hidden
          />
          <aside
            role="dialog"
            aria-label="Genie"
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-border bg-card shadow-xl"
          >
            <header className="flex items-start justify-between gap-3 border-b border-border p-4">
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center gap-2">
                  <Sparkles className="size-4 text-[var(--brand)]" aria-hidden />
                  <span className="text-sm font-medium text-foreground">Genie</span>
                  <Badge variant="outline" className="gap-1 text-[10px]">
                    <Lock className="size-3" aria-hidden /> Read-only
                  </Badge>
                </div>
                <p className="text-[11px] leading-snug text-muted-foreground">
                  Answers from the control views and shows the SQL it ran. It cannot trigger a job
                  or change a table.
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setOpen(false)}
                aria-label="Close Genie"
              >
                <X className="size-4" aria-hidden />
              </Button>
            </header>

            <div className="flex min-h-0 flex-1 flex-col">
              {genieAvailable ? (
                <GenieLive />
              ) : (
                <GenieUnavailable />
              )}
            </div>
          </aside>
        </>
      ) : null}
    </>
  );
}

function GenieLive() {
  return (
    <>
      <div className="border-b border-border px-4 py-3">
        <SectionHeader copy={SECTION_COPY.shared.genieTry} variant="label" className="text-[10px] tracking-[0.1em]" />
        <div className="mt-2 flex flex-wrap gap-1.5">
          {SUGGESTED_QUESTIONS.map((q) => (
            <span
              key={q}
              className="rounded-full border border-border bg-muted/40 px-2.5 py-1 text-[11px] text-muted-foreground"
            >
              {q}
            </span>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <GenieChat
          alias="default"
          className="h-full"
          placeholder="Ask about the migration estate…"
        />
      </div>
    </>
  );
}

function GenieUnavailable() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="flex size-10 items-center justify-center rounded-full bg-muted">
        <Info className="size-5 text-muted-foreground" aria-hidden />
      </div>
      <p className="text-sm font-medium text-foreground">Genie isn&rsquo;t set up yet</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        Run the installer first. It creates the Genie space over the control tables. Genie comes
        online here when it finishes, with no redeploy.
      </p>
    </div>
  );
}
