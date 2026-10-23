import { useState } from 'react';
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
  Input,
  Label,
  Spinner,
  Textarea,
} from '@databricks/appkit-ui/react';
import { useCatalog } from '../context/CatalogProvider';

/**
 * ActionNotesDialog — the ONE writable surface in the app. The engine owns an action's
 * lifecycle (status, resolution) — those are read-only Badges elsewhere. Only `notes` and
 * `assignee` are editable, via POST /api/actions/update keyed by action_key. On success we
 * bumpRefresh() centrally so every read re-fetches. A plain Dialog (not AlertDialog) — this
 * is an edit form, not a destructive confirm.
 */
export function ActionNotesDialog({
  actionKey,
  actionTitle,
  currentNotes,
  currentAssignee,
}: {
  actionKey: string;
  actionTitle: string;
  currentNotes: string | null;
  currentAssignee: string | null;
}) {
  const { bumpRefresh } = useCatalog();
  const [open, setOpen] = useState(false);
  const [assignee, setAssignee] = useState('');
  const [notes, setNotes] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (next) {
      // Seed the form from the current values each time it opens.
      setAssignee(currentAssignee ?? '');
      setNotes(currentNotes ?? '');
      setError(null);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const requestId = crypto.randomUUID();
      const res = await fetch('/api/actions/update', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action_key: actionKey, request_id: requestId, notes, assignee }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => ({}))) as { error?: string };
        setError(data.error ?? `Save failed (${res.status})`);
        return;
      }
      bumpRefresh();
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Edit
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader className="pr-8">
          <DialogTitle>Edit action</DialogTitle>
          <DialogDescription className="min-w-0 [overflow-wrap:anywhere]">
            {actionTitle}. Only assignee and notes are editable — status and resolution are
            owned by the engine.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="action-assignee">Assignee</Label>
            <Input
              id="action-assignee"
              value={assignee}
              placeholder="e.g. alice@company.com"
              onChange={(e) => setAssignee(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="action-notes">Notes</Label>
            <Textarea
              id="action-notes"
              value={notes}
              rows={4}
              placeholder="Context, hand-off, or a link to the ticket…"
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Couldn’t save</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline" disabled={saving}>
              Cancel
            </Button>
          </DialogClose>
          <Button onClick={() => void save()} disabled={saving} className="gap-2">
            {saving ? <Spinner className="size-4" aria-hidden /> : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
