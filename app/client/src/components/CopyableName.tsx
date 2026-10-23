import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import { LABEL_COPY } from '../lib/sectionCopy';
import { copyText } from '../lib/copyText.pure';

const COPIED_MS = 1500;

/**
 * An icon-only copy button for an identifier (catalog.schema.table, wave id, job id). Dimmed at
 * rest and full on hover/focus, so it stays visible on touch screens without crowding dense rows.
 * A failed copy (no Clipboard API, permission refused) is silent — the name next to it is
 * selectable text either way.
 */
export function CopyIconButton({ value, onDark = false }: { value: string; onDark?: boolean }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    void copyText(value, typeof navigator === 'undefined' ? null : navigator.clipboard).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), COPIED_MS);
    });
  };

  const label = `${copied ? LABEL_COPY.copiedName : LABEL_COPY.copyName} ${value}`;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`inline-flex size-5 shrink-0 items-center justify-center rounded opacity-50 outline-none transition-opacity hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-[var(--ring)] ${
        onDark ? 'text-[var(--fg-on-dark-2)] hover:bg-white/10' : 'text-muted-foreground hover:bg-muted'
      }`}
    >
      {copied ? (
        <Check className="size-3 text-[var(--success)]" aria-hidden />
      ) : (
        <Copy className="size-3" aria-hidden />
      )}
    </button>
  );
}

/** A selectable mono identifier followed by its copy button. `display` overrides the shown text. */
export function CopyableName({
  value,
  display,
  onDark = false,
  className,
}: {
  value: string;
  display?: ReactNode;
  onDark?: boolean;
  className?: string;
}) {
  return (
    <span className="inline-flex min-w-0 max-w-full items-start gap-1">
      <span className={`min-w-0 select-text ${className ?? 'font-mono'}`}>{display ?? value}</span>
      <CopyIconButton value={value} onDark={onDark} />
    </span>
  );
}
