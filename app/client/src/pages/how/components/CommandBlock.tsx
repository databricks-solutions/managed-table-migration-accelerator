import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { cn } from '@databricks/appkit-ui/react';

/**
 * CommandBlock — a mono command/snippet with a copy-to-clipboard button. Progressive: falls
 * back silently if the Clipboard API is unavailable (e.g. non-secure context). Used by the
 * command builder and the cockpit walkthrough. Read-only teaching — it copies text, nothing more.
 */
export function CommandBlock({ command, className }: { command: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  const onCopy = () => {
    void navigator.clipboard
      ?.writeText(command)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {
        /* non-secure context or denied — leave the text selectable */
      });
  };

  return (
    <div className={cn('group relative rounded-lg border border-border bg-muted', className)}>
      <button
        type="button"
        onClick={onCopy}
        aria-label={copied ? 'Copied' : 'Copy command'}
        className="how-press absolute right-2 top-2 flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-[10px] font-medium text-muted-foreground hover:text-foreground"
      >
        {copied ? <Check className="size-3 text-[var(--success)]" aria-hidden /> : <Copy className="size-3" aria-hidden />}
        {copied ? 'Copied' : 'Copy'}
      </button>
      <code className="block overflow-x-auto whitespace-pre-wrap px-3 py-2.5 pr-16 font-mono text-[12px] leading-relaxed text-foreground">
        {command}
      </code>
    </div>
  );
}
