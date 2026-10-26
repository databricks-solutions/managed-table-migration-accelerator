import { useState, type ReactNode } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Spinner,
} from '@databricks/appkit-ui/react';
import { type DialogCopy, dialogProps } from '../lib/sectionCopy';

/**
 * The one confirm step for anything the app runs as the signed-in user (Grant, Grant convert
 * rights, Hand back): what will run, as whom, and the plan fingerprint it is pinned to. `onConfirm`
 * returns an error message, or null when it worked (the dialog closes).
 */
export function PlanConfirmDialog({
  copy,
  vars = {},
  statements,
  asUser,
  planId,
  disabled = false,
  variant = 'default',
  defaultOpen = false,
  children,
  onConfirm,
}: {
  copy: DialogCopy;
  vars?: Record<string, string | number>;
  statements: string[];
  asUser: string | null;
  planId: string;
  disabled?: boolean;
  variant?: 'default' | 'outline';
  /** Open on mount (a trigger that loaded the plan first). */
  defaultOpen?: boolean;
  children?: ReactNode;
  onConfirm: () => Promise<string | null>;
}) {
  const d = dialogProps(copy, vars);
  const [open, setOpen] = useState(defaultOpen);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const err = await onConfirm();
      if (err) setError(err);
      else setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setError(null);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant={variant} disabled={disabled || !statements.length}>
          {d.triggerLabel}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader className="pr-8">
          <DialogTitle>{d.title}</DialogTitle>
          <DialogDescription>{d.description}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 py-1 text-sm">
          {asUser ? (
            <p className="text-muted-foreground">
              Runs as <span className="font-mono text-foreground">{asUser}</span>.
            </p>
          ) : null}
          <pre className="max-h-64 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
            {statements.join('\n')}
          </pre>
          <p className="text-muted-foreground">
            Plan <span className="font-mono text-foreground">{planId}</span>. If anything changed since the preview, nothing runs.
          </p>
          {children}
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Nothing more ran</AlertTitle>
              <AlertDescription className="[overflow-wrap:anywhere]">{error}</AlertDescription>
            </Alert>
          ) : null}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline" disabled={busy}>
              Cancel
            </Button>
          </DialogClose>
          <Button onClick={() => void confirm()} disabled={busy} className="gap-2">
            {busy ? <Spinner className="size-4" aria-hidden /> : null}
            {d.confirmLabel ?? d.title}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
